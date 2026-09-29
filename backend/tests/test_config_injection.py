"""Scoring, grading and QC take the typed configuration; nothing reads YAML or assumes a value (SPEC-01 §3.8)."""
import ast
import inspect
import uuid
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.core.pipeline_config import (
    MitoticThresholds,
    NottinghamGradingConfig,
    QcConfig,
    get_config_hash,
    get_pipeline_config,
)
from app.main import app
from pipeline.grading import aggregate_grading_findings, calculate_nottingham_grade, calculate_tubule_score
from pipeline.qc_checks import check_tissue_coverage, run_all_qc_checks
from pipeline.scoring import compute_nottingham_mitotic_score
from worker.execution import STAGE_HANDLERS

BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "module", ["pipeline/scoring.py", "pipeline/grading.py", "pipeline/qc_checks.py", "app/routers/grading.py"]
)
def test_no_yaml_is_read_outside_the_config_loader(module):
    tree = ast.parse((BACKEND / module).read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "yaml" not in imported
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "open" not in calls


def test_the_injected_mitotic_thresholds_decide_the_score():
    scoring = get_pipeline_config().mitosis.scoring
    # 8 mitoses in 10 HPFs of 262 um is 3.71 per mm2: score 2 under the configured 3.65.
    assert compute_nottingham_mitotic_score(8, 10, 262.0, scoring)["mitotic_score"] == 2
    stricter = scoring.model_copy(update={"thresholds": MitoticThresholds(score2_min=4.0, score3_min=8.0)})
    assert compute_nottingham_mitotic_score(8, 10, 262.0, stricter)["mitotic_score"] == 1


def test_the_injected_tubule_and_grade_bounds_decide_the_grade():
    scoring = get_pipeline_config().scoring
    assert calculate_tubule_score(50.0, scoring) == 2
    assert calculate_nottingham_grade(2, 2, 2, scoring) == (6, 2)
    wider = scoring.model_copy(update={"nottingham_grading": NottinghamGradingConfig(grade1_max_sum=6, grade2_max_sum=7)})
    assert calculate_nottingham_grade(2, 2, 2, wider) == (6, 1)


def test_hpfs_without_a_radius_are_an_error_not_262_um():
    with pytest.raises(KeyError, match="radius_um"):
        compute_nottingham_mitotic_score(1, 1, None, get_pipeline_config().mitosis.scoring, hpfs=[{"seq": 1}])


def test_a_missing_mitotic_score_needs_a_human_and_yields_no_grade():
    tubule = [{"tubule_percent": 40.0, "tumor_present": True} for _ in range(8)]
    pleo = [{"pleomorphism_score": 2} for _ in range(8)]
    result = aggregate_grading_findings(tubule, pleo, mitotic_score=None, cfg=get_pipeline_config().scoring)
    assert result["needs_human"] is True
    assert "no_mitotic_score" in result["flags"]
    assert result["grade"] is None and result["nottingham_sum"] is None
    assert result["tubule_score"] == 2 and result["pleo_score"] == 2


def test_an_unknown_confidence_fails_instead_of_weighing_as_medium():
    scoring = get_pipeline_config().scoring
    pleo = [{"pleomorphism_score": 2}] * 8
    tubule = [{"tubule_percent": 40.0, "tumor_present": True, "confidence": "certain"}] * 8
    with pytest.raises(AttributeError):
        aggregate_grading_findings(tubule, pleo, mitotic_score=1, cfg=scoring)


def test_qc_uses_the_injected_thresholds_and_stamps_the_pipeline_hash():
    base = get_pipeline_config().qc
    mask = np.zeros((100, 100), dtype=bool)
    mask[:, :4] = True  # 4% tissue
    assert check_tissue_coverage(mask, base)["status"] == "warn"
    data = base.model_dump()
    data["tissue_coverage"] = {"fail_threshold": 0.01, "warn_threshold": 0.03}
    assert check_tissue_coverage(mask, QcConfig.model_validate(data))["status"] == "pass"

    from PIL import Image

    slide = Image.new("RGB", (512, 512), color=(240, 230, 240))
    result = run_all_qc_checks(slide, mask, stain_params={}, config=base, config_hash=get_config_hash())
    assert result["config_hash"] == get_config_hash()


@pytest.mark.parametrize("stage", sorted(STAGE_HANDLERS))
def test_every_stage_handler_receives_the_runtime(stage):
    assert list(inspect.signature(STAGE_HANDLERS[stage]).parameters)[2] == "runtime"


# --- the grade preview never assumes a score -----------------------------------

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def client():
    Base.metadata.create_all(bind=engine)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def test_the_grade_preview_refuses_missing_scores_instead_of_assuming_2(client):
    res = client.post("/api/v1/stages/grading/recompute", json={"case_id": str(uuid.uuid4()), "tubule_score": 1})
    assert res.status_code == 400
    assert "pleo, mitotic" in res.json()["detail"]


def test_the_grade_preview_uses_the_configured_bounds(client):
    res = client.post(
        "/api/v1/stages/grading/recompute",
        json={"case_id": str(uuid.uuid4()), "tubule_score": 2, "pleo_score": 2, "mitotic_score": 2},
    )
    assert res.status_code == 200
    assert res.json()["nottingham_sum"] == 6 and res.json()["grade"] == 2
