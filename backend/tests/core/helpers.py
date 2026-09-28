"""Helpers for tests that load a copy of the repo's configs/ directory."""
import shutil
from pathlib import Path

import yaml

from app.core.config import settings

REPO_CONFIGS = Path(settings.CONFIGS_DIR).resolve()

VARIABLES = {
    "VERTEX_PATH_FOUNDATION_ENDPOINT_ID": "1111",
    "VERTEX_PATH_FOUNDATION_LOCATION": "us-central1",
    "VERTEX_MITOSIS_ENDPOINT_ID": None,
    "VERTEX_MITOSIS_LOCATION": "us-central1",
    "VERTEX_MEDGEMMA_ENDPOINT_ID": "2222",
    "VERTEX_MEDGEMMA_LOCATION": "us-central1",
    "GEMINI_REFEREE_MODEL": "gemini-2.5-flash",
}


def copy_configs(tmp_path: Path) -> Path:
    target = tmp_path / "configs"
    shutil.copytree(REPO_CONFIGS, target)
    return target


def edit_yaml(path: Path, edit) -> None:
    """Load ``path``, apply ``edit(data)`` in place, and write it back."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
