"""Batch API and worker-driven runs (SPEC-02 §6.1; AC7; owner decision 2026-09-30, option A)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.core.db import get_db
from app.core.gcs import upload_blob_from_bytes
from app.main import app
from app.models.audit import AuditEvent
from app.models.stage_execution import StageExecution
from app.models.validation import ValidationItem, ValidationRun
from eval.harness.driver import drive_active_runs, take_lease
from tests.harness import fake_pipeline
from tests.harness.test_controller import (  # noqa: F401  (fixtures)
    ALL_STAGES,
    Session,
    db,
    lock,
    manifest_row,
    write_manifest,
)

RESEARCHER = {"X-Test-Role": "researcher", "X-Test-User-Id": "researcher-1"}
ADMIN = {"X-Test-Role": "admin", "X-Test-User-Id": "admin-1"}
LEASE_S = 60.0


@pytest.fixture
def client(db):
    def override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def put_slides(*names: str) -> str:
    prefix = f"incoming-{uuid.uuid4().hex[:8]}"
    for name in names:
        upload_blob_from_bytes("og-batches", f"{prefix}/{name}", b"slide bytes", "application/octet-stream")
    return f"gs://og-batches/{prefix}"


def prefix_batch(prefix: str, **values) -> dict:
    body = {"name": "adhoc batch", "source": {"gcs_prefix": prefix, "specimen_type": "resection", "mpp_override": 0.25},
            "stages": list(ALL_STAGES), "mode": "auto", "concurrency": 2}
    body.update(values)
    return body


def work(db, *, owner="worker:a", passes=40, now=None) -> None:
    """Workers: drive the runs they hold, then run the queued stages, until no run is active."""
    for _ in range(passes):
        drive_active_runs(Session, owner, lease_s=LEASE_S, **({"now": now} if now else {}))
        fake_pipeline.drain(db)
        db.expire_all()
        if not db.scalars(select(ValidationRun).where(ValidationRun.status.in_(("created", "running")))).first():
            return
    raise AssertionError("runs did not finish")


def test_create_progress_cancel_retry(client, db):
    """AC7: create -> progress -> cancel -> retry-failed, driven by the workers."""
    prefix = put_slides("ok-a.svs", "boom-b.svs", "ok-c.ndpi", "notes.txt")
    created = client.post("/api/v1/batches", json=prefix_batch(prefix), headers=RESEARCHER)
    assert created.status_code == 201
    batch_id = created.json()["batch_id"]

    queued = client.get(f"/api/v1/batches/{batch_id}", headers=RESEARCHER).json()
    assert queued["status"] == "created" and queued["counts"] == {"pending": 3}  # notes.txt is not a slide
    assert (queued["dataset"], queued["split"]) == ("adhoc", "adhoc")

    work(db)
    done = client.get(f"/api/v1/batches/{batch_id}", headers=RESEARCHER).json()
    assert done["status"] == "completed"
    assert done["counts"] == {"succeeded": 2, "failed": 1}
    assert done["failures_by_error_class"] == {"RuntimeError": 1}
    failed = next(i for i in done["items"] if i["status"] == "failed")
    assert (failed["slide_id"], failed["failed_stage"]) == ("boom-b.svs", "triage")

    # Ingest had no hash to check against, so the slide keeps the one ingest records.
    ok_item = next(i for i in done["items"] if i["slide_id"] == "ok-a.svs")
    ingest = db.scalars(select(StageExecution).where(StageExecution.case_id == ok_item["case_id"],
                                                     StageExecution.stage == "ingest")).one()
    assert ingest.run_mode == "eval" and str(ingest.run_id) == batch_id

    fake_pipeline.BOOM["on"] = False
    retried = client.post(f"/api/v1/batches/{batch_id}/retry", json={"statuses": ["failed"]}, headers=RESEARCHER)
    assert retried.status_code == 202 and retried.json()["retried_items"] == 1
    work(db)
    after = client.get(f"/api/v1/batches/{batch_id}", headers=RESEARCHER).json()
    assert after["status"] == "completed" and after["counts"] == {"succeeded": 3}

    listed = client.get("/api/v1/batches", headers={"X-Test-Role": "pathologist"})
    assert listed.status_code == 200 and [b["batch_id"] for b in listed.json()] == [batch_id]


def test_cancel_stops_pending_items(client, db):
    prefix = put_slides("ok-a.svs", "ok-b.svs", "ok-c.svs")
    batch_id = client.post("/api/v1/batches", json=prefix_batch(prefix, concurrency=1), headers=RESEARCHER).json()["batch_id"]
    drive_active_runs(Session, "worker:a", lease_s=LEASE_S)  # starts one item

    cancelled = client.post(f"/api/v1/batches/{batch_id}/cancel", headers=RESEARCHER)
    assert cancelled.status_code == 202 and cancelled.json()["cancelled_items"] == 2
    work(db)
    assert client.get(f"/api/v1/batches/{batch_id}", headers=RESEARCHER).json()["counts"] == {"cancelled": 2, "succeeded": 1}


def test_events_stream_progress_until_the_batch_ends(client, db):
    prefix = put_slides("ok-a.svs")
    batch_id = client.post("/api/v1/batches", json=prefix_batch(prefix), headers=RESEARCHER).json()["batch_id"]
    work(db)
    with client.stream("GET", f"/api/v1/batches/{batch_id}/events", headers=RESEARCHER) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    assert body.startswith("data: ") and '"status": "completed"' in body and '"succeeded": 1' in body


# --- one controller per run -------------------------------------------------------


def test_one_worker_drives_a_run_and_another_takes_over_when_it_stops(client, db):
    prefix = put_slides("ok-a.svs", "ok-b.svs")
    batch_id = uuid.UUID(client.post("/api/v1/batches", json=prefix_batch(prefix), headers=RESEARCHER).json()["batch_id"])
    t0 = datetime.now(timezone.utc)

    assert drive_active_runs(Session, "worker:a", lease_s=LEASE_S, now=lambda: t0) == [str(batch_id)]
    assert drive_active_runs(Session, "worker:b", lease_s=LEASE_S, now=lambda: t0) == []  # a holds the lease
    fake_pipeline.drain(db)

    # worker:a dies; after its lease runs out, worker:b continues the run without repeating a stage.
    later = t0 + timedelta(seconds=LEASE_S + 1)
    assert take_lease(db, batch_id, "worker:b", lease_s=LEASE_S, now=later)
    work(db, owner="worker:b", now=lambda: later)
    items = db.scalars(select(ValidationItem).where(ValidationItem.run_id == batch_id)).all()
    assert [i.status for i in items] == ["succeeded", "succeeded"]
    for item in items:
        attempts = db.scalars(select(StageExecution.attempt).where(StageExecution.case_id == item.case_id)).all()
        assert attempts == [1] * len(ALL_STAGES)
    run = db.get(ValidationRun, batch_id)
    assert run.controller_lease_owner is None  # released when the run ended


def test_a_controller_error_fails_the_run_loudly(client, db, tmp_path, lock, monkeypatch):
    monkeypatch.setattr(settings, "SPLITS_LOCK_PATH", str(lock))
    monkeypatch.setattr(settings, "SPLITS_ROOT", str(lock.parent))
    manifest = write_manifest(tmp_path, [manifest_row("s1", "ok")])
    upload_blob_from_bytes("og-manifests", "tcga/m.parquet", open(manifest, "rb").read(), "application/octet-stream")
    body = {"name": "tcga", "source": {"manifest_uri": "gs://og-manifests/tcga/m.parquet", "split": "val"},
            "stages": list(ALL_STAGES)}
    batch_id = client.post("/api/v1/batches", json=body, headers=RESEARCHER).json()["batch_id"]
    upload_blob_from_bytes("og-manifests", "tcga/m.parquet", b"changed", "application/octet-stream")

    with pytest.raises(Exception, match="SHA-256|Parquet|parquet"):
        drive_active_runs(Session, "worker:a", lease_s=LEASE_S)
    db.expire_all()
    assert db.get(ValidationRun, uuid.UUID(batch_id)).status == "failed"
    event = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "validation_run_failed")).one()
    assert event.payload["run_id"] == batch_id


# --- creation rules ---------------------------------------------------------------


def test_manifest_batches_on_the_test_split_need_the_permission_and_a_reason(client, db, tmp_path, lock, monkeypatch):
    monkeypatch.setattr(settings, "SPLITS_LOCK_PATH", str(lock))
    monkeypatch.setattr(settings, "SPLITS_ROOT", str(lock.parent))
    manifest = write_manifest(tmp_path, [manifest_row("s1", "ok", split="test")])
    upload_blob_from_bytes("og-manifests", "tcga/test.parquet", open(manifest, "rb").read(), "application/octet-stream")
    source = {"manifest_uri": "gs://og-manifests/tcga/test.parquet", "split": "test"}
    body = {"name": "locked", "source": source, "stages": list(ALL_STAGES)}

    assert client.post("/api/v1/batches", json=body, headers=RESEARCHER).status_code == 403
    refused = client.post("/api/v1/batches", json=body, headers=ADMIN)
    assert refused.status_code == 422 and refused.json()["detail"].startswith("test_split_locked")
    body["source"] = {**source, "confirm_test_access": "locked evaluation of release 6.0"}
    created = client.post("/api/v1/batches", json=body, headers=ADMIN)
    assert created.status_code == 201
    run = db.get(ValidationRun, uuid.UUID(created.json()["batch_id"]))
    assert run.is_locked_test and run.created_by == "admin-1"


@pytest.mark.parametrize("names, detail", [
    (("scan-a.jpg", "ok-b.svs"), "JPEG"),
    (("notes.txt",), "no slide"),
])
def test_prefixes_without_supported_slides_are_refused(client, db, names, detail):
    resp = client.post("/api/v1/batches", json=prefix_batch(put_slides(*names)), headers=RESEARCHER)
    assert resp.status_code == 422 and detail in resp.json()["detail"]
    assert db.scalars(select(ValidationRun)).first() is None


def test_bad_requests_are_refused(client, db):
    prefix = put_slides("ok-a.svs")
    assert client.post("/api/v1/batches", json=prefix_batch(prefix, stages=["ingest", "qc"]), headers=RESEARCHER).status_code == 422
    assert client.post("/api/v1/batches", json=prefix_batch(prefix), headers={"X-Test-Role": "pathologist"}).status_code == 403
    assert client.get(f"/api/v1/batches/{uuid.uuid4()}", headers=RESEARCHER).status_code == 404
    batch_id = client.post("/api/v1/batches", json=prefix_batch(prefix), headers=RESEARCHER).json()["batch_id"]
    bad_retry = client.post(f"/api/v1/batches/{batch_id}/retry", json={"statuses": ["succeeded"]}, headers=RESEARCHER)
    assert bad_retry.status_code == 422
