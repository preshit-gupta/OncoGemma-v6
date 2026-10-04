"""Scoring, grading and QC take the typed configuration; nothing reads YAML or assumes a value (SPEC-01 §3.8)."""
import ast
import inspect
from pathlib import Path

import numpy as np
import pytest

from app.core.pipeline_config import (
    MitoticThresholds,
    NottinghamGradingConfig,
    SpecimenQcConfig,
    get_config_hash,
    get_pipeline_config,
)
from pipeline.grading import aggregate_components, calculate_nottingham_grade, calculate_tubule_score, pleomorphism_mode
from pipeline.qc_checks import check_tissue_coverage
from pipeline.tissue_mask import TissueMask
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


def test_a_missing_mitotic_score_yields_no_grade():
    result = aggregate_components(2, 2, None, get_pipeline_config().scoring)
    assert result == {"total": None, "grade": None, "near_grade_boundary": False}


def test_the_injected_tie_break_decides_a_tied_pleomorphism_mode():
    scoring = get_pipeline_config().scoring
    assert scoring.grading.pleo_tie_break == "max"  # owner decision 2026-10-04
    assert pleomorphism_mode([2, 3], scoring.grading.pleo_tie_break) == (3, True)


def test_qc_uses_the_injected_thresholds():
    profile = get_pipeline_config().specimen_profiles.profiles["resection"].qc
    mask = TissueMask(np.ones((100, 100), dtype=bool), 10.0)  # 1 mm² of tissue
    assert check_tissue_coverage(mask, profile)["status"] == "fail"
    data = profile.model_dump()
    data.update(tissue_area_fail_mm2=0.5, tissue_area_warn_mm2=0.9)
    assert check_tissue_coverage(mask, SpecimenQcConfig.model_validate(data))["status"] == "pass"


@pytest.mark.parametrize("stage", sorted(STAGE_HANDLERS))
def test_every_stage_handler_receives_the_runtime(stage):
    assert list(inspect.signature(STAGE_HANDLERS[stage]).parameters)[2] == "runtime"
