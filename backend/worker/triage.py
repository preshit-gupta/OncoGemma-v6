"""
Triage stage worker handler (SPEC-05 §3-4).
Embeds every tissue tile of the global 224 µm grid with the registry's embedding model, scores
each tile with the trained tumour head and its calibrator (all through the model gateway),
thresholds the calibrated tumour probability into the tumour mask, extracts hotspot candidates,
has the configured VLM check them, and writes the tile-resolution heatmap.

Outputs (SPEC-05 §4.3) under ``cases/<case>/triage/``: ``tiles.parquet`` (every tile's class
probabilities, ``p_tumor_cal`` and ``is_tumor``), ``heatmap.png`` + ``heatmap.json`` (1 px per tile,
alpha 0 only off tissue) and ``tumor_mask.png`` + ``tumor_mask.json`` on the same grid.

Every model call is a DecisionRecord (SPEC-01 §3.3). A slide that cannot be read fails the
stage (SlideReadError); nothing is synthesised in its place (SPEC-01 §3.9). Tissue comes from
the registered mask and colour from the slide's persisted stain profile (SPEC-04).
"""
import hashlib
import io
import json
import time
import tempfile
import shutil
import uuid
import numpy as np
import matplotlib
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.slide_source import download_slide
from app.core.gcs import (
    upload_blob_from_bytes,
    get_gcs_artifact_direct_url,
    resolve_slide_raw_uri,
)
from app.core.pipeline_config import canonical_json
from app.core.stain_profiles import usable_stain_transform
from app.core.tasks import DecisionStatus, EntityType, ProducerKind, Task
from app.core.tissue_mask_store import load_tissue_mask
from app.inference.gateway import EntityRef, FallbackResult, ImageInput, InputSpec, ModelInputs
from app.inference.schemas import TumorVerdict
from app.models.case import Case
from app.models.decision_record import DecisionRecord
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.audit import AuditEvent
from pipeline.hotspots_v6 import HotspotWindow, score_lattice_windows, select_verified_hotspots
from pipeline.errors import DegenerateStainProfileError, SlideReadError
from pipeline.slide_io import SlideReader, centered_origin_um, normalize_region, read_region_at_mpp, require_mpp
from pipeline.stain import StainTransform
from pipeline.tile_embeddings import embed_tile_grid
from pipeline.tile_grid import TileGrid, tissue_tile_grid
from pipeline.tumor_head import TileScores, score_tiles, smooth_tile_probabilities, tile_raster
from worker.runtime import StageRuntime

TILES_FORMAT = "triage_tiles_v1"


def heatmap_png(p_raster: np.ndarray) -> bytes:
    """``p_tumor_cal`` in viridis, one pixel per tile; alpha 0 only where the tile is not tissue (NaN)."""
    tissue = ~np.isnan(p_raster)
    rgba = matplotlib.colormaps["viridis"](np.clip(np.nan_to_num(p_raster, nan=0.0), 0.0, 1.0))
    rgba[..., 3] = np.where(tissue, 1.0, 0.0)
    buf = io.BytesIO()
    Image.fromarray((rgba * 255).round().astype(np.uint8), mode="RGBA").save(buf, "PNG")
    return buf.getvalue()


def tumor_mask_png(is_tumor: np.ndarray) -> bytes:
    """The tumour mask, one pixel per tile: 255 tumour, 0 not (or not tissue)."""
    buf = io.BytesIO()
    Image.fromarray(np.where(is_tumor, 255, 0).astype(np.uint8), mode="L").save(buf, "PNG")
    return buf.getvalue()


def tiles_parquet(grid: TileGrid, scores: TileScores, p_tumor_cal: np.ndarray, is_tumor: np.ndarray, head_version: str) -> bytes:
    """``triage/tiles.parquet`` (SPEC-05 §4.3): one row per tissue tile; ``p`` in the head's class order."""
    k = len(scores.classes)
    table = pa.table({
        "i": pa.array(grid.i, pa.int32()),
        "j": pa.array(grid.j, pa.int32()),
        "x_um": pa.array(grid.x_um, pa.float32()),
        "y_um": pa.array(grid.y_um, pa.float32()),
        "tissue_fraction": pa.array(grid.tissue_fraction, pa.float32()),
        "p": pa.FixedSizeListArray.from_arrays(pa.array(scores.probabilities.astype(np.float32).ravel(), pa.float32()), k),
        "p_tumor_cal": pa.array(p_tumor_cal, pa.float32()),
        "is_tumor": pa.array(is_tumor, pa.bool_()),
    })
    metadata = {"format": TILES_FORMAT, "classes": scores.classes, "tile_um": grid.tile_um, "grid_version": grid.version,
                "head_version": head_version}
    table = table.replace_schema_metadata({b"oncogemma.triage_tiles": json.dumps(metadata).encode()})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def _png(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "PNG")
    return buf.getvalue()


def _image_input(region, image_format: str = "png") -> ImageInput:
    height_px, width_px = region.rgb.shape[:2]
    spec = InputSpec(mpp=region.target_mpp, size_px=(width_px, height_px), color=region.color, format=image_format)
    return ImageInput(_png(region.rgb), spec)


def _centered_region(reader: SlideReader, cx_um: float, cy_um: float, field_um: float, size_px: int):
    """A ``field_um`` square centred on a point (shifted inside the slide), at ``field_um / size_px`` µm/px."""
    x_um, y_um = centered_origin_um(reader, cx_um, cy_um, field_um, field_um)
    return read_region_at_mpp(reader, x_um, y_um, field_um, field_um, field_um / size_px)


def _window_max(p_raster: np.ndarray, tile_um: float, window: HotspotWindow) -> float:
    """The highest ``p_tumor_cal`` of the tissue tiles a window overlaps."""
    half = window.window_um / 2.0
    c0, c1 = int(np.floor((window.cx - half) / tile_um)), int(np.ceil((window.cx + half) / tile_um))
    r0, r1 = int(np.floor((window.cy - half) / tile_um)), int(np.ceil((window.cy + half) / tile_um))
    return float(np.nanmax(p_raster[max(r0, 0):r1, max(c0, 0):c1]))


def _stain_transform(session: Session, slide_id, od_beta: float) -> StainTransform | None:
    """The slide's persisted stain transform, or None when its fit is degenerate (colour cannot be normalised)."""
    return usable_stain_transform(session, slide_id, od_beta=od_beta)  # StainProfileMissingError: run preprocess again


def run_triage(stage_execution: StageExecution, session: Session, runtime: StageRuntime) -> tuple[str, dict]:
    """
    Triage stage worker handler execution:
    1. Loads the registered tissue mask and the slide's persisted stain profile.
    2. Downloads raw slide to transient temp file for high-res patch sampling.
    3. Embeds every tissue tile of the global grid (cached per slide), scores every tile with the
       tumour head and its calibrator (gateway) and thresholds it into the tumour mask.
    4. Selects hotspot windows on the lattice over the tumour mask, checked by the configured VLM (gateway).
    5. Writes tiles.parquet, the heatmap, the tumour mask and the hotspot thumbnails.
    6. Uploads all triage outputs directly to GCS artifacts bucket.
    7. Purges all temporary scratch files.
    """
    start_time = time.time()
    config = runtime.config
    triage_cfg = config.triage
    head_cfg = triage_cfg.tumor_head
    registry = config.models
    gateway, ctx = runtime.gateway, runtime.ctx
    embed_key = triage_cfg.embedding_model
    referee_cfg = triage_cfg.tumor_referee

    input_ref = stage_execution.input_ref or {}
    slide_id = input_ref.get("slide_id")
    case_id = stage_execution.case_id

    if not slide_id:
        slide_obj = session.query(Slide).filter(Slide.case_id == case_id).first()
        if slide_obj:
            slide_id = str(slide_obj.id)

    if not slide_id:
        raise ValueError(f"Slide not found for case {case_id}")

    slide_obj = session.get(Slide, str(slide_id))
    if not slide_obj:
        raise ValueError(f"Slide record '{slide_id}' not found in database for case '{case_id}'.")

    require_mpp(slide_obj)
    if not slide_obj.width_px or not slide_obj.height_px:
        raise ValueError(f"Slide {slide_obj.id} has no pixel dimensions; ingest must record them before triage.")

    width_px = int(slide_obj.width_px)
    height_px = int(slide_obj.height_px)
    width_um = width_px * float(slide_obj.mpp_x)
    height_um = height_px * float(slide_obj.mpp_y)
    profile = config.specimen_profiles.for_type(session.get(Case, case_id).specimen_type)
    od_beta = profile.stain_fit.od_beta
    tissue = load_tissue_mask(case_id)

    scratch_dir = tempfile.mkdtemp(prefix="og_triage_")
    reader = None

    try:
        # 1. Download raw slide from GCS to transient scratch file for patch and overview extraction
        gcs_uri_original = resolve_slide_raw_uri(case_id, slide_obj) or slide_obj.gcs_uri_original or f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"

        try:
            local_slide_path = download_slide(gcs_uri_original, scratch_dir)
        except OSError as exc:
            raise SlideReadError(f"could not download slide {gcs_uri_original}: {exc}") from exc
        reader = SlideReader.from_slide_row(local_slide_path, slide_obj)

        # 2. Every tissue tile of the global grid from the slide origin (SPEC-05 §3). There is no
        # sample cap, so every tissue tile gets a probability.
        tile_um = triage_cfg.patch_size_px * triage_cfg.mpp_target
        grid = tissue_tile_grid(tissue, (width_um, height_um), tile_um, profile.triage.min_tissue_fraction)
        tile_ids = grid.tile_ids()
        print(f"[Triage Worker] {grid.n_tiles} tissue tiles of {tile_um:g} µm on a {grid.n_cols}x{grid.n_rows} grid")

        # The persisted stain transform; None when the slide's fit is degenerate. A model that must see
        # normalised colour cannot be run without it.
        stain = _stain_transform(session, slide_obj.id, od_beta)
        embed_color = registry.models[embed_key].input.color
        if embed_color == "normalized" and stain is None:
            raise DegenerateStainProfileError(
                f"the tile embedder is configured for normalized colour but slide {slide_obj.id}'s stain fit is degenerate"
            )

        # 3. Embed them through the gateway, in batches within the embedder's request limits,
        # reusing the slide's embedding cache.
        grid_embeddings = embed_tile_grid(
            reader, grid, slide_sha256=slide_obj.checksum_sha256, producer_id=embed_key, gateway=gateway, ctx=ctx,
            stain=stain if embed_color == "normalized" else None,
        )
        embeddings = grid_embeddings.embeddings
        tiles_sent = grid_embeddings.tiles_sent

        # 4. Every tile's seven class probabilities and calibrated tumour probability (SPEC-05 §4.2).
        scores = score_tiles(
            embeddings, tile_ids, head_key=head_cfg.model, calibrator_key=head_cfg.calibrator, embed_key=embed_key,
            positive_class=head_cfg.positive_class, gateway=gateway, ctx=ctx,
        )
        head_version = registry.version_of(head_cfg.model)
        p_raster = tile_raster(grid.i, grid.j, scores.p_tumor_cal, grid.n_cols, grid.n_rows)  # NaN off tissue
        mask_raster = p_raster if head_cfg.smoothing_sigma_tiles is None else smooth_tile_probabilities(p_raster, head_cfg.smoothing_sigma_tiles)
        is_tumor_raster = np.nan_to_num(mask_raster, nan=-1.0) >= head_cfg.threshold
        is_tumor = is_tumor_raster[grid.j, grid.i]
        print(f"[Triage Worker] {int(is_tumor.sum())} of {grid.n_tiles} tiles are invasive tumour at τ {head_cfg.threshold:g} ({head_version})")

        # Candidate windows on the lattice over the tumour mask, ranked by mean p_tumor_cal
        # (SPEC-05 §5.1, §5.2 arm H1).
        hs_cfg = profile.hotspots
        windows = score_lattice_windows(p_raster, is_tumor_raster, grid.tile_um, tissue, (width_um, height_um), hs_cfg)
        print(f"[Triage Worker] {len(windows)} valid {hs_cfg.window_um:g} µm hotspot windows")

        # VLM check down the ranked list (SPEC-05 §5.4 arm), at most tumor_referee.candidates
        # windows. A failure fails the stage unless configs/fallbacks.yaml allows it in a clinical
        # run; then the window is unverified (needs_human).
        if referee_cfg.color == "normalized" and stain is None:
            raise DegenerateStainProfileError(
                f"the tumour referee is configured for normalized colour but slide {slide_obj.id}'s stain fit is degenerate"
            )
        referee_by_window: dict[str, dict] = {}

        def verify(window: HotspotWindow) -> bool | None:
            crop = _centered_region(reader, window.cx, window.cy, referee_cfg.field_um, referee_cfg.size_px)
            if referee_cfg.color == "normalized":
                crop = normalize_region(crop, stain)
            result = gateway.invoke_or_fallback(
                Task.TUMOR_REFEREE,
                referee_cfg.producer,
                ModelInputs(images=(_image_input(crop),), prompt_id=referee_cfg.prompt),
                ctx,
                EntityRef(EntityType.HOTSPOT, window.id),
                TumorVerdict,
            )
            if isinstance(result, FallbackResult):
                referee_by_window[window.id] = {
                    "producer_id": referee_cfg.producer,
                    "record_id": str(result.record_id),
                    "tumor_present": None,
                    "needs_human": True,
                    "error_class": type(result.error).__name__,
                }
            else:
                referee_by_window[window.id] = {
                    "producer_id": result.producer_id,
                    "record_id": str(result.record_id),
                    "tumor_present": result.output.tumor_present,
                    "lesion_type": result.output.lesion_type,
                    "rationale": result.output.rationale,
                    "needs_human": False,
                }
            return referee_by_window[window.id]["tumor_present"]

        # Greedy selection with hard Chebyshev non-overlap; rejected windows are removed and never
        # padded back, so fewer than k_max hotspots (or none) is a result (SPEC-05 §5.3).
        k_max = hs_cfg.k_max
        selected, checked = select_verified_hotspots(
            windows, k_max=k_max, w=hs_cfg.window_um, gap=hs_cfg.gap_um, verify=verify, max_checks=referee_cfg.candidates,
        )
        flags = []
        if not selected:
            flags.append("no_invasive_tumor_detected")
        elif len(selected) < k_max:
            flags.append("hotspots_limited_by_tissue")

        hotspots = []
        for window in selected:
            hotspot = window.to_dict()
            hotspot["prob_mean"] = window.rank_score
            hotspot["prob_max"] = _window_max(p_raster, grid.tile_um, window)
            hotspot["referee"] = referee_by_window[window.candidate_id]
            hotspots.append(hotspot)

        # The selection itself is a decision (SPEC-01 §3.3), committed with the stage's outputs.
        selection_input = {
            "candidates_sha256": hashlib.sha256(canonical_json(
                [[c.cx, c.cy, c.rank_score, c.tumor_fraction] for c in windows]
            ).encode("utf-8")).hexdigest(),
            "n_candidates": len(windows),
            "checked": [{"id": c.id, "cx": c.cx, "cy": c.cy, "rank_score": c.rank_score,
                         "tumor_fraction": c.tumor_fraction, "tumor_present": verdict} for c, verdict in checked],
            "k_max": k_max,
            "window_um": hs_cfg.window_um,
            "gap_um": hs_cfg.gap_um,
        }
        session.add(DecisionRecord(**{
            "id": uuid.uuid4(),
            "case_id": ctx.case_id,
            "stage_execution_id": ctx.stage_execution_id,
            "run_id": ctx.run_id,
            "stage": ctx.stage,
            "task": Task.HOTSPOT_SELECT.value,
            "entity_type": EntityType.HOTSPOT.value,
            "entity_id": "hotspots",
            "entity_ids_uri": None,
            "producer_kind": ProducerKind.HEURISTIC.value,
            "producer_id": "hotspot_select",
            # The selection rule is part of the configuration, so its version is the config hash.
            "producer_version": ctx.config_hash,
            "endpoint": None,
            "prompt_id": None,
            "prompt_sha256": None,
            "input_sha256": hashlib.sha256(canonical_json(selection_input).encode("utf-8")).hexdigest(),
            "input_spec": selection_input,
            "params": {"ranking_arm": hs_cfg.ranking_arm, "lattice_step_um": hs_cfg.lattice_step_um,
                       "min_tissue_fraction": hs_cfg.min_tissue_fraction, "min_tumor_fraction": hs_cfg.min_tumor_fraction},
            "output": {"hotspot_ids": [h["id"] for h in hotspots], "flags": flags},
            "raw_output_uri": None,
            "status": DecisionStatus.OK.value,
            "error_class": None,
            "error_detail": None,
            "latency_ms": 0,
            "cost_usd": None,
            "cache_hit": False,
            "run_mode": ctx.run_mode.value,
            "config_hash": ctx.config_hash,
            "supersedes_id": None,
        }))

        # SPEC-05 §4.3 outputs: every tile's scores, the tile-resolution heatmap and the tumour mask.
        triage_prefix = f"cases/{case_id}/triage"
        grid_geometry = {"tile_um": grid.tile_um, "origin_um": [0.0, 0.0], "nx": grid.n_cols, "ny": grid.n_rows,
                         "head_version": head_version}
        heatmap_meta = {**grid_geometry, "value": "p_tumor_cal"}
        tumor_mask_meta = {**grid_geometry, "threshold": head_cfg.threshold, "smoothing_sigma_tiles": head_cfg.smoothing_sigma_tiles,
                           "n_tumor_tiles": int(is_tumor.sum()), "n_tissue_tiles": grid.n_tiles}
        uploads = [
            ("tiles.parquet", tiles_parquet(grid, scores, scores.p_tumor_cal, is_tumor, head_version), "application/octet-stream"),
            ("heatmap.png", heatmap_png(p_raster), "image/png"),
            ("heatmap.json", json.dumps(heatmap_meta).encode("utf-8"), "application/json"),
            ("tumor_mask.png", tumor_mask_png(is_tumor_raster), "image/png"),
            ("tumor_mask.json", json.dumps(tumor_mask_meta).encode("utf-8"), "application/json"),
        ]
        for name, data, content_type in uploads:
            upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, f"{triage_prefix}/{name}", data, content_type)

        # Review thumbnails of every hotspot at three magnifications (10x: 512 um, 20x: 256 um, 40x: 128 um),
        # as scanned and, when the slide's stain fit allows it, normalised. A degenerate fit leaves no
        # normalised variant (and the output says so); it is never replaced by the raw image.
        for hs in hotspots:
            hs_id = hs["id"]
            poly = np.array(hs["polygon_um"])
            cx_um = float(poly[:, 0].mean())
            cy_um = float(poly[:, 1].mean())

            mag_configs = [
                ("10x", 512.0),
                ("20x", 256.0),
                ("40x", 128.0)
            ]

            for mag_name, field_um in mag_configs:
                patch_orig = _centered_region(reader, cx_um, cy_um, field_um, referee_cfg.size_px)
                upload_blob_from_bytes(
                    settings.GCS_ARTIFACTS_BUCKET,
                    f"cases/{case_id}/triage/patches/{hs_id}_{mag_name}_orig.png",
                    _png(patch_orig.rgb),
                    "image/png"
                )
                if stain is None:
                    continue

                norm_bytes = _png(normalize_region(patch_orig, stain).rgb)
                upload_blob_from_bytes(
                    settings.GCS_ARTIFACTS_BUCKET,
                    f"cases/{case_id}/triage/patches/{hs_id}_{mag_name}_norm.png",
                    norm_bytes,
                    "image/png"
                )

                # Also save default thumbnail (10x norm)
                if mag_name == "10x":
                    upload_blob_from_bytes(
                        settings.GCS_ARTIFACTS_BUCKET,
                        f"cases/{case_id}/triage/patches/{hs_id}_thumb.png",
                        norm_bytes,
                        "image/png"
                    )

            thumb_variant = "orig" if stain is None else "norm"
            hs["thumbnail_uri"] = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/triage/patches/{hs_id}_10x_{thumb_variant}.png"
            hs["thumbnail_url"] = get_gcs_artifact_direct_url(f"cases/{case_id}/triage/patches/{hs_id}_10x_{thumb_variant}.png")

        model_versions = {
            key: registry.version_of(key) for key in (embed_key, head_cfg.model, head_cfg.calibrator, referee_cfg.producer)
        }
        wall_time_s = round(time.time() - start_time, 2)
        unit_price = config.pricing.path_foundation.unit_price_per_1k_patches
        estimated_usd = round((tiles_sent / 1000.0) * unit_price, 4)

        output_result = {
            "heatmap_png_uri": f"gs://{settings.GCS_ARTIFACTS_BUCKET}/{triage_prefix}/heatmap.png",
            "heatmap_direct_url": get_gcs_artifact_direct_url(f"{triage_prefix}/heatmap.png"),
            "heatmap": heatmap_meta,
            "tumor_threshold": head_cfg.threshold,
            "tumor_mask": {**tumor_mask_meta, "uri": f"gs://{settings.GCS_ARTIFACTS_BUCKET}/{triage_prefix}/tumor_mask.png"},
            "tiles_uri": f"gs://{settings.GCS_ARTIFACTS_BUCKET}/{triage_prefix}/tiles.parquet",
            "tumor_head": {
                "head": head_cfg.model, "calibrator": head_cfg.calibrator, "positive_class": head_cfg.positive_class,
                "head_record_id": scores.head_record_id, "calibrator_record_id": scores.calibrator_record_id,
            },
            "tile_grid": {
                "version": grid.version,
                "tile_um": grid.tile_um,
                "n_cols": grid.n_cols,
                "n_rows": grid.n_rows,
                "n_tiles": grid.n_tiles
            },
            "embedding_cache": {
                "uri": f"gs://{settings.GCS_ARTIFACTS_BUCKET}/{grid_embeddings.cache_path}",
                "tiles_cached": grid_embeddings.tiles_cached,
                "tiles_embedded": grid_embeddings.tiles_embedded
            },
            "hotspots": hotspots,
            "flags": flags,
            "stain_normalization": "unavailable" if stain is None else "available",
            "model_versions": model_versions,
            "audit": {
                "endpoint_calls_made": tiles_sent,
                "wall_time_s": wall_time_s,
                "estimated_cost_usd": estimated_usd
            }
        }

        output_ref = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/triage/output.json"

        # Upload output.json directly to GCS artifacts bucket
        upload_blob_from_bytes(
            settings.GCS_ARTIFACTS_BUCKET,
            f"cases/{case_id}/triage/output.json",
            json.dumps(output_result, indent=2).encode("utf-8"),
            "application/json"
        )

        # Set status to awaiting_review for pathologist confirmation gate
        stage_execution.status = "awaiting_review"

        # Audit log (#521, #522)
        audit_invoc = AuditEvent(
            case_id=str(case_id),
            actor="worker_triage",
            event_type="model_invocation",
            stage="triage",
            payload={
                "model_id": embed_key,
                "endpoint": registry.models[embed_key].endpoint_id,
                "version": model_versions[embed_key],
                "request_count": tiles_sent,
                "latency_ms": int(wall_time_s * 1000),
                "cost_estimate_usd": estimated_usd
            }
        )
        audit_out = AuditEvent(
            case_id=str(case_id),
            actor="worker_triage",
            event_type="stage_output",
            stage="triage",
            payload={
                "hotspots_found": len(hotspots),
                "output_ref": output_ref
            }
        )
        session.add(audit_invoc)
        session.add(audit_out)
        session.commit()

        return output_ref, model_versions

    finally:
        if reader is not None:
            reader.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
