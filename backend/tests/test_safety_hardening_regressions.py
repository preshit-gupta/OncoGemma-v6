"""
Regression tests for the WP-4.3 review fixes (SPEC-03 §5):
- Idempotency keys are released when the route fails, scoped per user, and bound to the path.
- The rate limiter cannot be bypassed with test headers, spoofed X-Forwarded-For or forged cookies.
- Geometry enforces the configured area range and rejects invalid active hotspots.
- Soft-deleted cases are 404 on stage routes; expired idempotency keys are purged.
- Signed uploads restrict the content type and sign the size range.
- AC7: every prompt render call site and every prompt_vars producer is enumerated.
"""
import ast
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import jwt
import pytest
from fastapi import HTTPException

from app.auth.idempotency import IdempotencyContext
from app.auth.sessions import JWT_ALGORITHM, SESSION_COOKIE, SIGNING_KEY_ENV
from app.core.geometry import validate_hotspots_non_overlapping, validate_polygon_geometry
from app.core.pipeline_config import get_pipeline_config
from app.models.case import Case
from app.models.idempotency import IdempotencyKeyRecord
from app.models.stage_execution import StageExecution
from app.services.purge import purge_expired_idempotency_keys
from tests.test_safety_hardening import safety_test_env  # noqa: F401  (shared fixture)

BACKEND = Path(__file__).resolve().parents[1]
SQUARE_200UM = [[0.0, 0.0], [200.0, 0.0], [200.0, 200.0], [0.0, 200.0]]  # 0.04 mm²


def _case(db, **kwargs) -> uuid.UUID:
    case_id = uuid.uuid4()
    db.add(Case(id=case_id, status="open", created_by="t", specimen_type="core_biopsy", **kwargs))
    db.commit()
    return case_id


# --- idempotency -------------------------------------------------------------

def test_idempotency_key_is_released_when_the_route_fails(safety_test_env):
    client, db, _, _ = safety_test_env
    case_id = _case(db)
    stage = StageExecution(id=uuid.uuid4(), case_id=case_id, stage="triage", attempt=1,
                           status="confirmed", output_ref="")
    db.add(stage)
    db.commit()
    body = {"case_id": str(case_id), "no_invasive_tumor": False}
    headers = {"Idempotency-Key": f"k-{uuid.uuid4()}"}

    first = client.post("/api/v1/stages/triage/confirm", json=body, headers=headers)
    assert first.status_code == 409 and "expected 'awaiting_review'" in first.text

    # The retry reaches the route again instead of a stale "in progress" claim.
    retry = client.post("/api/v1/stages/triage/confirm", json=body, headers=headers)
    assert retry.status_code == 409 and "expected 'awaiting_review'" in retry.text
    assert db.query(IdempotencyKeyRecord).count() == 0


def test_idempotency_keys_are_scoped_per_user_and_bound_to_the_route(safety_test_env):
    _, db, _, _ = safety_test_env
    a = IdempotencyContext("shared-key", "user-a", "approve /api/v1/cases/1/stages/qc/approve", "h", db)
    a.begin()
    a.complete({"status": "approved"}, 202)

    # Another user with the same key and body gets their own claim, not user A's response.
    IdempotencyContext("shared-key", "user-b", "approve /api/v1/cases/1/stages/qc/approve", "h", db).begin()

    # The same user reusing the key on another case's route is a conflict, never a replay.
    with pytest.raises(HTTPException) as exc:
        IdempotencyContext("shared-key", "user-a", "approve /api/v1/cases/2/stages/qc/approve", "h", db).begin()
    assert exc.value.status_code == 422


def test_expired_idempotency_keys_are_purged(safety_test_env):
    _, db, _, _ = safety_test_env
    now = datetime.now(timezone.utc)
    for key, expires in (("old", now - timedelta(hours=1)), ("live", now + timedelta(hours=1))):
        db.add(IdempotencyKeyRecord(key=key, user_id="u", endpoint="e", request_hash="h",
                                    status="completed", status_code=200, response_body={},
                                    created_at=now, expires_at=expires))
    db.commit()
    assert purge_expired_idempotency_keys(db) == 1
    assert [r.key for r in db.query(IdempotencyKeyRecord).all()] == ["live"]


# --- rate limits ---------------------------------------------------------------

def _mutating_codes(client, n, headers=None, cookies=None):
    client.cookies.clear()
    if cookies:
        client.cookies.update(cookies)
    return [client.post("/api/v1/cases", json={}, headers=headers or {}).status_code for _ in range(n)]


def test_rate_limit_ignores_test_user_header(safety_test_env):
    client, _, _, _ = safety_test_env
    limit = get_pipeline_config().safety.rate_limits.mutating_per_user_per_min
    codes = [
        client.post("/api/v1/cases", json={}, headers={"X-Test-User-Id": f"u{i}"}).status_code
        for i in range(limit + 1)
    ]
    assert codes[-1] == 429


def test_sign_in_rate_limit_ignores_client_supplied_forwarded_entries(safety_test_env):
    client, _, _, _ = safety_test_env
    rate_cfg = get_pipeline_config().safety.rate_limits
    real_chain = ["203.0.113.7", "198.51.100.1"][-rate_cfg.trusted_proxy_hops:]
    codes = []
    for i in range(rate_cfg.auth_session_per_ip_per_min + 1):
        spoofed = f"10.0.0.{i}"
        headers = {"X-Forwarded-For": ", ".join([spoofed, *real_chain])}
        codes.append(client.post("/api/v1/auth/session", json={}, headers=headers).status_code)
    assert codes[-1] == 429


def test_rate_limit_meters_signed_sessions_per_user_and_forged_ones_per_ip(safety_test_env, monkeypatch):
    client, _, _, _ = safety_test_env
    key = "k" * 48
    monkeypatch.setenv(SIGNING_KEY_ENV, key)
    limit = get_pipeline_config().safety.rate_limits.mutating_per_user_per_min

    def token(uid, signing_key=key):
        now = int(time.time())
        claims = {"sid": str(uuid.uuid4()), "uid": uid, "iat": now, "exp": now + 600}
        return jwt.encode(claims, signing_key, algorithm=JWT_ALGORITHM)

    # Forged cookies with a new uid each time all land in the one IP bucket.
    forged = []
    for i in range(limit + 1):
        client.cookies.set(SESSION_COOKIE, token(f"u{i}", signing_key="x" * 48))
        forged.append(client.post("/api/v1/cases", json={}).status_code)
    assert forged[-1] == 429

    # Two validly signed users each get their own budget, independent of the exhausted IP bucket.
    client.cookies.set(SESSION_COOKIE, token("alice"))
    assert client.post("/api/v1/cases", json={}).status_code != 429
    client.cookies.set(SESSION_COOKIE, token("bob"))
    assert client.post("/api/v1/cases", json={}).status_code != 429
    client.cookies.clear()


# --- geometry ------------------------------------------------------------------

def test_polygon_area_must_be_within_configured_range():
    geom = get_pipeline_config().safety.geometry
    assert validate_polygon_geometry(SQUARE_200UM) == pytest.approx(0.04)

    tiny = [[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [0.0, 5.0]]
    with pytest.raises(HTTPException) as small:
        validate_polygon_geometry(tiny)
    assert small.value.status_code == 422 and "outside the allowed range" in small.value.detail

    side = (geom.max_area_mm2 * 1e6) ** 0.5 * 1.1
    huge = [[0.0, 0.0], [side, 0.0], [side, side], [0.0, side]]
    with pytest.raises(HTTPException) as big:
        validate_polygon_geometry(huge)
    assert big.value.status_code == 422


def test_closing_vertex_is_not_counted_and_non_finite_is_rejected():
    closed = SQUARE_200UM + [SQUARE_200UM[0]]
    assert validate_polygon_geometry(closed) == pytest.approx(0.04)
    with pytest.raises(HTTPException):
        validate_polygon_geometry([[0.0, 0.0], [float("nan"), 0.0], [200.0, 200.0]])


def test_invalid_active_hotspot_is_rejected_not_skipped():
    bowtie = [[0.0, 0.0], [300.0, 300.0], [0.0, 300.0], [300.0, 0.0]]
    hotspots = [
        {"id": "hs_ok", "polygon_um": SQUARE_200UM},
        {"id": "hs_bad", "polygon_um": bowtie},
    ]
    with pytest.raises(HTTPException) as exc:
        validate_hotspots_non_overlapping(hotspots)
    assert "hs_bad" in exc.value.detail

    # An excluded hotspot is not part of the effective set.
    validate_hotspots_non_overlapping([hotspots[0], {**hotspots[1], "excluded": True}])


def test_triage_edits_fail_loudly_when_machine_output_is_unreadable(safety_test_env):
    client, db, _, _ = safety_test_env
    case_id = _case(db)
    db.add(StageExecution(id=uuid.uuid4(), case_id=case_id, stage="triage", attempt=1,
                          status="awaiting_review", output_ref=""))
    db.commit()
    with patch("app.routers.triage.download_blob_as_bytes", side_effect=FileNotFoundError("gone")):
        resp = client.post("/api/v1/stages/triage/edits", json={
            "case_id": str(case_id),
            "edits": [{"op": "add", "polygon_um": SQUARE_200UM}],
        })
    assert resp.status_code == 502


def test_triage_edits_reject_overlap_with_machine_hotspot(safety_test_env):
    client, db, _, _ = safety_test_env
    case_id = _case(db)
    db.add(StageExecution(id=uuid.uuid4(), case_id=case_id, stage="triage", attempt=1,
                          status="awaiting_review", output_ref=""))
    db.commit()
    machine = json.dumps({"hotspots": [{"id": "hs_01", "polygon_um": SQUARE_200UM}]}).encode()
    shifted = [[x + 100.0, y + 100.0] for x, y in SQUARE_200UM]
    with patch("app.routers.triage.download_blob_as_bytes", return_value=machine):
        resp = client.post("/api/v1/stages/triage/edits", json={
            "case_id": str(case_id),
            "edits": [{"op": "add", "polygon_um": shifted}],
        })
    assert resp.status_code == 422 and "cannot overlap" in resp.text


# --- soft delete -----------------------------------------------------------------

def test_soft_deleted_case_is_gone_on_stage_routes(safety_test_env):
    client, db, _, _ = safety_test_env
    case_id = _case(db, deleted_at=datetime.now(timezone.utc))
    db.add(StageExecution(id=uuid.uuid4(), case_id=case_id, stage="mitosis", attempt=1,
                          status="awaiting_review", output_ref=""))
    db.commit()

    assert client.get(f"/api/v1/stages/triage/{case_id}").status_code == 404
    assert client.get(f"/api/v1/stages/grading/{case_id}").status_code == 404
    confirm = client.post("/api/v1/stages/mitosis/confirm", json={"case_id": str(case_id)},
                          headers={"Idempotency-Key": f"k-{uuid.uuid4()}"})
    assert confirm.status_code == 404
    # The audit trail of a deleted case stays readable.
    assert client.get(f"/api/v1/cases/{case_id}/audit").status_code == 200


# --- signed uploads --------------------------------------------------------------

def test_upload_url_restricts_content_type_and_signs_size_range(safety_test_env):
    client, db, _, _ = safety_test_env
    case_id = _case(db)
    upload_cfg = get_pipeline_config().safety.signed_upload

    bad = client.post(f"/api/v1/cases/{case_id}/slide/upload-url",
                      json={"filename": "s.svs", "size_bytes": 10, "content_type": "text/html"})
    assert bad.status_code == 400 and "content type" in bad.text

    ok = client.post(f"/api/v1/cases/{case_id}/slide/upload-url",
                     json={"filename": "s.tif", "size_bytes": 10, "content_type": "image/tiff"})
    assert ok.status_code == 200
    assert ok.json()["upload_headers"] == {
        "Content-Type": "image/tiff",
        "x-goog-content-length-range": f"0,{upload_cfg.max_bytes}",
    }


# --- AC7: prompt render call sites -------------------------------------------------

# Every place that renders a prompt template, and every place that supplies prompt variables.
# Adding one must be a reviewed change to this list (SPEC-03 §5.2, AC7).
RENDER_CALL_SITES = {"app/inference/gateway.py"}
PROMPT_VARS_PRODUCERS: set[str] = set()


def _python_files():
    for root in ("app", "worker", "pipeline", "eval"):
        yield from (BACKEND / root).rglob("*.py")


def test_ac7_prompt_render_call_sites_are_enumerated():
    renders, producers = set(), set()
    for path in _python_files():
        rel = path.relative_to(BACKEND).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name == "render_prompt":
                renders.add(rel)
            if any(kw.arg == "prompt_vars" for kw in node.keywords):
                producers.add(rel)
    assert renders == RENDER_CALL_SITES
    assert producers == PROMPT_VARS_PRODUCERS
