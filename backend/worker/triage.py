"""
Triage stage worker handler (v4.2 Hotspot Triage).
Extracts 1.0 µm/px tiles, embeds them with the registry's embedding model and scores them
with its tumour classifier (both through the model gateway), extracts hotspot candidates,
has the configured VLM check them, and renders the viridis heatmap overlay.

Every model call is a DecisionRecord (SPEC-01 §3.3). A slide that cannot be read fails the
stage (SlideReadError); nothing is synthesised in its place (SPEC-01 §3.9).
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
import openslide
from PIL import Image
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import (
    parse_gcs_uri,
    upload_blob_from_bytes,
    download_blob_as_bytes,
    download_blob_to_filename,
    get_gcs_artifact_direct_url,
    resolve_slide_raw_uri
)
from app.core.tasks import EntityType, Task
from app.inference.batching import plan_batches
from app.inference.gateway import EntityRef, FallbackResult, ImageInput, InputSpec, ModelInputs
from app.inference.outputs import ClassProbabilities, EmbeddingBatch
from app.inference.schemas import TumorVerdict
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.audit import AuditEvent
from pipeline.errors import SlideReadError
from pipeline.hotspots import extract_hotspots
from pipeline.probe import l2_normalize
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


def _read_region(os_slide, location: tuple[int, int], size: tuple[int, int]) -> Image.Image:
    try:
        return os_slide.read_region(location, 0, size).convert("RGB")
    except openslide.OpenSlideError as exc:
        raise SlideReadError(f"could not read {size} px at {location}: {exc}") from exc


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def _centered_crop(os_slide, cx_px: int, cy_px: int, crop_w_px: int, crop_h_px: int) -> Image.Image:
    dim_w, dim_h = os_slide.dimensions
    x0 = max(0, min(dim_w - crop_w_px, cx_px - crop_w_px // 2))
    y0 = max(0, min(dim_h - crop_h_px, cy_px - crop_h_px // 2))
    return _read_region(os_slide, (x0, y0), (crop_w_px, crop_h_px))


def run_triage(stage_execution: StageExecution, session: Session, runtime: StageRuntime) -> tuple[str, dict]:
    """
    Triage stage worker handler execution:
    1. Downloads preprocess mask & stain params from GCS.
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

    if not getattr(slide_obj, "mpp_x", None) or slide_obj.mpp_x <= 0 or not getattr(slide_obj, "mpp_y", None) or slide_obj.mpp_y <= 0:
        raise ValueError(f"Slide {slide_obj.id} is missing valid MPP (status='needs_mpp'). Cannot execute triage stage.")
    if not slide_obj.width_px or not slide_obj.height_px:
        raise ValueError(f"Slide {slide_obj.id} has no pixel dimensions; ingest must record them before triage.")

    mpp_x = float(slide_obj.mpp_x)
    mpp_y = float(slide_obj.mpp_y)
    width_px = int(slide_obj.width_px)
    height_px = int(slide_obj.height_px)

    # Compute grid dimensions
    width_um = width_px * mpp_x
    height_um = height_px * mpp_y

    # Issue #86: Define triage overview grid dimensions matching slide aspect ratio
    nx = 80
    ny = max(1, int(round(nx * (height_px / max(width_px, 1)))))

    stride_x_um = (width_px * mpp_x) / nx
    stride_y_um = (height_px * mpp_y) / ny
    grid_origin_um = (0.0, 0.0)

    scratch_dir = tempfile.mkdtemp(prefix="og_triage_")
    os_slide = None

    try:
        # Check for preprocess tissue mask in GCS (Issue #86)
        tissue_mask = None
        try:
            mask_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/preprocess/tissue_mask.png")
            mask_img = Image.open(io.BytesIO(mask_bytes)).convert("L").resize((nx, ny), Image.Resampling.NEAREST)
            tissue_mask = np.array(mask_img) > 10
        except Exception:
            tissue_mask = None

        # 1. Download raw slide from GCS to transient scratch file for patch and thumbnail extraction
        gcs_uri_original = resolve_slide_raw_uri(case_id, slide_obj) or slide_obj.gcs_uri_original or f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
        raw_bucket_name, blob_name = parse_gcs_uri(gcs_uri_original)
        ext = os.path.splitext(blob_name)[1] or ".svs"
        local_slide_path = os.path.join(scratch_dir, f"slide{ext}")

        try:
            download_blob_to_filename(raw_bucket_name, blob_name, local_slide_path)
            os_slide = openslide.OpenSlide(local_slide_path)
        except (openslide.OpenSlideError, OSError) as exc:
            raise SlideReadError(f"could not open slide {gcs_uri_original}: {exc}") from exc

        thumb = os_slide.get_thumbnail((nx, ny)).convert("RGB")
        thumb = thumb.resize((nx, ny), Image.Resampling.BILINEAR)
        arr = np.array(thumb).astype(float)
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        is_glass = (r > 215) & (g > 215) & (b > 215)
        # Issue #86: Prioritize preprocessed QC tissue mask if available
        if tissue_mask is not None and tissue_mask.sum() > 0:
            tissue_mask_overview = tissue_mask & ~is_glass
        else:
            tissue_mask_overview = ~is_glass
        od = np.maximum(0, -np.log10(np.clip(arr / 255.0, 1e-4, 1.0)))
        stain_map = od.sum(axis=-1)

        # Morphological sanitization: remove dust specks, glass borders, and isolated noise
        from scipy import ndimage
        opened_tissue = ndimage.binary_opening(tissue_mask_overview, structure=np.ones((3, 3)))
        lbl_t, n_comp_t = ndimage.label(opened_tissue)
        if n_comp_t > 0:
            comp_sizes = ndimage.sum(tissue_mask_overview, lbl_t, range(1, n_comp_t + 1))
            clean_tissue = np.zeros_like(tissue_mask_overview)
            for c_idx, c_sz in enumerate(comp_sizes, 1):
                if c_sz >= 25:  # Keep coherent tissue structures (biopsy cores / fragments >= 25 cells)
                    clean_tissue[lbl_t == c_idx] = True
            if np.any(clean_tissue):
                tissue_mask_overview = clean_tissue

        # Fallback to center region if mask is still entirely blank
        if tissue_mask_overview.sum() == 0:
            tissue_mask_overview[int(ny*0.2):int(ny*0.8), int(nx*0.2):int(nx*0.8)] = True

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
        tile_px = triage_cfg.patch_size_px
        tile_um = tile_px * triage_cfg.mpp_target
        patch_dim_px = int(round(tile_um / mpp_x))
        # The tile is resampled from patch_dim_px to tile_px, so this is the resolution sent.
        tile_mpp = patch_dim_px * mpp_x / tile_px
        cols = width_px // patch_dim_px
        rows = height_px // patch_dim_px

        # Enumerate strictly non-overlapping patch tile slots across the whole slide
        candidate_slots = []
        for r in range(rows):
            for c in range(cols):
                x0 = c * patch_dim_px
                y0 = r * patch_dim_px
                cx_px = x0 + patch_dim_px // 2
                cy_px = y0 + patch_dim_px // 2
                ix = min(nx - 1, max(0, int(cx_px / (width_px / nx))))
                iy = min(ny - 1, max(0, int(cy_px / (height_px / ny))))

                if tissue_mask_overview[iy, ix]:
                    cellularity_score = float(stain_map[iy, ix])
                    candidate_slots.append({
                        "c": c,
                        "r": r,
                        "x0": x0,
                        "y0": y0,
                        "cx_px": cx_px,
                        "cy_px": cy_px,
                        "ix": ix,
                        "iy": iy,
                        "score": cellularity_score
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
        tile_spec = InputSpec(mpp=tile_mpp, size_px=(tile_px, tile_px), color="raw", format="png")
        tiles = [
            ImageInput(
                _png(_read_region(os_slide, (s["x0"], s["y0"]), (patch_dim_px, patch_dim_px))
                     .resize((tile_px, tile_px), Image.Resampling.BILINEAR)),
                tile_spec,
            )
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
        crop_w_px = max(1, int(round(referee_cfg.field_um / mpp_x)))
        crop_h_px = max(1, int(round(referee_cfg.field_um / mpp_y)))
        crop_spec = InputSpec(
            mpp=referee_cfg.field_um / referee_cfg.size_px,
            size_px=(referee_cfg.size_px, referee_cfg.size_px),
            color="raw",
            format="png",
        )
        verified_candidates = []
        for n_cand, cand in enumerate(raw_candidates):
            poly = np.array(cand["polygon_um"])
            cx_px = int(float(poly[:, 0].mean()) / mpp_x)
            cy_px = int(float(poly[:, 1].mean()) / mpp_y)
            crop = _centered_crop(os_slide, cx_px, cy_px, crop_w_px, crop_h_px).resize(
                (referee_cfg.size_px, referee_cfg.size_px), Image.Resampling.BILINEAR
            )
            result = gateway.invoke_or_fallback(
                Task.TUMOR_REFEREE,
                referee_cfg.producer,
                ModelInputs(images=(ImageInput(_png(crop), crop_spec),), prompt_id=referee_cfg.prompt),
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

        # Stain normalizer for patch extraction
        stain_normalizer = None
        try:
            stain_json_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/preprocess/stain_params.json")
            stain_p = json.loads(stain_json_bytes.decode("utf-8"))
            from pipeline.stain import PureNumpyMacenkoNormalizer
            norm_obj = PureNumpyMacenkoNormalizer()
            norm_obj.stain_matrix_target = np.array(stain_p["stain_matrix"])
            norm_obj.max_conc_target = np.array(stain_p["max_concentrations"])
            stain_normalizer = norm_obj
        except Exception as ne:
            print(f"[Triage Worker Note] Stain normalizer load note: {ne}")

        for hs in hotspots:
            hs_id = hs["id"]
            poly = np.array(hs["polygon_um"])
            cx_um = float(poly[:, 0].mean())
            cy_um = float(poly[:, 1].mean())
            cx_px = int(cx_um / mpp_x)
            cy_px = int(cy_um / mpp_y)

            # Generate all 3 magnification levels (10x: 512um, 20x: 256um, 40x: 128um)
            mag_configs = [
                ("10x", 512.0),
                ("20x", 256.0),
                ("40x", 128.0)
            ]

            for mag_name, field_um in mag_configs:
                field_w_px = max(1, int(round(field_um / mpp_x)))
                field_h_px = max(1, int(round(field_um / mpp_y)))
                patch_orig = _centered_crop(os_slide, cx_px, cy_px, field_w_px, field_h_px).resize(
                    (512, 512), Image.Resampling.BILINEAR
                )

                # Save Orig variant
                upload_blob_from_bytes(
                    settings.GCS_ARTIFACTS_BUCKET,
                    f"cases/{case_id}/triage/patches/{hs_id}_{mag_name}_orig.png",
                    _png(patch_orig),
                    "image/png"
                )

                # Generate Norm variant
                patch_norm = patch_orig
                if stain_normalizer:
                    try:
                        norm_arr = stain_normalizer.transform(np.array(patch_orig))
                        patch_norm = Image.fromarray(norm_arr)
                    except Exception:
                        patch_norm = patch_orig

                norm_bytes = _png(patch_norm)
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

            hs["thumbnail_uri"] = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/triage/patches/{hs_id}_10x_norm.png"
            hs["thumbnail_url"] = get_gcs_artifact_direct_url(f"cases/{case_id}/triage/patches/{hs_id}_10x_norm.png")

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
        if os_slide is not None:
            os_slide.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
