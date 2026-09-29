"""Model gateway (SPEC-01 §3.4, §5): contract checks, retries, strict outputs, cache, records."""
import io
import json
import uuid

import pyarrow.parquet as pq
import pytest

from app.core.model_registry import RequestLimits
from app.core.pipeline_config import get_pipeline_config
from app.core.run_context import RunMode
from app.core.tasks import EntityType, Task
from app.inference import schemas
from app.inference.adapters.base import CallRejected, CallTimeout, RawResponse, TransientCallError
from app.inference.errors import (
    InputContractError,
    ModelCallError,
    ModelTimeoutError,
    ModelUnavailableError,
    SchemaInvalidError,
    UnpinnedModelError,
)
from app.inference.gateway import (
    CacheIntegrityError,
    EntityRef,
    FallbackResult,
    ModelInputs,
    render_prompt,
)
from app.inference.outputs import ClassProbabilities, EmbeddingBatch
from app.inference.records import DecisionLog
from tests.fakes.gateway import (
    FakeAdapter,
    InMemoryBlobStore,
    decision_context,
    json_text,
    make_gateway,
    png_image,
    statuses,
)

TUBULE = {"tumor_present": True, "tubule_percent": 40, "rationale": "Glands in a third of the tumour."}
VERDICT = {
    "verdict": "MITOTIC_FIGURE",
    "criteria": {
        "membrane_absent": True,
        "condensed_chromosome_projections": True,
        "phase": "metaphase",
        "neoplastic_cell": True,
    },
    "mimic": "none",
    "rationale": "Metaphase plate.",
}
PATCH = EntityRef(EntityType.PATCH, "p_001")
CANDIDATE = EntityRef(EntityType.CANDIDATE, "m_0001")


class DetectionList(schemas.StrictModel):
    """Stand-in with the registry's output_schema name for the detector."""

    detections: list[list[float]]


def with_entry(config, key, **changes):
    registry = config.models
    models = {**registry.models, key: registry.models[key].model_copy(update=changes)}
    return config.model_copy(update={"models": registry.model_copy(update={"models": models})})


def with_fallback(config, task: str, *errors: str):
    policy = config.fallbacks.model_validate({"fallbacks": [{"task": task, "on": list(errors), "to": None}]})
    return config.model_copy(update={"fallbacks": policy})


def pinned(config):
    return with_entry(config, "gemini_referee", model="gemini-2.5-flash-001")


def tubule_inputs(**overrides):
    values = {"images": (png_image((512, 512), 1.0),), "prompt_id": "tubule@v1.md"}
    return ModelInputs(**{**values, **overrides})


def detector_config():
    return with_entry(get_pipeline_config(), "kongnet_det_midog_1", endpoint_id="1234")


def gemini(adapter, config=None, **kwargs):
    log = kwargs.pop("log", DecisionLog())
    return make_gateway(config or get_pipeline_config(), {"vertex_genai": adapter}, log=log, **kwargs), log


# --- success path and records -------------------------------------------------------------


def test_vlm_answer_is_validated_and_recorded():
    adapter = FakeAdapter(json_text(TUBULE))
    blobs = InMemoryBlobStore()
    gateway, log = gemini(adapter, blobs=blobs)
    ctx = decision_context(stage="grading")

    result = gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), ctx, PATCH, schemas.TubuleEstimate)

    assert result.output == schemas.TubuleEstimate(**TUBULE)
    assert not result.cache_hit and result.producer_id == "gemini_referee"
    (row,) = log.pending()
    config = get_pipeline_config()
    assert row["id"] == result.record_id
    assert row["status"] == "ok" and row["producer_kind"] == "model"
    assert row["task"] == "tubule_patch" and row["stage"] == "grading"
    assert (row["entity_type"], row["entity_id"]) == ("patch", "p_001")
    assert row["producer_version"] == config.models.version_of("gemini_referee")
    assert row["prompt_id"] == "tubule@v1.md" and len(row["prompt_sha256"]) == 64
    assert len(row["input_sha256"]) == 64
    assert row["input_spec"]["images"] == [
        {"mpp": 1.0, "size_px": [512, 512], "color": "raw", "format": "png", "stain_profile_id": None}
    ]
    assert row["params"] == {"generation": {"temperature": 0.0}}
    assert row["output"] == TUBULE
    assert row["run_mode"] == "clinical" and row["config_hash"] == ctx.config_hash
    assert row["case_id"] == ctx.case_id and row["stage_execution_id"] == ctx.stage_execution_id
    # The verbatim answer is always stored for VLMs.
    raw_path = row["raw_output_uri"].removeprefix(f"gs://{blobs.bucket}/")
    assert json.loads(blobs.blobs[raw_path]) == TUBULE

    (_, request, timeout_s), = adapter.calls
    assert request.prompt == config.prompts["tubule@v1.md"]
    assert request.output_model is schemas.TubuleEstimate
    assert request.generation == {"temperature": 0.0}
    assert timeout_s == config.models.call_limits("gemini_referee")[1]
    assert [image.mime_type for image in request.images] == ["image/png"]


def test_shadow_decisions_are_recorded_as_shadow():
    gateway, log = gemini(FakeAdapter(json_text(TUBULE)), pinned(get_pipeline_config()))
    gateway.invoke(
        Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(RunMode.SHADOW), PATCH,
        schemas.TubuleEstimate,
    )
    assert [row["producer_kind"] for row in log.pending()] == ["shadow"]


def test_structured_predictions_are_validated_with_the_output_model():
    adapter = FakeAdapter(RawResponse(data={"detections": [[10.0, 20.0, 0.9]]}, endpoint="projects/p/endpoints/1234"))
    log = DecisionLog()
    gateway = make_gateway(detector_config(), {"vertex_endpoint_raw_predict": adapter}, log=log)
    result = gateway.invoke(
        Task.MITOSIS_DETECT, "kongnet_det_midog_1",
        ModelInputs(images=(png_image((512, 512), 0.25),)), decision_context(), EntityRef(EntityType.TILE, "t_0001"),
        DetectionList,
    )
    assert result.output.detections == [[10.0, 20.0, 0.9]]
    (row,) = log.pending()
    assert row["endpoint"] == "projects/p/endpoints/1234" and row["raw_output_uri"] is None


def test_prompt_variables_are_rendered_and_hashed():
    assert render_prompt("Count {{n}} fields at {{mpp}}.", {"n": 10, "mpp": 0.25}) == "Count 10 fields at 0.25."
    for variables in ({}, {"n": 1, "extra": 2}, {"n": [1]}):
        with pytest.raises(ValueError):
            render_prompt("Count {{n}}.", variables)


# --- input contract (SPEC-01 AC5) ------------------------------------------------------


def detector_call(image, **input_overrides):
    adapter = FakeAdapter(RawResponse(data={"detections": []}))
    log = DecisionLog()
    gateway = make_gateway(detector_config(), {"vertex_endpoint_raw_predict": adapter}, log=log)
    inputs = ModelInputs(images=(image,), **input_overrides)
    return gateway, adapter, log, lambda: gateway.invoke(
        Task.MITOSIS_DETECT, "kongnet_det_midog_1", inputs, decision_context(), EntityRef(EntityType.TILE, "t_1"),
        DetectionList,
    )


@pytest.mark.parametrize(
    "image, message",
    [
        (png_image((512, 512), 0.5), "mpp 0.5 is outside 0.25"),          # AC5: a 20x tile never reaches KongNet
        (png_image((256, 256), 0.25), "size_px"),
        (png_image((512, 512), 0.25, color="normalized"), "color normalized"),
    ],
)
def test_input_contract_violations_never_reach_the_model(image, message):
    _, adapter, log, call = detector_call(image)
    with pytest.raises(InputContractError, match=message) as raised:
        call()
    assert adapter.calls == []
    assert statuses(log) == ["error"]
    assert log.pending()[0]["error_class"] == "InputContractError"
    assert raised.value.record_id == str(log.pending()[0]["id"])


def test_mpp_within_tolerance_is_accepted():
    _, adapter, _, call = detector_call(png_image((512, 512), 0.26))
    call()
    assert len(adapter.calls) == 1


def test_declared_spec_must_describe_the_bytes():
    real = png_image((256, 256), 0.25)
    lying = type(real)(real.data, png_image((512, 512), 0.25).spec)
    _, adapter, _, call = detector_call(lying)
    with pytest.raises(InputContractError, match=r"declared size_px \[512, 512\] but the image is \[256, 256\]"):
        call()
    assert adapter.calls == []


def test_undecodable_image_is_refused():
    spec = png_image((512, 512), 0.25).spec
    _, _, _, call = detector_call(type(png_image((1, 1), 1.0))(b"not an image", spec))
    with pytest.raises(InputContractError, match="not a decodable image"):
        call()


def test_vision_models_require_an_image():
    gateway, _ = gemini(FakeAdapter())
    with pytest.raises(InputContractError, match="requires an image"):
        gateway.invoke(
            Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(images=()), decision_context(), PATCH,
            schemas.TubuleEstimate,
        )


def test_vlms_require_a_prompt_and_other_models_refuse_one():
    gateway, _ = gemini(FakeAdapter())
    with pytest.raises(InputContractError, match="prompt is required"):
        gateway.invoke(
            Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(prompt_id=None), decision_context(), PATCH,
            schemas.TubuleEstimate,
        )
    _, _, _, call = detector_call(png_image((512, 512), 0.25), prompt_id="tubule@v1.md")
    with pytest.raises(InputContractError, match="prompt is required"):
        call()


def test_unknown_prompt_and_bad_variables_are_contract_errors():
    gateway, log = gemini(FakeAdapter())
    with pytest.raises(InputContractError, match="not in configs/prompts"):
        gateway.invoke(
            Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(prompt_id="tubule@v9.md"), decision_context(),
            PATCH, schemas.TubuleEstimate,
        )
    with pytest.raises(InputContractError, match="unused"):
        gateway.invoke(
            Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(prompt_vars={"n": 1}), decision_context(), PATCH,
            schemas.TubuleEstimate,
        )
    assert statuses(log) == ["error", "error"]


def test_output_model_must_match_the_registry():
    gateway = make_gateway(detector_config(), {"vertex_endpoint_raw_predict": FakeAdapter()})
    with pytest.raises(InputContractError, match="output_schema is DetectionList"):
        gateway.invoke(
            Task.MITOSIS_DETECT, "kongnet_det_midog_1", ModelInputs(images=(png_image((512, 512), 0.25),)),
            decision_context(), EntityRef(EntityType.TILE, "t_1"), ClassProbabilities,
        )


def test_unknown_producer_is_refused():
    gateway, log = gemini(FakeAdapter())
    with pytest.raises(InputContractError, match="not a model in configs/models.yaml"):
        gateway.invoke(Task.TUBULE_PATCH, "gpt_vision", tubule_inputs(), decision_context(), PATCH, schemas.TubuleEstimate)
    assert log.pending() == []


def test_batch_limits_are_enforced():
    limits = RequestLimits(max_batch=2, max_request_bytes=10_000, qps=1.0, deadline_s=5.0, max_attempts=2)
    config = with_entry(get_pipeline_config(), "path_foundation", limits=limits)
    gateway = make_gateway(config, {"vertex_endpoint_raw_predict": FakeAdapter()})
    tiles = tuple(png_image((224, 224), 1.0) for _ in range(3))
    batch = EntityRef(EntityType.TILE_BATCH, "tb_0001", ids=("t_1", "t_2", "t_3"))
    with pytest.raises(InputContractError, match="3 images exceed max_batch 2"):
        gateway.invoke(Task.PF_EMBED, "path_foundation", ModelInputs(images=tiles), decision_context(), batch, EmbeddingBatch)


def test_feature_models_take_features_from_their_declared_producer():
    import numpy as np

    adapter = FakeAdapter(RawResponse(data={"classes": [0, 1], "probabilities": [[0.9, 0.1], [0.1, 0.9]]}))
    gateway = make_gateway(get_pipeline_config(), {"local_sklearn": adapter})
    batch = EntityRef(EntityType.TILE_BATCH, "tb_1", ids=("t_1", "t_2"))
    features = np.zeros((2, 384), dtype=np.float32)
    for inputs, message in [
        (ModelInputs(features=features, features_producer="medgemma"), "expects features from path_foundation"),
        (ModelInputs(images=(png_image((224, 224), 1.0),)), "expects features from path_foundation"),
        (ModelInputs(features=np.zeros((0, 384)), features_producer="path_foundation"), "non-empty"),
    ]:
        with pytest.raises(InputContractError, match=message):
            gateway.invoke(Task.TUMOR_HEAD, "triage_probe", inputs, decision_context(), batch, ClassProbabilities)
    result = gateway.invoke(
        Task.TUMOR_HEAD, "triage_probe", ModelInputs(features=features, features_producer="path_foundation"),
        decision_context(), batch, ClassProbabilities,
    )
    assert result.output.column(1).tolist() == pytest.approx([0.1, 0.9])
    assert adapter.calls[0][1].features is features


def test_unconfigured_endpoint_is_unavailable_without_a_call():
    config = get_pipeline_config()
    assert config.models.models["kongnet_det_midog_1"].endpoint_id is None
    adapter = FakeAdapter()
    log = DecisionLog()
    gateway = make_gateway(config, {"vertex_endpoint_raw_predict": adapter}, log=log)
    with pytest.raises(ModelUnavailableError, match="endpoint_id is not configured"):
        gateway.invoke(
            Task.MITOSIS_DETECT, "kongnet_det_midog_1", ModelInputs(images=(png_image((512, 512), 0.25),)),
            decision_context(), EntityRef(EntityType.TILE, "t_1"), DetectionList,
        )
    assert adapter.calls == [] and statuses(log) == ["unavailable"]


def test_missing_adapter_is_unavailable():
    gateway = make_gateway(get_pipeline_config(), {})
    with pytest.raises(ModelUnavailableError, match="no adapter is installed for provider vertex_genai"):
        gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(), PATCH, schemas.TubuleEstimate)


# --- EVAL pins model versions (SPEC-01 §3.7) ------------------------------------------------


@pytest.mark.parametrize("model", ["gemini-flash-latest", "gemini-2.5-flash-latest", "gemini-pro", "gemini-3-flash-preview"])
def test_eval_refuses_a_floating_gemini_alias(model):
    config = with_entry(get_pipeline_config(), "gemini_referee", model=model)
    adapter = FakeAdapter()
    gateway, log = gemini(adapter, config)
    with pytest.raises(UnpinnedModelError, match="floating alias"):
        gateway.invoke(
            Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(RunMode.EVAL), PATCH,
            schemas.TubuleEstimate,
        )
    assert adapter.calls == [] and statuses(log) == ["error"]


def test_the_configured_gemini_is_a_pinned_release():
    """gemini-2.5-flash is a fixed stable release with no numbered versions (Vertex, 2026-09-29)."""
    from app.inference.gateway import is_pinned_model_id

    assert is_pinned_model_id(get_pipeline_config().models.models["gemini_referee"].model)


@pytest.mark.parametrize(
    "model", ["gemini-2.5-flash", "gemini-2.5-flash-001", "gemini-2.5-flash-preview-05-2025", "gemini-exp-1206"]
)
def test_eval_accepts_pinned_gemini_versions(model):
    config = with_entry(get_pipeline_config(), "gemini_referee", model=model)
    gateway, _ = gemini(FakeAdapter(json_text(TUBULE)), config)
    gateway.invoke(
        Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(RunMode.EVAL), PATCH,
        schemas.TubuleEstimate,
    )


# --- retries (transport only) -------------------------------------------------------------


def test_transport_errors_are_retried_with_backoff_and_every_attempt_is_recorded():
    adapter = FakeAdapter(TransientCallError("503"), CallTimeout("deadline"), json_text(TUBULE))
    sleeps = []
    gateway, log = gemini(adapter, sleeps=sleeps)
    result = gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(), PATCH, schemas.TubuleEstimate)
    assert result.output.tubule_percent == 40
    assert statuses(log) == ["unavailable", "timeout", "ok"]
    # Full jitter: the fake jitter returns the upper bound, base * 2**(n-1).
    policy = get_pipeline_config().models.call_policy
    assert sleeps == [policy.backoff_base_s, 2 * policy.backoff_base_s]


def test_backoff_is_capped():
    policy = get_pipeline_config().models.call_policy
    config = with_entry(get_pipeline_config(), "gemini_referee", max_attempts=12)
    sleeps = []
    gateway, _ = gemini(FakeAdapter(*[TransientCallError("503")] * 11, json_text(TUBULE)), config, sleeps=sleeps)
    gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(), PATCH, schemas.TubuleEstimate)
    assert len(sleeps) == 11
    assert sleeps == sorted(sleeps) and max(sleeps) == policy.backoff_cap_s


@pytest.mark.parametrize(
    "failure, error_type, status",
    [(TransientCallError("503"), ModelUnavailableError, "unavailable"), (CallTimeout("deadline"), ModelTimeoutError, "timeout")],
)
def test_exhausted_retries_raise_and_are_recorded(failure, error_type, status):
    attempts = get_pipeline_config().models.call_limits("gemini_referee")[0]
    adapter = FakeAdapter(then=failure)
    gateway, log = gemini(adapter)
    ctx = decision_context()
    with pytest.raises(error_type) as raised:
        gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), ctx, PATCH, schemas.TubuleEstimate)
    assert len(adapter.calls) == attempts
    assert statuses(log) == [status] * attempts
    error = raised.value
    assert error.record_id == str(log.pending()[-1]["id"])
    assert error.to_error_json()["class"] == error_type.__name__
    assert error.to_error_json()["entity"] == {"type": "patch", "id": "p_001"}
    assert error.to_error_json()["producer_id"] == "gemini_referee" and error.to_error_json()["task"] == "tubule_patch"


def test_rejected_requests_are_not_retried():
    adapter = FakeAdapter(CallRejected("400 INVALID_ARGUMENT"))
    gateway, log = gemini(adapter)
    with pytest.raises(ModelCallError, match="400"):
        gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(), PATCH, schemas.TubuleEstimate)
    assert len(adapter.calls) == 1 and statuses(log) == ["error"]


# --- strict outputs (SPEC-01 AC4) -----------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "{}",
        '{"tumor_present": true}',
        '{"tumor_present": true, "tubule_percent": 140, "rationale": ""}',
        '{"tumor_present": "yes", "tubule_percent": 40, "rationale": ""}',
        '{"tumor_present": true, "tubule_percent": "40%", "rationale": ""}',
        '{"tumor_present": true, "tubule_percent": 40, "rationale": "", "confidence": "high"}',
        "Tubules form about 40% of the tumour.",
        "",
    ],
)
def test_invalid_answers_are_schema_invalid_never_a_default(answer):
    blobs = InMemoryBlobStore()
    gateway, log = gemini(FakeAdapter(RawResponse(text=answer)), blobs=blobs)
    with pytest.raises(SchemaInvalidError) as raised:
        gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(), PATCH, schemas.TubuleEstimate)
    assert isinstance(raised.value.__cause__, (schemas.SchemaInvalidError, ValueError))
    (row,) = log.pending()
    assert row["status"] == "schema_invalid" and row["output"] is None
    # The rejected answer is kept verbatim for review.
    assert blobs.blobs[row["raw_output_uri"].removeprefix(f"gs://{blobs.bucket}/")] == answer.encode()


@pytest.mark.parametrize("verdict", ["NOT CONFIRMED", "UNCONFIRMED", "CANNOT CONFIRM", "CONFIRMED", "mitotic figure"])
def test_verdict_negations_are_never_read_as_a_confirmation(verdict):
    gateway, _ = gemini(FakeAdapter(json_text({**VERDICT, "verdict": verdict})))
    with pytest.raises(SchemaInvalidError):
        gateway.invoke(
            Task.MITOSIS_REFEREE, "gemini_referee", tubule_inputs(prompt_id="mitosis_confirmation@v1.md"),
            decision_context(), CANDIDATE, schemas.MitosisVerdict,
        )


def test_schema_retries_re_ask_with_the_identical_prompt():
    config = with_entry(get_pipeline_config(), "gemini_referee", schema_retries=1)
    adapter = FakeAdapter(RawResponse(text="{}"), json_text(TUBULE))
    gateway, log = gemini(adapter, config)
    result = gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(), PATCH, schemas.TubuleEstimate)
    assert result.output.tubule_percent == 40
    assert statuses(log) == ["schema_invalid", "ok"]
    assert adapter.calls[0][1] == adapter.calls[1][1]


def test_schema_invalid_is_not_retried_by_default():
    adapter = FakeAdapter(RawResponse(text="{}"))
    gateway, _ = gemini(adapter)
    with pytest.raises(SchemaInvalidError):
        gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(), PATCH, schemas.TubuleEstimate)
    assert len(adapter.calls) == 1


# --- cache ------------------------------------------------------------------------------------


def eval_call(gateway, inputs=None, params=None):
    return gateway.invoke(
        Task.TUBULE_PATCH, "gemini_referee", inputs or tubule_inputs(), decision_context(RunMode.EVAL), PATCH,
        schemas.TubuleEstimate, params,
    )


def test_eval_cache_hit_skips_the_call_and_is_still_recorded():
    adapter = FakeAdapter(json_text(TUBULE))
    gateway, log = gemini(adapter, pinned(get_pipeline_config()))
    first = eval_call(gateway)
    second = eval_call(gateway)
    assert len(adapter.calls) == 1
    assert (first.cache_hit, second.cache_hit) == (False, True)
    assert second.output == first.output and second.record_id != first.record_id
    hit = log.pending()[-1]
    assert hit["cache_hit"] is True and hit["status"] == "ok" and hit["output"] == TUBULE
    assert hit["input_sha256"] == log.pending()[0]["input_sha256"]


def test_cache_key_covers_inputs_and_params():
    adapter = FakeAdapter(then=json_text(TUBULE))
    gateway, _ = gemini(adapter, pinned(get_pipeline_config()))
    eval_call(gateway)
    eval_call(gateway, inputs=tubule_inputs(images=(png_image((512, 512), 1.0, rgb=(10, 10, 10)),)))
    eval_call(gateway, params={"sample": 2})
    assert len(adapter.calls) == 3


def test_clinical_caches_only_deterministic_models():
    config = pinned(get_pipeline_config())
    adapter = FakeAdapter(then=json_text(TUBULE))
    gateway, _ = gemini(adapter, config)
    clinical = decision_context(RunMode.CLINICAL)
    for _ in range(2):
        gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), clinical, PATCH, schemas.TubuleEstimate)
    assert len(adapter.calls) == 1  # temperature 0

    params = config.models.models["gemini_referee"].params.model_copy(update={"temperature": 0.7})
    warm = with_entry(config, "gemini_referee", params=params)
    adapter = FakeAdapter(then=json_text(TUBULE))
    gateway, _ = gemini(adapter, warm)
    for _ in range(2):
        gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), clinical, PATCH, schemas.TubuleEstimate)
    assert len(adapter.calls) == 2
    # EVAL caches regardless of temperature.
    for _ in range(2):
        eval_call(gateway)
    assert len(adapter.calls) == 3


def test_cache_can_be_turned_off():
    adapter = FakeAdapter(then=json_text(TUBULE))
    blobs = InMemoryBlobStore()
    gateway, _ = gemini(adapter, pinned(get_pipeline_config()), blobs=blobs, cache_enabled=False)
    eval_call(gateway)
    eval_call(gateway)
    assert len(adapter.calls) == 2
    assert not any(path.startswith("cache/") for path in blobs.blobs) and blobs.reads == []


def test_corrupt_cache_entries_raise():
    blobs = InMemoryBlobStore()
    gateway, _ = gemini(FakeAdapter(json_text(TUBULE)), pinned(get_pipeline_config()), blobs=blobs)
    eval_call(gateway)
    (path,) = [p for p in blobs.blobs if p.startswith("cache/tubule_patch/")]
    blobs.blobs[path] = b'{"tumor_present": true, "tubule_percent": 999, "rationale": ""}'
    with pytest.raises(CacheIntegrityError):
        eval_call(gateway)


def test_failures_are_never_cached():
    adapter = FakeAdapter(RawResponse(text="{}"), json_text(TUBULE))
    blobs = InMemoryBlobStore()
    gateway, _ = gemini(adapter, pinned(get_pipeline_config()), blobs=blobs)
    with pytest.raises(SchemaInvalidError):
        eval_call(gateway)
    assert eval_call(gateway).cache_hit is False


# --- batch records --------------------------------------------------------------------------


def test_batch_records_list_their_entities_in_a_sidecar():
    adapter = FakeAdapter(RawResponse(data={"classes": [0, 1], "probabilities": [[0.8, 0.2], [0.2, 0.8]]}))
    blobs = InMemoryBlobStore()
    log = DecisionLog()
    gateway = make_gateway(get_pipeline_config(), {"local_sklearn": adapter}, blobs=blobs, log=log)
    import numpy as np

    batch = EntityRef(EntityType.TILE_BATCH, "tb_0001", ids=("t_0001", "t_0002"))
    gateway.invoke(
        Task.TUMOR_HEAD, "triage_probe",
        ModelInputs(features=np.ones((2, 384), dtype=np.float32), features_producer="path_foundation"),
        decision_context(stage="triage"), batch, ClassProbabilities,
    )
    (row,) = log.pending()
    assert row["entity_type"] == "tile_batch" and row["entity_id"] == "tb_0001"
    sidecar = blobs.blobs[row["entity_ids_uri"].removeprefix(f"gs://{blobs.bucket}/")]
    assert pq.read_table(io.BytesIO(sidecar)).column("entity_id").to_pylist() == ["t_0001", "t_0002"]
    assert row["input_spec"]["features"] == {"producer": "path_foundation", "shape": [2, 384], "dtype": "float32"}


def test_batch_entities_need_ids_and_single_entities_refuse_them():
    with pytest.raises(ValueError):
        EntityRef(EntityType.TILE_BATCH, "tb_1")
    with pytest.raises(ValueError):
        EntityRef(EntityType.PATCH, "p_1", ids=("p_1",))


# --- fallback policy (SPEC-01 §3.6) ---------------------------------------------------------


def test_clinical_fallback_yields_no_verdict_and_records_it():
    config = with_fallback(get_pipeline_config(), "tubule_patch", "ModelUnavailableError")
    gateway, log = gemini(FakeAdapter(then=TransientCallError("503")), config)
    ctx = decision_context(stage="grading")
    result = gateway.invoke_or_fallback(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), ctx, PATCH, schemas.TubuleEstimate)
    assert isinstance(result, FallbackResult) and result.output is None
    assert isinstance(result.error, ModelUnavailableError)
    fallback = log.pending()[-1]
    assert fallback["id"] == result.record_id
    assert fallback["producer_kind"] == "fallback" and fallback["status"] == "skipped"
    assert fallback["error_class"] == "ModelUnavailableError" and fallback["output"] is None
    assert fallback["params"]["failed_record_id"] == result.error.record_id
    assert fallback["input_sha256"] == log.pending()[0]["input_sha256"]
    assert (fallback["entity_type"], fallback["entity_id"]) == ("patch", "p_001")


@pytest.mark.parametrize("run_mode", [RunMode.EVAL, RunMode.SHADOW])
def test_fallbacks_never_apply_outside_clinical(run_mode):
    config = pinned(with_fallback(get_pipeline_config(), "tubule_patch", "ModelUnavailableError"))
    gateway, log = gemini(FakeAdapter(then=TransientCallError("503")), config)
    with pytest.raises(ModelUnavailableError):
        gateway.invoke_or_fallback(
            Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(run_mode), PATCH,
            schemas.TubuleEstimate,
        )
    assert "skipped" not in statuses(log)


def test_unlisted_failures_still_raise_in_clinical():
    config = with_fallback(get_pipeline_config(), "tubule_patch", "ModelUnavailableError")
    gateway, _ = gemini(FakeAdapter(RawResponse(text="{}")), config)
    with pytest.raises(SchemaInvalidError):
        gateway.invoke_or_fallback(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(), PATCH, schemas.TubuleEstimate)


def test_successful_calls_pass_through_invoke_or_fallback():
    gateway, _ = gemini(FakeAdapter(json_text(TUBULE)))
    result = gateway.invoke_or_fallback(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(), PATCH, schemas.TubuleEstimate)
    assert result.output.tubule_percent == 40


def test_records_are_unique_per_attempt():
    gateway, log = gemini(FakeAdapter(TransientCallError("503"), json_text(TUBULE)))
    gateway.invoke(Task.TUBULE_PATCH, "gemini_referee", tubule_inputs(), decision_context(), PATCH, schemas.TubuleEstimate)
    ids = [row["id"] for row in log.pending()]
    assert len(set(ids)) == 2 and all(isinstance(i, uuid.UUID) for i in ids)


def test_request_size_limit_counts_base64_bytes():
    import math

    image = png_image((224, 224), 1.0)
    encoded = 4 * math.ceil(len(image.data) / 3)
    limits = RequestLimits(max_batch=6, max_request_bytes=2 * encoded - 1, qps=1.0, deadline_s=5.0, max_attempts=2)
    config = with_entry(get_pipeline_config(), "path_foundation", limits=limits)
    adapter = FakeAdapter(RawResponse(data={"embeddings": [[0.0]]}))
    gateway = make_gateway(config, {"vertex_endpoint_raw_predict": adapter})
    with pytest.raises(InputContractError, match="exceed max_request_bytes"):
        gateway.invoke(
            Task.PF_EMBED, "path_foundation", ModelInputs(images=(image, image)), decision_context(),
            EntityRef(EntityType.TILE_BATCH, "tb_1", ids=("t_1", "t_2")), EmbeddingBatch,
        )
    gateway.invoke(
        Task.PF_EMBED, "path_foundation", ModelInputs(images=(image,)), decision_context(),
        EntityRef(EntityType.TILE_BATCH, "tb_2", ids=("t_1",)), EmbeddingBatch,
    )
    assert len(adapter.calls) == 1
