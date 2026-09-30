"""The evaluation worker: run-mode split, scratch-space check, idle exit (SPEC-02 §6.2)."""
import uuid
from collections import namedtuple

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.gcs import upload_blob_from_bytes
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from tests.harness.test_controller import Session, db  # noqa: F401  (fixture)
import worker.main as worker_main
import worker.scratch as scratch

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def worker(db, monkeypatch):
    """The worker loop's claiming path over the test DB; executing a stage just records its run mode."""
    ran = []

    def execute(session, execution, handlers=None):
        ran.append(execution.run_mode)
        execution.status = "done"
        session.commit()

    monkeypatch.setattr(worker_main, "SessionLocal", Session)
    monkeypatch.setattr(worker_main, "HANDLERS", {"triage": None})
    monkeypatch.setattr(worker_main, "execute_stage", execute)
    return ran


def queue(db, run_mode: str, slide_bytes: int = 1000) -> StageExecution:
    case = Case(id=uuid.uuid4(), created_by="t", status="open", specimen_type="resection")
    name = f"slides/{case.id}.svs"
    upload_blob_from_bytes("og-scratch-test", name, b"x" * slide_bytes, "application/octet-stream")
    db.add_all([case, Slide(case_id=case.id, gcs_uri_original=f"gs://og-scratch-test/{name}")])
    execution = StageExecution(case_id=case.id, stage="triage", attempt=1, status="queued", run_mode=run_mode)
    db.add(execution)
    db.commit()
    return execution


def test_a_worker_takes_only_its_run_modes(db, worker, monkeypatch):
    clinical, evaluation = queue(db, "clinical"), queue(db, "eval")
    monkeypatch.setattr(settings, "WORKER_RUN_MODES", "clinical")
    assert worker_main.poll_and_execute_single_task() is True
    assert worker_main.poll_and_execute_single_task() is False  # the eval stage is not this worker's
    assert worker == ["clinical"]
    monkeypatch.setattr(settings, "WORKER_RUN_MODES", "eval")
    assert worker_main.poll_and_execute_single_task() is True
    assert worker == ["clinical", "eval"]
    db.expire_all()
    assert {db.get(StageExecution, e.id).status for e in (clinical, evaluation)} == {"done"}


def test_unknown_run_modes_are_refused(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RUN_MODES", "clinical,batch")
    with pytest.raises(ValueError, match="WORKER_RUN_MODES"):
        worker_main.worker_run_modes()


def test_a_stage_is_not_claimed_without_twice_the_slide_in_scratch(db, worker, monkeypatch):
    execution = queue(db, "eval", slide_bytes=1000)
    monkeypatch.setattr(settings, "WORKER_RUN_MODES", "eval")
    monkeypatch.setattr(scratch.shutil, "disk_usage", lambda path: Usage(10_000, 8_500, 1_500))
    assert worker_main.poll_and_execute_single_task() is False
    db.expire_all()
    assert db.get(StageExecution, execution.id).status == "queued" and worker == []

    monkeypatch.setattr(scratch.shutil, "disk_usage", lambda path: Usage(10_000, 7_000, 3_000))
    assert worker_main.poll_and_execute_single_task() is True


def test_a_missing_slide_object_is_an_error(db, monkeypatch):
    execution = queue(db, "eval")
    slide = db.scalars(select(Slide).where(Slide.case_id == execution.case_id)).one()
    slide.gcs_uri_original = "gs://og-scratch-test/nowhere.svs"
    db.commit()
    with pytest.raises(scratch.SlideObjectMissing):
        scratch.check_scratch(db, execution, 2.0)


def test_an_idle_daemon_exits(db, monkeypatch):
    monkeypatch.setattr(worker_main, "SessionLocal", Session)
    monkeypatch.setattr(settings, "WORKER_RUN_MODES", "eval")
    monkeypatch.setattr(settings, "WORKER_IDLE_EXIT_S", 0.01)
    monkeypatch.setattr(worker_main.time, "sleep", lambda s: None)
    worker_main.run_worker_loop()  # returns instead of polling forever
