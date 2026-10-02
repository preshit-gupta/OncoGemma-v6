"""BCSS masks rasterised onto the global 224 µm tile grid (SPEC-05 §4.1).

A BCSS mask (the figshare release in ``meta/roiBounds_BaseMagnification.csv``) has one pixel per
slide level-0 pixel over the ROI ``[xmin, xmax) x [ymin, ymax)``. Mask pixel ``(u, v)`` is slide
pixel ``(xmin + u, ymin + v)``, and it belongs to the tile its centre falls in:
``i = floor((xmin + u + 0.5) * mpp_x / tile_um)``, likewise ``j``. That is the grid the worker
embeds (``pipeline.tile_grid``), so a training tile is exactly an inference tile.

The Google Drive masks named ``..._MPP-0.2500.png`` are not at 0.25 µm/px (their size over the
ROI bounds gives about 0.30 µm/px), so the geometry is taken from the base-magnification masks.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

CONFIG_PATH = Path(__file__).with_name("config.yaml")
NOT_ANNOTATED = -1

LABEL_COLUMNS = ["i", "j", "annotated_fraction", "majority_group", "majority_fraction", "label", "exclusion"]


class UnknownMaskCodeError(ValueError):
    """A mask holds a code that is not in the BCSS code table."""


@dataclass(frozen=True)
class TileRule:
    min_annotated_fraction: float
    min_majority_fraction: float


@dataclass(frozen=True)
class LabelSpace:
    classes: tuple[str, ...]
    positive_class: str
    class_map: dict[str, str]  # BCSS code name -> head class
    not_annotated: frozenset[str]
    rule: TileRule


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    if config.get("schema_version") != 1:
        raise ValueError(f"{path}: schema_version must be 1")
    return config


def label_space(config: dict) -> LabelSpace:
    classes = tuple(config["classes"])
    if len(set(classes)) != len(classes) or config["positive_class"] not in classes:
        raise ValueError(f"classes must be distinct and contain the positive class: {classes}")
    class_map = {str(k): str(v) for k, v in config["bcss_class_map"].items()}
    unknown = sorted(set(class_map.values()) - set(classes))
    if unknown:
        raise ValueError(f"bcss_class_map targets classes that are not head classes: {unknown}")
    rule = TileRule(**config["tile_rule"])
    return LabelSpace(classes, str(config["positive_class"]), class_map, frozenset(config["bcss_not_annotated"]), rule)


def group_table(code_names: dict[int, str], space: LabelSpace) -> tuple[np.ndarray, list[str]]:
    """A lookup from BCSS code to group id, and the group names.

    Groups ``0..K-1`` are the head classes in order; each unmapped annotated code gets its own
    group after them (named ``unmapped:<code name>``); not-annotated codes map to -1.
    """
    names = list(space.classes)
    lut = np.full(max(code_names) + 1, NOT_ANNOTATED, dtype=np.int64)
    for code, name in sorted(code_names.items()):
        if name in space.not_annotated:
            continue
        if name in space.class_map:
            lut[code] = names.index(space.class_map[name])
        else:
            names.append(f"unmapped:{name}")
            lut[code] = len(names) - 1
    missing = sorted((space.not_annotated | set(space.class_map)) - set(code_names.values()))
    if missing:
        raise ValueError(f"the config names BCSS codes the code table does not have: {missing}")
    return lut, names


def _tile_index(origin_px: int, n_px: int, mpp: float, tile_um: float) -> np.ndarray:
    return np.floor((origin_px + np.arange(n_px) + 0.5) * mpp / tile_um).astype(np.int64)


def label_roi_tiles(
    mask: np.ndarray,
    roi_origin_px: tuple[int, int],
    mpp_xy: tuple[float, float],
    tile_um: float,
    code_names: dict[int, str],
    space: LabelSpace,
) -> pd.DataFrame:
    """One row per grid tile the ROI touches, with its label or the reason it has none.

    ``annotated_fraction`` and the per-class ``frac_<class>`` columns are fractions of the whole
    tile area (``(tile_um / mpp_x) * (tile_um / mpp_y)`` slide pixels); area outside the ROI is not
    annotated. ``exclusion`` is ``few_annotated``, ``minor_majority`` or ``unmapped_majority`` for an
    unlabelled tile, and empty for a labelled one.
    """
    if mask.ndim != 2:
        raise ValueError(f"a BCSS mask is 2-D, got shape {mask.shape}")
    codes = np.unique(mask)
    unknown = [int(c) for c in codes if int(c) not in code_names]
    if unknown:
        raise UnknownMaskCodeError(f"mask codes {unknown} are not in the BCSS code table")
    lut, group_names = group_table(code_names, space)
    n_groups = len(group_names)
    k = len(space.classes)
    mpp_x, mpp_y = mpp_xy
    tile_area_px = (tile_um / mpp_x) * (tile_um / mpp_y)
    col_tile = _tile_index(roi_origin_px[0], mask.shape[1], mpp_x, tile_um)
    row_tile = _tile_index(roi_origin_px[1], mask.shape[0], mpp_y, tile_um)
    i0 = int(col_tile[0])
    n_cols = int(col_tile[-1]) - i0 + 1
    local_col = col_tile - i0

    rows = []
    for j in np.unique(row_tile):
        block = lut[mask[row_tile == j]]  # (rows in this tile row, W) group ids
        annotated = block != NOT_ANNOTATED
        cols = np.broadcast_to(local_col, block.shape)[annotated]
        counts = np.bincount(cols * n_groups + block[annotated], minlength=n_cols * n_groups).reshape(n_cols, n_groups)
        for c in range(n_cols):
            area = counts[c].astype(np.float64)
            annotated_px = area.sum()
            row = {"i": i0 + c, "j": int(j), "annotated_fraction": annotated_px / tile_area_px}
            for g in range(k):
                row[f"frac_{space.classes[g]}"] = area[g] / tile_area_px
            if annotated_px == 0:
                row.update(majority_group=None, majority_fraction=0.0, label=None, exclusion="few_annotated")
                rows.append(row)
                continue
            top = int(np.argmax(area))
            majority = area[top] / annotated_px
            row.update(majority_group=group_names[top], majority_fraction=majority, label=None, exclusion="")
            if row["annotated_fraction"] < space.rule.min_annotated_fraction:
                row["exclusion"] = "few_annotated"
            elif majority < space.rule.min_majority_fraction:
                row["exclusion"] = "minor_majority"
            elif top >= k:
                row["exclusion"] = "unmapped_majority"
            else:
                row["label"] = space.classes[top]
            rows.append(row)
    frame = pd.DataFrame(rows)
    return frame[LABEL_COLUMNS + [f"frac_{c}" for c in space.classes]]
