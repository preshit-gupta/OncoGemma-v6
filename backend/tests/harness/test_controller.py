"""The validation harness: runs, controller, collect, CLI (SPEC-02 §5.3, §5.5; AC1, AC2, AC8)."""
import hashlib
import json
import uuid

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base
from app.models.audit import AuditEvent
from app.models.case import Case
from app.models.stage_execution import StageExecution
from app.models.validation import ValidationItem, ValidationRun
from app.services import stages as stage_service
from eval import cli
from eval.datasets.manifest import MANIFEST_COLUMNS
from eval.harness.controller import ManifestChangedError, RunController, cancel_run, retry_items
from eval.harness.runs import RunConfigError, RunRequest, LockedTestSplitError, check_stages, create_run
from tests.harness import fake_pipeline

ALL_STAGES = ("ingest", "preprocess", "qc", "triage", "mitosis", "grading")

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    fake_pipeline.BOOM["on"] = True
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


def manifest_row(slide_id: str, kind: str, split: str = "val", **values) -> dict:
    row = {column: None for column in MANIFEST_COLUMNS}
    row.update({
        "dataset": "tcga_brca_dx", "patient_id": f"TCGA-{slide_id}", "slide_id": slide_id,
        "uri": f"gs://og-datasets/tcga/{kind}-{slide_id}.svs", "sha256": hashlib.sha256(slide_id.encode()).hexdigest(),
        "specimen_type": "resection", "mpp_source": "file", "split": split,
        "gt_grade": 2, "gt_total": 6, "gt_tubule": 2, "gt_pleo": 2, "gt_mitoses": 2,
    })
    row.update(values)
    return row


def write_manifest(tmp_path, rows) -> str:
    frame = pd.DataFrame(rows).astype(MANIFEST_COLUMNS)
    path = tmp_path / "manifest.parquet"
    frame.to_parquet(path)
    return str(path)


@pytest.fixture
def lock(tmp_path):
    split_file = tmp_path / "splits" / "tcga_brca_dx.parquet"
    split_file.parent.mkdir()
    split_file.write_bytes(b"splits")
    lock_path = tmp_path / "SPLITS.lock"
    lock_path.write_text(json.dumps({"splits/tcga_brca_dx.parquet": hashlib.sha256(b"splits").hexdigest()}))
    return lock_path


def request(manifest: str, lock, **values) -> RunRequest:
    fields = dict(name="tcga-val", manifest_uri=manifest, split="val", stages=ALL_STAGES, mode="auto",
                  concurrency=2, actor="researcher@example.org", splits_lock=lock, splits_root=lock.parent)
    fields.update(values)
    return RunRequest(**fields)


def drive(db, run_id, *, max_passes=50, watch=None) -> RunController:
    controller = RunController(db, run_id)
    for _ in range(max_passes):
        progress = controller.step()
        if watch is not None:
            watch(progress)
        if controller.run.status == "completed":
            return controller
        fake_pipeline.drain(db)
    raise AssertionError("run did not complete")


def items_by_slide(db, run_id) -> dict[str, ValidationItem]:
    return {i.slide_id: i for i in db.scalars(select(ValidationItem).where(ValidationItem.run_id == run_id))}


# --- AC1: every item ends succeeded, failed with a real error class, or excluded_qc -------


def test_auto_run_ends_every_item_in_one_terminal_state(db, tmp_path, lock):
    manifest = write_manifest(tmp_path, [
        manifest_row("s1", "ok"), manifest_row("s2", "qcfail"), manifest_row("s3", "boom"),
        manifest_row("s4", "notumour"), manifest_row("s5", "ok", mpp_override=0.2527, mpp_source="dataset_doc"),
        manifest_row("t1", "ok", split="train"),
    ])
    run = create_run(db, request(manifest, lock))
    peak = []

    drive(db, run.id, watch=lambda p: peak.append(p.counts.get("running", 0)))

    items = items_by_slide(db, run.id)
    assert set(items) == {"s1", "s2", "s3", "s4", "s5"}  # only the requested split
    assert max(peak) <= 2  # --concurrency
    assert {s: i.status for s, i in items.items()} == {
        "s1": "succeeded", "s2": "excluded_qc", "s3": "failed", "s4": "succeeded", "s5": "succeeded",
    }
    assert (items["s3"].failed_stage, items["s3"].error_class) == ("triage", "RuntimeError")
    assert "tumour head unavailable" in items["s3"].error_detail
    assert (items["s2"].failed_stage, items["s2"].error_class) == ("qc", "QcHardFail")

    ok = items["s1"].prediction
    assert ok["grading"]["grade"] == 2 and ok["grading"]["total"] == 6
    assert ok["mitosis"]["count"] == 3 and len(ok["mitosis"]["points_um"]) == 3
    assert ok["no_invasive_tumor"] is False and ok["stage_outputs"]["grading"]["status"] == "awaiting_review"
    assert items["s4"].prediction["no_invasive_tumor"] is True and "grading" not in items["s4"].prediction
    assert all(i.runtime_s is not None and i.cost_usd == 0 for i in items.values())

    s5_case = db.get(Case, items["s5"].case_id)
    assert s5_case.slides[0].mpp_x == 0.2527 and s5_case.slides[0].mpp_source == "dataset_doc"

    run = db.get(ValidationRun, run.id)
    assert run.status == "completed" and run.finished_at is not None
    executions = db.scalars(select(StageExecution)).all()
    assert executions and all(e.run_mode == "eval" and str(e.run_id) == str(run.id) for e in executions)
    confirmed = [e for e in executions if e.status == "confirmed"]
    assert confirmed and all(e.reviewed_by == f"harness:{run.id}" for e in confirmed)
    # The last stage is never confirmed: the prediction is the machine's.
    assert all(e.status == "awaiting_review" for e in executions if e.stage == "grading")


def test_a_run_can_end_at_triage(db, tmp_path, lock):
    manifest = write_manifest(tmp_path, [manifest_row("s1", "ok")])
    run = create_run(db, request(manifest, lock, stages=ALL_STAGES[:4]))
    drive(db, run.id)
    item = items_by_slide(db, run.id)["s1"]
    assert item.status == "succeeded" and item.prediction["hotspots"] == {"active": 1, "excluded": 0}
    assert stage_service.latest_execution(db, item.case_id, "mitosis") is None


# --- AC2: resume repeats no stage ----------------------------------------------------


def test_resume_after_the_controller_dies_repeats_no_stage(db, tmp_path, lock):
    manifest = write_manifest(tmp_path, [manifest_row(f"s{i}", "ok") for i in range(4)])
    run = create_run(db, request(manifest, lock, concurrency=3))

    first = RunController(db, run.id)
    for _ in range(3):
        first.step()
        fake_pipeline.drain(db)
    del first  # killed mid-run
    assert db.get(ValidationRun, run.id).status == "running"

    drive(db, run.id)

    items = items_by_slide(db, run.id)
    assert len(items) == 4 and all(i.status == "succeeded" for i in items.values())
    for item in items.values():
        attempts = db.scalars(select(StageExecution.attempt).where(StageExecution.case_id == item.case_id)).all()
        assert attempts == [1] * len(ALL_STAGES)
    assert len(db.scalars(select(Case)).all()) == 4


# --- manual mode, cancel, retry -----------------------------------------------------


def test_manual_mode_waits_for_a_person_to_confirm(db, tmp_path, lock):
    manifest = write_manifest(tmp_path, [manifest_row("s1", "ok")])
    run = create_run(db, request(manifest, lock, mode="manual"))
    controller = RunController(db, run.id)
    for _ in range(6):
        controller.step()
        fake_pipeline.drain(db)
    item = items_by_slide(db, run.id)["s1"]
    assert item.status == "running"
    assert stage_service.latest_execution(db, item.case_id, "triage").status == "awaiting_review"

    stage_service.confirm_stage(db, item.case_id, "triage", "pathologist-1", accept_fewer_hpfs=True)  # one site: inadequate tissue
    fake_pipeline.drain(db)
    stage_service.confirm_stage(db, item.case_id, "mitosis", "pathologist-1")
    drive(db, run.id)
    assert items_by_slide(db, run.id)["s1"].status == "succeeded"


def test_retry_runs_the_failed_stage_again_and_keeps_the_old_attempt(db, tmp_path, lock):
    manifest = write_manifest(tmp_path, [manifest_row("s1", "boom")])
    run = create_run(db, request(manifest, lock))
    drive(db, run.id)
    assert items_by_slide(db, run.id)["s1"].status == "failed"

    fake_pipeline.BOOM["on"] = False
    assert retry_items(db, run.id, ("failed",), "researcher@example.org") == 1
    fake_pipeline.drain(db)
    drive(db, run.id)

    item = items_by_slide(db, run.id)["s1"]
    assert item.status == "succeeded" and item.error_class is None
    attempts = db.scalars(
        select(StageExecution.status).where(StageExecution.case_id == item.case_id, StageExecution.stage == "triage")
        .order_by(StageExecution.attempt)
    ).all()
    assert attempts == ["failed", "confirmed"]


def test_cancel_stops_pending_items_only(db, tmp_path, lock):
    manifest = write_manifest(tmp_path, [manifest_row(f"s{i}", "ok") for i in range(3)])
    run = create_run(db, request(manifest, lock, concurrency=1))
    controller = RunController(db, run.id)
    controller.step()
    assert cancel_run(db, run.id, "researcher@example.org") == 2
    drive(db, run.id)
    statuses = sorted(i.status for i in items_by_slide(db, run.id).values())
    assert statuses == ["cancelled", "cancelled", "succeeded"]
    with pytest.raises(ValueError):
        retry_items(db, run.id, ("succeeded",), "researcher@example.org")


# --- run creation, AC8 test lock -----------------------------------------------------


def test_test_split_needs_a_reason_and_is_audited(db, tmp_path, lock):
    manifest = write_manifest(tmp_path, [manifest_row("s1", "ok", split="test")])
    with pytest.raises(LockedTestSplitError):
        create_run(db, request(manifest, lock, split="test"))
    assert db.scalars(select(ValidationRun)).first() is None

    run = create_run(db, request(manifest, lock, split="test", test_access_reason="final locked evaluation, v6.0"))
    assert run.is_locked_test
    event = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "test_split_access")).one()
    assert event.actor == "researcher@example.org"
    assert event.payload["reason"] == "final locked evaluation, v6.0" and event.payload["run_id"] == str(run.id)
    assert event.payload["config_hash"] == run.config_hash


def test_cli_refuses_the_test_split_without_confirmation(db, tmp_path, lock):
    manifest = write_manifest(tmp_path, [manifest_row("s1", "ok", split="test")])
    base = ["run", "--manifest", manifest, "--stages", ",".join(ALL_STAGES), "--name", "t",
            "--splits-lock", str(lock), "--splits-root", str(lock.parent), "--no-wait"]
    assert cli.main([*base, "--split", "test"], session_factory=Session) == 1
    assert cli.main([*base, "--split", "test", "--confirm-test-access", "locked run"], session_factory=Session) == 0
    assert db.scalars(select(ValidationRun)).one().is_locked_test


def test_run_records_what_it_evaluated(db, tmp_path, lock):
    manifest = write_manifest(tmp_path, [manifest_row("s1", "ok")])
    run = create_run(db, request(manifest, lock, arm="A4"))
    with open(manifest, "rb") as f:
        assert run.manifest_sha256 == hashlib.sha256(f.read()).hexdigest()
    assert run.splits_lock_sha256 == hashlib.sha256(lock.read_bytes()).hexdigest()
    assert len(run.config_hash) == 64 and len(run.registry_sha256) == 64
    assert (run.arm, run.status, run.stages) == ("A4", "created", list(ALL_STAGES))


def test_a_changed_manifest_is_refused(db, tmp_path, lock):
    manifest = write_manifest(tmp_path, [manifest_row("s1", "ok")])
    run = create_run(db, request(manifest, lock))
    write_manifest(tmp_path, [manifest_row("s1", "ok"), manifest_row("s2", "ok")])
    with pytest.raises(ManifestChangedError):
        RunController(db, run.id).step()


def test_a_broken_splits_lock_is_refused(db, tmp_path, lock):
    from eval.splits import LockMismatchError

    manifest = write_manifest(tmp_path, [manifest_row("s1", "ok")])
    (lock.parent / "splits" / "tcga_brca_dx.parquet").write_bytes(b"edited")
    with pytest.raises(LockMismatchError):
        create_run(db, request(manifest, lock))


@pytest.mark.parametrize("stages", [
    ("preprocess", "qc", "triage"),
    ("ingest", "qc", "triage"),
    ("ingest", "preprocess"),
    (),
])
def test_stages_run_from_ingest_to_a_review_stage(stages):
    with pytest.raises(RunConfigError):
        check_stages(stages)


def test_an_unknown_run_is_reported(db):
    assert cli.main(["status", "--run", str(uuid.uuid4())], session_factory=Session) == 1
