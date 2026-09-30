"""``oncogemma-eval``: the validation harness CLI (SPEC-02 §5.5).

    python -m eval.cli run     --manifest eval/manifests/tcga.parquet --split val \\
                               --stages ingest,preprocess,qc,triage,mitosis,grading \\
                               --mode auto --concurrency 8 --arm A4 --name "tcga-val-A4"
    python -m eval.cli resume  --run <run_id>
    python -m eval.cli status  --run <run_id>
    python -m eval.cli cancel  --run <run_id>
    python -m eval.cli retry   --run <run_id> --statuses failed

The CLI talks to the app's database (``DATABASE_URL`` or the Cloud SQL settings) and queue; the
app's workers run the stages. ``--split test`` is refused without ``--confirm-test-access``.
Exit codes: 0 done, 1 refused or not found, 2 the run finished with failed items.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

DEFAULT_SPLITS_LOCK = Path("eval/splits/SPLITS.lock")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oncogemma-eval", description=__doc__.split("\n")[0])
    parser.add_argument("--actor", default=f"cli:{getpass.getuser()}", help="who is recorded as running the command")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="create a run from a manifest split and drive it to the end")
    run.add_argument("--manifest", required=True, help="manifest parquet: local path or gs:// URI")
    run.add_argument("--split", required=True, choices=("train", "val", "test"))
    run.add_argument("--stages", required=True, help="comma-separated, from ingest to triage, mitosis or grading")
    run.add_argument("--mode", default="auto", choices=("auto", "manual"))
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--arm", default=None, help="ablation arm id (SPEC-06/07)")
    run.add_argument("--name", required=True)
    run.add_argument("--confirm-test-access", default=None, metavar="REASON",
                     help="required for --split test; written to the audit log")
    run.add_argument("--splits-lock", type=Path, default=DEFAULT_SPLITS_LOCK)
    run.add_argument("--splits-root", type=Path, default=Path("."),
                     help="directory the lock's paths are relative to")
    run.add_argument("--no-wait", action="store_true", help="create the run and exit; drive it with resume")
    run.add_argument("--poll", type=float, default=5.0, help="seconds between controller passes")

    for name, text in (("resume", "drive an existing run to the end"), ("status", "print a run's progress")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--run", required=True)
        if name == "resume":
            sub.add_argument("--poll", type=float, default=5.0)

    cancel = commands.add_parser("cancel", help="cancel pending items; running items finish their stage")
    cancel.add_argument("--run", required=True)

    retry = commands.add_parser("retry", help="run failed or cancelled items again")
    retry.add_argument("--run", required=True)
    retry.add_argument("--statuses", default="failed", help="comma-separated: failed, cancelled")
    return parser


def print_status(session, run_id, out=sys.stdout) -> dict[str, int]:
    from app.models.validation import ValidationItem, ValidationRun

    run = session.get(ValidationRun, run_id)
    if run is None:
        raise LookupError(f"validation run {run_id} not found")
    items = session.scalars(select(ValidationItem).where(ValidationItem.run_id == run.id)).all()
    counts = Counter(item.status for item in items)
    print(f"run {run.id} '{run.name}' {run.dataset}/{run.split} [{run.status}] "
          + ", ".join(f"{status} {n}" for status, n in sorted(counts.items())), file=out)
    failures = Counter((item.failed_stage, item.error_class) for item in items if item.status == "failed")
    for (stage, error_class), n in failures.most_common():
        print(f"  failed at {stage}: {error_class} x{n}", file=out)
    return dict(counts)


def drive(session, run_id, poll_s: float) -> int:
    from eval.harness.controller import RunController

    controller = RunController(session, run_id)
    controller.run_until_done(poll_s, on_step=lambda p: print(
        "  " + ", ".join(f"{s} {n}" for s, n in sorted(p.counts.items())), flush=True
    ))
    counts = print_status(session, run_id)
    return 2 if counts.get("failed") else 0


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    args = build_parser().parse_args(argv)

    from app.core.pipeline_config import init_pipeline_config

    init_pipeline_config()
    if session_factory is None:
        from app.core.db import SessionLocal as session_factory

    from eval.harness.controller import cancel_run, retry_items
    from eval.harness.runs import RunConfigError, RunRequest, LockedTestSplitError, create_run

    session = session_factory()
    try:
        if args.command == "run":
            request = RunRequest(
                name=args.name, manifest_uri=args.manifest, split=args.split,
                stages=tuple(s.strip() for s in args.stages.split(",") if s.strip()),
                mode=args.mode, concurrency=args.concurrency, actor=args.actor,
                splits_lock=args.splits_lock, splits_root=args.splits_root, arm=args.arm,
                test_access_reason=args.confirm_test_access,
            )
            try:
                run = create_run(session, request)
            except (LockedTestSplitError, RunConfigError) as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1
            print(f"created run {run.id}", flush=True)
            return 0 if args.no_wait else drive(session, run.id, args.poll)
        if args.command == "resume":
            return drive(session, args.run, args.poll)
        if args.command == "status":
            print_status(session, args.run)
            return 0
        if args.command == "cancel":
            print(f"cancelled {cancel_run(session, args.run, args.actor)} pending items")
            return 0
        if args.command == "retry":
            statuses = tuple(s.strip() for s in args.statuses.split(",") if s.strip())
            print(f"retrying {retry_items(session, args.run, statuses, args.actor)} items")
            return 0
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        session.close()
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
