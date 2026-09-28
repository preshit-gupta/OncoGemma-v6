"""
Patient-level stratified splits, lock files, and leakage verification.
SPEC-02 §5.2 and WP-5.3.
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
from pathlib import Path
from typing import Sequence
import pandas as pd


SPLITS = ("train", "val", "test")


class SplitLeakError(Exception):
    """Raised when patient units leak across train/val/test splits."""


class UnassignedUnitError(Exception):
    """Raised when a unit in the target dataset cannot be found in the source split mapping."""


class LockMismatchError(Exception):
    """Raised when split files differ from hashes in the lock file."""


def make_splits(
    df: pd.DataFrame,
    *,
    seed: int,
    unit_col: str = "patient_id",
    strata_cols: tuple[str, ...] = ("gt_grade", "tss_group", "native_mag"),
    ratios: tuple[float, float, float] = (0.6, 0.2, 0.2),
    min_stratum: int = 10
) -> pd.DataFrame:
    """
    Generate deterministic patient-level stratified train/val/test splits (SPEC-02 §5.2).
    """
    if abs(sum(ratios) - 1.0) >= 1e-9:
        raise ValueError(f"Split ratios must sum to 1.0, got sum={sum(ratios)}")

    # 1. Deduplicate by unit to ensure 1 stratum record per unit
    sort_cols = [unit_col]
    if "slide_id" in df.columns:
        sort_cols.append("slide_id")
    unit_df = df.sort_values(sort_cols).drop_duplicates(subset=[unit_col], keep="first")

    # 2. Derive stratum key per unit
    valid_strata_cols = [col for col in strata_cols if col in unit_df.columns]
    raw_strata: dict[str, tuple[Any, ...]] = {}
    stratum_counts: dict[tuple[Any, ...], int] = collections.defaultdict(int)

    for _, row in unit_df.iterrows():
        unit_id = row[unit_col]
        key = tuple(row[col] for col in valid_strata_cols)
        raw_strata[unit_id] = key
        stratum_counts[key] += 1

    # 3. Merge strata smaller than min_stratum into ("__other__",)
    stratum_to_units: dict[tuple[Any, ...], list[str]] = collections.defaultdict(list)
    for unit_id, key in raw_strata.items():
        if stratum_counts[key] < min_stratum:
            assigned_key = ("__other__",)
        else:
            assigned_key = key
        stratum_to_units[assigned_key].append(unit_id)

    # 4. Allocate splits per stratum using largest-remainder method
    unit_to_split: dict[str, str] = {}
    r_train, r_val, r_test = ratios

    for stratum_key, units in stratum_to_units.items():
        # Deterministically sort units by sha256(seed:unit)
        sorted_units = sorted(
            units,
            key=lambda u: hashlib.sha256(f"{seed}:{u}".encode("utf-8")).hexdigest()
        )
        n = len(sorted_units)
        exact_counts = [r_train * n, r_val * n, r_test * n]
        floor_counts = [math.floor(x) for x in exact_counts]
        remainder = n - sum(floor_counts)
        fractions = [x - f for x, f in zip(exact_counts, floor_counts)]

        # Break fractional ties in (train, val, test) order: sort by (-fraction, index)
        ranked_indices = sorted(range(3), key=lambda idx: (-fractions[idx], idx))
        for idx in ranked_indices[:remainder]:
            floor_counts[idx] += 1

        n_train, n_val, n_test = floor_counts
        train_units = sorted_units[:n_train]
        val_units = sorted_units[n_train:n_train + n_val]
        test_units = sorted_units[n_train + n_val:]

        for u in train_units:
            unit_to_split[u] = "train"
        for u in val_units:
            unit_to_split[u] = "val"
        for u in test_units:
            unit_to_split[u] = "test"

    # 5. Map back onto original dataframe rows
    out = df.copy()
    out["split"] = out[unit_col].map(unit_to_split)
    return out


def check_disjoint(
    frames: dict[str, pd.DataFrame],
    unit_col: str = "patient_id"
) -> None:
    """
    Ensure no patient unit leaks across multiple splits within or across frames (SPEC-02 §5.2).
    Raises SplitLeakError if any unit has multiple distinct splits.
    """
    unit_splits: dict[Any, set[str]] = collections.defaultdict(set)
    leaked_units: set[Any] = set()

    for frame_name, frame in frames.items():
        if unit_col not in frame.columns or "split" not in frame.columns:
            continue
        for unit, grp in frame.groupby(unit_col):
            distinct = set(grp["split"].dropna().unique())
            if len(distinct) > 1:
                leaked_units.add(unit)
            unit_splits[unit].update(distinct)

    for unit, splits in unit_splits.items():
        if len(splits) > 1:
            leaked_units.add(unit)

    if leaked_units:
        sorted_leaks = sorted(str(u) for u in leaked_units)
        raise SplitLeakError(f"Split leakage detected for patient units: {sorted_leaks}")


def inherit_splits(
    target: pd.DataFrame,
    source: pd.DataFrame,
    unit_col: str = "patient_id"
) -> pd.DataFrame:
    """
    Inherit split assignments from a source dataset by unit_col (SPEC-02 §5.2).
    Preserves target row order. Raises UnassignedUnitError if any target unit is missing from source.
    """
    if unit_col not in source.columns or "split" not in source.columns:
        raise ValueError(f"Source DataFrame must contain '{unit_col}' and 'split' columns")
    if unit_col not in target.columns:
        raise ValueError(f"Target DataFrame must contain '{unit_col}' column")

    source_mapping = (
        source.drop_duplicates(subset=[unit_col])
        .set_index(unit_col)["split"]
        .to_dict()
    )

    target_units = target[unit_col].unique()
    missing = [u for u in target_units if u not in source_mapping]
    if missing:
        raise UnassignedUnitError(f"Missing units in source splits: {sorted(str(m) for m in missing)}")

    out = target.copy()
    out["split"] = out[unit_col].map(source_mapping)
    return out


def write_lock(
    paths: list[Path],
    lock_path: Path,
    root: Path
) -> dict[str, str]:
    """
    Compute SHA-256 hashes of split files and write JSON lock file (SPEC-02 §5.2).
    Paths stored relative to root in POSIX format.
    """
    lock_data: dict[str, str] = {}
    for p in paths:
        rel_posix = p.relative_to(root).as_posix()
        with open(p, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        lock_data[rel_posix] = digest

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as f:
        json.dump(lock_data, f, indent=2, sort_keys=True)

    return lock_data


def verify_lock(
    lock_path: Path,
    root: Path
) -> bool:
    """
    Verify SHA-256 integrity of all files listed in lock file (SPEC-02 §5.2).
    Raises LockMismatchError on any missing file or hash discrepancy.
    """
    if not lock_path.is_file():
        raise LockMismatchError(f"Lock file not found: {lock_path}")

    with open(lock_path, "r", encoding="utf-8") as f:
        try:
            lock_data = json.load(f)
        except Exception as e:
            raise LockMismatchError(f"Failed to parse lock file {lock_path}: {e}") from e

    for rel_posix, expected_hash in lock_data.items():
        file_path = root / rel_posix
        if not file_path.is_file():
            raise LockMismatchError(f"File missing from lock: {file_path}")

        with open(file_path, "rb") as f:
            actual_hash = hashlib.sha256(f.read()).hexdigest()

        if actual_hash != expected_hash:
            raise LockMismatchError(
                f"SHA-256 mismatch for {rel_posix}: expected {expected_hash}, got {actual_hash}"
            )

    return True
