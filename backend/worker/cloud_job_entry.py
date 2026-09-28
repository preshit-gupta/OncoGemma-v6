"""
OncoGemma v5 - Cloud Run Job & Cloud Batch Single-Stage Execution Entrypoint.
Runs a single pipeline stage inside a serverless GCP container with high vCPU & memory,
persists output to Cloud SQL & GCS, and terminates cleanly with zero idle cost.
"""

import os
import sys
import argparse
from datetime import datetime, timezone
from uuid import UUID

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.orm import Session
from app.core.db import SessionLocal
from app.core.pipeline_config import init_pipeline_config
from app.models.stage_execution import StageExecution
from worker.execution import STAGE_HANDLERS, StageFailedError, execute_stage, mark_running

HANDLERS = STAGE_HANDLERS


def execute_cloud_job(case_id_str: str, stage_name: str, exec_id_str: str | None = None) -> int:
    stage_name = stage_name.lower().strip()
    if stage_name not in HANDLERS:
        print(f"[CloudJob Error] Unknown stage '{stage_name}'. Valid: {list(HANDLERS.keys())}", file=sys.stderr)
        return 1

    print(f"================================================================================")
    print(f"[CloudJob] Starting OncoGemma Stage: {stage_name.upper()} | Case: {case_id_str}")
    print(f"[CloudJob] Started at: {datetime.now(timezone.utc).isoformat()}")
    print(f"================================================================================")

    db: Session = SessionLocal()
    try:
        stage_exec = None
        if exec_id_str:
            try:
                stage_exec = db.get(StageExecution, UUID(exec_id_str))
            except Exception:
                stage_exec = db.get(StageExecution, exec_id_str)

        if not stage_exec:
            try:
                c_uuid = UUID(case_id_str)
            except Exception:
                c_uuid = case_id_str

            stage_exec = (
                db.query(StageExecution)
                .filter(StageExecution.case_id == c_uuid, StageExecution.stage == stage_name)
                .order_by(StageExecution.attempt.desc())
                .first()
            )

        if not stage_exec:
            print(f"[CloudJob] Creating new StageExecution for {stage_name} on case {case_id_str}...")
            stage_exec = StageExecution(
                case_id=UUID(case_id_str) if isinstance(case_id_str, str) else case_id_str,
                stage=stage_name,
                attempt=1,
            )
            db.add(stage_exec)
        mark_running(stage_exec)
        db.commit()
        db.refresh(stage_exec)

        try:
            execute_stage(db, stage_exec, handlers=HANDLERS)
        except StageFailedError as exc:
            print(f"[CloudJob FAILED] Stage '{stage_name}' failed: {exc}\n{exc.error['traceback']}", file=sys.stderr)
            return 2

        print(f"================================================================================")
        print(f"[CloudJob] COMPLETED Stage: {stage_name.upper()} | Status: {stage_exec.status}")
        print(f"[CloudJob] Output Ref: {stage_exec.output_ref}")
        print(f"[CloudJob] Completed at: {datetime.now(timezone.utc).isoformat()}")
        print(f"================================================================================")
        return 0

    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="OncoGemma Cloud Run Job Runner")
    parser.add_argument("--case-id", default=os.getenv("CASE_ID"))
    parser.add_argument("--stage", default=os.getenv("STAGE_NAME"))
    parser.add_argument("--exec-id", default=os.getenv("STAGE_EXEC_ID"))
    parser.add_argument("--daemon", action="store_true", help="Run polling loop if no case/stage specified")

    args = parser.parse_args()
    init_pipeline_config()

    if args.case_id and args.stage:
        sys.exit(execute_cloud_job(args.case_id, args.stage, args.exec_id))
    elif args.daemon or os.getenv("RUN_DAEMON", "false").lower() in ("true", "1"):
        from worker.main import run_worker_loop
        print("[CloudJob] No specific case/stage provided. Starting polling daemon loop...")
        run_worker_loop()
    else:
        print("[CloudJob Error] Neither CASE_ID/STAGE_NAME env vars nor --case-id/--stage CLI flags provided.", file=sys.stderr)
        parser.print_help(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
