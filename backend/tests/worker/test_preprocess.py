"""Stage 2 (preprocess): specimen gating, the registered mask, the stain profile, the normalised pyramid
(SPEC-04 §3.2-3.6, AC8)."""
import json
import math
import uuid
from pathlib import Path

import numpy as np
import openslide
import pytest
from openslide.deepzoom import DeepZoomGenerator
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.core.db import Base, engine
from app.core.gcs import blob_exists, download_blob_as_bytes, upload_blob_from_bytes
from app.core.pipeline_config import NormPyramidConfig, get_pipeline_config, load_pipeline_config
from app.core.stain_profiles import latest_stain_profile
from app.core.tissue_mask_store import load_tissue_mask
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.stain_profile import StainProfile
from pipeline.errors import SlideReadError, SpecimenTypeRequired
from pipeline.slide_io import SlideReader, read_region_at_mpp
from pipeline.stain import StainTransform
from tests.core.helpers import REPO_CONFIGS, VARIABLES, copy_configs, edit_yaml
from tests.fakes.he import GLASS, he_slide_rgb
from tests.fakes.runtime import make_runtime
from tests.fakes.tiff import tissue_rgb, write_pyramid_tiff
from worker.preprocess import DZI_TILE_PX, norm_pyramid_levels, run_preprocess

MPP = 1.0  # 10x: a 2048 x 1536 µm section
WIDTH_PX, HEIGHT_PX = 2048, 1536


@pytest.fixture
def db():
    from app.core.db import SessionLocal

    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def add_case(db, tmp_path, specimen="resection", rgb=None, checksum="cd" * 32) -> tuple[Case, Slide, StageExecution]:
    """A case whose raw slide is a real pyramidal TIFF in the GCS mock."""
    rgb = he_slide_rgb(WIDTH_PX, HEIGHT_PX) if rgb is None else rgb
    case = Case(created_by="tester", status="open", specimen_type=specimen)
    db.add(case)
    db.flush()
    path = write_pyramid_tiff(tmp_path / f"{uuid.uuid4().hex}.tif", rgb, MPP)
    blob = f"cases/{case.id}/slide.tif"
    upload_blob_from_bytes(settings.GCS_RAW_BUCKET, blob, path.read_bytes(), "image/tiff")
    slide = Slide(
        case_id=case.id, gcs_uri_original=f"gs://{settings.GCS_RAW_BUCKET}/{blob}", mpp_x=MPP, mpp_y=MPP,
        width_px=rgb.shape[1], height_px=rgb.shape[0], checksum_sha256=checksum, format="tif",
    )
    db.add(slide)
    db.flush()
    stage = StageExecution(case_id=case.id, stage="preprocess", attempt=1, status="running", input_ref={"slide_id": str(slide.id)})
    db.add(stage)
    db.commit()
    return case, slide, stage


def output_of(case_id) -> dict:
    return json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/preprocess/output.json"))


# -- gating ---------------------------------------------------------------------------------------------


def test_ac8_preprocess_refuses_an_unknown_specimen_type(db, tmp_path):
    case, slide, stage = add_case(db, tmp_path, specimen="unknown")
    with pytest.raises(SpecimenTypeRequired, match="resection or core_biopsy"):
        run_preprocess(stage, db, make_runtime(stage))
    assert db.scalars(select(StainProfile).where(StainProfile.slide_id == slide.id)).first() is None
    assert not blob_exists(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case.id}/preprocess/tissue_mask.png")


def test_ac8_changing_a_profile_changes_the_config_hash(tmp_path):
    base = get_pipeline_config().config_hash()
    for edit in (
        lambda d: d["profiles"]["core_biopsy"]["tissue_mask"].update(open_radius_um=13.0),
        lambda d: d["profiles"]["resection"]["qc"].update(tissue_area_fail_mm2=5.0),
    ):
        configs = copy_configs(tmp_path / uuid.uuid4().hex)
        edit_yaml(configs / "specimen_profiles.yaml", edit)
        assert load_pipeline_config(configs, VARIABLES).config_hash() != base


def test_a_slide_without_a_checksum_has_no_sampling_seed(db, tmp_path):
    _, _, stage = add_case(db, tmp_path, checksum=None)
    with pytest.raises(ValueError, match="no checksum"):
        run_preprocess(stage, db, make_runtime(stage))


def test_an_unreadable_slide_fails_the_stage(db, tmp_path):
    case, slide, stage = add_case(db, tmp_path)
    upload_blob_from_bytes(settings.GCS_RAW_BUCKET, f"cases/{case.id}/slide.tif", b"not a slide", "image/tiff")
    with pytest.raises(SlideReadError, match="could not open slide"):
        run_preprocess(stage, db, make_runtime(stage))


# -- what the stage produces -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stage2_outputs(tmp_path_factory):
    """One full preprocess run of a resection, shared by the tests that inspect its outputs."""
    from app.core.db import SessionLocal

    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    tmp_path = tmp_path_factory.mktemp("stage2")
    case, slide, stage = add_case(session, tmp_path)
    output_ref, versions = run_preprocess(stage, session, make_runtime(stage))
    yield SimpleOutputs(session, case, slide, stage, output_ref, versions)
    session.close()


class SimpleOutputs:
    def __init__(self, session, case, slide, stage, output_ref, versions):
        self.session, self.case, self.slide, self.stage = session, case, slide, stage
        self.output_ref, self.versions = output_ref, versions
        self.output = output_of(case.id)


def test_the_stage_completes_and_names_its_outputs(stage2_outputs):
    out, case = stage2_outputs.output, stage2_outputs.case
    assert stage2_outputs.output_ref == f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case.id}/preprocess/output.json"
    assert stage2_outputs.stage.status == "done"
    assert (out["specimen_type"], out["stain_fit_status"], out["native_mpp"]) == ("resection", "fitted", MPP)
    assert out["norm_pyramid_uri"] == f"gs://{settings.GCS_PYRAMIDS_BUCKET}/{stage2_outputs.slide.id}/norm/"
    assert set(stage2_outputs.versions) == {"stain_fitter", "tissue_mask"}


def test_the_registered_mask_is_persisted_at_the_profiles_resolution(stage2_outputs):
    mask = load_tissue_mask(stage2_outputs.case.id)
    assert mask.mpp == 8.0 and mask.profile == "resection"
    assert (mask.width_px, mask.height_px) == (math.ceil(WIDTH_PX * MPP / 8.0), math.ceil(HEIGHT_PX * MPP / 8.0))
    # 6% glass margins: the section is the middle 88% x 88% of the slide
    expected_mm2 = (WIDTH_PX * 0.88) * (HEIGHT_PX * 0.88) * MPP * MPP / 1e6
    assert mask.area_mm2 == pytest.approx(expected_mm2, rel=0.03)
    assert stage2_outputs.output["tissue_area_mm2"] == pytest.approx(mask.area_mm2, abs=0.01)
    assert not mask.contains_um(10, 10) and mask.contains_um(WIDTH_PX / 2, HEIGHT_PX / 2)


def test_the_stain_profile_is_fitted_once_and_persisted(stage2_outputs):
    row = latest_stain_profile(stage2_outputs.session, stage2_outputs.slide.id)
    assert (row.fit_status, row.reference_id) == ("fitted", "v5_patch@v1")
    assert row.n_patches == get_pipeline_config().specimen_profiles.profiles["resection"].stain_fit.n_patches
    assert stage2_outputs.output["stain_profile_id"] == str(row.id)
    assert len(stage2_outputs.session.scalars(select(StainProfile)).all()) >= 1
    reference = get_pipeline_config().stain_refs["v5_patch@v1"]
    assert row.w_tgt == reference.w_tgt and row.maxc_tgt == reference.maxc_tgt


def test_the_normalised_pyramid_is_rendered_from_the_persisted_profile(stage2_outputs, tmp_path):
    slide, case, session = stage2_outputs.slide, stage2_outputs.case, stage2_outputs.session
    row = latest_stain_profile(session, slide.id)
    transform = StainTransform.from_profile(row, od_beta=get_pipeline_config().specimen_profiles.profiles["resection"].stain_fit.od_beta)
    max_level = math.ceil(math.log2(max(WIDTH_PX, HEIGHT_PX)))
    tile = np.array(Image.open(__import__("io").BytesIO(download_blob_as_bytes(settings.GCS_PYRAMIDS_BUCKET, f"{slide.id}/norm/{max_level}/1_1.png"))))

    path = tmp_path / "raw.tif"
    path.write_bytes(download_blob_as_bytes(settings.GCS_RAW_BUCKET, f"cases/{case.id}/slide.tif"))
    with SlideReader(str(path), MPP, MPP, "tif") as reader:
        raw = read_region_at_mpp(reader, DZI_TILE_PX * MPP, DZI_TILE_PX * MPP, DZI_TILE_PX * MPP, DZI_TILE_PX * MPP, MPP)
    assert tile.shape == (DZI_TILE_PX, DZI_TILE_PX, 3)
    assert np.array_equal(tile, transform.apply(raw.rgb))  # the tile is the raw region through the profile, nothing more
    assert not np.array_equal(tile, raw.rgb)
    assert blob_exists(settings.GCS_PYRAMIDS_BUCKET, f"{slide.id}/norm/{max_level}/1_1.jpg")


def test_the_qc_stage_is_queued_with_the_preprocess_output(stage2_outputs):
    qc = stage2_outputs.session.scalars(
        select(StageExecution).where(StageExecution.case_id == stage2_outputs.case.id, StageExecution.stage == "qc")
    ).one()
    assert qc.status == "queued" and qc.input_ref["preprocess_output_ref"] == stage2_outputs.output_ref


def test_the_v5_stain_params_file_is_still_written_for_unmigrated_stages(stage2_outputs):
    params = json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage2_outputs.case.id}/preprocess/stain_params.json"))
    assert params["fit_status"] == "fitted" and len(params["stain_matrix"]) == 2


def test_a_glass_only_slide_is_degenerate_and_gets_no_normalised_pyramid(db, tmp_path):
    glass = np.full((HEIGHT_PX, WIDTH_PX, 3), GLASS, dtype=np.uint8)
    case, slide, stage = add_case(db, tmp_path, rgb=glass)
    run_preprocess(stage, db, make_runtime(stage))
    out = output_of(case.id)
    assert (out["stain_fit_status"], out["norm_pyramid_uri"], out["tissue_area_mm2"]) == ("degenerate", None, 0.0)
    assert latest_stain_profile(db, slide.id).fit_status == "degenerate"
    assert not blob_exists(settings.GCS_PYRAMIDS_BUCKET, f"{slide.id}/norm/0/0_0.png")
    assert stage.status == "done"  # QC judges the empty slide


def test_a_core_biopsy_uses_its_own_profile(db, tmp_path):
    case, slide, stage = add_case(db, tmp_path, specimen="core_biopsy")
    run_preprocess(stage, db, make_runtime(stage))
    mask = load_tissue_mask(case.id)
    assert mask.mpp == 4.0 and mask.profile == "core_biopsy"
    assert latest_stain_profile(db, slide.id).n_patches == get_pipeline_config().specimen_profiles.profiles["core_biopsy"].stain_fit.n_patches


# -- pyramid geometry ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("width_px, height_px", [(2048, 1536), (5000, 3001), (1000, 1000), (777, 4321)])
def test_the_pyramid_levels_are_openslides_deepzoom_levels(tmp_path, width_px, height_px):
    path = write_pyramid_tiff(tmp_path / "s.tif", tissue_rgb(width_px, height_px, 0.5), 0.5, levels=3)
    dz = DeepZoomGenerator(openslide.OpenSlide(str(path)), tile_size=DZI_TILE_PX, overlap=0, limit_bounds=False)
    cfg = NormPyramidConfig(max_mpp=2.0, max_tiles=100000)
    levels = norm_pyramid_levels(width_px, height_px, 0.5, cfg)
    assert levels, "at least the coarsest level"
    for level in levels:
        assert (level["width_px"], level["height_px"]) == dz.level_dimensions[level["z"]]
        assert (level["cols"], level["rows"]) == dz.level_tiles[level["z"]]
        assert level["scale"] == 2 ** (dz.level_count - 1 - level["z"])
    assert [lv["z"] for lv in levels] == list(range(len(levels)))


def test_the_pyramid_stops_at_the_configured_resolution_and_tile_count():
    # 0.25 µm/px: the level at 1.0 µm/px is two below the deepest.
    deepest = math.ceil(math.log2(60000))
    levels = norm_pyramid_levels(60000, 40000, 0.25, NormPyramidConfig(max_mpp=1.0, max_tiles=10**9))
    assert levels[-1]["z"] == deepest - 2
    limited = norm_pyramid_levels(60000, 40000, 0.25, NormPyramidConfig(max_mpp=1.0, max_tiles=1500))
    assert sum(lv["cols"] * lv["rows"] for lv in limited) <= 1500 and len(limited) < len(levels)
    # A slide already coarser than max_mpp is covered down to its own resolution.
    coarse = norm_pyramid_levels(4000, 3000, 2.0, NormPyramidConfig(max_mpp=1.0, max_tiles=10**9))
    assert coarse[-1]["z"] == math.ceil(math.log2(4000))
