"""Stage execution wrapper (SPEC-01 §3.2, §3.6): context, records and failure semantics."""
import json
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.core.pipeline_config import get_config_hash
from app.core.run_context import RunMode
from app.core.tasks import EntityType, Task
from app.inference import schemas
from app.inference.adapters.base import TransientCallError
from app.inference.gateway import EntityRef, ModelInputs
from app.models.case import Case
from app.models.decision_record import DecisionRecord
from app.models.stage_execution import StageExecution
from tests.fakes.gateway import FakeAdapter, json_text, make_gateway, png_image
from worker.execution import StageFailedError, execute_stage, mark_running

TUBULE = {"tumor_present": True, "tubule_percent": 40, "rationale": ""}


@pytest.fixture
def Session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def queued_execution(Session, run_mode: str = "clinical", stage: str = "grading") -> uuid.UUID:
    case_id, exec_id = uuid.uuid4(), uuid.uuid4()
    with Session() as db:
        db.add(Case(id=case_id, created_by="execution_test"))
        db.add(StageExecution(id=exec_id, case_id=case_id, stage=stage, attempt=1, status="queued", run_mode=run_mode))
        db.commit()
    return exec_id


def gateway_factory(adapter):
    return lambda config, log: make_gateway(config, {"vertex_genai": adapter}, log=log)


def tubule_call(runtime, entity_id="p_001"):
    return runtime.gateway.invoke(
        Task.TUBULE_PATCH,
        "gemini_referee",
        ModelInputs(images=(png_image((512, 512), 1.0),), prompt_id="tubule@v1.md"),
        runtime.ctx,
        EntityRef(EntityType.PATCH, entity_id),
        schemas.TubuleEstimate,
    )


def run(Session, exec_id, handler, adapter):
    with Session() as db:
        execution = db.get(StageExecution, exec_id)
        mark_running(execution)
        db.commit()
        return execute_stage(
            db, execution, handlers={execution.stage: handler}, gateway_factory=gateway_factory(adapter)
        )


def records(Session, exec_id):
    with Session() as db:
        return db.scalars(
            select(DecisionRecord).where(DecisionRecord.stage_execution_id == exec_id).order_by(DecisionRecord.created_at)
        ).all()


def test_success_commits_outputs_and_records_together(Session):
    exec_id = queued_execution(Session)
    seen = {}

    def handler(stage_execution, db, runtime):
        seen["ctx"] = runtime.ctx
        seen["config"] = runtime.config
        tubule_call(runtime)
        return "gs://out/grading.json", {"gemini_referee": "v"}

    run(Session, exec_id, handler, FakeAdapter(json_text(TUBULE)))

    with Session() as db:
        done = db.get(StageExecution, exec_id)
        assert done.status == "done" and done.output_ref == "gs://out/grading.json"
        assert done.config_hash == get_config_hash() and done.error is None
    ctx = seen["ctx"]
    assert ctx.run_mode is RunMode.CLINICAL and ctx.stage == "grading" and ctx.config_hash == get_config_hash()
    assert str(ctx.stage_execution_id) == str(exec_id)
    (record,) = records(Session, exec_id)
    assert record.status == "ok" and record.output == TUBULE and record.producer_id == "gemini_referee"


def test_eval_executions_run_in_eval_mode(Session):
    exec_id = queued_execution(Session, run_mode="eval")
    seen = {}

    def handler(stage_execution, db, runtime):
        seen["mode"] = runtime.ctx.run_mode
        return "gs://out", {}

    run(Session, exec_id, handler, FakeAdapter())
    assert seen["mode"] is RunMode.EVAL


def test_gateway_failure_fails_the_stage_but_keeps_its_records(Session):
    exec_id = queued_execution(Session)

    def handler(stage_execution, db, runtime):
        db.add(Case(id=uuid.uuid4(), created_by="partial output that must be rolled back"))
        db.flush()
        tubule_call(runtime, entity_id="p_007")
        return "gs://never", {}

    with pytest.raises(StageFailedError) as raised:
        run(Session, exec_id, handler, FakeAdapter(then=TransientCallError("503")))

    error = raised.value.error
    assert error["class"] == "ModelUnavailableError"
    assert error["task"] == "tubule_patch" and error["producer_id"] == "gemini_referee"
    assert error["entity"] == {"type": "patch", "id": "p_007"}
    with Session() as db:
        failed = db.get(StageExecution, exec_id)
        assert failed.status == "failed" and failed.completed_at is not None
        stored = json.loads(failed.error)
        assert {k: stored[k] for k in ("class", "task", "producer_id", "entity", "record_id")} == {
            k: error[k] for k in ("class", "task", "producer_id", "entity", "record_id")
        }
        assert db.scalars(select(Case).where(Case.created_by.like("partial%"))).all() == []
    kept = records(Session, exec_id)
    assert {r.status for r in kept} == {"unavailable"}
    assert str(kept[-1].id) == error["record_id"]


def test_other_exceptions_fail_the_stage_with_their_class(Session):
    exec_id = queued_execution(Session)

    def handler(stage_execution, db, runtime):
        raise ValueError("slide has no mpp")

    with pytest.raises(StageFailedError):
        run(Session, exec_id, handler, FakeAdapter())
    with Session() as db:
        stored = json.loads(db.get(StageExecution, exec_id).error)
    assert stored["class"] == "ValueError" and stored["detail"] == "slide has no mpp"
    assert "Traceback" in stored["traceback"]


def test_handler_may_leave_the_stage_awaiting_review(Session):
    exec_id = queued_execution(Session)

    def handler(stage_execution, db, runtime):
        stage_execution.status = "awaiting_review"
        return "gs://out", {}

    run(Session, exec_id, handler, FakeAdapter())
    with Session() as db:
        assert db.get(StageExecution, exec_id).status == "awaiting_review"


def test_poll_loop_runs_stages_through_the_wrapper(Session):
    from worker.main import poll_and_execute_single_task

    exec_id = queued_execution(Session)

    def handler(stage_execution, db, runtime):
        tubule_call(runtime)
        return "gs://out", {}

    with patch("worker.main.SessionLocal", Session), \
         patch("worker.main.HANDLERS", {"grading": handler}), \
         patch("worker.execution.production_gateway", gateway_factory(FakeAdapter(json_text(TUBULE)))):
        assert poll_and_execute_single_task() is True
    with Session() as db:
        assert db.get(StageExecution, exec_id).status == "done"
    assert [r.status for r in records(Session, exec_id)] == ["ok"]


def test_cloud_job_reports_failure_with_exit_code_2(Session):
    from worker.cloud_job_entry import execute_cloud_job

    exec_id = queued_execution(Session)
    with Session() as db:
        case_id = db.get(StageExecution, exec_id).case_id

    def handler(stage_execution, db, runtime):
        raise RuntimeError("boom")

    with patch("worker.cloud_job_entry.SessionLocal", Session), \
         patch("worker.cloud_job_entry.HANDLERS", {"grading": handler}):
        assert execute_cloud_job(str(case_id), "grading", str(exec_id)) == 2
    with Session() as db:
        failed = db.get(StageExecution, exec_id)
        assert failed.status == "failed" and failed.config_hash == get_config_hash()


def test_webhook_stamps_the_config_hash(Session):
    import app.main as main

    exec_id = queued_execution(Session)
    with Session() as db:
        case_id = str(db.get(StageExecution, exec_id).case_id)

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db] = override_db
    try:
        with patch.dict("worker.execution.STAGE_HANDLERS", {"grading": lambda se, db, rt: ("gs://out", {})}):
            response = TestClient(main.app).post(
                "/api/v1/internal/execute-stage",
                json={"case_id": case_id, "stage": "grading", "stage_exec_id": str(exec_id)},
            )
    finally:
        main.app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 200, response.text
    with Session() as db:
        done = db.get(StageExecution, exec_id)
        assert done.status == "done" and done.config_hash == get_config_hash()
