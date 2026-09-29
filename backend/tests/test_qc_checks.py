"""Stage 1 QC checks (SPEC-04 §3.7): absolute tissue area, focus over the mask, resolution."""
from types import SimpleNamespace

import numpy as np
import openslide
import pytest

from app.core.pipeline_config import QcConfig, SpecimenQcConfig, get_config_hash, get_pipeline_config
from pipeline.errors import SlideReadError
from pipeline.qc_checks import (
    check_focus_sharpness,
    check_pen_marks,
    check_resolution,
    check_stain_sanity,
    check_tissue_coverage,
    check_tissue_folds,
    run_all_qc_checks,
)
from pipeline.slide_io import SlideReader
from pipeline.tissue_mask import TissueMask
from tests.fakes.slide import FakeOpenSlide


def qc(**sections) -> QcConfig:
    """The repo's configs/qc.yaml with some fields of the named sections replaced."""
    data = get_pipeline_config().qc.model_dump()
    for name, fields in sections.items():
        data[name] = {**data[name], **fields}
    return QcConfig.model_validate(data)


def specimen_qc(specimen: str = "resection", **fields) -> SpecimenQcConfig:
    base = get_pipeline_config().specimen_profiles.profiles[specimen].qc
    return SpecimenQcConfig.model_validate({**base.model_dump(), **fields})


def tissue(width_px: int, height_px: int, mpp: float) -> TissueMask:
    return TissueMask(np.ones((height_px, width_px), dtype=bool), mpp)


def reader_of(monkeypatch, slide: FakeOpenSlide, mpp: float = 1.0) -> SlideReader:
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: slide)
    return SlideReader("qc.svs", mpp, mpp, "svs")


def noise_picture(xs, ys):
    """Sharp detail everywhere: white noise from a position hash."""
    hashed = (xs.astype(np.int64) * 73856093) ^ (ys.astype(np.int64) * 19349663)
    value = (hashed % 97).astype(np.uint8) * 2 + 40
    return np.repeat(value[..., None], 3, axis=-1)


def flat_picture(xs, ys):
    rgb = np.empty(xs.shape + (3,), dtype=np.uint8)
    rgb[:] = (225, 150, 185)
    return rgb


# -- tissue area ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "side_px, status",
    [(50, "fail"), (150, "fail"), (200, "warn"), (300, "warn"), (450, "pass")],
)
def test_tissue_area_is_absolute_and_uses_the_resection_thresholds(side_px, status):
    """Resection: fail below 4 mm², warn below 20 mm². A 10 µm/px mask of side_px x side_px is (side_px / 100)² mm²."""
    res = check_tissue_coverage(tissue(side_px, side_px, 10.0), specimen_qc("resection"))
    assert res["status"] == status
    assert res["metric"] == pytest.approx((side_px / 100) ** 2)
    assert res["name"] == "tissue_coverage" and "mm²" in res["message"]


def test_a_small_core_is_judged_by_the_core_biopsy_thresholds():
    """1.0 x 1.5 mm = 1.5 mm² of tissue: a fail for a resection, only a warning for a core biopsy (fail 0.5, warn 2.0)."""
    mask = tissue(150, 100, 10.0)
    assert check_tissue_coverage(mask, specimen_qc("resection"))["status"] == "fail"
    assert check_tissue_coverage(mask, specimen_qc("core_biopsy"))["status"] == "warn"


def test_the_area_does_not_depend_on_how_much_glass_surrounds_the_tissue():
    """v5 judged a fraction of the slide, so a single core on a big slide failed."""
    small = np.zeros((100, 100), dtype=bool)
    small[:50, :50] = True
    big = np.zeros((1000, 1000), dtype=bool)
    big[:50, :50] = True
    profile = specimen_qc("core_biopsy")
    assert check_tissue_coverage(TissueMask(small, 10.0), profile)["metric"] == check_tissue_coverage(TissueMask(big, 10.0), profile)["metric"]


# -- focus ----------------------------------------------------------------------------------------


EXTENT_PX = 2560  # 2.56 mm at 1 µm/px: a 5 x 5 grid of 512 µm tiles


def focus_of(monkeypatch, picture, *, seed=1, mask=None, qc_config=None, profile=None):
    slide = FakeOpenSlide(EXTENT_PX, EXTENT_PX, picture=picture)
    reader = reader_of(monkeypatch, slide)
    mask = tissue(EXTENT_PX // 8, EXTENT_PX // 8, 8.0) if mask is None else mask
    result = check_focus_sharpness(
        reader, mask, config=qc_config or qc(), specimen_qc=profile or specimen_qc("resection"), seed=seed
    )
    return result, slide


def test_sharp_tissue_passes_the_focus_check(monkeypatch):
    res, slide = focus_of(monkeypatch, noise_picture)
    assert (res["name"], res["status"], res["metric"]) == ("focus", "pass", 0.0)
    assert len(slide.regions_read) == 25


def test_out_of_focus_tissue_fails_the_focus_check(monkeypatch):
    res, _ = focus_of(monkeypatch, flat_picture)
    assert (res["status"], res["metric"]) == ("fail", 1.0)
    assert "Critical focus blur" in res["message"]


def test_a_few_blurry_tiles_only_warn(monkeypatch):
    def three_flat_tiles(xs, ys):
        rgb = noise_picture(xs, ys)
        rgb[(ys < 512) & (xs < 3 * 512)] = (225, 150, 185)  # the first three tiles of the top row
        return rgb

    res, _ = focus_of(monkeypatch, three_flat_tiles)
    assert res["status"] == "warn" and res["metric"] == pytest.approx(3 / 25, abs=1e-4)  # warn > 10%, fail > 30%


def test_focus_thresholds_come_from_the_specimen_profile(monkeypatch):
    def three_flat_tiles(xs, ys):
        rgb = noise_picture(xs, ys)
        rgb[(ys < 512) & (xs < 3 * 512)] = (225, 150, 185)
        return rgb

    lenient = specimen_qc("resection", focus_warn_ratio=0.2, focus_fail_ratio=0.5)
    assert focus_of(monkeypatch, three_flat_tiles, profile=lenient)[0]["status"] == "pass"


def test_focus_only_samples_tiles_that_are_mostly_tissue(monkeypatch):
    mask = np.zeros((EXTENT_PX // 8, EXTENT_PX // 8), dtype=bool)
    mask[: 512 // 8, : 2 * 512 // 8] = True  # two tiles of tissue
    res, slide = focus_of(monkeypatch, noise_picture, mask=TissueMask(mask, 8.0))
    assert len(slide.regions_read) == 2 and res["status"] == "pass"


def test_focus_samples_at_most_the_configured_number_of_tiles_reproducibly(monkeypatch):
    capped = qc(focus={"sample_max_tiles": 6})
    _, slide_a = focus_of(monkeypatch, noise_picture, seed=3, qc_config=capped)
    _, slide_b = focus_of(monkeypatch, noise_picture, seed=3, qc_config=capped)
    _, slide_c = focus_of(monkeypatch, noise_picture, seed=4, qc_config=capped)
    assert len(slide_a.regions_read) == 6
    assert slide_a.regions_read == slide_b.regions_read
    assert slide_a.regions_read != slide_c.regions_read


def test_focus_is_not_assessed_when_no_tile_is_mostly_tissue(monkeypatch):
    sparse = np.zeros((EXTENT_PX // 8, EXTENT_PX // 8), dtype=bool)
    sparse[::4, ::4] = True
    res, slide = focus_of(monkeypatch, noise_picture, mask=TissueMask(sparse, 8.0))
    assert (res["status"], res["metric"]) == ("warn", None)
    assert "not assessed" in res["message"] and slide.regions_read == []


def test_a_tile_that_cannot_be_read_fails_the_check_instead_of_passing_it(monkeypatch):
    slide = FakeOpenSlide(EXTENT_PX, EXTENT_PX, picture=noise_picture)
    reader = reader_of(monkeypatch, slide)

    def broken(location, level, size):
        raise openslide.OpenSlideError("TIFFRGBAImageGet failed")

    slide.read_region = broken
    with pytest.raises(SlideReadError, match="TIFFRGBAImageGet failed"):
        check_focus_sharpness(
            reader, tissue(320, 320, 8.0), config=qc(), specimen_qc=specimen_qc(), seed=1
        )


# -- pen marks and folds (on the overview) ------------------------------------------------------------


def slide_array(color=(245, 240, 245), size=400) -> np.ndarray:
    return np.full((size, size, 3), color, dtype=np.uint8)


def test_pen_marks_pass_on_a_clean_slide():
    res = check_pen_marks(slide_array(), 10.0, config=qc())
    assert (res["name"], res["status"], res["metric"]) == ("pen_marks", "pass", 0.0)


def test_a_large_green_mark_warns_with_its_area_in_mm2():
    rgb = slide_array()
    rgb[100:200, 100:250] = (0, 200, 50)  # 100 x 150 px at 10 µm/px = 1 x 1.5 mm
    res = check_pen_marks(rgb, 10.0, config=qc())
    assert res["status"] == "warn" and res["metric"] == pytest.approx(1.5, rel=0.05)
    assert check_pen_marks(rgb, 10.0, config=qc(pen_marks={"min_component_area_mm2": 2.0}))["status"] == "pass"


def test_a_dark_fold_ridge_within_tissue_warns():
    rgb = slide_array((240, 230, 240))
    rgb[50:350, 100:112] = (80, 0, 50)  # a 3 mm long dark, saturated ridge at 10 µm/px
    tissue_px = np.ones(rgb.shape[:2], dtype=bool)
    res = check_tissue_folds(rgb, tissue_px, 10.0, config=qc(folds={"min_skeleton_length_mm": 2.0, "saturation_min": 50, "brightness_max": 120}))
    assert res["name"] == "folds" and res["status"] == "warn" and res["metric"] > 2.9


def test_a_fold_outside_the_tissue_mask_is_ignored():
    rgb = slide_array((240, 230, 240))
    rgb[50:350, 100:112] = (80, 0, 50)
    off_tissue = np.zeros(rgb.shape[:2], dtype=bool)
    res = check_tissue_folds(rgb, off_tissue, 10.0, config=qc(folds={"min_skeleton_length_mm": 2.0, "saturation_min": 50, "brightness_max": 120}))
    assert res["status"] == "pass" and res["metric"] == 0.0


# -- stain sanity ---------------------------------------------------------------------------------------


def stain_profile(status="fitted", maxc=(1.0, 0.6), n_patches=30):
    return SimpleNamespace(fit_status=status, maxc_src=list(maxc), n_patches=n_patches)


def test_a_normal_stain_profile_passes_and_reports_the_fit():
    res = check_stain_sanity(stain_profile(), qc())
    assert res["status"] == "pass" and res["metric"] == pytest.approx(1.0 / 0.6, abs=1e-3)
    assert "fitted fit" in res["message"]
    assert "sparse fit" in check_stain_sanity(stain_profile("sparse", n_patches=3), qc())["message"]


def test_the_slides_own_concentrations_are_checked_not_the_reference():
    faded = check_stain_sanity(stain_profile(maxc=(0.08, 0.05)), qc())
    assert faded["status"] == "warn" and "Faded" in faded["message"]
    lopsided = check_stain_sanity(stain_profile(maxc=(3.0, 0.4)), qc())
    assert lopsided["status"] == "warn" and "ratio" in lopsided["message"]


def test_a_degenerate_stain_fit_warns_with_its_code():
    res = check_stain_sanity(stain_profile("degenerate", n_patches=0), qc())
    assert res["status"] == "warn" and "stain_fit_degenerate" in res["message"]


# -- resolution -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "native_mpp, status",
    [(0.25, "pass"), (0.2529, "pass"), (0.30, "pass"), (0.31, "warn"), (0.5, "warn"), (0.55, "warn"), (0.56, "fail"), (1.0, "fail")],
)
def test_resolution_warns_below_40x_and_fails_below_20x(native_mpp, status):
    res = check_resolution(native_mpp, qc())
    assert (res["name"], res["status"], res["metric"]) == ("resolution", status, native_mpp)


def test_the_resolution_message_says_what_happens_to_the_slide():
    assert "upsampled" in check_resolution(0.5, qc())["message"]
    assert "not supported" not in check_resolution(0.5, qc())["message"]
    assert "coarse" in check_resolution(0.7, qc())["message"]


# -- the whole suite ------------------------------------------------------------------------------------


def run_suite(monkeypatch, picture=noise_picture, mpp=1.0, profile=None, stain=None, extent_px=EXTENT_PX):
    slide = FakeOpenSlide(extent_px, extent_px, picture=picture)
    reader = reader_of(monkeypatch, slide, mpp=mpp)
    mask = tissue(int(extent_px * mpp / 8), int(extent_px * mpp / 8), 8.0)
    return run_all_qc_checks(
        reader, mask, stain or stain_profile(),
        config=qc(), specimen_qc=profile or specimen_qc(), seed=1, config_hash=get_config_hash(),
    ), slide


def test_the_full_suite_runs_six_checks_and_stamps_the_hash(monkeypatch):
    res, _ = run_suite(monkeypatch, noise_picture, mpp=2.0 / 8)  # 0.25 µm/px: 640 µm across is too small a tissue area
    assert {c["name"] for c in res["checks"]} == {"tissue_coverage", "focus", "pen_marks", "folds", "stain_sanity", "resolution"}
    assert res["config_hash"] == get_config_hash() and res["native_mpp"] == 0.25
    assert res["verdict"] == "fail"  # 0.41 mm² of tissue is below the resection fail area of 4 mm²


# A 0.25 µm/px slide of 2560 px is 640 µm across (0.41 mm²): these thresholds let its tissue pass.
SMALL_TISSUE_OK = dict(tissue_area_fail_mm2=0.1, tissue_area_warn_mm2=0.2)


def test_a_slide_that_passes_every_check_passes(monkeypatch):
    res, _ = run_suite(monkeypatch, noise_picture, mpp=0.25, profile=specimen_qc("resection", **SMALL_TISSUE_OK))
    assert [c["status"] for c in res["checks"]] == ["pass"] * 6
    assert res["verdict"] == "pass"


def test_the_verdict_is_the_worst_check(monkeypatch):
    profile = specimen_qc("resection", **SMALL_TISSUE_OK)
    res, _ = run_suite(monkeypatch, noise_picture, mpp=0.25, profile=profile, stain=stain_profile("degenerate", n_patches=0))
    assert res["verdict"] == "warn"
    res, _ = run_suite(monkeypatch, flat_picture, mpp=0.25, profile=profile)  # out of focus
    assert res["verdict"] == "fail"


def test_a_coarse_slide_fails_on_resolution(monkeypatch):
    profile = specimen_qc("resection", tissue_area_fail_mm2=0.1, tissue_area_warn_mm2=0.2)
    res, _ = run_suite(monkeypatch, noise_picture, mpp=1.0, profile=profile)  # 1 µm/px is 10x, not 40x
    resolution = next(c for c in res["checks"] if c["name"] == "resolution")
    assert resolution["status"] == "fail" and res["verdict"] == "fail"


def test_the_overview_is_read_once_for_pen_marks_and_folds(monkeypatch):
    _, slide = run_suite(monkeypatch, noise_picture, mpp=0.25, profile=specimen_qc("resection", **SMALL_TISSUE_OK))
    overview_reads = [r for r in slide.regions_read if r[1] == (EXTENT_PX, EXTENT_PX)]  # the whole 2560 px level 0
    assert len(overview_reads) == 1
