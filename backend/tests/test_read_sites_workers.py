"""Triage, mitosis and grading read pixels through read_region_at_mpp, in the configured colour, over the
registered mask (SPEC-04 §3.4, §3.5, AC1)."""
import io
import json

import numpy as np
import pytest
from PIL import Image

from app.core.config import settings
from app.core.gcs import blob_exists, delete_blob, download_blob_as_bytes
from app.core.pipeline_config import get_pipeline_config
from app.core.stain_profiles import latest_stain_profile, save_stain_profile
from app.core.tissue_mask_store import mask_blob_names, save_tissue_mask
from app.inference.records import DecisionLog
from app.models import Case
from pipeline.errors import (
    DegenerateStainProfileError,
    SpecimenTypeRequired,
    StainProfileMissingError,
    TissueMaskMissingError,
)
from pipeline.stain import StainTransform
from pipeline.tissue_mask import TissueMask
from tests.fakes.gateway import FakeAdapter
from tests.fakes.runtime import make_runtime
from tests.fakes.slide import FakeOpenSlide, install_fake_slide
from tests.fakes.stage2 import stain_fit
from tests import test_grading_worker as grading_t
from tests import test_mitosis_worker as mitosis_t
from tests import test_triage_worker as triage_t
from tests.test_grading_worker import db_session  # noqa: F401 - the shared fixture
from worker.grading import run_grading
from pipeline.grading import sample_blob
from worker.mitosis import run_mitosis
from worker.triage import run_triage

def od_beta() -> float:
    return get_pipeline_config().specimen_profiles.profiles["resection"].stain_fit.od_beta


def png_array(blob: str) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, blob))).convert("RGB"))


def transform_of(db, slide_id) -> StainTransform:
    return StainTransform.from_profile(latest_stain_profile(db, slide_id), od_beta=od_beta())


def make_degenerate(db, slide_id):
    """A newer stain fit that found no two stains: the slide's profile is now degenerate."""
    save_stain_profile(db, slide_id, stain_fit("degenerate"))
    db.commit()


def drop_mask(case_id):
    for name in mask_blob_names(case_id):
        delete_blob(settings.GCS_ARTIFACTS_BUCKET, name)


def slide_id_of(stage, db):
    return db.get(Case, stage.case_id).slides[0].id


# -- triage ---------------------------------------------------------------------------------------------------------------


def run_seeded_triage(db, monkeypatch, config=None, **seed_kwargs):
    stage, raw_uri = triage_t.seed(db, **seed_kwargs)
    slide = install_fake_slide(monkeypatch, FakeOpenSlide(triage_t.WIDTH_PX, triage_t.HEIGHT_PX), raw_uri)
    return stage, slide, make_runtime(stage, triage_t.adapters(), config=config)


@pytest.fixture
def triage_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_triage_review_thumbnails_are_the_raw_region_through_the_persisted_profile(triage_db, monkeypatch):
    stage, slide, runtime = run_seeded_triage(triage_db, monkeypatch)
    run_triage(stage, triage_db, runtime)
    output = triage_t.output_json(stage)
    assert output["stain_normalization"] == "available" and output["hotspots"]
    transform = transform_of(triage_db, slide_id_of(stage, triage_db))
    for hotspot in output["hotspots"]:
        for mag in ("10x", "20x", "40x"):
            base = f"cases/{stage.case_id}/triage/patches/{hotspot['id']}_{mag}"
            orig, norm = png_array(f"{base}_orig.png"), png_array(f"{base}_norm.png")
            assert orig.shape == (512, 512, 3) and np.array_equal(norm, transform.apply(orig))
        assert hotspot["thumbnail_uri"].endswith(f"{hotspot['id']}_10x_norm.png")
    assert blob_exists(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/triage/patches/{output['hotspots'][0]['id']}_thumb.png")


def normalised_embedder_config():
    """Path Foundation fed stain-normalised tiles: supported, not the registry default (raw, 2026-10-02)."""
    config = get_pipeline_config()
    entry = config.models.models["path_foundation"]
    normalised = entry.model_copy(update={"input": entry.input.model_copy(update={"color": "normalized"})})
    return config.model_copy(update={"models": config.models.model_copy(update={"models": {**config.models.models, "path_foundation": normalised}})})


def test_a_normalised_tile_embedder_cannot_run_on_a_degenerate_fit(triage_db, monkeypatch):
    stage, slide, runtime = run_seeded_triage(triage_db, monkeypatch, config=normalised_embedder_config())
    make_degenerate(triage_db, slide_id_of(stage, triage_db))
    with pytest.raises(DegenerateStainProfileError, match="tile embedder"):
        run_triage(stage, triage_db, runtime)


def test_triage_tiles_are_embedded_in_the_normalised_colour_of_the_persisted_profile(triage_db, monkeypatch):
    config = normalised_embedder_config()
    stage, slide, runtime = run_seeded_triage(triage_db, monkeypatch, config=config)
    log = DecisionLog()
    run_triage(stage, triage_db, make_runtime(stage, triage_t.adapters(), config=config, log=log))
    profile_id = str(latest_stain_profile(triage_db, slide_id_of(stage, triage_db)).id)
    specs = [s for r in log.pending() if r["task"] == "pf_embed" for s in r["input_spec"]["images"]]
    assert specs and all(s["color"] == "normalized" and s["stain_profile_id"] == profile_id for s in specs)


def test_a_degenerate_stain_fit_leaves_no_normalised_thumbnail_and_says_so(triage_db, monkeypatch):
    stage, slide, runtime = run_seeded_triage(triage_db, monkeypatch)
    make_degenerate(triage_db, slide_id_of(stage, triage_db))
    run_triage(stage, triage_db, runtime)  # the tumour referee sees raw colour, so the stage can run
    output = triage_t.output_json(stage)
    assert output["stain_normalization"] == "unavailable" and output["hotspots"]
    for hotspot in output["hotspots"]:
        base = f"cases/{stage.case_id}/triage/patches/{hotspot['id']}"
        assert blob_exists(settings.GCS_ARTIFACTS_BUCKET, f"{base}_10x_orig.png")
        assert not blob_exists(settings.GCS_ARTIFACTS_BUCKET, f"{base}_10x_norm.png")
        assert not blob_exists(settings.GCS_ARTIFACTS_BUCKET, f"{base}_thumb.png")
        assert hotspot["thumbnail_uri"].endswith("_10x_orig.png")


def test_a_referee_that_needs_normalised_colour_cannot_run_on_a_degenerate_fit(triage_db, monkeypatch):
    config = get_pipeline_config()
    referee = config.triage.tumor_referee.model_copy(update={"color": "normalized"})
    config = config.model_copy(update={"triage": config.triage.model_copy(update={"tumor_referee": referee})})
    stage, slide, runtime = run_seeded_triage(triage_db, monkeypatch, config=config)
    make_degenerate(triage_db, slide_id_of(stage, triage_db))
    with pytest.raises(DegenerateStainProfileError, match="normalized colour"):
        run_triage(stage, triage_db, runtime)


def test_a_normalised_referee_is_shown_the_persisted_transform_of_the_crop(triage_db, monkeypatch):
    config = get_pipeline_config()
    referee = config.triage.tumor_referee.model_copy(update={"color": "normalized"})
    config = config.model_copy(update={"triage": config.triage.model_copy(update={"tumor_referee": referee})})
    stage, slide, runtime = run_seeded_triage(triage_db, monkeypatch, config=config)
    log = DecisionLog()
    runtime = make_runtime(stage, triage_t.adapters(), config=config, log=log)
    run_triage(stage, triage_db, runtime)
    specs = [s for r in log.pending() if r["task"] == "tumor_referee" for s in r["input_spec"]["images"]]
    assert specs and all(s["color"] == "normalized" and s["size_px"] == [512, 512] and s["mpp"] == 1.0 for s in specs)


def test_triage_tiles_are_the_tissue_tiles_of_the_registered_mask(triage_db, monkeypatch):
    """Only tiles the registered mask calls tissue reach the embedder, and none is dropped or invented."""
    stage, raw_uri = triage_t.seed(triage_db)
    install_fake_slide(monkeypatch, FakeOpenSlide(triage_t.WIDTH_PX, triage_t.HEIGHT_PX), raw_uri)
    width_um, height_um = triage_t.WIDTH_PX * triage_t.MPP, triage_t.HEIGHT_PX * triage_t.MPP
    cells = np.zeros((int(np.ceil(height_um / 8)), int(np.ceil(width_um / 8))), dtype=bool)
    cells[:, : cells.shape[1] // 2] = True  # tissue on the left half only
    mask = TissueMask(cells, 8.0)
    save_tissue_mask(stage.case_id, mask)
    cfg = get_pipeline_config().triage
    min_fraction = get_pipeline_config().specimen_profiles.for_type(triage_db.get(Case, stage.case_id).specimen_type).triage.min_tissue_fraction
    expected = list(mask.tiles(cfg.patch_size_px * cfg.mpp_target, min_fraction))
    assert expected and len(expected) < len(list(TissueMask(np.ones_like(cells), 8.0).tiles(224.0, min_fraction)))

    pf = FakeAdapter(then=triage_t.embed)
    run_triage(stage, triage_db, make_runtime(stage, triage_t.adapters(pf=pf)))
    assert sum(len(request.images) for _, request, _ in pf.calls) == len(expected)


def test_triage_needs_the_mask_the_profile_and_a_known_specimen(triage_db, monkeypatch):
    stage, slide, runtime = run_seeded_triage(triage_db, monkeypatch)
    drop_mask(stage.case_id)
    with pytest.raises(TissueMaskMissingError, match="run the preprocess stage"):
        run_triage(stage, triage_db, runtime)

    case = triage_db.get(Case, stage.case_id)
    case.specimen_type = "unknown"
    triage_db.commit()
    with pytest.raises(SpecimenTypeRequired):
        run_triage(stage, triage_db, runtime)


def test_triage_without_a_stain_profile_needs_preprocess(triage_db, monkeypatch):
    from sqlalchemy import delete

    from app.models.stain_profile import StainProfile

    stage, slide, runtime = run_seeded_triage(triage_db, monkeypatch)
    triage_db.execute(delete(StainProfile))
    triage_db.commit()
    with pytest.raises(StainProfileMissingError, match="run the preprocess stage"):
        run_triage(stage, triage_db, runtime)


# -- mitosis --------------------------------------------------------------------------------------------------------------


def run_seeded_mitosis(db, monkeypatch, referee_color=None, mpp_slide=None):
    stage, raw_uri = mitosis_t.seed(db)
    install_fake_slide(monkeypatch, FakeOpenSlide(mitosis_t.SIDE_PX, mitosis_t.SIDE_PX), raw_uri)
    config = mitosis_t.configured()
    if referee_color:
        referee = config.mitosis.referee.model_copy(update={"color": referee_color})
        config = config.model_copy(update={"mitosis": config.mitosis.model_copy(update={"referee": referee})})
    log = DecisionLog()
    return stage, log, mitosis_t.runtime_for(stage, config=config, log=log)


def test_the_mitosis_referee_is_shown_normalised_crops_and_the_review_keeps_the_raw_one(db_session, monkeypatch):
    stage, log, runtime = run_seeded_mitosis(db_session, monkeypatch)
    run_mitosis(stage, db_session, runtime)
    referee_specs = [s for r in log.pending() if r["task"] == "mitosis_referee" for s in r["input_spec"]["images"]]
    assert referee_specs and {s["color"] for s in referee_specs} == {"normalized"}
    focus = [s for s in referee_specs if s["format"] == "png"]
    assert focus and all(s["mpp"] == 0.25 and s["size_px"] == [128, 128] for s in focus)

    # The review images are the contract crops, as scanned, whatever colour the referee saw (WP-7.6a).
    candidate = mitosis_t.detections(db_session, stage)[0].id
    for kind in ("crop", "context"):
        assert png_array(f"cases/{stage.case_id}/mitosis/crops/{candidate}_{kind}.png").shape == (256, 256, 3)


def test_a_raw_referee_config_sends_raw_crops(db_session, monkeypatch):
    stage, log, runtime = run_seeded_mitosis(db_session, monkeypatch, referee_color="raw")
    run_mitosis(stage, db_session, runtime)
    assert {s["color"] for r in log.pending() if r["task"] == "mitosis_referee" for s in r["input_spec"]["images"]} == {"raw"}
    candidate = mitosis_t.detections(db_session, stage)[0].id
    assert png_array(f"cases/{stage.case_id}/mitosis/crops/{candidate}_crop.png").shape == (256, 256, 3)


def test_hpf_review_images_are_raw_and_normalised_at_three_magnifications(db_session, monkeypatch):
    stage, log, runtime = run_seeded_mitosis(db_session, monkeypatch)
    run_mitosis(stage, db_session, runtime)
    output = mitosis_t.download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/mitosis/output.json")
    hpf = json.loads(output)["hpfs"][0]["seq"]
    base = f"cases/{stage.case_id}/mitosis/hpfs/hpf_{hpf}"
    for mag, side in (("40x", 2048), ("20x", 1024), ("10x", 512)):
        orig, norm = png_array(f"{base}_{mag}_orig.png"), png_array(f"{base}_{mag}_norm.png")
        assert orig.shape == norm.shape == (side, side, 3)
        assert not np.array_equal(orig, norm)


def test_a_degenerate_fit_stops_a_normalised_referee_but_not_a_raw_one(db_session, monkeypatch):
    stage, log, runtime = run_seeded_mitosis(db_session, monkeypatch)
    make_degenerate(db_session, slide_id_of(stage, db_session))
    with pytest.raises(DegenerateStainProfileError, match="normalized colour"):
        run_mitosis(stage, db_session, runtime)

    stage, log, runtime = run_seeded_mitosis(db_session, monkeypatch, referee_color="raw")
    make_degenerate(db_session, slide_id_of(stage, db_session))
    run_mitosis(stage, db_session, runtime)
    output = json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/mitosis/output.json"))
    assert output["stain_normalization"] == "unavailable"
    hpf = output["hpfs"][0]["seq"]
    assert blob_exists(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/mitosis/hpfs/hpf_{hpf}_10x_orig.png")
    assert not blob_exists(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/mitosis/hpfs/hpf_{hpf}_10x_norm.png")


def test_mitosis_needs_the_registered_mask(db_session, monkeypatch):
    stage, log, runtime = run_seeded_mitosis(db_session, monkeypatch)
    drop_mask(stage.case_id)
    with pytest.raises(TissueMaskMissingError, match="run the preprocess stage"):
        run_mitosis(stage, db_session, runtime)


# -- grading --------------------------------------------------------------------------------------------------------------


def run_seeded_grading(db, monkeypatch, color=None):
    stage, raw_uri = grading_t.seed(db)
    install_fake_slide(monkeypatch, FakeOpenSlide(grading_t.SIDE_PX, grading_t.SIDE_PX), raw_uri)
    config = grading_t.small_config()
    if color:
        estimators = config.scoring.grading.estimators.model_copy(update={"color": color})
        grading = config.scoring.grading.model_copy(update={"estimators": estimators})
        config = config.model_copy(update={"scoring": config.scoring.model_copy(update={"grading": grading})})
    log = DecisionLog()
    vlm = FakeAdapter(then=grading_t.answer_by_schema)
    return stage, log, make_runtime(stage, {"vertex_genai": vlm}, config=config, log=log)


def estimator_specs(log):
    return [s for r in log.pending() if r["task"] in ("tubule_patch", "pleo_field", "histotype") for s in r["input_spec"]["images"]]


def test_the_estimators_are_shown_normalised_patches_through_the_persisted_profile(db_session, monkeypatch):
    stage, log, runtime = run_seeded_grading(db_session, monkeypatch)
    run_grading(stage, db_session, runtime)
    specs = estimator_specs(log)
    assert specs and {s["color"] for s in specs} == {"normalized"}
    assert all(s["mpp"] in (1.0, 0.25) and s["size_px"] == [512, 512] for s in specs)
    for kind, sid in (("tubule", "t_01"), ("pleo", "p_01")):
        assert png_array(sample_blob(str(stage.case_id), kind, sid)).shape == (512, 512, 3)


def test_a_raw_estimator_config_sends_the_slide_as_scanned(db_session, monkeypatch):
    stage_n, log_n, runtime_n = run_seeded_grading(db_session, monkeypatch)
    run_grading(stage_n, db_session, runtime_n)
    stage_r, log_r, runtime_r = run_seeded_grading(db_session, monkeypatch, color="raw")
    run_grading(stage_r, db_session, runtime_r)
    assert {s["color"] for s in estimator_specs(log_r)} == {"raw"}
    normalised, raw = png_array(sample_blob(str(stage_n.case_id), "tubule", "t_01")), png_array(sample_blob(str(stage_r.case_id), "tubule", "t_01"))
    assert np.array_equal(normalised, transform_of(db_session, slide_id_of(stage_n, db_session)).apply(raw))  # same tissue, one through the profile


def test_grading_stops_on_a_degenerate_fit_only_when_it_needs_normalised_colour(db_session, monkeypatch):
    stage, log, runtime = run_seeded_grading(db_session, monkeypatch)
    make_degenerate(db_session, slide_id_of(stage, db_session))
    with pytest.raises(DegenerateStainProfileError, match="normalized colour"):
        run_grading(stage, db_session, runtime)

    stage, log, runtime = run_seeded_grading(db_session, monkeypatch, color="raw")
    make_degenerate(db_session, slide_id_of(stage, db_session))
    run_grading(stage, db_session, runtime)
    assert {s["color"] for s in estimator_specs(log)} == {"raw"}


