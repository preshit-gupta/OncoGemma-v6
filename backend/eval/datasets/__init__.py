"""
Benchmark dataset adapters, manifest schemas, and storage interfaces for OncoGemma v6 evaluation.
SPEC-00 §3, SPEC-02 §3, §5.1, and WP-5.2.
"""
from __future__ import annotations

from .base import (
    DatasetAdapter,
    DatasetConfigMissing,
    FetchedFile,
    FetchIntegrityError,
    load_config,
    load_registry,
)
from .bcnb import BCNBAdapter, convert_to_pyramidal_tiff
from .bcss import BCSSAdapter
from .manifest import MANIFEST_COLUMNS, empty_manifest, validate_manifest
from .midogpp import MIDOGppAdapter
from .storage import GcsStorage, LocalStorage, Storage, storage_from_uri
from .tcga import TCGABRCAAdapter, TUPAC16Adapter


def get_adapter(key: str, **kwargs) -> DatasetAdapter:
    """Instantiate the registered dataset adapter for the specified dataset key."""
    if key == "tcga_brca_dx":
        return TCGABRCAAdapter(**kwargs)
    elif key in ("midogpp_breast", "midogpp_other"):
        return MIDOGppAdapter(key=key, **kwargs)
    elif key == "bcss":
        return BCSSAdapter(**kwargs)
    elif key == "bcnb":
        return BCNBAdapter(**kwargs)
    elif key == "tupac16":
        return TUPAC16Adapter(**kwargs)
    else:
        raise ValueError(f"Unknown dataset key: '{key}'. Registered keys are: tcga_brca_dx, midogpp_breast, midogpp_other, bcss, bcnb, tupac16")


__all__ = [
    "BCNBAdapter",
    "BCSSAdapter",
    "DatasetAdapter",
    "DatasetConfigMissing",
    "FetchIntegrityError",
    "FetchedFile",
    "GcsStorage",
    "LocalStorage",
    "MANIFEST_COLUMNS",
    "MIDOGppAdapter",
    "Storage",
    "TCGABRCAAdapter",
    "TUPAC16Adapter",
    "convert_to_pyramidal_tiff",
    "empty_manifest",
    "get_adapter",
    "load_config",
    "load_registry",
    "storage_from_uri",
    "validate_manifest",
]
