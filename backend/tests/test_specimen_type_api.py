"""Specimen type on cases: creation, the PATCH route and the response (SPEC-04 §3.2)."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import Base, SessionLocal, engine
from app.main import app
from app.models.audit import AuditEvent
from app.models.case import Case

PATHOLOGIST = {"X-Test-Role": "pathologist"}


@pytest.fixture
def client():
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


def test_a_case_without_a_stated_specimen_type_is_unknown(client):
    res = client.post("/api/v1/cases", headers=PATHOLOGIST)
    assert res.status_code == 201 and res.json()["specimen_type"] == "unknown"
    assert client.post("/api/v1/cases", headers=PATHOLOGIST, json={}).json()["specimen_type"] == "unknown"


@pytest.mark.parametrize("specimen", ["resection", "core_biopsy"])
def test_a_case_can_be_created_with_its_specimen_type(client, specimen):
    res = client.post("/api/v1/cases", headers=PATHOLOGIST, json={"specimen_type": specimen})
    assert res.status_code == 201 and res.json()["specimen_type"] == specimen
    detail = client.get(f"/api/v1/cases/{res.json()['id']}", headers=PATHOLOGIST)
    assert detail.status_code == 200 and detail.json()["specimen_type"] == specimen
    with SessionLocal() as db:
        event = db.scalars(select(AuditEvent).where(AuditEvent.case_id == res.json()["id"], AuditEvent.event_type == "case_created")).one()
        assert event.payload["specimen_type"] == specimen


@pytest.mark.parametrize("bad", ["unknown", "biopsy", "", None, 5])
def test_creation_refuses_anything_but_the_two_specimen_types(client, bad):
    if bad is None:  # null is the same as not stating it
        assert client.post("/api/v1/cases", headers=PATHOLOGIST, json={"specimen_type": None}).status_code == 201
    else:
        assert client.post("/api/v1/cases", headers=PATHOLOGIST, json={"specimen_type": bad}).status_code == 422


def test_the_specimen_type_can_be_set_afterwards_and_is_audited(client):
    case_id = client.post("/api/v1/cases", headers=PATHOLOGIST).json()["id"]
    res = client.patch(f"/api/v1/cases/{case_id}/specimen-type", headers=PATHOLOGIST, json={"specimen_type": "core_biopsy"})
    assert res.status_code == 200 and res.json() == {"case_id": case_id, "specimen_type": "core_biopsy"}
    assert client.get(f"/api/v1/cases/{case_id}", headers=PATHOLOGIST).json()["specimen_type"] == "core_biopsy"
    client.patch(f"/api/v1/cases/{case_id}/specimen-type", headers=PATHOLOGIST, json={"specimen_type": "resection"})
    with SessionLocal() as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.case_id == case_id, AuditEvent.event_type == "specimen_type_set").order_by(AuditEvent.id)
        ).all()
        assert [(e.payload["from"], e.payload["to"]) for e in events] == [("unknown", "core_biopsy"), ("core_biopsy", "resection")]


def test_the_specimen_type_route_checks_role_case_and_value(client):
    case_id = client.post("/api/v1/cases", headers=PATHOLOGIST).json()["id"]
    url = f"/api/v1/cases/{case_id}/specimen-type"
    assert client.patch(url, headers={"X-Test-Role": "viewer"}, json={"specimen_type": "resection"}).status_code == 403
    assert client.patch(f"/api/v1/cases/{uuid.uuid4()}/specimen-type", headers=PATHOLOGIST, json={"specimen_type": "resection"}).status_code == 404
    assert client.patch(url, headers=PATHOLOGIST, json={"specimen_type": "unknown"}).status_code == 422
    assert client.patch(url, headers=PATHOLOGIST, json={}).status_code == 422
    assert client.get(f"/api/v1/cases/{case_id}", headers=PATHOLOGIST).json()["specimen_type"] == "unknown"


def test_the_database_refuses_an_unlisted_specimen_type():
    from sqlalchemy.exc import IntegrityError

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.add(Case(created_by="t", specimen_type="biopsy"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
