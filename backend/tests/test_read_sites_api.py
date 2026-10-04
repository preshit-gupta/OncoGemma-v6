"""The routers read pixels through read_region_at_mpp and the slide's persisted stain profile (SPEC-04 §3.4)."""
import io
import os
import tempfile
import uuid
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

import app.routers.mitosis as mitosis_router
import app.routers.triage as triage_router
from app.core.config import settings
from app.core.db import Base, SessionLocal, engine
from app.core.gcs import upload_blob_from_bytes
from app.core.pipeline_config import get_pipeline_config
from app.core.stain_profiles import save_stain_profile
from app.main import app
from app.models.case import Case
from app.models.detection import Detection
from app.models.hpf_site import HpfSite
from app.models.slide import Slide
from app.routers.tiles import generate_tile_on_the_fly, stream_slide_tile
from pipeline.slide_io import SlideReader, dzi_max_level, read_dzi_tile, read_region_at_mpp
from pipeline.stain import StainTransform
from tests.fakes.stage2 import stain_fit
from tests.fakes.tiff import tissue_rgb, write_pyramid_tiff

MPP = 0.5
WIDTH_PX, HEIGHT_PX = 1300, 700  # 650 x 350 µm
def od_beta() -> float:
    return get_pipeline_config().specimen_profiles.profiles["resection"].stain_fit.od_beta


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def add_case(db, tmp_path, *, profile=True, mpp=MPP, specimen="resection", content: bytes | None = None):
    """A case whose raw slide is a real pyramidal TIFF in the GCS mock (or the given bytes)."""
    case = Case(created_by="tester", status="open", specimen_type=specimen)
    db.add(case)
    db.flush()
    tiff = write_pyramid_tiff(
        tmp_path / f"{uuid.uuid4().hex}.tif", tissue_rgb(WIDTH_PX, HEIGHT_PX, MPP, [(120.0, 80.0, 60.0)]), MPP, levels=3
    )
    blob = f"cases/{case.id}/slide.tif"
    upload_blob_from_bytes(settings.GCS_RAW_BUCKET, blob, tiff.read_bytes() if content is None else content, "image/tiff")
    slide = Slide(
        case_id=case.id, gcs_uri_original=f"gs://{settings.GCS_RAW_BUCKET}/{blob}", mpp_x=mpp, mpp_y=mpp,
        width_px=WIDTH_PX, height_px=HEIGHT_PX, format="tif", base_mag=20.0,
    )
    db.add(slide)
    db.flush()
    if profile:
        save_stain_profile(db, slide.id, stain_fit())
    db.commit()
    return case, slide, tiff


def transform(db, slide) -> StainTransform:
    from app.core.stain_profiles import stain_transform_for_slide

    return stain_transform_for_slide(db, slide.id, od_beta=od_beta())


def decode(response) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(response.body)).convert("RGB"))


@pytest.fixture
def local_slide_cache():
    """Slides the tile route may render from live in this cache directory; leave it as we found it."""
    cache = Path(tempfile.gettempdir()) / "og_slides_cache"
    cache.mkdir(exist_ok=True)
    added = []
    yield lambda slide, tiff: added.append(Path(shutil_copy(tiff, cache / f"{slide.id}.tif")))
    for path in added:
        path.unlink(missing_ok=True)


def shutil_copy(source, target):
    import shutil

    shutil.copyfile(source, target)
    return target


# -- tiles ------------------------------------------------------------------------------------------------------------------


def test_an_on_the_fly_tile_is_the_deepzoom_tile_of_the_slide(db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path)
    level = dzi_max_level(WIDTH_PX, HEIGHT_PX) - 1
    png, layer = generate_tile_on_the_fly(str(tiff), slide, level, 1, 0, "orig", db)
    tile = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
    with SlideReader(str(tiff), MPP, MPP, "tif") as reader:
        expected = read_dzi_tile(reader, level, 1, 0)
    assert layer == "orig" and np.array_equal(tile, expected.rgb)
    assert tile.shape[:2] == (350, 650 - 256) or tile.shape[0] <= 256  # an edge tile is smaller than 256 px


def test_a_norm_tile_is_the_raw_tile_through_the_persisted_profile(db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path)
    level = dzi_max_level(WIDTH_PX, HEIGHT_PX) - 2
    raw, _ = generate_tile_on_the_fly(str(tiff), slide, level, 0, 0, "orig", db)
    norm, layer = generate_tile_on_the_fly(str(tiff), slide, level, 0, 0, "norm", db)
    assert layer == "norm"
    raw_array = np.array(Image.open(io.BytesIO(raw)).convert("RGB"))
    assert np.array_equal(np.array(Image.open(io.BytesIO(norm)).convert("RGB")), transform(db, slide).apply(raw_array))


def test_a_slide_without_a_profile_is_served_as_the_original_layer_and_the_layer_says_so(db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path, profile=False)
    level = dzi_max_level(WIDTH_PX, HEIGHT_PX) - 2
    png, layer = generate_tile_on_the_fly(str(tiff), slide, level, 0, 0, "norm", db)
    assert layer == "orig" and png


def test_a_slide_with_an_unknown_specimen_cannot_be_normalised_either(db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path, specimen="unknown")
    level = dzi_max_level(WIDTH_PX, HEIGHT_PX) - 2
    assert generate_tile_on_the_fly(str(tiff), slide, level, 0, 0, "norm", db)[1] == "orig"


def test_the_tile_route_reports_the_layer_it_served(db, tmp_path, local_slide_cache):
    case, slide, tiff = add_case(db, tmp_path)
    local_slide_cache(slide, tiff)
    level = dzi_max_level(WIDTH_PX, HEIGHT_PX) - 2  # below the 10x cap, so a normalised tile is allowed
    response = stream_slide_tile(slide, "norm", level, "0_0.png", case_id=case.id, db=db)
    assert response.headers["X-Tile-Layer"] == "norm" and response.media_type == "image/png"
    other_case, other_slide, other_tiff = add_case(db, tmp_path, profile=False)
    local_slide_cache(other_slide, other_tiff)
    unnormalised = stream_slide_tile(other_slide, "norm", level, "0_0.png", case_id=other_case.id, db=db)
    assert unnormalised.headers["X-Tile-Layer"] == "orig"


# -- the case thumbnail ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def client():
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


def test_the_case_thumbnail_keeps_the_slides_aspect_ratio(client, db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path)
    response = client.get(f"/api/v1/cases/{case.id}/thumbnail")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    width, height = Image.open(io.BytesIO(response.content)).size
    assert width == 256 and height == round(256 * HEIGHT_PX / WIDTH_PX)


def test_a_slide_without_mpp_has_no_thumbnail_yet(client, db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path)
    slide.mpp_x = slide.mpp_y = None
    db.commit()
    response = client.get(f"/api/v1/cases/{case.id}/thumbnail")
    assert response.status_code == 409 and "needs_mpp" in response.json()["detail"]


def test_an_unreadable_slide_has_no_thumbnail_not_a_grey_one(client, db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path, content=b"this is not a slide")
    response = client.get(f"/api/v1/cases/{case.id}/thumbnail")
    assert response.status_code == 404 and "could not be read" in response.json()["detail"]


# -- hotspot review patches -------------------------------------------------------------------------------------------------------


def hotspot_patch(db, case, mag="10x", stain="orig"):
    return triage_router.get_hotspot_thumbnail(
        case_id=str(case.id), hotspot_id="hs_01", mag=mag, stain=stain, cx=325.0, cy=175.0, db=db
    )


def test_a_hotspot_patch_is_the_field_at_the_magnifications_resolution(db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path)
    for mag, field_um in (("10x", 512.0), ("20x", 256.0), ("40x", 128.0)):
        patch = decode(hotspot_patch(db, case, mag))
        assert patch.shape == (512, 512, 3)
    with SlideReader(str(tiff), MPP, MPP, "tif") as reader:
        # 128 µm field centred on (325, 175) µm, 0.25 µm/px
        expected = read_region_at_mpp(reader, 325.0 - 64.0, 175.0 - 64.0, 128.0, 128.0, 0.25).rgb
    assert np.array_equal(decode(hotspot_patch(db, case, "40x")), expected)


def test_a_normalised_hotspot_patch_is_the_original_through_the_profile(db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path)
    orig, norm = decode(hotspot_patch(db, case, "20x", "orig")), decode(hotspot_patch(db, case, "20x", "norm"))
    assert np.array_equal(norm, transform(db, slide).apply(orig)) and not np.array_equal(norm, orig)


def test_a_normalised_hotspot_patch_of_a_slide_without_a_profile_is_a_409_not_the_original(db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path, profile=False)
    with pytest.raises(HTTPException) as raised:
        hotspot_patch(db, case, "10x", "norm")
    assert raised.value.status_code == 409 and "preprocess" in raised.value.detail
    assert decode(hotspot_patch(db, case, "10x", "orig")).shape == (512, 512, 3)  # the original is still available


# -- HPF review images and candidate crops -------------------------------------------------------------------------------------------


def add_hpf_and_detection(db, case):
    db.add(HpfSite(case_id=case.id, seq=1, center_um=[325.0, 175.0], radius_um=262.0, mitotic_count=0))
    db.add(Detection(id="m_0001", case_id=case.id, centroid_um=[325.0, 175.0], p_a=0.9, final_decision="mitosis", decision_path="A"))
    db.commit()


def test_an_hpf_review_image_has_the_configured_size_and_format(db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path)
    add_hpf_and_detection(db, case)
    cfg = get_pipeline_config().mitosis.hpf
    forty = mitosis_router.get_hpf_thumbnail(case_id=str(case.id), seq=1, mag="40x", stain="orig", db=db)
    ten = mitosis_router.get_hpf_thumbnail(case_id=str(case.id), seq=1, mag="10x", stain="norm", db=db)
    assert forty.media_type == "image/jpeg" and Image.open(io.BytesIO(forty.body)).size == (cfg.review_px, cfg.review_px)
    assert ten.media_type == "image/png" and Image.open(io.BytesIO(ten.body)).size == (cfg.review_px // 4, cfg.review_px // 4)


def test_a_normalised_hpf_image_of_a_slide_without_a_profile_is_a_409(db, tmp_path):
    case, slide, tiff = add_case(db, tmp_path, profile=False)
    add_hpf_and_detection(db, case)
    with pytest.raises(HTTPException) as raised:
        mitosis_router.get_hpf_thumbnail(case_id=str(case.id), seq=1, mag="10x", stain="norm", db=db)
    assert raised.value.status_code == 409


def add_mitosis_stage(db, case):
    from app.models.stage_execution import StageExecution

    from tests.test_mitosis_gate import save_tumor_mask

    db.add(StageExecution(case_id=case.id, stage="mitosis", attempt=1, status="awaiting_review"))
    db.commit()
    save_tumor_mask(case.id, np.ones((2, 3), dtype=bool))  # the 650 x 350 µm slide on 224 µm tiles, all tumour


def crop_blob(case, candidate_id: str, kind: str) -> np.ndarray:
    from app.core.gcs import download_blob_as_bytes

    data = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case.id}/mitosis/crops/{candidate_id}_{kind}.png")
    return np.array(Image.open(io.BytesIO(data)).convert("RGB"))


def expected_view(tiff, cx_um, cy_um, size_um, mpp):
    with SlideReader(str(tiff), MPP, MPP, "tif") as reader:
        return read_region_at_mpp(reader, cx_um - size_um / 2, cy_um - size_um / 2, size_um, size_um, mpp).rgb


@pytest.mark.parametrize("profile", [True, False])
def test_a_pathologist_added_mitosis_gets_the_contract_crop_and_context_in_raw_colour(client, db, tmp_path, profile):
    """The added figure's crop_url / context_url images are read through read_region_at_mpp as scanned, whether or
    not the slide has a stain profile (contract mitosis_v6; the referee's normalised crop is not used)."""
    case, slide, tiff = add_case(db, tmp_path, profile=profile)
    add_mitosis_stage(db, case)
    crops = get_pipeline_config().mitosis.review_crops

    response = client.post("/api/v1/stages/mitosis/add", json={"case_id": str(case.id), "centroid_um": [325.0, 175.0]})
    assert response.status_code == 200, response.text
    added = [c for c in response.json()["candidates"] if c["decision_path"] == "human"]
    assert len(added) == 1 and added[0]["review_label"] == "mitosis" and added[0]["counted"] is True
    assert added[0]["in_tumor"] is True  # the tumour gate runs on an added figure too
    candidate_id = added[0]["id"]
    assert np.array_equal(crop_blob(case, candidate_id, "crop"), expected_view(tiff, 325.0, 175.0, crops.crop_um, crops.crop_mpp))
    assert np.array_equal(crop_blob(case, candidate_id, "context"), expected_view(tiff, 325.0, 175.0, crops.context_um, crops.context_mpp))
    served = client.get(added[0]["crop_url"])
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
