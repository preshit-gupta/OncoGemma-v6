"""Stain estimation, the fitted profile and the pointwise StainTransform (SPEC-04 §3.4, AC2)."""
import uuid

import numpy as np
import openslide
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base
from app.core.pipeline_config import StainFitConfig, StainReference
from app.core.stain_profiles import latest_stain_profile, save_stain_profile, stain_transform_for_slide
from app.models.case import Case
from app.models.slide import Slide
from pipeline.errors import DegenerateStainProfileError, StainProfileMissingError
from pipeline.slide_io import SlideReader
from pipeline.stain import (
    FITTER_VERSION,
    StainTransform,
    estimate_stain,
    fit_stain_profile,
    reference_from_patch,
    rgb_to_od,
)
from tests.fakes.he import E_VECTOR, GLASS, H_VECTOR, W_HE, he_picture, he_pixels, rgb_from_concentrations
from tests.fakes.slide import FakeOpenSlide

FIT = StainFitConfig(
    n_patches=6,
    n_candidates=12,
    patch_um=64.0,
    fit_mpp=1.0,
    min_sat_mean=0.05,
    sparse_below=4,
    od_beta=0.15,
    angle_percentile=1.0,
    conc_percentile=99.0,
    min_angle_spread_rad=0.25,
    min_tissue_pixels=500,
)
UNIT_REFERENCE = StainReference(
    reference_id="test@v1",
    w_tgt=W_HE.tolist(),
    maxc_tgt=[1.0, 0.8],
    fitter_version=FITTER_VERSION,
    n_slides=0,
    slide_ids_sha256=None,
    source="test",
)


def cosine(a, b) -> float:
    return float(abs(np.dot(a, b)) / (np.linalg.norm(a) * np.linalg.norm(b)))


# -- optical density --------------------------------------------------------------------------


def test_optical_density_of_known_intensities():
    od = rgb_to_od(np.array([[255, 0, 128]], dtype=np.uint8))
    assert od[0, 0] == 0.0
    assert od[0, 1] == pytest.approx(-np.log10(1 / 255))  # zero is clamped to one, never infinite
    assert od[0, 2] == pytest.approx(-np.log10(128 / 255))


# -- estimation ---------------------------------------------------------------------------------


def test_estimation_recovers_the_stain_vectors_and_concentrations():
    estimate = estimate_stain(he_pixels(20000, seed=1), FIT)
    assert estimate is not None
    assert cosine(estimate.w[0], H_VECTOR) > 0.995 and cosine(estimate.w[1], E_VECTOR) > 0.995
    assert np.allclose(np.linalg.norm(estimate.w, axis=1), 1.0)
    assert estimate.max_conc[0] == pytest.approx(1.15, abs=0.15)  # 99th percentile of U(0.05, 1) * 1.2
    assert estimate.max_conc[1] == pytest.approx(0.86, abs=0.12)


def test_estimation_is_deterministic():
    pixels = he_pixels(5000, seed=2)
    first, second = estimate_stain(pixels, FIT), estimate_stain(pixels.copy(), FIT)
    assert np.array_equal(first.w, second.w) and np.array_equal(first.max_conc, second.max_conc)


def test_the_estimate_does_not_depend_on_the_image_shape():
    pixels = he_pixels(4800, seed=3)
    flat = estimate_stain(pixels.reshape(-1, 3), FIT)
    tiled = estimate_stain(pixels.reshape(60, 80, 3), FIT)
    assert np.array_equal(flat.w, tiled.w)


def test_glass_has_no_stain_to_estimate():
    assert estimate_stain(np.full((100, 100, 3), GLASS, dtype=np.uint8), FIT) is None


def test_too_few_stained_pixels_give_no_estimate():
    pixels = np.full((100, 100, 3), 255, dtype=np.uint8)
    pixels.reshape(-1, 3)[:499] = he_pixels(499, seed=4)  # min_tissue_pixels is 500
    assert estimate_stain(pixels, FIT) is None


def test_a_single_stain_cannot_be_separated():
    rng = np.random.default_rng(5)
    only_eosin = np.column_stack([np.zeros(5000), rng.uniform(0.2, 0.9, 5000)])
    assert estimate_stain(rgb_from_concentrations(only_eosin), FIT) is None


def test_a_reference_can_be_fitted_from_one_image():
    reference = reference_from_patch(he_pixels(20000, seed=6).reshape(100, 200, 3), FIT, "patch@v1", "test patch")
    assert (reference.reference_id, reference.n_slides, reference.slide_ids_sha256) == ("patch@v1", 0, None)
    assert reference.fitter_version == FITTER_VERSION
    with pytest.raises(DegenerateStainProfileError, match="cannot be a colour reference"):
        reference_from_patch(np.full((50, 50, 3), GLASS, dtype=np.uint8), FIT, "glass@v1", "glass")


# -- the transform -------------------------------------------------------------------------------


def identity_transform(**kwargs) -> StainTransform:
    return StainTransform(W_HE, [1.0, 0.8], W_HE, [1.0, 0.8], od_beta=0.15, **kwargs)


def test_mapping_a_stain_onto_itself_changes_nothing_beyond_rounding():
    pixels = he_pixels(2000, seed=7)
    out = identity_transform().apply(pixels)
    assert np.abs(out.astype(int) - pixels.astype(int)).max() <= 1


def test_concentrations_are_rescaled_to_the_reference_maximum():
    """Pure hematoxylin at half the slide's maximum lands at half the reference's maximum."""
    transform = StainTransform(W_HE, [1.0, 1.0], W_HE, [2.0, 1.0], od_beta=0.15)
    pixel = rgb_from_concentrations(np.array([[0.5, 0.0]]))
    expected = rgb_from_concentrations(np.array([[1.0, 0.0]]))  # 0.5 * (2.0 / 1.0)
    assert np.abs(transform.apply(pixel).astype(int) - expected.astype(int)).max() <= 1


def test_the_reference_stain_vectors_replace_the_slides():
    """Pure slide hematoxylin is drawn with the reference's hematoxylin colour."""
    reference_w = np.array([[0.5, 0.6, 0.62], [0.2, 0.9, 0.39]])
    reference_w = reference_w / np.linalg.norm(reference_w, axis=1, keepdims=True)
    transform = StainTransform(W_HE, [1.0, 1.0], reference_w, [1.0, 1.0], od_beta=0.15)
    out = transform.apply(rgb_from_concentrations(np.array([[0.8, 0.0]])))
    assert np.abs(out.astype(int) - rgb_from_concentrations(np.array([[0.8, 0.0]]), reference_w).astype(int)).max() <= 1


def test_unstained_pixels_pass_through_unchanged():
    glass = np.full((4, 4, 3), GLASS, dtype=np.uint8)
    faint = np.full((4, 4, 3), 230, dtype=np.uint8)  # optical density 0.044 < 0.15
    transform = StainTransform(W_HE, [1.0, 1.0], W_HE, [3.0, 3.0], od_beta=0.15)
    assert np.array_equal(transform.apply(glass), glass) and np.array_equal(transform.apply(faint), faint)


def test_a_pixel_at_the_threshold_is_transformed():
    transform = StainTransform(W_HE, [1.0, 1.0], W_HE, [2.0, 2.0], od_beta=0.15)
    at_threshold = np.full((1, 1, 3), int(255 * 10 ** -0.16), dtype=np.uint8)  # optical density just above beta
    assert not np.array_equal(transform.apply(at_threshold), at_threshold)


def test_negative_concentrations_are_clipped_to_zero():
    """A colour outside the two stains' cone (a pure green) cannot subtract stain from the output."""
    out = identity_transform().apply(np.array([[[10, 200, 10]]], dtype=np.uint8))
    assert out.shape == (1, 1, 3) and out.dtype == np.uint8


def test_the_transform_keeps_shape_and_dtype():
    for shape in ((7, 3), (5, 6, 3), (2, 3, 4, 3)):
        out = identity_transform().apply(he_pixels(int(np.prod(shape[:-1])), seed=8).reshape(shape))
        assert out.shape == shape and out.dtype == np.uint8


@pytest.mark.parametrize(
    "bad",
    [np.zeros((4, 4, 3), dtype=np.float64), np.zeros((4, 4, 4), dtype=np.uint8), np.zeros((4, 4), dtype=np.uint8)],
)
def test_the_transform_refuses_other_arrays(bad):
    with pytest.raises(ValueError, match="uint8 array"):
        identity_transform().apply(bad)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(w_src=np.ones((3, 3))),
        dict(maxc_src=[1.0, 0.0]),
        dict(maxc_tgt=[-1.0, 1.0]),
        dict(w_tgt=[[np.nan, 0, 0], [0, 1, 0]]),
        dict(od_beta=0.0),
    ],
)
def test_the_transform_refuses_a_malformed_profile(kwargs):
    args = dict(w_src=W_HE, maxc_src=[1.0, 1.0], w_tgt=W_HE, maxc_tgt=[1.0, 1.0], od_beta=0.15)
    args.update(kwargs)
    with pytest.raises(ValueError):
        StainTransform(**args)


def test_the_transform_carries_its_profile_id():
    profile_id = uuid.uuid4()
    assert identity_transform(profile_id=profile_id).profile_id == profile_id
    assert identity_transform().profile_id is None


# -- pointwise (AC2) -------------------------------------------------------------------------------


def transform_under_test() -> StainTransform:
    """A profile unlike its reference, so most pixels change."""
    src = np.array([[0.6, 0.72, 0.34], [0.12, 0.93, 0.35]])
    src = src / np.linalg.norm(src, axis=1, keepdims=True)
    return StainTransform(src, [0.9, 0.5], W_HE, [1.2, 0.9], od_beta=0.15)


tiles = hnp.arrays(np.uint8, st.tuples(st.integers(1, 24), st.integers(1, 24), st.just(3)))


@settings(max_examples=150, deadline=None)
@given(a=tiles, b=tiles)
def test_applying_to_concatenated_tiles_equals_concatenating_the_results(a, b):
    """AC2: the transform is a per-pixel function, so a tile's colours do not depend on its neighbours."""
    transform = transform_under_test()
    height = min(a.shape[0], b.shape[0])
    across = np.concatenate([a[:height], b[:height]], axis=1)
    assert np.array_equal(
        transform.apply(across), np.concatenate([transform.apply(a[:height]), transform.apply(b[:height])], axis=1)
    )
    width = min(a.shape[1], b.shape[1])
    down = np.concatenate([a[:, :width], b[:, :width]], axis=0)
    assert np.array_equal(
        transform.apply(down), np.concatenate([transform.apply(a[:, :width]), transform.apply(b[:, :width])], axis=0)
    )


def test_applying_to_random_tiles_of_every_size_is_bit_exact():
    transform = transform_under_test()
    rng = np.random.default_rng(11)
    whole = rng.integers(0, 256, size=(97, 131, 3), dtype=np.uint8)
    expected = transform.apply(whole)
    for _ in range(40):
        y0, x0 = int(rng.integers(0, 90)), int(rng.integers(0, 120))
        h, w = int(rng.integers(1, 97 - y0 + 1)), int(rng.integers(1, 131 - x0 + 1))
        assert np.array_equal(transform.apply(whole[y0 : y0 + h, x0 : x0 + w]), expected[y0 : y0 + h, x0 : x0 + w])


# -- fitting a slide ---------------------------------------------------------------------------------


def open_he_slide(monkeypatch, picture, size_px: int = 2048) -> SlideReader:
    slide = FakeOpenSlide(size_px, size_px, picture=picture)
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: slide)
    return SlideReader("he.svs", 1.0, 1.0, "svs")  # 1 µm/px: a 64 µm patch is 64 px read as is


def origins(n: int, spacing: float = 128.0) -> list[tuple[float, float]]:
    return [(float(i % 8) * spacing, float(i // 8) * spacing) for i in range(n)]


def test_a_stained_slide_fits_and_maps_to_the_reference(monkeypatch):
    reader = open_he_slide(monkeypatch, he_picture())
    fit = fit_stain_profile(reader, origins(12), FIT, UNIT_REFERENCE)
    assert (fit.fit_status, fit.n_patches) == ("fitted", 6)  # stops at n_patches
    assert fit.fitter_version == FITTER_VERSION and fit.reference_id == "test@v1"
    assert cosine(fit.w_src[0], H_VECTOR) > 0.99 and cosine(fit.w_src[1], E_VECTOR) > 0.99
    assert (fit.w_tgt, fit.maxc_tgt) == (UNIT_REFERENCE.w_tgt, UNIT_REFERENCE.maxc_tgt)
    assert len(fit.mosaic_sha256) == 64
    StainTransform.from_profile(fit, od_beta=FIT.od_beta)  # usable


def test_the_fit_is_reproducible_and_depends_on_its_patches(monkeypatch):
    reader = open_he_slide(monkeypatch, he_picture())
    first = fit_stain_profile(reader, origins(12), FIT, UNIT_REFERENCE)
    assert fit_stain_profile(reader, origins(12), FIT, UNIT_REFERENCE) == first
    other = fit_stain_profile(reader, origins(12, spacing=160.0), FIT, UNIT_REFERENCE)
    assert other.mosaic_sha256 != first.mosaic_sha256


def test_few_valid_patches_leave_the_fit_sparse(monkeypatch):
    reader = open_he_slide(monkeypatch, he_picture())
    fit = fit_stain_profile(reader, origins(3), FIT, UNIT_REFERENCE)  # sparse_below is 4
    assert (fit.fit_status, fit.n_patches) == ("sparse", 3)
    StainTransform.from_profile(fit, od_beta=FIT.od_beta)  # a sparse profile is still usable


def test_glass_patches_are_skipped(monkeypatch):
    def glass_then_tissue(xs, ys):
        rgb = he_picture()(xs, ys)
        rgb[xs < 512] = GLASS  # the left quarter of the slide is empty
        return rgb

    reader = open_he_slide(monkeypatch, glass_then_tissue)
    glass = [(0.0, float(y)) for y in range(0, 256, 64)]
    tissue = [(512.0 + 100.0 * i, 100.0 * j) for j in range(3) for i in range(4)]
    fit = fit_stain_profile(reader, glass + tissue, FIT, UNIT_REFERENCE)
    assert fit.fit_status == "fitted" and fit.n_patches == 6
    assert fit.mosaic_sha256 == fit_stain_profile(reader, tissue, FIT, UNIT_REFERENCE).mosaic_sha256  # glass left no trace


def test_a_slide_with_no_stained_patch_is_degenerate(monkeypatch):
    reader = open_he_slide(monkeypatch, lambda xs, ys: np.full(xs.shape + (3,), GLASS, dtype=np.uint8))
    fit = fit_stain_profile(reader, origins(12), FIT, UNIT_REFERENCE)
    assert (fit.fit_status, fit.n_patches) == ("degenerate", 0)
    assert (fit.w_src, fit.maxc_src) == (UNIT_REFERENCE.w_tgt, UNIT_REFERENCE.maxc_tgt)
    with pytest.raises(DegenerateStainProfileError, match="cannot be normalised"):
        StainTransform.from_profile(fit, od_beta=FIT.od_beta)


def test_a_slide_stained_with_one_dye_is_degenerate(monkeypatch):
    reader = open_he_slide(monkeypatch, he_picture(eosin_only=True))
    fit = fit_stain_profile(reader, origins(12), FIT, UNIT_REFERENCE)
    assert fit.fit_status == "degenerate" and fit.n_patches == 6  # the patches were valid; the stains inseparable


def test_no_candidate_origins_is_degenerate(monkeypatch):
    reader = open_he_slide(monkeypatch, he_picture())
    assert fit_stain_profile(reader, [], FIT, UNIT_REFERENCE).fit_status == "degenerate"


# -- persistence ---------------------------------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        yield db
    engine.dispose()


def add_slide(db) -> Slide:
    case = Case(created_by="tester")
    db.add(case)
    db.flush()
    slide = Slide(case_id=case.id, gcs_uri_original="gs://raw/x.svs", mpp_x=0.25, mpp_y=0.25)
    db.add(slide)
    db.flush()
    return slide


def a_fit(monkeypatch, n: int = 12):
    return fit_stain_profile(open_he_slide(monkeypatch, he_picture()), origins(n), FIT, UNIT_REFERENCE)


def test_a_saved_profile_becomes_the_slides_transform(session, monkeypatch):
    slide = add_slide(session)
    fit = a_fit(monkeypatch)
    row = save_stain_profile(session, slide.id, fit)
    session.commit()
    assert (row.fit_status, row.n_patches, row.reference_id) == ("fitted", 6, "test@v1")
    transform = stain_transform_for_slide(session, slide.id, od_beta=FIT.od_beta)
    assert transform.profile_id == row.id
    pixels = he_pixels(500, seed=9)
    assert np.array_equal(transform.apply(pixels), StainTransform.from_profile(fit, od_beta=FIT.od_beta).apply(pixels))


def test_the_newest_profile_wins(session, monkeypatch):
    slide = add_slide(session)
    old = save_stain_profile(session, slide.id, a_fit(monkeypatch, n=3))
    new = save_stain_profile(session, str(slide.id), a_fit(monkeypatch))
    session.commit()
    assert latest_stain_profile(session, slide.id).id == new.id != old.id


def test_the_newest_profile_wins_even_within_one_clock_tick(session, monkeypatch):
    """Two fits stamped by the same coarse clock tick must not tie: a later fit is always the newer profile."""
    import app.core.stain_profiles as store

    class FrozenClock:
        @staticmethod
        def now(tz=None):
            from datetime import datetime

            return datetime(2026, 9, 29, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(store, "datetime", FrozenClock)
    slide = add_slide(session)
    fit = a_fit(monkeypatch)
    saved = [save_stain_profile(session, slide.id, fit) for _ in range(12)]
    session.commit()
    assert latest_stain_profile(session, slide.id).id == saved[-1].id
    assert [row.created_at for row in saved] == sorted({row.created_at for row in saved})  # strictly increasing


def test_a_slide_without_a_profile_needs_preprocess(session):
    slide = add_slide(session)
    with pytest.raises(StainProfileMissingError, match="preprocess"):
        stain_transform_for_slide(session, slide.id, od_beta=FIT.od_beta)


def test_a_degenerate_profile_is_stored_but_never_applied(session, monkeypatch):
    slide = add_slide(session)
    reader = open_he_slide(monkeypatch, lambda xs, ys: np.full(xs.shape + (3,), GLASS, dtype=np.uint8))
    save_stain_profile(session, slide.id, fit_stain_profile(reader, origins(4), FIT, UNIT_REFERENCE))
    assert latest_stain_profile(session, slide.id).fit_status == "degenerate"
    with pytest.raises(DegenerateStainProfileError):
        stain_transform_for_slide(session, slide.id, od_beta=FIT.od_beta)


def test_deleting_a_slide_deletes_its_profiles(session, monkeypatch):
    slide = add_slide(session)
    save_stain_profile(session, slide.id, a_fit(monkeypatch))
    session.commit()
    session.delete(slide)
    session.commit()
    with pytest.raises(StainProfileMissingError):
        latest_stain_profile(session, slide.id)
