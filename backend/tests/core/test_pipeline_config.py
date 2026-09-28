"""Typed, hashed pipeline configuration (SPEC-01 §3.8, AC7)."""
import math
import re
import uuid

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from app.core import pipeline_config
from app.core.db import Base
from app.core.pipeline_config import (
    ConfigLoadError,
    ConfigNotLoadedError,
    canonical_json,
    get_config_hash,
    load_pipeline_config,
)
from app.models.case import Case
from app.models.stage_execution import StageExecution
from tests.core.helpers import REPO_CONFIGS, VARIABLES, copy_configs, edit_yaml


def load(configs_dir, variables=VARIABLES):
    return load_pipeline_config(configs_dir, variables)


# --- the repo's own configuration --------------------------------------------

def test_repo_configs_load_and_hash_is_sha256_hex():
    config = load(REPO_CONFIGS)
    assert re.fullmatch(r"[0-9a-f]{64}", config.config_hash())
    assert config.mitosis.detector.mpp == 0.25
    assert "tubule@v1.md" in config.prompts


def test_startup_config_is_active_in_tests():
    """conftest loads configs/ the way the entrypoints do; the worker stamps this hash."""
    assert get_config_hash() == pipeline_config.get_pipeline_config().config_hash()


# --- the hash ------------------------------------------------------------------

def test_hash_is_reproducible():
    assert load(REPO_CONFIGS).config_hash() == load(REPO_CONFIGS).config_hash()


def test_hash_ignores_key_order_comments_and_line_endings(tmp_path):
    configs = copy_configs(tmp_path)
    # Reordered keys, no comments.
    for name in ("mitosis.yaml", "qc.yaml"):
        data = yaml.safe_load((configs / name).read_text(encoding="utf-8"))
        (configs / name).write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    # CRLF prompt, as a Windows checkout with autocrlf produces.
    prompt = configs / "prompts" / "tubule@v1.md"
    prompt.write_bytes(prompt.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))

    assert load(configs).config_hash() == load(REPO_CONFIGS).config_hash()


def test_hash_changes_when_a_threshold_changes(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "mitosis.yaml", lambda d: d["detector"].update(det_threshold=0.36))
    assert load(configs).config_hash() != load(REPO_CONFIGS).config_hash()


def test_hash_changes_when_a_prompt_changes(tmp_path):
    configs = copy_configs(tmp_path)
    prompt = configs / "prompts" / "pleo@v1.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\nOne more sentence.\n", encoding="utf-8")
    assert load(configs).config_hash() != load(REPO_CONFIGS).config_hash()


def test_hash_changes_when_a_prompt_is_added(tmp_path):
    configs = copy_configs(tmp_path)
    (configs / "prompts" / "tubule@v2.md").write_text("A new template.\n", encoding="utf-8")
    assert load(configs).config_hash() != load(REPO_CONFIGS).config_hash()


def test_hash_covers_the_model_registry(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "models.yaml", lambda d: d["models"]["triage_probe"].update(version="probe_v2"))
    assert load(configs).config_hash() != load(REPO_CONFIGS).config_hash()


def test_hash_covers_resolved_registry_variables():
    other_model = {**VARIABLES, "GEMINI_REFEREE_MODEL": "gemini-2.5-flash-lite"}
    assert load(REPO_CONFIGS, other_model).config_hash() != load(REPO_CONFIGS).config_hash()


def test_canonical_json_is_sorted_compact_and_refuses_nan():
    assert canonical_json({"b": 1, "a": [1.5, None]}) == '{"a":[1.5,null],"b":1}'
    with pytest.raises(ValueError):
        canonical_json({"x": math.nan})


# --- strictness ----------------------------------------------------------------

def test_unknown_key_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "qc.yaml", lambda d: d["focus"].update(vol_threshhold=45.0))
    with pytest.raises(ConfigLoadError, match="vol_threshhold"):
        load(configs)


def test_number_as_string_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "mitosis.yaml", lambda d: d["detector"].update(det_threshold="0.35"))
    with pytest.raises(ConfigLoadError, match="det_threshold"):
        load(configs)


@pytest.mark.parametrize("mpp", [0.0, -0.25, 2.5])
def test_mpp_out_of_range_is_rejected(tmp_path, mpp):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "triage.yaml", lambda d: d.update(mpp_target=mpp))
    with pytest.raises(ConfigLoadError, match="mpp_target"):
        load(configs)


def test_probability_above_one_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "triage.yaml", lambda d: d["hotspot_extraction"].update(prob_threshold=1.2))
    with pytest.raises(ConfigLoadError, match="prob_threshold"):
        load(configs)


def test_inconsistent_tile_geometry_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "mitosis.yaml", lambda d: d["detector"].update(stride_px=1000))
    with pytest.raises(ConfigLoadError, match="stride_px"):
        load(configs)


def test_overlapping_hpfs_are_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "mitosis.yaml", lambda d: d["hpf"].update(min_separation_um=400.0))
    with pytest.raises(ConfigLoadError, match="min_separation_um"):
        load(configs)


def test_files_that_disagree_are_rejected(tmp_path):
    """mitosis.yaml and scoring.yaml both carry the mitotic-score thresholds; different code reads each."""
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "scoring.yaml", lambda d: d["mitotic_score"]["thresholds"].update(score2_min=4.0))
    with pytest.raises(ConfigLoadError, match="same basis and thresholds"):
        load(configs)


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda d: d.update(embedding_model="medgemma"), "embedding_model 'medgemma' must be an embedding model"),
        (lambda d: d.update(embedding_model="missing"), "embedding_model 'missing'"),
        (lambda d: d.update(tumor_model="path_foundation"), "tumor_model 'path_foundation' must be a classifier"),
        (lambda d: d["tumor_referee"].update(producer="triage_probe"), "must be a VLM"),
        (lambda d: d["tumor_referee"].update(prompt="tumor_verification@v9.md"), "is not in configs/prompts"),
        (lambda d: d["tumor_referee"].update(candidates=5), "at least hotspot_extraction.max_hotspots"),
        (lambda d: d.update(vertex_ai={"batch_size": 64}), "vertex_ai"),
    ],
)
def test_triage_models_must_exist_in_the_registry(tmp_path, edit, message):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "triage.yaml", edit)
    with pytest.raises(ConfigLoadError, match=message):
        load(configs)


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda d: d["detector"].update(producer="medgemma"), "must be a detector with an image input contract"),
        (lambda d: d["detector"].update(tile_size_px=768, stride_px=704, tile_size_um=192.0),
         "must be a multiple of the detector's"),
        (lambda d: d["detector"].update(mpp=0.5, tile_size_um=512.0), "must equal the detector's input mpp 0.25"),
        (lambda d: d["referee"].update(producer="kongnet_det_midog_1"), "must be a VLM"),
        (lambda d: d["referee"].update(prompt="mitosis_referee@v2.md"), "is not in configs/prompts"),
        (lambda d: d.update(verifier={"enabled": True}), "verifier"),
        (lambda d: d["hpf"].update(min_tissue_coverage=1.5), "min_tissue_coverage"),
    ],
)
def test_mitosis_models_must_exist_in_the_registry(tmp_path, edit, message):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "mitosis.yaml", edit)
    with pytest.raises(ConfigLoadError, match=message):
        load(configs)


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda d: d["grading"]["estimators"].update(producer="triage_probe"), "must be a VLM"),
        (lambda d: d["grading"]["estimators"].update(pleo_prompt="pleo@v2.md"), "pleo_prompt 'pleo@v2.md' is not in configs/prompts"),
        (lambda d: d["grading"]["estimators"].update(histotype_images=30), "must not exceed n_patches"),
        (lambda d: d["grading"]["estimators"].pop("tubule_prompt"), "tubule_prompt"),
    ],
)
def test_grading_estimators_must_exist_in_the_registry(tmp_path, edit, message):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "scoring.yaml", edit)
    with pytest.raises(ConfigLoadError, match=message):
        load(configs)


def test_duplicate_yaml_key_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    text = (configs / "triage.yaml").read_text(encoding="utf-8")
    (configs / "triage.yaml").write_text(text + "\nmpp_target: 0.5\n", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="duplicate key 'mpp_target'"):
        load(configs)


def test_missing_required_file_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    (configs / "qc.yaml").unlink()
    with pytest.raises(ConfigLoadError, match="qc"):
        load(configs)


def test_yaml_file_without_schema_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    (configs / "extras.yaml").write_text("threshold: 0.5\n", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="extras.yaml"):
        load(configs)


def test_empty_yaml_file_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    (configs / "pricing.yaml").write_text("", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="mapping"):
        load(configs)


def test_prompt_with_bad_name_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    (configs / "prompts" / "notes.txt").write_text("scratch", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="notes.txt"):
        load(configs)


def test_missing_configs_dir_is_rejected(tmp_path):
    with pytest.raises(ConfigLoadError, match="not found"):
        load(tmp_path / "nowhere")


def test_config_is_immutable():
    config = load(REPO_CONFIGS)
    with pytest.raises(ValidationError):
        config.mitosis.detector.det_threshold = 0.1


# --- ${NAME} interpolation in models.yaml -------------------------------------

def test_unset_variable_resolves_to_none():
    config = load(REPO_CONFIGS)
    assert config.models.models["kongnet_det_midog_1"].endpoint_id is None
    assert config.models.models["path_foundation"].endpoint_id == "1111"


def test_variable_outside_the_allowlist_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "models.yaml", lambda d: d["models"]["medgemma"].update(endpoint_id="${GEMINI_API_KEY}"))
    with pytest.raises(ConfigLoadError, match="GEMINI_API_KEY"):
        load(configs)


def test_partial_interpolation_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "models.yaml", lambda d: d["models"]["medgemma"].update(region="eu-${VERTEX_MEDGEMMA_LOCATION}"))
    with pytest.raises(ConfigLoadError, match="whole value"):
        load(configs)


def test_required_value_from_unset_variable_is_rejected():
    with pytest.raises(ConfigLoadError, match="region"):
        load(REPO_CONFIGS, {**VARIABLES, "VERTEX_MEDGEMMA_LOCATION": ""})


def test_registry_variables_never_include_secrets():
    names = set(pipeline_config.REGISTRY_VARIABLES)
    assert not {n for n in names if re.search("KEY|SECRET|PASSWORD|TOKEN|DATABASE_URL", n)}


# --- the active configuration --------------------------------------------------

def test_accessors_refuse_before_init(monkeypatch):
    monkeypatch.setattr(pipeline_config._Active, "config", None)
    monkeypatch.setattr(pipeline_config._Active, "config_hash", None)
    with pytest.raises(ConfigNotLoadedError):
        pipeline_config.get_pipeline_config()
    with pytest.raises(ConfigNotLoadedError):
        get_config_hash()


def test_worker_stamps_config_hash_on_the_execution():
    from worker.main import poll_and_execute_single_task

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    case_id, exec_id = uuid.uuid4(), uuid.uuid4()
    with Session() as db:
        db.add(Case(id=case_id, created_by="config_hash_test"))
        db.add(StageExecution(id=exec_id, case_id=case_id, stage="qc", attempt=1, status="queued"))
        db.commit()

    with patch("worker.main.SessionLocal", Session), \
         patch("worker.main.HANDLERS", {"qc": lambda st, db, rt: ("gs://out.json", {})}):
        assert poll_and_execute_single_task() is True

    with Session() as db:
        stamped = db.get(StageExecution, exec_id)
        assert stamped.status == "done"
        assert stamped.config_hash == get_config_hash()
