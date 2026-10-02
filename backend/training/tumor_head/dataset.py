"""The tumour-head training set: labelled BCSS tiles with their Path Foundation embeddings.

For each BCSS ROI (decision D20):

1. the base-magnification mask is rasterised onto the slide's 224 µm tile grid (``labels``);
2. the labelled tiles are read from the TCGA slide on GDC by HTTP range requests
   (``eval.datasets.remote_slide``) through ``read_region_at_mpp``, in the embedder's input
   contract (raw colour, 1.0 µm/px, ICC to sRGB), exactly as the worker reads them;
3. they are embedded through the model gateway in EVAL mode (``pipeline.tile_embeddings.embed_tiles``),
   one DecisionRecord per batch; the gateway caches every answer under ``--work``, so a rerun
   makes no endpoint calls;
4. the tile's v5 stain darkness (``od_sum``) is kept for the ``od_fusion_v5`` ablation row.

The dataset carries no split: training joins each slide's split from the locked
``eval/splits/bcss.parquet``, so a split can never go stale inside the dataset.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image

from training.tumor_head.labels import LabelSpace, label_roi_tiles

Image.MAX_IMAGE_PIXELS = None
MPP_FIELD = re.compile(r"\|\s*MPP\s*=\s*([0-9.]+)")
TILE_COLUMNS = [
    "slide_id", "patient_id", "file_id", "i", "j", "x_um", "y_um", "label",
    "annotated_fraction", "majority_fraction", "od_sum",
]


class SlideMppMissingError(ValueError):
    """A remote slide states no MPP, so its tiles have no physical size."""


@dataclass(frozen=True)
class RoiSource:
    slide_id: str
    patient_id: str
    file_id: str
    origin_px: tuple[int, int]
    size_px: tuple[int, int]
    mask_path: Path
    mask_sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tile_od_sum(rgb: np.ndarray) -> float:
    """v5's cellularity signal for one tile: optical density of its mean colour, summed over RGB.

    v5 read one 80-px thumbnail pixel per heatmap cell (the cell's mean colour) and summed
    ``-log10(clip(rgb / 255, 1e-4, 1))`` over the channels (``worker/triage.py`` before v6).
    """
    mean = rgb.reshape(-1, 3).astype(np.float64).mean(axis=0)
    return float(np.maximum(0.0, -np.log10(np.clip(mean / 255.0, 1e-4, 1.0))).sum())


def roi_sources(roi_bounds: Path, masks_dir: Path, dx: pd.DataFrame, exclude: frozenset[str] = frozenset()) -> list[RoiSource]:
    """Every ROI not in ``exclude``; each must have a mask and a GDC slide, or this raises."""
    from eval.make_splits import short_barcode

    bounds = pd.read_csv(roi_bounds, index_col=0)
    hashes = json.loads((masks_dir / "SHA256.json").read_text())
    slide_files = {short_barcode(name): fid for name, fid in zip(dx["file_name"], dx["file_id"])}
    sources = []
    unknown = sorted(exclude - set(bounds.index))
    if unknown:
        raise KeyError(f"excluded slides are not BCSS ROIs: {unknown}")
    for slide_id, row in bounds.iterrows():
        if slide_id in exclude:
            continue
        if slide_id not in slide_files:
            raise KeyError(f"BCSS slide {slide_id} has no open-access GDC diagnostic slide")
        mask_path = masks_dir / f"{slide_id}_xmin{row.xmin}_ymin{row.ymin}_base.png"
        expected = hashes[slide_id]["sha256"]
        actual = sha256_file(mask_path)
        if actual != expected:
            raise ValueError(f"{mask_path} has sha256 {actual}, SHA256.json records {expected}")
        sources.append(RoiSource(
            slide_id=str(slide_id), patient_id=str(slide_id)[:12], file_id=slide_files[slide_id], origin_px=(int(row.xmin), int(row.ymin)),
            size_px=(int(row.xmax - row.xmin), int(row.ymax - row.ymin)), mask_path=mask_path, mask_sha256=actual,
        ))
    return sources


def slide_mpp_from_description(description: str) -> float:
    match = MPP_FIELD.search("|" + description)
    if match is None:
        raise SlideMppMissingError(f"no MPP in the slide description {description[:120]!r}")
    return float(match.group(1))


class TileDatasetBuilder:
    def __init__(self, space: LabelSpace, code_names: dict[int, str], tile_um: float, embed_key: str, gateway, config_hash: str):
        self.space = space
        self.code_names = code_names
        self.tile_um = tile_um
        self.embed_key = embed_key
        self.gateway = gateway
        self.config_hash = config_hash
        self._lock = threading.Lock()

    def build_roi(self, source: RoiSource) -> tuple[pd.DataFrame, np.ndarray, dict]:
        from app.core.run_context import DecisionContext, RunMode
        from eval.datasets.remote_slide import (
            gdc_file_url,
            open_remote_slide,
            read_first_page_description,
        )
        from pipeline.slide_io import read_region_at_mpp
        from pipeline.tile_embeddings import embed_tiles
        from pipeline.tile_grid import TileGrid

        mask = np.asarray(Image.open(source.mask_path))
        if mask.shape != (source.size_px[1], source.size_px[0]):
            raise ValueError(f"{source.mask_path}: mask is {mask.shape[::-1]} px, the ROI bounds say {source.size_px}")
        url = gdc_file_url(source.file_id)
        mpp = slide_mpp_from_description(read_first_page_description(url))
        tiles = label_roi_tiles(mask, source.origin_px, (mpp, mpp), self.tile_um, self.code_names, self.space)
        summary = {
            "slide_id": source.slide_id, "file_id": source.file_id, "slide_mpp": mpp,
            "mask_sha256": source.mask_sha256, "tiles_touched": len(tiles),
            "exclusions": {k: int(v) for k, v in tiles["exclusion"].value_counts().items() if k},
        }
        labelled = tiles[tiles["label"].notna()].reset_index(drop=True)
        summary["tiles_labelled"] = len(labelled)
        if labelled.empty:
            return labelled.assign(od_sum=[]), np.zeros((0, 0), np.float32), summary
        reader = open_remote_slide(url, mpp, mpp)
        try:
            width_um, height_um = reader.extent_um()
            grid = TileGrid(
                tile_um=self.tile_um,
                n_cols=int(np.ceil(width_um / self.tile_um)),
                n_rows=int(np.ceil(height_um / self.tile_um)),
                i=labelled["i"].to_numpy(np.int32),
                j=labelled["j"].to_numpy(np.int32),
                tissue_fraction=labelled["annotated_fraction"].to_numpy(np.float32),
            )
            ctx = DecisionContext(
                case_id=uuid.uuid5(uuid.NAMESPACE_URL, f"bcss/{source.slide_id}"), stage_execution_id=uuid.uuid4(),
                stage="triage", run_mode=RunMode.EVAL, run_id=None, config_hash=self.config_hash,
            )
            vectors, sent = embed_tiles(reader, grid, list(range(grid.n_tiles)), producer_id=self.embed_key, gateway=self.gateway, ctx=ctx)
            od = [
                tile_od_sum(read_region_at_mpp(reader, float(x), float(y), self.tile_um, self.tile_um, 1.0).rgb)
                for x, y in zip(grid.x_um, grid.y_um)
            ]
        finally:
            reader.close()
        summary["tiles_sent"] = int(sent)
        labelled = labelled.assign(
            slide_id=source.slide_id, patient_id=source.patient_id, file_id=source.file_id,
            x_um=grid.x_um, y_um=grid.y_um, od_sum=od,
        )
        return labelled, np.stack(vectors).astype(np.float32), summary


def write_dataset(frames: list[pd.DataFrame], embeddings: list[np.ndarray], path: Path) -> str:
    """Tiles and embeddings in one parquet file (``emb`` a 384-float list per row). Returns its sha256."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    frame = pd.concat([f for f in frames if len(f)], ignore_index=True)
    emb = np.concatenate([e for e in embeddings if e.size], axis=0)
    if len(frame) != emb.shape[0]:
        raise ValueError(f"{len(frame)} tiles for {emb.shape[0]} embeddings")
    frac_cols = [c for c in frame.columns if c.startswith("frac_")]
    frame = frame[TILE_COLUMNS + frac_cols].sort_values(["slide_id", "j", "i"], kind="stable")
    emb = emb[frame.index.to_numpy()]
    table = pa.Table.from_pandas(frame.reset_index(drop=True), preserve_index=False)
    table = table.append_column("emb", pa.FixedSizeListArray.from_arrays(pa.array(emb.ravel(), pa.float32()), emb.shape[1]))
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return sha256_file(path)


def build(args) -> int:
    from app.core.pipeline_config import get_config_hash, init_pipeline_config
    from app.inference.adapters.registry import production_adapters
    from app.inference.blobs import LocalBlobStore
    from app.inference.gateway import ModelGateway
    from app.inference.records import DecisionLog
    from eval.datasets.base import load_config as load_dataset_config
    from training.tumor_head.labels import label_space, load_config

    space = label_space(load_config())
    code_names = {int(k): str(v) for k, v in load_dataset_config()["bcss"]["class_map"].items()}
    config = init_pipeline_config()
    log = DecisionLog()
    gateway = ModelGateway(config, production_adapters(), log, LocalBlobStore(args.work / "blobs"))
    embed_key = config.triage.embedding_model
    tile_um = config.triage.patch_size_px * config.triage.mpp_target
    builder = TileDatasetBuilder(space, code_names, tile_um, embed_key, gateway, get_config_hash())

    excluded = frozenset(args.exclude or [])
    sources = roi_sources(args.roi_bounds, args.masks, pd.read_parquet(args.dx), excluded)
    if args.limit:
        sources = sources[: args.limit]
    print(f"{len(sources)} ROIs; excluded {sorted(excluded)}", flush=True)

    def one(source):
        frame, emb, summary = builder.build_roi(source)
        print(f"{source.slide_id}: {summary['tiles_labelled']}/{summary['tiles_touched']} tiles", flush=True)
        return frame, emb, summary

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(one, sources))
    out = args.out
    dataset_sha = write_dataset([r[0] for r in results], [r[1] for r in results], out / "bcss_tiles.parquet")
    records = out / "decision_records.jsonl"
    with open(records, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row, default=str) + "\n" for row in log.pending())
    meta = {
        "dataset_sha256": dataset_sha,
        "config_hash": get_config_hash(),
        "embedder": embed_key,
        "embedder_version": config.models.version_of(embed_key),
        "tile_um": tile_um,
        "excluded_slides": sorted(excluded),
        "rois": [r[2] for r in results],
    }
    (out / "bcss_tiles.json").write_text(json.dumps(meta, indent=1))
    print(f"wrote {out / 'bcss_tiles.parquet'} sha256 {dataset_sha}")
    return 0
