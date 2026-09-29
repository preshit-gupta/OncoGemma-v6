"""
Base dataset adapter protocol, data classes, and dataset exceptions.
SPEC-02 §3 and WP-5.2.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence, TYPE_CHECKING
import pandas as pd
import yaml

if TYPE_CHECKING:
    from .storage import Storage


class DatasetConfigMissing(Exception):
    """Raised when required dataset configuration keys are missing or null."""

    def __init__(self, missing_keys: Sequence[str], message: str | None = None) -> None:
        self.missing_keys = list(missing_keys)
        msg = message or f"Missing required dataset configuration keys: {', '.join(self.missing_keys)}"
        super().__init__(msg)


class FetchIntegrityError(Exception):
    """Raised when downloaded data fails hash or size verification."""
    pass


class SlideMetadataError(Exception):
    """Raised when a slide file cannot be opened to read its metadata (mpp, magnification, scanner)."""

    def __init__(self, slide_path: str, reason: str) -> None:
        self.slide_path = slide_path
        super().__init__(f"Cannot read slide metadata from {slide_path}: {reason}")


@dataclass(frozen=True)
class FetchedFile:
    slide_id: str
    uri: str
    sha256: str
    md5: str | None
    size_bytes: int


class DatasetAdapter(Protocol):
    key: str

    def discover(self) -> pd.DataFrame:
        """Remote listing producing raw manifest rows without downloading payload."""
        ...

    def fetch(self, row: pd.Series, dest: Storage) -> FetchedFile:
        """Stream slide payload to destination storage with hash computation and verification."""
        ...

    def labels(self) -> pd.DataFrame:
        """Ground-truth rows keyed by (patient_id, slide_id)."""
        ...

    def to_manifest(self, discovered: pd.DataFrame, fetched: dict[str, FetchedFile]) -> pd.DataFrame:
        """Emit validated manifest conforming to MANIFEST_COLUMNS (SPEC-02 §5.1)."""
        ...


def load_registry(registry_path: Path | str | None = None) -> dict[str, Any]:
    """Load dataset registry from yaml."""
    path = Path(registry_path) if registry_path else Path(__file__).parent / "registry.yaml"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("datasets", {})


def load_config(config_path: Path | str | None = None) -> dict[str, Any]:
    """Load dataset adapter configuration from yaml."""
    path = Path(config_path) if config_path else Path(__file__).parent / "config.yaml"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("datasets", {})
