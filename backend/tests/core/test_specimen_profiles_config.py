"""configs/specimen_profiles.yaml and configs/stain_refs/*.json in the hashed PipelineConfig (SPEC-04 §3.2, §3.3)."""
import json

import pytest

from app.core.pipeline_config import ConfigLoadError, load_pipeline_config
from tests.core.helpers import REPO_CONFIGS, VARIABLES, copy_configs, edit_yaml

REFERENCE = "v5_patch@v1.json"


def load(configs_dir):
    return load_pipeline_config(configs_dir, VARIABLES)


def edit_reference(configs, edit, name=REFERENCE):
    path = configs / "stain_refs" / name
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_the_repo_profiles_and_reference_load():
    config = load(REPO_CONFIGS)
    resection, biopsy = (config.specimen_profiles.profiles[k] for k in ("resection", "core_biopsy"))
    assert (resection.stain_fit.n_patches, resection.stain_fit.patch_um) == (50, 512.0)
    assert (biopsy.stain_fit.n_patches, biopsy.stain_fit.patch_um) == (30, 256.0)
    assert resection.stain_target.ref == biopsy.stain_target.ref == "v5_patch@v1"
    reference = config.stain_refs["v5_patch@v1"]
    assert reference.n_slides == 0 and len(reference.w_tgt) == 2


def test_changing_a_profile_changes_the_config_hash(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "specimen_profiles.yaml", lambda d: d["profiles"]["core_biopsy"]["stain_fit"].update(n_patches=31))
    assert load(configs).config_hash() != load(REPO_CONFIGS).config_hash()


def test_changing_a_colour_reference_changes_the_config_hash(tmp_path):
    """The reference decides every normalised pixel, so it is part of what a run is stamped with."""
    configs = copy_configs(tmp_path)
    edit_reference(configs, lambda d: d.update(maxc_tgt=[d["maxc_tgt"][0] + 0.01, d["maxc_tgt"][1]]))
    assert load(configs).config_hash() != load(REPO_CONFIGS).config_hash()


def test_a_profile_must_name_an_existing_reference(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "specimen_profiles.yaml", lambda d: d["profiles"]["resection"]["stain_target"].update(ref="tcga_train_median@v1"))
    with pytest.raises(ConfigLoadError, match="tcga_train_median@v1.*not in configs/stain_refs"):
        load(configs)


def test_every_specimen_type_needs_a_profile(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "specimen_profiles.yaml", lambda d: d["profiles"].pop("core_biopsy"))
    with pytest.raises(ConfigLoadError, match="core_biopsy"):
        load(configs)


def test_unknown_specimen_type_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "specimen_profiles.yaml", lambda d: d["profiles"].update(unknown=d["profiles"]["resection"]))
    with pytest.raises(ConfigLoadError, match="unknown"):
        load(configs)


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("sparse_below", 51, "sparse_below must not exceed n_patches"),
        ("angle_percentile", 60.0, "angle_percentile"),
        ("conc_percentile", 40.0, "conc_percentile"),
        ("fit_mpp", 3.0, "fit_mpp"),
        ("min_sat_mean", 1.5, "min_sat_mean"),
        ("od_beta", 0.0, "od_beta"),
        ("n_patches", "50", "n_patches"),
    ],
)
def test_bad_stain_fit_values_are_rejected(tmp_path, field, value, message):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "specimen_profiles.yaml", lambda d: d["profiles"]["resection"]["stain_fit"].update({field: value}))
    with pytest.raises(ConfigLoadError, match=message):
        load(configs)


def test_unknown_profile_key_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "specimen_profiles.yaml", lambda d: d["profiles"]["resection"]["stain_fit"].update(od_bta=0.15))
    with pytest.raises(ConfigLoadError, match="od_bta"):
        load(configs)


def test_a_reference_must_name_itself_after_its_file(tmp_path):
    configs = copy_configs(tmp_path)
    edit_reference(configs, lambda d: d.update(reference_id="v5_patch@v2"))
    with pytest.raises(ConfigLoadError, match="names itself 'v5_patch@v2'"):
        load(configs)


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda d: d.update(w_tgt=[[1.0, 1.0, 1.0], [0.0, 1.0, 0.0]]), "unit vectors"),
        (lambda d: d.update(w_tgt=[[1.0, 0.0, 0.0]]), "two stain vectors"),
        (lambda d: d.update(maxc_tgt=[1.0, 0.0]), "positive"),
        (lambda d: d.update(maxc_tgt=[1.0]), "two positive"),
        (lambda d: d.update(slide_ids_sha256="abc"), "slide_ids_sha256"),
        (lambda d: d.update(n_slides=-1), "n_slides"),
        (lambda d: d.update(extra=1), "extra"),
        (lambda d: d.pop("source"), "source"),
    ],
)
def test_a_malformed_reference_is_rejected(tmp_path, edit, message):
    configs = copy_configs(tmp_path)
    edit_reference(configs, edit)
    with pytest.raises(ConfigLoadError, match=message):
        load(configs)


def test_a_reference_file_with_a_bad_name_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    (configs / "stain_refs" / "notes.txt").write_text("scratch", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="notes.txt"):
        load(configs)


def test_a_reference_with_an_unversioned_name_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    (configs / "stain_refs" / "v5_patch@v1.json").rename(configs / "stain_refs" / "v5_patch.json")
    edit_yaml(configs / "specimen_profiles.yaml", lambda d: [p["stain_target"].update(ref="v5_patch@v1") for p in d["profiles"].values()])
    with pytest.raises(ConfigLoadError, match="(?s)stain_refs.v5_patch.*should match pattern"):
        load(configs)


def test_a_duplicate_key_in_a_reference_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    path = configs / "stain_refs" / REFERENCE
    path.write_text(path.read_text(encoding="utf-8").replace('"n_slides": 0,', '"n_slides": 0, "n_slides": 1,'), encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="duplicate key 'n_slides'"):
        load(configs)


def test_a_missing_reference_directory_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    for path in (configs / "stain_refs").iterdir():
        path.unlink()
    (configs / "stain_refs").rmdir()
    with pytest.raises(ConfigLoadError, match="stain reference directory not found"):
        load(configs)


def test_invalid_json_is_rejected(tmp_path):
    configs = copy_configs(tmp_path)
    (configs / "stain_refs" / REFERENCE).write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match=REFERENCE):
        load(configs)
