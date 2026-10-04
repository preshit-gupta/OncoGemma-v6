"""
KongNet (MIDOG) detector wire formats, detection persistence and HPF placement.

The v5 YoloMitosisDetector, its optical-density fallback and the lenient MedGemma/Gemini
mitosis schema were deleted in WP-2.3c (SPEC-01 §3.9). The detector and referee now run
through the model gateway; tests/test_mitosis_worker.py covers the stage end to end.
"""
import base64
import io
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.core.pipeline_config import get_pipeline_config
from app.core.tasks import EntityType, Task
from app.inference.adapters.base import AdapterImage, AdapterRequest, CallRejected
from app.inference.adapters.vertex_endpoint import VertexEndpointAdapter
from app.inference.errors import ModelCallError
from app.inference.gateway import EntityRef, ModelInputs
from app.inference.outputs import DetectionList
from tests.fakes.gateway import decision_context, make_gateway, png_image


def patch_png(value=200) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (512, 512), (value, value, value)).save(buffer, format="PNG")
    return buffer.getvalue()


def detector_entry(**changes):
    entry = get_pipeline_config().models.models["kongnet_det_midog_1"]
    return entry.model_copy(update={"endpoint_id": "456", **changes})


def v1_entry():
    """The legacy v5 contract, kept for rollback: predict, boxes, no weights hash."""
    return detector_entry(provider="vertex_endpoint_predict", wire_format="kongnet_midog_v1", weights_sha256=None)


def registry_weights():
    return get_pipeline_config().models.models["kongnet_det_midog_1"].weights_sha256


class RecordingEndpoint:
    """predict (v1) and raw_predict (v2) over canned predictions; v2 answers name ``weights``."""

    def __init__(self, predictions, weights="registry"):
        self.predictions = predictions
        self.weights = weights
        self.calls = []

    def predict(self, instances, parameters=None, timeout=None):
        self.calls.append({"instances": instances, "parameters": parameters, "timeout": timeout})
        return SimpleNamespace(predictions=self.predictions)

    def raw_predict(self, body, headers=None, timeout=None):
        import json

        sent = json.loads(body)
        self.calls.append({"instances": sent["instances"], "parameters": sent.get("parameters"), "timeout": timeout})
        payload = {"predictions": self.predictions}
        if self.weights is not None:
            payload["model_sha256"] = registry_weights() if self.weights == "registry" else self.weights
        return SimpleNamespace(status_code=200, json=lambda: payload, text=str(payload))


def adapter_for(endpoint):
    return VertexEndpointAdapter(project="p", endpoint_factory=lambda *args: endpoint)


def request(n=4, min_prob=0.35):
    images = tuple(AdapterImage(patch_png(), "image/png", 0.25) for _ in range(n))
    return AdapterRequest("kongnet_det_midog_1", images=images, parameters={"min_prob": min_prob})


def test_v1_request_is_what_the_legacy_service_reads():
    endpoint = RecordingEndpoint([{"boxes": []}] * 4)
    adapter_for(endpoint).call(v1_entry(), request(), 60.0)
    (call,) = endpoint.calls
    assert len(call["instances"]) == 4 and call["parameters"] is None and call["timeout"] == 60.0
    first = call["instances"][0]
    assert set(first) == {"image_bytes", "confidence_threshold"}
    assert first["confidence_threshold"] == 0.35
    # Lossless PNG, as the registry's input contract requires (v5 sent JPEG q90).
    assert base64.b64decode(first["image_bytes"]) == patch_png()


def test_v1_boxes_become_points_per_patch_in_request_order():
    endpoint = RecordingEndpoint([
        {"boxes": [{"cx": 100.0, "cy": 120.0, "width": 48.0, "height": 48.0, "confidence": 0.88}]},
        {"boxes": []},
        {"boxes": []},
        {"boxes": [{"cx": 50.0, "cy": 60.0, "width": 48.0, "height": 48.0, "confidence": 0.92}]},
    ])
    raw = adapter_for(endpoint).call(v1_entry(), request(), 60.0)
    assert raw.data == {"detections": [
        [{"x": 100.0, "y": 120.0, "prob": 0.88}], [], [], [{"x": 50.0, "y": 60.0, "prob": 0.92}],
    ]}


@pytest.mark.parametrize(
    "predictions, message",
    [
        # The error v5 turned into a silent switch to the OD heuristic (SPEC-01 §1.1).
        ([{"boxes": [], "error": "Expected dimensions (512, 512), but got (1024, 1024)."}] * 4, "Expected dimensions"),
        ([{"boxes": []}] * 3, "expected 4 predictions, got 3"),
        ([{"boxes": [{"x1": 400.0, "y1": 500.0, "x2": 450.0, "y2": 550.0, "conf": 0.72}]}] + [{"boxes": []}] * 3,
         "malformed box"),
        ([{"boxes": [{"cx": 900.0, "cy": 10.0, "confidence": 0.9}]}] + [{"boxes": []}] * 3, "outside the 512x512"),
        ([{"points": []}] * 4, "no boxes list"),
    ],
)
def test_v1_malformed_answers_are_rejected_not_substituted(predictions, message):
    with pytest.raises(CallRejected, match=message):
        adapter_for(RecordingEndpoint(predictions)).call(v1_entry(), request(), 60.0)


def test_the_registry_serves_kongnet_through_v2_raw_predict_with_pinned_weights():
    entry = detector_entry()
    assert (entry.provider, entry.wire_format) == ("vertex_endpoint_raw_predict", "kongnet_midog_v2")
    assert entry.weights_sha256 is not None and entry.input.mpp == 0.25


def test_v2_request_and_points():
    endpoint = RecordingEndpoint([{"points": [{"x": 10.5, "y": 20.0, "prob": 0.41}], "error": None}, {"points": [], "error": None}])
    raw = adapter_for(endpoint).call(detector_entry(), request(n=2, min_prob=0.01), 60.0)
    (call,) = endpoint.calls
    assert call["parameters"] == {"min_prob": 0.01}
    assert call["instances"][0]["mpp"] == 0.25 and set(call["instances"][0]) == {"image_png_b64", "mpp"}
    assert raw.data == {"detections": [[{"x": 10.5, "y": 20.0, "prob": 0.41}], []]}


def test_v2_service_errors_are_rejected():
    endpoint = RecordingEndpoint([{"points": [], "error": "mpp_mismatch: expected 0.25, got 0.5"}])
    with pytest.raises(CallRejected, match="mpp_mismatch"):
        adapter_for(endpoint).call(detector_entry(), request(n=1), 60.0)


@pytest.mark.parametrize("weights", ["0" * 64, None])
def test_an_answer_from_other_or_unnamed_weights_is_rejected(weights):
    endpoint = RecordingEndpoint([{"points": [], "error": None}], weights=weights)
    with pytest.raises(CallRejected, match="the registry pins"):
        adapter_for(endpoint).call(detector_entry(), request(n=1), 60.0)


def test_empty_detections_are_trusted_and_recorded_through_the_gateway():
    """A negative tile stays negative: no OD sweep is appended (v5 rescued it with the heuristic)."""
    config = get_pipeline_config()
    registry = config.models
    kongnet = registry.models["kongnet_det_midog_1"].model_copy(update={"endpoint_id": "456"})
    config = config.model_copy(update={"models": registry.model_copy(update={"models": {**registry.models, "kongnet_det_midog_1": kongnet}})})
    endpoint = RecordingEndpoint([{"points": [], "error": None}] * 2)
    gateway = make_gateway(config, {"vertex_endpoint_raw_predict": adapter_for(endpoint)})
    dark = png_image((512, 512), 0.25, rgb=(30, 180, 200))
    result = gateway.invoke(
        Task.MITOSIS_DETECT, "kongnet_det_midog_1", ModelInputs(images=(dark, dark)), decision_context(),
        EntityRef(EntityType.TILE_BATCH, "t0000", ids=("p0", "p1")), DetectionList, params={"min_prob": 0.35},
    )
    assert result.output.detections == [[], []]
    assert gateway.log.pending()[0]["output"] == {"points": [[], []]}


def test_error_payload_fails_the_call_through_the_gateway():
    config = get_pipeline_config()
    registry = config.models
    kongnet = registry.models["kongnet_det_midog_1"].model_copy(update={"endpoint_id": "456"})
    config = config.model_copy(update={"models": registry.model_copy(update={"models": {**registry.models, "kongnet_det_midog_1": kongnet}})})
    gateway = make_gateway(config, {"vertex_endpoint_raw_predict": adapter_for(RecordingEndpoint([{"points": [], "error": "boom"}]))})
    with pytest.raises(ModelCallError, match="boom"):
        gateway.invoke(
            Task.MITOSIS_DETECT, "kongnet_det_midog_1", ModelInputs(images=(png_image((512, 512), 0.25),)),
            decision_context(), EntityRef(EntityType.TILE_BATCH, "t0000", ids=("p0",)), DetectionList,
            params={"min_prob": 0.35},
        )


def test_van_diest_morphometric_verification_rejection():
    """The ablation-only v5 verifier heuristic rejects small round pyknotic / apoptotic fragments."""
    import cv2
    from pipeline.heuristics.morph_verifier import morphometric_mitosis_probability

    crop = np.ones((128, 128, 3), dtype=np.uint8) * 230
    cv2.circle(crop, (64, 64), 8, (40, 20, 60), -1)
    score, contour = morphometric_mitosis_probability(crop)
    assert score < 0.35


def test_greedy_place_hpfs_strictly_guarantees_10_hpfs_with_hotspots():
    """Verify that when hotspots only fit 9 HPFs, Pass 3 places the 10th HPF in tumor bed tissue to achieve >=2.0 mm²."""
    from pipeline.hpf import greedy_place_hpfs

    # 20000 x 20000 um slide
    ny, nx = 40, 40
    stride = 500.0
    density_map = np.zeros((ny, nx), dtype=np.float32)
    grid_meta = {
        "origin_um": [0.0, 0.0],
        "stride_um": stride,
        "nx": nx,
        "ny": ny
    }

    # Hotspot polygon that can only fit 9 HPFs
    hotspot_polygon = [
        [1000.0, 1000.0],
        [4500.0, 1000.0],
        [4500.0, 4500.0],
        [1000.0, 4500.0]
    ]

    placed = greedy_place_hpfs(
        density_map=density_map,
        grid_meta=grid_meta,
        hotspot_polygons_um=[hotspot_polygon],
        count=10,
        radius_um=262.0,
        slide_dimensions_um=(20000.0, 20000.0)
    )

    # Strictly 10 HPFs placed (>2.0 mm² area)
    assert len(placed) == 10
    import math
    total_area_mm2 = 10 * (math.pi * (0.262 ** 2))
    assert total_area_mm2 > 2.0
