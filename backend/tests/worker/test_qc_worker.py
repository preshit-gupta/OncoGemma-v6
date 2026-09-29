"""Stage 1 QC worker: it judges the artifacts preprocess persisted and re-fits nothing (SPEC-04 §3.7)."""
import json
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.db import Base, engine
from app.core.gcs import download_blob_as_bytes
from app.core.pipeline_config import get_pipeline_config
from app.core.stain_profiles import latest_stain_profile
from app.models.case import Case
from app.models.stage_execution import StageExecution
from app.models.stain_profile import StainProfile
from pipeline.errors import SpecimenTypeRequired, StainProfileMissingError, TissueMaskMissingError
from tests.fakes.runtime import make_runtime
from tests.worker.test_preprocess import WIDTH_PX, HEIGHT_PX, add_case, db  # noqa: F401 - the db fixture
from worker.preprocess import run_preprocess
from worker.qc import run_qc


def qc_stage(db, case) -> StageExecution:
    """The QC execution preprocess queued, or a fresh one for a case that never ran preprocess."""
    stage = db.scalars(select(StageExecution).where(StageExecution.case_id == case.id, StageExecution.stage == "qc")).first()
    if stage is None:
        stage = StageExecution(case_id=case.id, stage="qc", attempt=1, input_ref={"slide_id": str(case.slides[0].id)})
        db.add(stage)
    stage.status = "running"
    db.commit()
    return stage


def qc_output(case) -> dict:
    return json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case.id}/qc/output.json"))


def preprocessed(db, tmp_path, specimen="resection"):
    case, slide, stage = add_case(db, tmp_path, specimen=specimen)
    run_preprocess(stage, db, make_runtime(stage))
    db.refresh(case)
    return case


def check(output: dict, name: str) -> dict:
    return next(c for c in output["checks"] if c["name"] == name)


def test_qc_judges_the_persisted_mask_and_profile_by_the_specimens_thresholds(db, tmp_path):
    """The same 10x, 2.4 mm² slide: too little tissue and too coarse for a resection; a core biopsy's area is fine."""
    resection = preprocessed(db, tmp_path, "resection")
    stage = qc_stage(db, resection)
    run_qc(stage, db, make_runtime(stage))
    out = qc_output(resection)
    assert {c["name"] for c in out["checks"]} == {"tissue_coverage", "focus", "pen_marks", "folds", "stain_sanity", "resolution"}
    assert check(out, "tissue_coverage")["status"] == "fail"  # 2.4 mm² < 4 mm²
    assert check(out, "resolution")["status"] == "fail"  # 1 µm/px is 10x
    assert check(out, "stain_sanity")["status"] == "pass"
    assert (out["specimen_type"], out["native_mpp"]) == ("resection", 1.0)
    assert out["config_hash"] == stage.config_hash or out["config_hash"] == make_runtime(stage).ctx.config_hash
    db.refresh(stage)
    assert stage.status == "failed" and "QC Hard Failure" in stage.error
    db.refresh(resection)
    assert resection.status == "needs_rescan"

    core = preprocessed(db, tmp_path, "core_biopsy")
    core_stage = qc_stage(db, core)
    run_qc(core_stage, db, make_runtime(core_stage))
    assert check(qc_output(core), "tissue_coverage")["status"] == "pass"  # 2.4 mm² >= 2 mm²


def test_qc_does_not_fit_a_stain_profile_of_its_own(db, tmp_path):
    case = preprocessed(db, tmp_path)
    slide_id = case.slides[0].id
    before = db.scalars(select(StainProfile).where(StainProfile.slide_id == slide_id)).all()
    stage = qc_stage(db, case)
    run_qc(stage, db, make_runtime(stage))
    after = db.scalars(select(StainProfile).where(StainProfile.slide_id == slide_id)).all()
    assert len(before) == len(after) == 1
    assert latest_stain_profile(db, slide_id).id == before[0].id


@pytest.mark.parametrize(
    "verdict, status, next_stage, case_status",
    [("pass", "done", "triage", "open"), ("warn", "awaiting_review", None, "open"), ("fail", "failed", None, "needs_rescan")],
)
def test_the_verdict_sets_the_stage_and_chains_triage(db, tmp_path, monkeypatch, verdict, status, next_stage, case_status):
    case = preprocessed(db, tmp_path)
    check_status = {"pass": "pass", "warn": "warn", "fail": "fail"}[verdict]
    monkeypatch.setattr("worker.qc.run_all_qc_checks", lambda *a, **k: {
        "verdict": verdict,
        "checks": [{"name": "focus", "status": check_status, "metric": 0.0, "message": "m"}],
        "config_hash": "h",
    })
    stage = qc_stage(db, case)
    run_qc(stage, db, make_runtime(stage))
    db.refresh(stage)
    db.refresh(case)
    assert (stage.status, case.status) == (status, case_status)
    triage = db.scalars(select(StageExecution).where(StageExecution.case_id == case.id, StageExecution.stage == "triage")).first()
    assert (triage is not None) == (next_stage == "triage")


def test_qc_needs_the_mask_the_profile_and_a_known_specimen(db, tmp_path):
    case, slide, _ = add_case(db, tmp_path)  # preprocess has not run
    stage = qc_stage(db, case)
    with pytest.raises(TissueMaskMissingError, match="run the preprocess stage"):
        run_qc(stage, db, make_runtime(stage))

    from app.core.tissue_mask_store import save_tissue_mask
    from pipeline.tissue_mask import TissueMask
    import numpy as np

    save_tissue_mask(case.id, TissueMask(np.ones((10, 10), dtype=bool), 8.0))
    with pytest.raises(StainProfileMissingError, match="run the preprocess stage"):
        run_qc(stage, db, make_runtime(stage))

    case.specimen_type = "unknown"
    db.commit()
    with pytest.raises(SpecimenTypeRequired):
        run_qc(stage, db, make_runtime(stage))


def test_a_case_preprocessed_before_v6_must_be_preprocessed_again(db, tmp_path):
    """v5 wrote only tissue_mask.png; without its registration json the mask is not usable."""
    from app.core.gcs import upload_blob_from_bytes

    case, _, _ = add_case(db, tmp_path)
    upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case.id}/preprocess/tissue_mask.png", b"\x89PNG", "image/png")
    stage = qc_stage(db, case)
    with pytest.raises(TissueMaskMissingError, match="tissue_mask.json"):
        run_qc(stage, db, make_runtime(stage))
