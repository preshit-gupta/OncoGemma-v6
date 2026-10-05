"""Integration and contract tests for Stage 3 Triage API v6 (WP-6.3, WP-6.5, SPEC-05 §5; D22).

Acceptance Criteria:
- AC4: Overlapping HPF circles rejected with 422 and collision pairs (frames may overlap).
- Confirmation overlap rejection with 409.
- DecisionRecord creation for human edits with supersedes_id.
- TriageStageV6 contract compliance; edits accumulate and the confirm gate needs the target or an acceptance.
"""
import json
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models
from app.main import app
from app.core.db import Base, get_db
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.hotspot import Hotspot
from app.models.decision_record import DecisionRecord
from app.core.tasks import Task, ProducerKind

CONTRACT_KEYS = {
    "case_id", "stage_execution_id", "status", "slide", "heatmap", "tumor_threshold",
    "hotspots", "machine_hotspots", "flags", "provenance", "hpf_target", "n_sites_available",
}
D, FRAME, TARGET = 500.0, 600.0, 10


@pytest.fixture
def client_and_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    db = TestingSessionLocal()
    yield client, db
    db.close()
    app.dependency_overrides.clear()


def frame(cx, cy, side=FRAME):
    h = side / 2
    return [[cx - h, cy - h], [cx + h, cy - h], [cx + h, cy + h], [cx - h, cy + h], [cx - h, cy - h]]


def model_site(n, cx, cy):
    return {
        "id": f"hs_{n:02d}", "center_um": [cx, cy], "hpf_diameter_um": D, "polygon_um": frame(cx, cy), "window_um": FRAME,
        "rank": n, "rank_score": 0.9 - n / 100, "score_kind": "mean_p_tumor", "tissue_fraction": 0.95, "tumor_fraction": 0.9,
        "prescan_expected": None, "source": "model", "excluded": False, "exclude_reason": None,
        "area_mm2": 0.196, "prob_mean": 0.9, "prob_max": 0.95,
    }


def machine_output(centres, **extra):
    return {
        "heatmap_png_uri": "/artifacts/heatmap.png",
        "heatmap": {"tile_um": 224.0, "origin_um": [0.0, 0.0], "nx": 30, "ny": 30, "value": "p_tumor_cal", "head_version": "1.0.0"},
        "tumor_threshold": 0.5,
        "hpf_diameter_um": D, "frame_um": FRAME, "hpf_target": TARGET, "n_sites_available": 40,
        "flags": ["hotspots_limited_by_tissue"] if len(centres) < TARGET else [],
        "hotspots": [model_site(n, cx, cy) for n, (cx, cy) in enumerate(centres, start=1)],
        **extra,
    }


def seed_case(db, centres, *, slide=True, review_edits=None, output=None):
    """A case awaiting Stage 3 review with the given model sites; returns (case_id, output)."""
    case_uuid, exec_id = uuid.uuid4(), uuid.uuid4()
    db.add(Case(id=case_uuid, created_by="pathologist_1", status="processing", specimen_type="resection"))
    db.add(StageExecution(
        id=exec_id, case_id=case_uuid, stage="triage", attempt=1, status="awaiting_review", input_ref={},
        output_ref=f"gs://og-artifacts-local/cases/{case_uuid}/triage/output.json", review_edits=review_edits,
    ))
    if slide:
        db.add(Slide(id=uuid.uuid4(), case_id=case_uuid, gcs_uri_original="gs://raw/x.svs", mpp_x=0.25, mpp_y=0.25,
                     width_px=40000, height_px=40000))  # 10 x 10 mm
    db.commit()
    return str(case_uuid), output if output is not None else machine_output(centres)


@contextmanager
def serving(output):
    payload = json.dumps(output).encode("utf-8")
    with patch("app.routers.triage.download_blob_as_bytes", return_value=payload), \
         patch("app.services.stages.download_blob_as_bytes", return_value=payload):
        yield


def post_edits(client, case_id, *ops):
    return client.post("/api/v1/stages/triage/edits", json={"case_id": case_id, "edits": list(ops)})


def test_triage_edits_overlap_rejection_ac4(client_and_db):
    """AC4: A pin whose circle overlaps an active circle is rejected with HTTP 422 and the colliding ids."""
    client, db = client_and_db
    case_id, output = seed_case(db, [(1300.0, 1300.0)])
    with serving(output):
        res = post_edits(client, case_id, {"op": "add", "center_um": [1500.0, 1500.0]})  # 283 µm from hs_01
    assert res.status_code == 422
    body = res.json()
    assert body["error"] == "hotspot_overlap"
    assert ["hs_01", "hs_u_1"] in body["ids"]
    assert db.get(StageExecution, db.scalars(select(StageExecution.id)).first()).review_edits is None  # nothing was saved


def test_a_pin_whose_frame_overlaps_but_whose_circle_does_not_is_accepted(client_and_db):
    client, db = client_and_db
    case_id, output = seed_case(db, [(1300.0, 1300.0)])
    with serving(output):
        res = post_edits(client, case_id, {"op": "add", "center_um": [1300.0 + D + 40.0, 1300.0]})  # frames overlap by 60 µm
    assert res.status_code == 200, res.text
    pinned = next(h for h in res.json()["hotspots"] if h["id"] == "hs_u_1")
    assert pinned["polygon_um"][0] == [1300.0 + D + 40.0 - 300.0, 1300.0 - 300.0] and pinned["window_um"] == FRAME


def test_triage_confirm_overlap_rejection(client_and_db):
    """Overlapping circles in the stored edits are refused at confirmation with HTTP 409."""
    client, db = client_and_db
    edits = [
        {"op": "add", "id": "hs_u_1", "center_um": [1000.0, 1000.0]},
        {"op": "add", "id": "hs_u_2", "center_um": [1200.0, 1200.0]},
    ]
    case_id, output = seed_case(db, [], review_edits=edits)
    with serving(output):
        res = client.post("/api/v1/stages/triage/confirm", json={"case_id": case_id, "no_invasive_tumor": False})
    assert res.status_code == 409
    body = res.json()
    assert body["error"] == "hotspot_overlap" and ["hs_u_1", "hs_u_2"] in body["ids"]


def test_triage_valid_edits_decision_record_and_contract(client_and_db):
    """Valid edits record a DecisionRecord and return TriageStageV6; confirm persists the sites' centres."""
    client, db = client_and_db
    case_id, output = seed_case(db, [(1300.0, 1300.0)])
    case_uuid = uuid.UUID(case_id)
    exec_id = db.scalars(select(StageExecution.id)).first()
    model_dr_id = uuid.uuid4()
    db.add(DecisionRecord(
        id=model_dr_id, case_id=case_uuid, stage_execution_id=exec_id, stage="triage", task=Task.HOTSPOT_SELECT.value,
        entity_type="hotspot", entity_id=str(exec_id), producer_kind="model", producer_id="hotspot_selector_v6",
        producer_version="1.0", input_sha256="0" * 64, input_spec={"dummy": True}, status="ok", latency_ms=100,
        run_mode="eval", config_hash="0" * 64,
    ))
    db.commit()

    with serving(output):
        res_edits = post_edits(client, case_id, {"op": "add", "center_um": [3000.0, 3000.0]})
        assert res_edits.status_code == 200
        data = res_edits.json()

        # TriageStageV6 contract fields (docs/contracts/triage_v6.md)
        assert CONTRACT_KEYS <= data.keys()
        assert data["status"] == "awaiting_review"
        assert len(data["hotspots"]) == 2 and len(data["machine_hotspots"]) == 1
        assert data["flags"] == ["hotspots_limited_by_tissue"]
        assert data["hpf_target"] == TARGET and data["n_sites_available"] == 40
        assert data["provenance"]["stage"] == "triage"
        assert data["heatmap"]["nx"] == 30 and data["heatmap"]["png_url"].endswith("/heatmap")
        assert data["tumor_threshold"] == 0.5 and data["edits_count"] == 1
        pinned = next(h for h in data["hotspots"] if h["id"] == "hs_u_1")
        assert pinned["center_um"] == [3000.0, 3000.0] and pinned["source"] == "pathologist_added"
        assert pinned["rank"] is None and pinned["tissue_fraction"] is None and pinned["tumor_fraction"] is None
        assert pinned["hpf_diameter_um"] == D and pinned["window_um"] == FRAME and len(pinned["polygon_um"]) == 5

        human_drs = db.scalars(
            select(DecisionRecord).where(DecisionRecord.case_id == case_uuid, DecisionRecord.task == Task.HUMAN_EDIT.value)
        ).all()
        assert len(human_drs) == 1
        assert human_drs[0].producer_kind == ProducerKind.HUMAN.value and str(human_drs[0].supersedes_id) == str(model_dr_id)

        get_data = client.get(f"/api/v1/stages/triage/{case_id}").json()
        assert CONTRACT_KEYS <= get_data.keys() and get_data["hotspots"] == data["hotspots"]

        # Two active sites are fewer than the target: confirming needs the acceptance (below). With it:
        res_confirm = client.post("/api/v1/stages/triage/confirm",
                                  json={"case_id": case_id, "no_invasive_tumor": False, "accept_fewer_hpfs": True})
        assert res_confirm.status_code == 200 and res_confirm.json()["accept_fewer_hpfs"] is True

    db_hotspots = db.scalars(select(Hotspot).where(Hotspot.case_id == case_uuid)).all()
    assert len(db_hotspots) == 2
    for hs in db_hotspots:
        assert hs.polygon_um is not None and hs.area_mm2 is not None
        assert hs.center_um is not None and hs.hpf_diameter_um == D


def test_a_stored_v6_0_output_or_polygon_edit_needs_triage_to_run_again(client_and_db):
    """Outputs and edits from before HPF sites have no centre: 409 triage_rerun_required, never a guess."""
    client, db = client_and_db
    old_output = {"hotspots": [{"id": "hs_01", "polygon_um": frame(1000.0, 1000.0), "source": "model", "excluded": False}]}
    case_id, _ = seed_case(db, [], output=old_output)
    with serving(old_output):
        assert client.get(f"/api/v1/stages/triage/{case_id}").status_code == 409
        res = post_edits(client, case_id, {"op": "add", "center_um": [3000.0, 3000.0]})
        assert res.status_code == 409 and res.json()["error"] == "triage_rerun_required"

    case_id, output = seed_case(db, [(1300.0, 1300.0)], review_edits=[{"op": "modify", "id": "hs_01", "polygon_um": frame(1.0, 1.0)}])
    with serving(output):
        res = client.get(f"/api/v1/stages/triage/{case_id}")
        assert res.status_code == 409 and res.json()["error"] == "triage_rerun_required"
