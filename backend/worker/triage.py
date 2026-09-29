"""
Triage stage worker handler (v4.2 Hotspot Triage).
Extracts 1.0 µm/px tiles, embeds them with the registry's embedding model and scores them
with its tumour classifier (both through the model gateway), extracts hotspot candidates,
has the configured VLM check them, and renders the viridis heatmap overlay.

Every model call is a DecisionRecord (SPEC-01 §3.3). A slide that cannot be read fails the
stage (SlideReadError); nothing is synthesised in its place (SPEC-01 §3.9). Tissue comes from
the registered mask and colour from the slide's persisted stain profile (SPEC-04).
"""
import os
import io
import json
import time
import tempfile
import shutil
import numpy as np
import matplotlib
import matplotlib.cm as cm
from PIL import Image
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import (
    parse_gcs_uri,
    upload_blob_from_bytes,
    download_blob_to_filename,
    get_gcs_artifact_direct_url,
    resolve_slide_raw_uri
)
from app.core.stain_profiles import usable_stain_transform
from app.core.tasks import EntityType, Task
from app.core.tissue_mask_store import load_tissue_mask
from app.inference.batching import plan_batches
from app.inference.gateway import EntityRef, FallbackResult, ImageInput, InputSpec, ModelInputs
from app.inference.outputs import ClassProbabilities, EmbeddingBatch
from app.inference.schemas import TumorVerdict
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.audit import AuditEvent
from pipeline.errors import DegenerateStainProfileError, SlideReadError
from pipeline.hotspots import extract_hotspots
from pipeline.probe import l2_normalize
from pipeline.slide_io import SlideReader, centered_origin_um, normalize_region, read_region_at_mpp, require_mpp
from pipeline.stain import StainTransform
from worker.runtime import StageRuntime

# Class label of "tumour" in the tumour classifier's predict_proba columns.
TUMOR_CLASS = 1


def render_viridis_heatmap_png(
    prob_grid: np.ndarray,
    output_path: str,
    scale: float = 1.0
) -> str:
    """
    Renders 2D probability grid as a full-spectrum Viridis color image with alpha channel for OSD overlay.
    """
    ny, nx = prob_grid.shape
    valid_mask = ~np.isnan(prob_grid)

    prob_norm = np.nan_to_num(prob_grid, nan=0.0)
    prob_norm = np.clip(prob_norm, 0.0, 1.0)

    try:
        colormap = matplotlib.colormaps["viridis"]
    except Exception:
        colormap = cm.get_cmap("viridis")

    rgba_mapped = colormap(prob_norm) # Shape (ny, nx, 4)

    # Set alpha channel: 0.0 for non-tissue (NaN), scaled alpha for tissue based on prob
    alpha = np.where(valid_mask, np.clip(0.35 + 0.55 * prob_norm, 0.25, 0.90), 0.0)
    rgba_mapped[..., 3] = alpha

    img_uint8 = (rgba_mapped * 255).astype(np.uint8)
    img = Image.fromarray(img_uint8, mode="RGBA")

    if scale != 1.0:
        new_w = max(1, int(nx * scale))
        new_h = max(1, int(ny * scale))
        img = img.resize((new_w, new_h), Image.BILINEAR)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    img.save(output_path, format="PNG")
    return output_path


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


def _stain_transform(session: Session, slide_id, od_beta: float) -> StainTransform | None:
    """The slide's persisted stain transform, or None when its fit is degenerate (colour cannot be normalised)."""
    return usable_stain_transform(session, slide_id, od_beta=od_beta)  # StainProfileMissingError: run preprocess again


def run_triage(stage_execution: StageExecution, session: Session, runtime: StageRuntime) -> tuple[str, dict]:
    """
    Triage stage worker handler execution:
    1. Loads the registered tissue mask and the slide's persisted stain profile.
    2. Downloads raw slide to transient temp file for high-res patch sampling.
    3. Embeds tissue tiles and scores them with the tumour classifier (gateway).
    4. Has the configured VLM check hotspot candidates (gateway).
    5. Renders Viridis heatmap & extracts hotspot thumbnails.
    6. Uploads all triage outputs directly to GCS artifacts bucket.
    7. Purges all temporary scratch files.
    """
    start_time = time.time()
    config = runtime.config
    triage_cfg = config.triage
    registry = config.models
    gateway, ctx = runtime.gateway, runtime.ctx
    embed_key, tumor_key = triage_cfg.embedding_model, triage_cfg.tumor_model
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
    od_beta = config.specimen_profiles.for_type(session.get(Case, case_id).specimen_type).stain_fit.od_beta
    tissue = load_tissue_mask(case_id)

    # Issue #86: Define triage overview grid dimensions matching slide aspect ratio
    nx = 80
    ny = max(1, int(round(nx * (height_px / max(width_px, 1)))))

    stride_x_um = width_um / nx
    stride_y_um = height_um / ny
    grid_origin_um = (0.0, 0.0)

    scratch_dir = tempfile.mkdtemp(prefix="og_triage_")
    reader = None

    try:
        # 1. Download raw slide from GCS to transient scratch file for patch and overview extraction
        gcs_uri_original = resolve_slide_raw_uri(case_id, slide_obj) or slide_obj.gcs_uri_original or f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
        raw_bucket_name, blob_name = parse_gcs_uri(gcs_uri_original)
        ext = os.path.splitext(blob_name)[1] or ".svs"
        local_slide_path = os.path.join(scratch_dir, f"slide{ext}")

        try:
            download_blob_to_filename(raw_bucket_name, blob_name, local_slide_path)
        except OSError as exc:
            raise SlideReadError(f"could not download slide {gcs_uri_original}: {exc}") from exc
        reader = SlideReader.from_slide_row(local_slide_path, slide_obj)

        # The overview: one cell of the heatmap grid per pixel, read at that resolution.
        overview = read_region_at_mpp(reader, 0.0, 0.0, width_um, height_um, stride_x_um).rgb
        if overview.shape[:2] != (ny, nx):  # the grid rounds its row count; squash the sub-cell difference
            overview = np.array(Image.fromarray(overview).resize((nx, ny), Image.Resampling.BOX))
        arr = overview.astype(float)
        od = np.maximum(0, -np.log10(np.clip(arr / 255.0, 1e-4, 1.0)))
        stain_map = od.sum(axis=-1)

        # A cell is tissue when the registered mask covers enough of it; no clean-up erodes it (SPEC-04 §1).
        cell_tissue_fraction = tissue.fraction_grid(stride_x_um, stride_y_um, nx, ny)
        tissue_mask_overview = cell_tissue_fraction >= triage_cfg.tissue_threshold_pct
        if not tissue_mask_overview.any():
            raise ValueError(f"The registered tissue mask has no heatmap cell with {triage_cfg.tissue_threshold_pct:.0%} tissue on slide {slide_obj.id}.")

        from scipy import ndimage
        dist_from_edge = ndimage.distance_transform_edt(tissue_mask_overview)
        margin_factor = np.clip(dist_from_edge / 2.0, 0.15, 1.0)

        # Normalized histological cellularity across valid tissue
        tissue_coords = [(ix, iy) for iy in range(ny) for ix in range(nx) if tissue_mask_overview[iy, ix]]
        stain_vals = [float(stain_map[iy, ix]) for (ix, iy) in tissue_coords]
        p10 = float(np.percentile(stain_vals, 10))
        p90 = float(np.percentile(stain_vals, 90))
        norm_cellularity = np.clip((stain_map - p10) / max(p90 - p10, 1e-4), 0.0, 1.0)

        # 2. Smart Scout: High-resolution, cellularity-guided non-overlapping patch sampling
        max_sample_patches = triage_cfg.max_sample_patches
        tile_um = triage_cfg.patch_size_px * triage_cfg.mpp_target

        # Strictly non-overlapping tiles of the registered grid that are tissue enough (exact mask area)
        candidate_slots = []
        for tile in tissue.tiles(tile_um, triage_cfg.tissue_threshold_pct):
            cx_um = tile.x_um + tile_um / 2
            cy_um = tile.y_um + tile_um / 2
            ix = min(nx - 1, max(0, int(cx_um / stride_x_um)))
            iy = min(ny - 1, max(0, int(cy_um / stride_y_um)))
            candidate_slots.append({
                "c": tile.col,
                "r": tile.row,
                "x_um": tile.x_um,
                "y_um": tile.y_um,
                "ix": ix,
                "iy": iy,
                "score": float(stain_map[iy, ix])
            })

        # Smart Scout Selection: Prioritize high-cellularity tumor nests while maintaining slide coverage
        if len(candidate_slots) <= max_sample_patches:
            selected_slots = candidate_slots
        else:
            candidate_slots.sort(key=lambda s: s["score"], reverse=True)
            # 80% budget for highest cellularity (dense tumor/epithelial regions)
            n_cellular = int(round(0.80 * max_sample_patches))
            dense_slots = candidate_slots[:n_cellular]
            dense_keys = set((s["c"], s["r"]) for s in dense_slots)

            # 20% budget spread evenly across remaining tissue (stroma/margins/background)
            remaining_slots = [s for s in candidate_slots if (s["c"], s["r"]) not in dense_keys]
            n_context = max_sample_patches - len(dense_slots)
            if remaining_slots and n_context > 0:
                step_ctx = max(1, len(remaining_slots) // n_context)
                context_slots = remaining_slots[::step_ctx][:n_context]
            else:
                context_slots = []

            selected_slots = dense_slots + context_slots

        if not selected_slots:
            raise ValueError(f"No {tile_um:g} µm tissue tiles fit on slide {slide_obj.id}; nothing to embed.")
        print(f"[Triage Smart Scout] Selected {len(selected_slots)} non-overlapping patches (from {len(candidate_slots)} tissue slots, max budget {max_sample_patches})")

        # Extract strictly non-overlapping tiles
        tile_ids = [f"t_{s['c']}_{s['r']}" for s in selected_slots]
        sampled_cells = [(s["ix"], s["iy"]) for s in selected_slots]
        tiles = [
            _image_input(read_region_at_mpp(reader, s["x_um"], s["y_um"], tile_um, tile_um, triage_cfg.mpp_target))
            for s in selected_slots
        ]

        # 3. Embed the tiles in batches within the embedding model's request limits
        limits = registry.models[embed_key].limits
        embeddings = []
        tiles_sent = 0
        for n_batch, batch in enumerate(plan_batches([len(t.data) for t in tiles], limits.max_batch, limits.max_request_bytes)):
            result = gateway.invoke(
                Task.PF_EMBED,
                embed_key,
                ModelInputs(images=tuple(tiles[i] for i in batch)),
                ctx,
                EntityRef(EntityType.TILE_BATCH, f"tb_{n_batch:04d}", ids=tuple(tile_ids[i] for i in batch)),
                EmbeddingBatch,
            )
            batch_embeddings = result.output.as_array()
            if batch_embeddings.shape[0] != len(batch):
                raise ValueError(f"{embed_key} returned {batch_embeddings.shape[0]} embeddings for {len(batch)} tiles")
            embeddings.append(batch_embeddings)
            if not result.cache_hit:
                tiles_sent += len(batch)
        embeddings = np.vstack(embeddings)

        # 4. Predict tumour probabilities with the registry's classifier over the embeddings
        scores = gateway.invoke(
            Task.TUMOR_HEAD,
            tumor_key,
            ModelInputs(features=l2_normalize(embeddings), features_producer=embed_key),
            ctx,
            EntityRef(EntityType.TILE_BATCH, "tiles", ids=tuple(tile_ids)),
            ClassProbabilities,
        )
        raw_probs = scores.output.column(TUMOR_CLASS)
        print(f"[Triage Worker] Embeddings shape: {embeddings.shape}, Mean Tumor Prob: {float(np.mean(raw_probs)):.3f}")

        # 5. Build 2D probability grid [ny, nx] by fusing probe predictions with cellularity and margin depth
        prob_grid = np.full((ny, nx), np.nan, dtype=np.float32)

        for k, (ix, iy) in enumerate(sampled_cells):
            base_prob = float(raw_probs[k])
            cell_score = float(norm_cellularity[iy, ix])
            m_factor = float(margin_factor[iy, ix])
            fused_prob = (0.35 * base_prob + 0.65 * cell_score) * (0.40 + 0.60 * m_factor) * 1.25
            prob_grid[iy, ix] = float(np.clip(fused_prob, 0.05, 0.98))

        unsampled_tissue = [(ix, iy) for (ix, iy) in tissue_coords if np.isnan(prob_grid[iy, ix])]
        if unsampled_tissue:
            from scipy.spatial import KDTree
            kdtree = KDTree(sampled_cells)
            k_val = min(3, len(sampled_cells))
            dists, nn_indices = kdtree.query(unsampled_tissue, k=k_val)
            if k_val == 1 or dists.ndim == 1:
                dists = dists[:, np.newaxis]
                nn_indices = nn_indices[:, np.newaxis]
            weights = 1.0 / np.maximum(dists, 1.0)
            weights /= np.sum(weights, axis=1, keepdims=True)
            for idx, (ux, uy) in enumerate(unsampled_tissue):
                interp_p = float(np.sum(weights[idx] * raw_probs[nn_indices[idx]]))
                cell_score = float(norm_cellularity[uy, ux])
                m_factor = float(margin_factor[uy, ux])
                fused_interp = (0.35 * interp_p + 0.65 * cell_score) * (0.40 + 0.60 * m_factor) * 1.25
                prob_grid[uy, ux] = float(np.clip(fused_interp, 0.05, 0.98))

        # Extract candidate hotspot ROIs for the referee
        candidate_cfg = triage_cfg.hotspot_extraction.model_dump()
        candidate_cfg["max_hotspots"] = referee_cfg.candidates
        raw_candidates = extract_hotspots(
            prob_grid=prob_grid,
            grid_origin_um=grid_origin_um,
            stride_um=(stride_x_um, stride_y_um),
            cfg=candidate_cfg,
            slide_dimensions_um=(width_um, height_um)
        )

        # VLM check of each candidate (SPEC-05 §5.4 arm). A failure fails the stage unless
        # configs/fallbacks.yaml allows it in a clinical run; then the candidate is unverified.
        # The persisted stain transform; None when the slide's fit is degenerate. A model that must see
        # normalised colour cannot be run without it.
        stain = _stain_transform(session, slide_obj.id, od_beta)
        if referee_cfg.color == "normalized" and stain is None:
            raise DegenerateStainProfileError(
                f"the tumour referee is configured for normalized colour but slide {slide_obj.id}'s stain fit is degenerate"
            )
        verified_candidates = []
        for n_cand, cand in enumerate(raw_candidates):
            poly = np.array(cand["polygon_um"])
            crop = _centered_region(reader, float(poly[:, 0].mean()), float(poly[:, 1].mean()), referee_cfg.field_um, referee_cfg.size_px)
            if referee_cfg.color == "normalized":
                crop = normalize_region(crop, stain)
            result = gateway.invoke_or_fallback(
                Task.TUMOR_REFEREE,
                referee_cfg.producer,
                ModelInputs(images=(_image_input(crop),), prompt_id=referee_cfg.prompt),
                ctx,
                EntityRef(EntityType.HOTSPOT, f"cand_{n_cand + 1:02d}"),
                TumorVerdict,
            )
            cand_copy = dict(cand)
            if isinstance(result, FallbackResult):
                cand_copy["referee"] = {
                    "producer_id": referee_cfg.producer,
                    "record_id": str(result.record_id),
                    "tumor_present": None,
                    "needs_human": True,
                    "error_class": type(result.error).__name__,
                }
            else:
                cand_copy["referee"] = {
                    "producer_id": result.producer_id,
                    "record_id": str(result.record_id),
                    "tumor_present": result.output.tumor_present,
                    "lesion_type": result.output.lesion_type,
                    "rationale": result.output.rationale,
                    "needs_human": False,
                }
            verified_candidates.append(cand_copy)

        # Prioritize confirmed tumor regions, ranking by prob_mean descending
        confirmed_tumors = [c for c in verified_candidates if c["referee"]["tumor_present"] is True]
        confirmed_tumors.sort(key=lambda c: c["prob_mean"], reverse=True)

        unconfirmed = [c for c in verified_candidates if c["referee"]["tumor_present"] is not True]
        unconfirmed.sort(key=lambda c: c["prob_mean"], reverse=True)

        max_hotspots = triage_cfg.hotspot_extraction.max_hotspots
        selected = confirmed_tumors[:max_hotspots]
        if len(selected) < max_hotspots and unconfirmed:
            needed = max_hotspots - len(selected)
            selected.extend(unconfirmed[:needed])

        # Finalize top hotspots with clean IDs
        hotspots = []
        for idx, item in enumerate(selected):
            item_copy = dict(item)
            item_copy["id"] = f"hs_{idx + 1:02d}"
            hotspots.append(item_copy)

        # Render Viridis heatmap overlay PNG
        heatmap_png_path = os.path.join(scratch_dir, "heatmap_triage.png")
        render_viridis_heatmap_png(prob_grid, heatmap_png_path)
        with open(heatmap_png_path, "rb") as hf:
            heatmap_bytes = hf.read()
        upload_blob_from_bytes(
            settings.GCS_ARTIFACTS_BUCKET,
            f"cases/{case_id}/triage/heatmap_triage.png",
            heatmap_bytes,
            "image/png"
        )

        # Save & upload prob_grid.npy
        prob_grid_path = os.path.join(scratch_dir, "prob_grid.npy")
        np.save(prob_grid_path, prob_grid)
        with open(prob_grid_path, "rb") as pgf:
            upload_blob_from_bytes(
                settings.GCS_ARTIFACTS_BUCKET,
                f"cases/{case_id}/triage/prob_grid.npy",
                pgf.read(),
                "application/octet-stream"
            )

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

        model_versions = {key: registry.version_of(key) for key in (embed_key, tumor_key, referee_cfg.producer)}
        wall_time_s = round(time.time() - start_time, 2)
        unit_price = config.pricing.path_foundation.unit_price_per_1k_patches
        estimated_usd = round((tiles_sent / 1000.0) * unit_price, 4)

        output_result = {
            "heatmap_png_uri": f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/triage/heatmap_triage.png",
            "heatmap_direct_url": get_gcs_artifact_direct_url(f"cases/{case_id}/triage/heatmap_triage.png"),
            "prob_grid_uri": f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/triage/prob_grid.npy",
            "grid": {
                "origin_um": list(grid_origin_um),
                "stride_um": [float(stride_x_um), float(stride_y_um)],
                "nx": nx,
                "ny": ny
            },
            "hotspots": hotspots,
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
