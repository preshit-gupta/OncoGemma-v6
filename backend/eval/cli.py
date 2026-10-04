"""``oncogemma-eval``: the validation harness CLI (SPEC-02 §5.5).

    python -m eval.cli run     --manifest eval/manifests/tcga.parquet --split val \\
                               --stages ingest,preprocess,qc,triage,mitosis,grading \\
                               --mode auto --concurrency 8 --arm A4 --name "tcga-val-A4"
    python -m eval.cli resume  --run <run_id>
    python -m eval.cli status  --run <run_id>
    python -m eval.cli cancel  --run <run_id>
    python -m eval.cli retry   --run <run_id> --statuses failed
    python -m eval.cli metrics --run <run_id> [--bootstrap 2000 --seed 7] [--upload gs://bucket/reports]
    python -m eval.cli compare --run-a <id> --run-b <id> --metric ns_g
    python -m eval.cli one-shot --slide gs://.../x.svs --specimen resection [--mpp 0.25] --out result.json

The CLI talks to the app's database (``DATABASE_URL`` or the Cloud SQL settings) and queue; the
app's workers run the stages (``one-shot`` runs them in-process unless ``--use-workers``).
``--split test`` is refused without ``--confirm-test-access``.
Exit codes: 0 done, 1 refused or not found, 2 the run finished with failed items (one-shot: the
slide did not succeed).
"""
from __future__ import annotations

import argparse
import getpass
import sys
import time
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

    metrics = commands.add_parser("metrics", help="write reports/<run_id>/metrics.json and report.html")
    metrics.add_argument("--run", required=True)
    metrics.add_argument("--bootstrap", type=int, default=2000, help="bootstrap resamples (B)")
    metrics.add_argument("--seed", type=int, default=7)
    metrics.add_argument("--out-dir", type=Path, default=Path("reports"))
    metrics.add_argument("--upload", default=None, metavar="GS_PREFIX",
                         help="also upload both files under this gs:// prefix and link metrics.json from the run")

    compare = commands.add_parser("compare", help="paired bootstrap of a metric between two runs")
    compare.add_argument("--run-a", required=True)
    compare.add_argument("--run-b", required=True)
    compare.add_argument("--metric", default="ns_g", help="ns_g, qwk, f1_high, macro_f1_lm, sum_mae, f1_t, f1_p or f1_m")
    compare.add_argument("--bootstrap", type=int, default=2000)
    compare.add_argument("--seed", type=int, default=7)

    one_shot = commands.add_parser("one-shot", help="one slide in EVAL mode; writes one JSON document")
    one_shot.add_argument("--slide", required=True, help="gs:// URI of the slide")
    one_shot.add_argument("--specimen", required=True, choices=("resection", "core_biopsy"))
    one_shot.add_argument("--mpp", type=float, default=None, help="µm/px when the file has none (recorded as manual)")
    one_shot.add_argument("--out", type=Path, required=True)
    one_shot.add_argument("--stages", default="ingest,preprocess,qc,triage,mitosis,grading")
    one_shot.add_argument("--use-workers", action="store_true", help="let the app's workers run the stages")
    return parser


def write_metrics(session, run_id, B: int, seed: int, out_dir: Path, upload: str | None) -> Path:
    from app.core.gcs import parse_gcs_uri, upload_blob_from_filename
    from app.models.validation import ValidationRun
    from eval.harness.report import compute_metrics, write_report

    doc = compute_metrics(session, run_id, B=B, seed=seed)
    metrics_path, page_path = write_report(doc, out_dir / doc.run.id)
    link = str(metrics_path)
    if upload is not None:
        bucket, prefix = parse_gcs_uri(upload.rstrip("/") + "/")
        for path, content_type in ((metrics_path, "application/json"), (page_path, "text/html")):
            upload_blob_from_filename(bucket, f"{prefix}{doc.run.id}/{path.name}", str(path), content_type)
        link = f"gs://{bucket}/{prefix}{doc.run.id}/metrics.json"
    run = session.get(ValidationRun, run_id)
    run.metrics_uri = link
    session.commit()
    print(f"wrote {metrics_path} and {page_path}" + (f"; uploaded to {link}" if upload else ""))
    return metrics_path


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
    from app.core.config import settings
    from eval.harness.controller import FINAL_RUN, RunController, item_progress
    from eval.harness.driver import step_under_lease, worker_identity

    # Under the workers' lease: while an eval worker drives the run, this loop only reports progress.
    controller, owner = RunController(session, run_id), worker_identity("cli")
    while True:
        step_under_lease(session, controller, owner, lease_s=settings.HARNESS_LEASE_S)
        counts = item_progress(session, run_id).counts
        print("  " + ", ".join(f"{s} {n}" for s, n in sorted(counts.items())), flush=True)
        if controller.run.status in FINAL_RUN:
            break
        time.sleep(poll_s)
    counts = print_status(session, run_id)
    return 2 if counts.get("failed") else 0


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    args = build_parser().parse_args(argv)

    from app.core.pipeline_config import init_pipeline_config

    init_pipeline_config()
    if session_factory is None:
        from app.core.db import SessionLocal as session_factory

    from eval.harness.controller import cancel_run, retry_items
    from eval.harness.one_shot import one_shot
    from eval.harness.report import RunNotFinishedError, RunsNotComparableError, compare_runs, dumps
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
        if args.command == "metrics":
            if args.upload is not None and not args.upload.startswith("gs://"):
                print(f"refused: --upload must be a gs:// prefix, got {args.upload!r}", file=sys.stderr)
                return 1
            try:
                write_metrics(session, args.run, args.bootstrap, args.seed, args.out_dir, args.upload)
            except RunNotFinishedError as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1
            return 0
        if args.command == "compare":
            try:
                result = compare_runs(session, args.run_a, args.run_b, args.metric, B=args.bootstrap, seed=args.seed)
            except (RunNotFinishedError, RunsNotComparableError, ValueError) as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1
            print(dumps(result))
            return 0
        if args.command == "one-shot":
            code, result = one_shot(
                session, args.slide, args.specimen, args.out, actor=args.actor, mpp=args.mpp,
                stages=tuple(s.strip() for s in args.stages.split(",") if s.strip()),
                in_process=not args.use_workers,
            )
            print(f"{result.status}: wrote {args.out}" + (f" ({result.failed_stage}: {result.error_class})" if code else ""))
            return code
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        session.close()
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
