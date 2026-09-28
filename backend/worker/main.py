import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, engine
from app.core.pipeline_config import init_pipeline_config
from app.models.stage_execution import StageExecution
from worker.execution import STAGE_HANDLERS, StageFailedError, execute_stage, mark_running

HANDLERS = STAGE_HANDLERS

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
        stmt = (
            select(StageExecution)
            .where(
                StageExecution.status == "queued",
                StageExecution.stage.in_(stages_list)
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
                        StageExecution.stage.in_(stages_list)
                    )
                    .order_by(StageExecution.started_at.asc().nulls_first(), StageExecution.id.asc())
                    .limit(1)
                )
                stage_exec = db.scalars(stmt_fallback).first()
            else:
                raise

        if not stage_exec:
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
    while True:
        try:
            if time.time() - last_reset_check > 60.0:
                reset_stuck_running_stages(timeout_seconds=1800)
                last_reset_check = time.time()

            executed = poll_and_execute_single_task()
            if not executed:
                time.sleep(1.0)
        except Exception as e:
            print(f"[Worker Loop Exception] {e}")
            time.sleep(3.0)

if __name__ == "__main__":
    run_worker_loop()
