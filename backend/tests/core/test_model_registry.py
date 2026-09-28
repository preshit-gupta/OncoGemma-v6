"""Model registry (SPEC-01 §3.7)."""
import re

import pytest

from app.core.config import settings
from app.core.model_registry import (
    FeatureInputContract,
    ImageInputContract,
    LocalArtifactModel,
    VertexEndpointModel,
    VertexGenAIModel,
)
from app.core.pipeline_config import ConfigLoadError, get_pipeline_config, load_pipeline_config
from tests.core.helpers import REPO_CONFIGS, VARIABLES, copy_configs, edit_yaml


def registry():
    return load_pipeline_config(REPO_CONFIGS, VARIABLES).models


def test_repo_registry_entries():
    reg = registry()
    assert set(reg.models) == {"path_foundation", "triage_probe", "kongnet_det_midog_1", "gemini_referee", "medgemma"}
    assert isinstance(reg.models["path_foundation"], VertexEndpointModel)
    assert isinstance(reg.models["triage_probe"], LocalArtifactModel)
    assert isinstance(reg.models["gemini_referee"], VertexGenAIModel)
    assert set(reg.heuristics) == {"od_hyperchromatic_sweep"}


def test_input_contracts_match_the_models_the_pipeline_calls():
    reg = registry()
    pf = reg.models["path_foundation"].input
    det = reg.models["kongnet_det_midog_1"].input
    assert isinstance(pf, ImageInputContract) and (pf.mpp, pf.size_px) == (1.0, [224, 224])
    assert isinstance(det, ImageInputContract) and (det.mpp, det.size_px) == (0.25, [512, 512])
    assert reg.models["triage_probe"].input == FeatureInputContract(features="path_foundation")


def test_versions_come_from_the_registry_and_its_variables():
    reg = registry()
    assert reg.version_of("gemini_referee") == VARIABLES["GEMINI_REFEREE_MODEL"]
    assert reg.version_of("medgemma") == "models/885564806952648704@1@2026-09-22"
    assert reg.version_of("od_hyperchromatic_sweep") == "v5-2b87ab7"
    with pytest.raises(KeyError):
        reg.version_of("yolo")


def test_vertex_endpoint_versions_are_verified_deployments():
    """Each Vertex endpoint version names the deployed model resource, version and deploy date."""
    for key, entry in registry().models.items():
        if entry.provider.startswith("vertex_endpoint"):
            assert re.fullmatch(r"models/\d+@\d+@\d{4}-\d{2}-\d{2}", entry.version), key


def test_startup_registry_resolves_settings():
    """The startup load takes ${NAME} from Settings, so the registry names what the code calls today."""
    reg = get_pipeline_config().models
    assert reg.models["gemini_referee"].model == settings.GEMINI_REFEREE_MODEL
    assert reg.models["path_foundation"].endpoint_id == settings.VERTEX_PATH_FOUNDATION_ENDPOINT_ID


def test_probe_sha256_matches_the_artifact():
    import hashlib

    entry = registry().models["triage_probe"]
    artifact = REPO_CONFIGS.parent / entry.artifact_uri
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == entry.artifact_sha256


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda m: m["models"]["medgemma"].update(provider="openai"), "provider"),
        (lambda m: m["models"]["triage_probe"].update(artifact_sha256="abc"), "artifact_sha256"),
        (lambda m: m["models"]["path_foundation"]["input"].update(size_px=[224]), "size_px"),
        (lambda m: m["models"]["path_foundation"]["input"].update(color="rgb"), "color"),
        (lambda m: m["models"]["triage_probe"]["input"].update(features="medgemma"), "embedding model"),
        (lambda m: m["models"]["gemini_referee"]["params"].update(temperature=3.0), "temperature"),
        (lambda m: m.update(schema_version=2), "schema_version"),
        (lambda m: m["heuristics"].update(medgemma={"version": "x", "module": "pipeline.detect"}), "both a model and a heuristic"),
        (lambda m: m["heuristics"]["od_hyperchromatic_sweep"].update(module="pipeline/detect.py"), "module"),
    ],
)
def test_invalid_registry_is_rejected(tmp_path, edit, message):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "models.yaml", edit)
    with pytest.raises(ConfigLoadError, match=message):
        load_pipeline_config(configs, VARIABLES)
