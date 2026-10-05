"""
Unit and integration tests for Batch 15:
WSI Pyramid Tiles, HPF Placement Engine, Probe Classifier & Storage Security.
Covers findings:
- #739, #640, #340, #641, #53, #54, #554, #730, #747, #720, #19, #5, #21, #22, #88, #86, #569, #101, #133
- #586, #596, #23, #219, #212, #211, #646, #645
"""
import io
import math
import uuid
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
from PIL import Image
from fastapi import HTTPException

from pipeline.tiles import extract_patch_from_pyramid
from pipeline.tumor_head import l2_normalize
from app.core.config import settings
from app.core.gcs import (
    generate_signed_upload_url,
    get_gcs_artifact_direct_url,
    ALLOWED_WSI_EXTS
)
from app.models.slide import Slide
from app.routers.tiles import stream_slide_tile, generate_tile_on_the_fly


# 1. HPF placement (#747, #720, #586) is one field per hotspot window since WP-7.6b; see test_hpf.py
# and test_mitosis_gate.py (no hotspots refused, disk inside the window and the slide, never overlapping).


# ---------------------------------------------------------------------------
# 2. WSI Color & Region Reading Tests (#53, #554, #54, #730)
# ---------------------------------------------------------------------------

def test_extract_patch_from_pyramid_coverage_guard():
    """Issue #730: Return None if pyramid tiles are missing or coverage < 75%."""
    with patch("app.core.gcs.download_blob_as_bytes", side_effect=Exception("Tile not found")):
        res = extract_patch_from_pyramid(
            slide_id="slide-test",
            cx_um=1000.0,
            cy_um=1000.0,
            field_um=200.0
        )
        assert res is None


# ---------------------------------------------------------------------------
# 3. Tile Router Security, Caching & Validation Tests (#739, #640, #340, #641, #211, #212)
# ---------------------------------------------------------------------------

def test_stream_slide_tile_validation():
    """Issue #211, #739, #641, #340: Validations and private cache headers."""
    # Uninitialized slide dimensions -> HTTP 404 (#739)
    slide_invalid = Slide(id=uuid.uuid4(), case_id=uuid.uuid4(), width_px=None, height_px=0)
    with pytest.raises(HTTPException) as exc_dim:
        stream_slide_tile(slide_invalid, layer="orig", z=10, filename="0_0.png")
    assert exc_dim.value.status_code == 404

    # Invalid layer -> HTTP 400 (#211)
    slide_valid = Slide(id=uuid.uuid4(), case_id=uuid.uuid4(), width_px=4096, height_px=4096, base_mag=40)
    with pytest.raises(HTTPException) as exc_layer:
        stream_slide_tile(slide_valid, layer="invalid_layer", z=10, filename="0_0.png")
    assert exc_layer.value.status_code == 400

    # Zoom out of bounds -> HTTP 404 (#641)
    with pytest.raises(HTTPException) as exc_zoom:
        stream_slide_tile(slide_valid, layer="orig", z=25, filename="0_0.png")
    assert exc_zoom.value.status_code == 404

    # Tile coordinate out of bounds -> HTTP 404 (#641)
    with pytest.raises(HTTPException) as exc_coord:
        stream_slide_tile(slide_valid, layer="orig", z=10, filename="999_999.png")
    assert exc_coord.value.status_code == 404


def test_tile_router_private_cache_header():
    """Issue #340: Ensure Cache-Control header is private, max-age=86400."""
    slide_valid = Slide(id=uuid.uuid4(), case_id=uuid.uuid4(), width_px=4096, height_px=4096, base_mag=40)

    # Mock GCS blob download to return a valid 1x1 PNG
    png_buf = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 255, 255)).save(png_buf, format="PNG")
    mock_png_bytes = png_buf.getvalue()

    with patch("app.routers.tiles.get_gcs_client") as mock_client:
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_blob.download_as_bytes.return_value = mock_png_bytes
        mock_bucket.blob.return_value = mock_blob
        mock_client.return_value.bucket.return_value = mock_bucket

        resp = stream_slide_tile(slide_valid, layer="orig", z=10, filename="0_0.png")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "private, max-age=86400"
        assert resp.headers["X-Tile-Layer"] == "orig"


# ---------------------------------------------------------------------------
# 4. GCS Signed URLs, Artifact URLs & Extension Allowlist (#19, #5, #22)
# ---------------------------------------------------------------------------

def test_generate_signed_upload_url_extension_allowlist():
    """Issue #19: Raw slide bucket rejects unsupported extensions with HTTP 400."""
    # Disallowed extension
    with pytest.raises(HTTPException) as exc:
        generate_signed_upload_url(settings.GCS_RAW_BUCKET, "cases/123/malicious.exe")
    assert exc.value.status_code == 400
    assert "Unsupported WSI file extension" in exc.value.detail

    # Allowed extension
    url = generate_signed_upload_url(settings.GCS_RAW_BUCKET, "cases/123/slide.svs")
    assert url is not None


def test_get_gcs_artifact_direct_url_regex_hotspot():
    """Issue #22: Hotspot IDs with suffixes like _10x_norm.png are not mangled."""
    url1 = get_gcs_artifact_direct_url("cases/case-123/triage/patches/hs_01_10x_norm.png")
    assert "hotspots/hs_01/thumbnail" in url1

    url2 = get_gcs_artifact_direct_url("cases/case-123/triage/patches/hs_02_40x_orig.png")
    assert "hotspots/hs_02/thumbnail" in url2

    url3 = get_gcs_artifact_direct_url("cases/case-123/triage/patches/hs_03_thumb.png")
    assert "hotspots/hs_03/thumbnail" in url3


# ---------------------------------------------------------------------------
# 5. Probe Classifier & Triage Stage Invariants (#88, #569)
# ---------------------------------------------------------------------------

def test_probe_runner_dimension_assertion():
    """Issue #88: the tumour classifier refuses embeddings of the wrong shape."""
    from app.core.pipeline_config import get_pipeline_config
    from app.inference.adapters.base import AdapterRequest, CallRejected
    from app.inference.adapters.local_sklearn import LocalSklearnAdapter

    # Wrong number of dimensions (1D instead of 2D)
    with pytest.raises(ValueError) as exc1:
        l2_normalize(np.zeros((384,), dtype=np.float32))
    assert "Embeddings must be a 2D array" in str(exc1.value)

    entry = get_pipeline_config().models.models["tumor_head"]
    adapter = LocalSklearnAdapter()

    # Wrong feature dimension (512 instead of 384)
    with pytest.raises(CallRejected) as exc2:
        adapter.call(entry, AdapterRequest("tumor_head", features=np.zeros((10, 512), dtype=np.float32)), 5.0)
    assert "expects 384 features, got 512" in str(exc2.value)

    # Correct dimension (384)
    raw = adapter.call(entry, AdapterRequest("tumor_head", features=l2_normalize(np.ones((5, 384), dtype=np.float32))), 5.0)
    probas = raw.data["probabilities"]
    assert len(probas) == 5
    assert all(0.0 <= p <= 1.0 for row in probas for p in row)


def test_confirm_triage_mutual_exclusion_invariant():
    """Issue #569: Reject confirmation if no_invasive_tumor=True but active hotspots exist."""
    from app.routers.triage import confirm_triage, TriageConfirmPayload

    db_mock = MagicMock()
    stage_exec = MagicMock()
    stage_exec.status = "awaiting_review"
    stage_exec.output_ref = ""
    stage_exec.review_edits = []
    db_mock.scalars.return_value.first.return_value = stage_exec

    # Machine output has 2 active tumor hotspots
    active_hotspots_json = b'{"hotspots": [{"id": "hs_01", "center_um": [300, 300]}, {"id": "hs_02", "center_um": [1300, 300]}], "hpf_diameter_um": 500.0, "frame_um": 600.0, "hpf_target": 10}'
    
    with patch("app.services.stages.download_blob_as_bytes", return_value=active_hotspots_json), \
         patch("app.routers.triage.slide_bounds_um", return_value=None):
        # Attempt to confirm no_invasive_tumor=True with 2 active hotspots -> Must raise HTTP 409 Conflict (#569)
        payload = TriageConfirmPayload(
            case_id="case-123",
            reviewed_by="dr_smith",
            no_invasive_tumor=True
        )
        with pytest.raises(HTTPException) as exc:
            confirm_triage(payload, db=db_mock, user=MagicMock(id="dr_smith"))
        assert exc.value.status_code == 409
        assert "active tumor hotspot" in exc.value.detail
