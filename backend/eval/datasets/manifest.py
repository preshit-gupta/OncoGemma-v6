"""
Manifest schema definition and validation per SPEC-02 §5.1 and WP-5.2.
"""
from __future__ import annotations

import re
from typing import Any
import pandas as pd


MANIFEST_COLUMNS: dict[str, str] = {
    "dataset": "string",
    "patient_id": "string",
    "slide_id": "string",
    "uri": "string",
    "sha256": "string",
    "specimen_type": "string",
    "mpp_override": "Float64",
    "mpp_source": "string",
    "native_mag": "Float64",
    "scanner": "string",
    "tss": "string",
    "split": "string",
    "gt_grade": "Int64",
    "gt_total": "Int64",
    "gt_tubule": "Int64",
    "gt_pleo": "Int64",
    "gt_mitoses": "Int64",
    "gt_histotype": "string",
    "gt_label_source": "string",
    "gt_label_confidence": "string",
    "regions_uri": "string",
}

VALID_SPECIMEN_TYPES = {"resection", "core_biopsy"}
VALID_MPP_SOURCES = {"file", "dataset_doc", "manual"}
VALID_SPLITS = {"train", "val", "test"}
SHA256_REGEX = re.compile(r"^[a-fA-F0-9]{64}$")


def validate_manifest(df: pd.DataFrame) -> None:
    """
    Validate a dataset manifest against the SPEC-02 §5.1 specification.

    Raises:
        ValueError: Listing every detected schema violation, missing value, or inconsistency.
    """
    errors: list[str] = []

    # 1. Column presence check
    missing_cols = [col for col in MANIFEST_COLUMNS if col not in df.columns]
    if missing_cols:
        errors.append(f"Missing required manifest columns: {', '.join(missing_cols)}")

    if len(df) == 0:
        errors.append("Manifest is empty (0 rows)")
        raise ValueError("Manifest validation failed with 1 error:\n- Manifest is empty (0 rows)")

    # 2. Check duplicates on (dataset, slide_id)
    if "dataset" in df.columns and "slide_id" in df.columns:
        dups = df[df.duplicated(subset=["dataset", "slide_id"], keep=False)]
        if not dups.empty:
            dup_keys = dups[["dataset", "slide_id"]].drop_duplicates().to_dict(orient="records")
            errors.append(f"Duplicate (dataset, slide_id) keys found: {dup_keys[:5]}")

    # 3. Row-level checks
    for idx, row in df.iterrows():
        # Non-null required fields
        for field in ("dataset", "patient_id", "slide_id", "uri", "sha256", "specimen_type", "mpp_source"):
            if field in df.columns:
                val = row[field]
                if pd.isna(val) or str(val).strip() == "":
                    errors.append(f"Row {idx}: required field '{field}' is null or empty")

        # URI validation
        if "uri" in df.columns and not pd.isna(row["uri"]):
            uri_str = str(row["uri"])
            if not (uri_str.startswith("gs://") or uri_str.startswith("file://") or uri_str.startswith("/") or (len(uri_str) > 2 and uri_str[1] == ":")):
                errors.append(f"Row {idx}: 'uri' must start with gs://, file://, or an absolute path (got '{uri_str}')")

        # sha256 validation
        if "sha256" in df.columns and not pd.isna(row["sha256"]):
            sha = str(row["sha256"]).strip()
            if not SHA256_REGEX.match(sha):
                errors.append(f"Row {idx}: 'sha256' must be a 64-char hex string (got '{sha}')")

        # Enum: specimen_type
        if "specimen_type" in df.columns and not pd.isna(row["specimen_type"]):
            st = str(row["specimen_type"]).strip()
            if st not in VALID_SPECIMEN_TYPES:
                errors.append(f"Row {idx}: 'specimen_type' must be in {VALID_SPECIMEN_TYPES} (got '{st}')")

        # Enum: mpp_source
        if "mpp_source" in df.columns and not pd.isna(row["mpp_source"]):
            ms = str(row["mpp_source"]).strip()
            if ms not in VALID_MPP_SOURCES:
                errors.append(f"Row {idx}: 'mpp_source' must be in {VALID_MPP_SOURCES} (got '{ms}')")

        # Enum: split (can be null before WP-5.3 runs)
        if "split" in df.columns and not pd.isna(row["split"]):
            sp = str(row["split"]).strip()
            if sp not in VALID_SPLITS:
                errors.append(f"Row {idx}: 'split' must be in {VALID_SPLITS} (got '{sp}')")

        # Numeric bounds: mpp_override
        if "mpp_override" in df.columns and not pd.isna(row["mpp_override"]):
            mpp = float(row["mpp_override"])
            if mpp <= 0.0:
                errors.append(f"Row {idx}: 'mpp_override' must be positive float (got {mpp})")

        # Numeric bounds: native_mag
        if "native_mag" in df.columns and not pd.isna(row["native_mag"]):
            mag = float(row["native_mag"])
            if mag <= 0.0:
                errors.append(f"Row {idx}: 'native_mag' must be positive float (got {mag})")

        # Nottingham grade bounds
        if "gt_grade" in df.columns and not pd.isna(row["gt_grade"]):
            g = int(row["gt_grade"])
            if g not in (1, 2, 3):
                errors.append(f"Row {idx}: 'gt_grade' must be in {{1, 2, 3}} (got {g})")

        if "gt_total" in df.columns and not pd.isna(row["gt_total"]):
            tot = int(row["gt_total"])
            if tot < 3 or tot > 9:
                errors.append(f"Row {idx}: 'gt_total' must be between 3 and 9 (got {tot})")

        for comp in ("gt_tubule", "gt_pleo", "gt_mitoses"):
            if comp in df.columns and not pd.isna(row[comp]):
                c_val = int(row[comp])
                if c_val not in (1, 2, 3):
                    errors.append(f"Row {idx}: '{comp}' must be in {{1, 2, 3}} (got {c_val})")

        # Consistency checks
        # 1. Total score vs sum of components
        if (
            all(col in df.columns for col in ("gt_tubule", "gt_pleo", "gt_mitoses", "gt_total"))
            and not pd.isna(row["gt_tubule"])
            and not pd.isna(row["gt_pleo"])
            and not pd.isna(row["gt_mitoses"])
            and not pd.isna(row["gt_total"])
        ):
            expected_sum = int(row["gt_tubule"]) + int(row["gt_pleo"]) + int(row["gt_mitoses"])
            if int(row["gt_total"]) != expected_sum:
                errors.append(
                    f"Row {idx}: Nottingham total mismatch: gt_total={row['gt_total']} != sum of components {expected_sum}"
                )

        # 2. Grade vs total score bands (G1: 3-5, G2: 6-7, G3: 8-9)
        if (
            "gt_grade" in df.columns
            and "gt_total" in df.columns
            and not pd.isna(row["gt_grade"])
            and not pd.isna(row["gt_total"])
        ):
            g = int(row["gt_grade"])
            tot = int(row["gt_total"])
            if (g == 1 and not (3 <= tot <= 5)) or (g == 2 and not (6 <= tot <= 7)) or (g == 3 and not (8 <= tot <= 9)):
                errors.append(f"Row {idx}: Inconsistent grade {g} with total score {tot} (expected G1: 3-5, G2: 6-7, G3: 8-9)")

    if errors:
        msg = f"Manifest validation failed with {len(errors)} error(s):\n" + "\n".join(f"- {e}" for e in errors)
        raise ValueError(msg)


def empty_manifest() -> pd.DataFrame:
    """Return an empty DataFrame structured according to MANIFEST_COLUMNS."""
    df = pd.DataFrame({col: pd.Series(dtype=dtype) for col, dtype in MANIFEST_COLUMNS.items()})
    return df
