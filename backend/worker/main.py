import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.core.pipeline_config import init_pipeline_config
from app.models.stage_execution import StageExecution
from eval.harness.driver import drive_active_runs, worker_identity
from worker.execution import STAGE_HANDLERS, StageFailedError, execute_stage, mark_running
from worker.scratch import check_scratch

HANDLERS = STAGE_HANDLERS


def worker_run_modes() -> list[str]:
    """Run modes this worker executes (settings.WORKER_RUN_MODES)."""
    modes = [m.strip() for m in settings.WORKER_RUN_MODES.split(",") if m.strip()]
    unknown = sorted(set(modes) - {"clinical", "eval", "shadow"})
    if not modes or unknown:
        raise ValueError(f"WORKER_RUN_MODES must list clinical, eval or shadow, got {settings.WORKER_RUN_MODES!r}")
    return modes

def reset_stuck_running_stages(timeout_seconds: int = 1800):
    """
    Reset orphan stages left in 'running' state exceeding timeout_seconds (default: 30 minutes / Cloud Run timeout).
    Prevents resetting actively executing sibling worker tasks on worker startup.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    db = SessionLocal()
    try:
        stmt = (
            update(StageExecution)
            .where(
                StageExecution.status == "running",
                (StageExecution.started_at <= cutoff) | (StageExecution.started_at.is_(None))
            )
            .values(status="queued", started_at=None)
        )
        res = db.execute(stmt)
        db.commit()
        if res.rowcount > 0:
            print(f"[Worker Reset] Reset {res.rowcount} stuck 'running' stages (> {timeout_seconds}s) back to 'queued'...")
    except Exception as e:
        print(f"[Worker Reset Note] {e}")
    finally:
        db.close()

def poll_and_execute_single_task():
    """
    Executes a single queued task using SQLAlchemy ORM queue fetch with row locking.
    Uses .with_for_update(skip_locked=True) on PostgreSQL and cleanly falls back on SQLite.
    """
    db: Session = SessionLocal()
    try:
        stages_list = list(HANDLERS.keys())
        run_modes = worker_run_modes()
        stmt = (
            select(StageExecution)
            .where(
                StageExecution.status == "queued",
                StageExecution.stage.in_(stages_list),
                StageExecution.run_mode.in_(run_modes)
            )
            .order_by(StageExecution.started_at.asc().nulls_first(), StageExecution.id.asc())
            .limit(1)
        )

        is_postgres = False
        try:
            bind = db.get_bind()
            if bind and bind.dialect.name == "postgresql":
                is_postgres = True
        except Exception:
            pass

        if is_postgres:
            stmt = stmt.with_for_update(skip_locked=True)

        try:
            stage_exec = db.scalars(stmt).first()
        except Exception as exc:
            if is_postgres and ("for update" in str(exc).lower() or "skip locked" in str(exc).lower()):
                stmt_fallback = (
                    select(StageExecution)
                    .where(
                        StageExecution.status == "queued",
                        StageExecution.stage.in_(stages_list),
                        StageExecution.run_mode.in_(run_modes)
                    )
                    .order_by(StageExecution.started_at.asc().nulls_first(), StageExecution.id.asc())
                    .limit(1)
                )
                stage_exec = db.scalars(stmt_fallback).first()
            else:
                raise

        if not stage_exec:
            return False

        scratch = check_scratch(db, stage_exec, settings.SCRATCH_HEADROOM_FACTOR)
        if not scratch.ok:
            print(
                f"[Worker] Not claiming '{stage_exec.stage}' for case {stage_exec.case_id}: it needs "
                f"{scratch.needed_bytes} bytes of scratch ({settings.SCRATCH_HEADROOM_FACTOR}x the slide), "
                f"{scratch.free_bytes} are free. It stays queued."
            )
            db.rollback()
            return False

        mark_running(stage_exec)
        db.commit()

        stage, case_id = stage_exec.stage, stage_exec.case_id
        print(f"[Worker] Processing stage '{stage}' for case {case_id} (attempt {stage_exec.attempt})...")
        try:
            execute_stage(db, stage_exec, handlers=HANDLERS)
            print(f"[Worker] Successfully completed stage '{stage}' for case {case_id} (Status: {stage_exec.status}).")
        except StageFailedError as e:
            print(f"[Worker ERROR] Stage '{stage}' failed for case {case_id}: {e}")

        return True

    finally:
        db.close()

def run_worker_loop():
    init_pipeline_config()
    print(f"[Worker] Starting OncoGemma stage worker poll loop. Engine: {engine.dialect.name}. Handlers: {list(HANDLERS.keys())}")
    reset_stuck_running_stages(timeout_seconds=1800)
    last_reset_check = time.time()
    last_harness_tick = 0.0
    last_activity = time.time()
    owner = worker_identity()
    drives_runs = "eval" in worker_run_modes()
    print(f"[Worker] Run modes: {worker_run_modes()}; drives validation runs: {drives_runs}.")
    while True:
        try:
            if time.time() - last_reset_check > 60.0:
                reset_stuck_running_stages(timeout_seconds=1800)
                last_reset_check = time.time()

            # Validation runs and batches: an eval worker drives the runs it holds a lease on (SPEC-02 §6).
            if drives_runs and time.time() - last_harness_tick > settings.HARNESS_TICK_S:
                last_harness_tick = time.time()
                if drive_active_runs(SessionLocal, owner, lease_s=settings.HARNESS_LEASE_S):
                    last_activity = time.time()

            executed = poll_and_execute_single_task()
            if executed:
                last_activity = time.time()
            elif settings.WORKER_IDLE_EXIT_S and time.time() - last_activity > settings.WORKER_IDLE_EXIT_S:
                print(f"[Worker] Idle for {settings.WORKER_IDLE_EXIT_S:.0f} s; exiting.")
                return
            else:
                time.sleep(1.0)
        except Exception as e:
            print(f"[Worker Loop Exception] {e}")
            time.sleep(3.0)

if __name__ == "__main__":
    run_worker_loop()
