"""
Stage 5 Worker Handler (Nottingham Histologic Grading).

Samples 10x evidence patches from the confirmed Stage 3 hotspots, has the configured VLM
estimate tubule formation and nuclear pleomorphism per patch and the histologic type over
the first patches (all through the model gateway, one DecisionRecord per decision), and
aggregates the grade in deterministic code.

A failed estimate is never replaced by a value (SPEC-01 §3.9). It fails the stage unless
configs/fallbacks.yaml allows it in a clinical run; then that patch or the type has no
estimate and the grading needs a human.
"""

import os
import io
import json
import math
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple, Optional
import numpy as np
import openslide
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import (
    parse_gcs_uri,
    upload_blob_from_bytes,
    download_blob_as_bytes,
    download_blob_to_filename,
    resolve_slide_raw_uri
)
from app.core.openslide_lock import OPENSLIDE_GLOBAL_LOCK
from app.core.tasks import EntityType, Task
from app.inference.gateway import EntityRef, FallbackResult, ImageInput, InputSpec, ModelInputs
from app.inference.schemas import HistotypeVerdict, PleoEstimate, TubuleEstimate
from app.models.case import Case
from app.models.stage_execution import StageExecution
from app.models.hotspot import Hotspot
from app.models.hpf_site import HpfSite
from app.models.detection import Detection
from app.models.grading import Grading
from app.models.audit import AuditEvent
from pipeline.errors import SlideReadError
from pipeline.stain import MacenkoNormalizer
from pipeline.grading import (
    aggregate_grading_findings,
    calculate_mitotic_score_from_detections_and_hpfs,
    calculate_mitotic_score_from_hpfs,
    calculate_tubule_score,
)
from worker.runtime import StageRuntime

# Concurrent VLM calls, as v5 limited them.
ESTIMATOR_THREADS = 4


def extract_10x_patch(
    slide_obj,
    center_x: int,
    center_y: int,
    patch_size_px: int = 512,
    target_mpp: float = 1.0,
    base_mpp: float | None = None
) -> Image.Image:
    """
    Extract 512x512 patch @ 1.0 um/pixel (10x magnification) centered at (center_x, center_y).
    """
    if base_mpp is None or base_mpp <= 0:
        raise ValueError(f"Valid positive base_mpp is required for patch extraction, got: {base_mpp}")
    downsample = target_mpp / base_mpp  # e.g., 1.0 / 0.25 = 4.0
    crop_w_l0 = int(patch_size_px * downsample)
    crop_h_l0 = int(patch_size_px * downsample)

    dims = slide_obj.dimensions
    top_left_x = max(0, min(dims[0] - crop_w_l0, int(center_x - crop_w_l0 / 2)))
    top_left_y = max(0, min(dims[1] - crop_h_l0, int(center_y - crop_h_l0 / 2)))

    try:
        with OPENSLIDE_GLOBAL_LOCK:
            rgba = slide_obj.read_region((top_left_x, top_left_y), 0, (crop_w_l0, crop_h_l0))
            rgb = rgba.convert("RGB")
    except openslide.OpenSlideError as exc:
        raise SlideReadError(f"could not read the patch at ({top_left_x}, {top_left_y}): {exc}") from exc

    if rgb.size != (patch_size_px, patch_size_px):
        rgb = rgb.resize((patch_size_px, patch_size_px), Image.Resampling.LANCZOS)

    return rgb


def select_max_density_hotspot_patches(
    hotspots: List[Any],
    tissue_mask: np.ndarray,
    slide_dims_um: Tuple[float, float],
    base_mpp: float,
    case_id: str,
    n_patches: int = 24,
    patch_size_um: float = 512.0,
    min_dist_um: float = 384.0,
    min_density: float = 0.50,
    checksum_sha256: Optional[str] = None,
    mpp_y: Optional[float] = None
) -> List[Dict[str, Any]]:
    """
    Selects n_patches (24) 10x evidence patches ensuring:
    1. Patches are taken from within or directly adjacent to confirmed Stage 3 hotspots.
    2. The patch with maximum tissue density within each hotspot is chosen first (preventing lumina/empty voids).
    3. Additional non-overlapping high-density sites inside hotspots or on invasive tumor margins are selected
       until exactly n_patches are obtained.
    4. Deterministic sampling is seeded by slide checksum (Issue #145).
    5. Centroid is calculated via true polygon area centroid and supports anisotropic mpp_y (Issue #765).
    """
    from scipy.ndimage import uniform_filter
    from shapely.geometry import Polygon, Point

    eff_mpp_y = mpp_y if mpp_y and mpp_y > 0 else base_mpp

    seed_int = int(checksum_sha256[:8], 16) if checksum_sha256 and checksum_sha256 != "default_checksum" else 42
    rng = np.random.default_rng(seed_int)

    H_m, W_m = tissue_mask.shape
    s_x = W_m / max(slide_dims_um[0], 1.0)
    s_y = H_m / max(slide_dims_um[1], 1.0)
    k_x = max(3, int(round(patch_size_um * s_x)))
    k_y = max(3, int(round(patch_size_um * s_y)))

    density_map = uniform_filter(tissue_mask.astype(np.float32), size=(k_y, k_x), mode='constant', cval=0.0)

    selected = []
    selected_coords = []

    def is_too_close(x, y, radius=min_dist_um):
        for cx, cy in selected_coords:
            if np.hypot(x - cx, y - cy) < radius:
                return True
        return False

    hs_data = []
    all_internal_cands = []

    if not hotspots:
        raise ValueError(f"No confirmed tumor hotspots provided for patch selection in case {case_id}.")

    for hs in hotspots:
        poly_raw = getattr(hs, "polygon_um", None) or (hs.get("polygon_um") if isinstance(hs, dict) else None)
        if not poly_raw:
            continue
        poly_arr = np.array(poly_raw)
        if len(poly_arr) < 3:
            continue
            
        poly_geom = Polygon(poly_arr).buffer(0)
        prob = float(getattr(hs, "prob_mean", None) or (hs.get("prob_mean") if isinstance(hs, dict) else None) or getattr(hs, "tumor_probability", 0.85) or 0.85)
        hs_id = getattr(hs, "id", None) or (hs.get("id") if isinstance(hs, dict) else "hs")

        min_x, min_y, max_x, max_y = poly_geom.bounds
        step_um = 64.0
        gx = np.arange(min_x, max_x, step_um)
        gy = np.arange(min_y, max_y, step_um)

        cand_points = []
        for x in gx:
            for y in gy:
                if poly_geom.contains(Point(x, y)):
                    pmx = int(np.clip(round(x * s_x), 0, W_m - 1))
                    pmy = int(np.clip(round(y * s_y), 0, H_m - 1))
                    d = float(density_map[pmy, pmx])
                    cand_points.append((x, y, d))
                    all_internal_cands.append((x, y, d, hs_id, prob, "hotspot_subregion"))

        if not cand_points:
            if poly_geom.is_valid and not poly_geom.is_empty:
                centroid = poly_geom.centroid
                cx_um = float(centroid.x)
                cy_um = float(centroid.y)
            else:
                cx_um = float(poly_arr[:, 0].mean())
                cy_um = float(poly_arr[:, 1].mean())
            pmx = int(np.clip(round(cx_um * s_x), 0, W_m - 1))
            pmy = int(np.clip(round(cy_um * s_y), 0, H_m - 1))
            d = float(density_map[pmy, pmx])
            cand_points.append((cx_um, cy_um, d))
            all_internal_cands.append((cx_um, cy_um, d, hs_id, prob, "hotspot_subregion"))

        cand_points.sort(key=lambda item: item[2], reverse=True)
        hs_data.append({
            "id": hs_id,
            "geom": poly_geom,
            "prob": prob,
            "cands": cand_points
        })

    # Phase 1: Peak point of EVERY hotspot (sorted by prob descending)
    hs_data.sort(key=lambda h: h["prob"], reverse=True)
    for h in hs_data:
        for x, y, d in h["cands"]:
            if not is_too_close(x, y):
                selected.append({
                    "hotspot_id": h["id"],
                    "center_um": [round(float(x), 2), round(float(y), 2)],
                    "center_x_px": int(round(x / base_mpp)),
                    "center_y_px": int(round(y / eff_mpp_y)),
                    "tissue_density": round(d, 4),
                    "tumor_probability": round(h["prob"], 4),
                    "source": "hotspot_peak"
                })
                selected_coords.append((x, y))
                break

    # Phase 2: High-density points inside hotspots, prioritized by density
    all_internal_cands.sort(key=lambda it: (it[2] >= min_density, it[2], it[4]), reverse=True)
    for x, y, d, hs_id, prob, src in all_internal_cands:
        if len(selected) >= n_patches:
            break
        if d >= min_density and not is_too_close(x, y):
            selected.append({
                "hotspot_id": hs_id,
                "center_um": [round(float(x), 2), round(float(y), 2)],
                "center_x_px": int(round(x / base_mpp)),
                "center_y_px": int(round(y / eff_mpp_y)),
                "tissue_density": round(d, 4),
                "tumor_probability": round(prob, 4),
                "source": src
            })
            selected_coords.append((x, y))

    # Phase 3: Immediate hotspot perimeter margin if still needed
    if len(selected) < n_patches:
        margin_cands = []
        for h in hs_data:
            margin_geom = h["geom"].buffer(350.0).difference(h["geom"])
            min_x, min_y, max_x, max_y = margin_geom.bounds
            gx = np.arange(min_x, max_x, 80.0)
            gy = np.arange(min_y, max_y, 80.0)
            for x in gx:
                for y in gy:
                    if margin_geom.contains(Point(x, y)):
                        pmx = int(np.clip(round(x * s_x), 0, W_m - 1))
                        pmy = int(np.clip(round(y * s_y), 0, H_m - 1))
                        d = float(density_map[pmy, pmx])
                        margin_cands.append((x, y, d, h["id"], h["prob"]))
        
        margin_cands.sort(key=lambda it: (it[2] >= min_density, it[2]), reverse=True)
        for x, y, d, hs_id, prob in margin_cands:
            if len(selected) >= n_patches:
                break
            if not is_too_close(x, y):
                selected.append({
                    "hotspot_id": hs_id,
                    "center_um": [round(float(x), 2), round(float(y), 2)],
                    "center_x_px": int(round(x / base_mpp)),
                    "center_y_px": int(round(y / eff_mpp_y)),
                    "tissue_density": round(d, 4),
                    "tumor_probability": round(prob * 0.95, 4),
                    "source": "hotspot_margin"
                })
                selected_coords.append((x, y))

    # Fallback if still under n_patches: relax distance threshold
    if len(selected) < n_patches:
        for x, y, d, hs_id, prob, src in all_internal_cands:
            if len(selected) >= n_patches:
                break
            if not is_too_close(x, y, radius=min_dist_um * 0.6):
                selected.append({
                    "hotspot_id": hs_id,
                    "center_um": [round(float(x), 2), round(float(y), 2)],
                    "center_x_px": int(round(x / base_mpp)),
                    "center_y_px": int(round(y / eff_mpp_y)),
                    "tissue_density": round(d, 4),
                    "tumor_probability": round(prob, 4),
                    "source": src
                })
                selected_coords.append((x, y))

    if not selected:
        raise ValueError(f"No valid tumor tissue patches could be sampled from confirmed hotspots for case {case_id}.")

    for idx, p in enumerate(selected[:n_patches]):
        p["id"] = f"p_{idx+1:03d}"
        p["index"] = idx + 1
        p["image_filename"] = f"p_{idx+1:03d}.png"
        p["image_url"] = f"/api/v1/stages/grading/{case_id}/patches/p_{idx+1:03d}/image"

    return selected[:n_patches]


def run_grading(stage_exec: StageExecution, db: Session, runtime: StageRuntime) -> Tuple[str, Dict[str, Any]]:
    """
    Main Stage 5 Grading Worker Execution.
    """
    config = runtime.config
    scoring = config.scoring
    mitotic_scoring = config.mitosis.scoring
    sampling = scoring.grading
    estimators = sampling.estimators
    gateway, ctx = runtime.gateway, runtime.ctx

    case_id = str(stage_exec.case_id)
    print(f"[Worker Stage 5: Grading] Commencing Nottingham grading pipeline for case {case_id}...")

    case = db.get(Case, stage_exec.case_id)
    if not case or not case.slides:
        raise ValueError(f"Case {case_id} has no valid slide records.")

    slide = case.slides[0]
    slide_id = str(slide.id)

    # Halt grading stage if MPP is missing per PRD 01-stage-v4.0 §2.3 step 4
    if not slide or not getattr(slide, "mpp_x", None) or slide.mpp_x <= 0 or not getattr(slide, "mpp_y", None) or slide.mpp_y <= 0:
        raise ValueError(f"Slide for case {case_id} is missing valid MPP (status='needs_mpp'). Cannot execute grading stage.")
    base_mpp = float(slide.mpp_x)

    # 1. Confirmed Stage 3 hotspots, from the database only (fail fast before downloading the slide)
    stmt_hotspots = select(Hotspot).where(Hotspot.case_id == case.id).order_by(Hotspot.prob_mean.desc())
    hotspots = [h for h in db.scalars(stmt_hotspots).all() if not h.excluded]
    if not hotspots:
        raise ValueError(f"No confirmed tumor hotspots available for case {case_id}. Cannot execute Nottingham grading stage.")

    # Mitotic component from the confirmed Stage 4 output: detections counted once across HPFs.
    hpf_sites = list(db.scalars(select(HpfSite).where(HpfSite.case_id == case.id).order_by(HpfSite.seq.asc())).all())
    if not hpf_sites:
        raise ValueError(f"Case {case_id} has no Stage 4 HPFs. Confirm the mitosis stage before grading.")
    confirmed_dets = list(db.scalars(
        select(Detection).where(Detection.case_id == case.id, Detection.label == "mitosis")
    ).all())
    if confirmed_dets:
        cands_for_score = [{"id": d.id, "centroid_um": d.centroid_um, "label": "mitosis"} for d in confirmed_dets]
        hpfs_for_score = [{"seq": h.seq, "center_um": h.center_um, "radius_um": h.radius_um, "count": 0} for h in hpf_sites]
        total_mitoses, mitotic_score = calculate_mitotic_score_from_detections_and_hpfs(
            cands_for_score, hpfs_for_score, mitotic_scoring
        )
    else:
        total_mitoses, mitotic_score = calculate_mitotic_score_from_hpfs(
            [h.mitotic_count for h in hpf_sites], mitotic_scoring, radius_um=hpf_sites[0].radius_um
        )

    n_patches = sampling.n_patches
    patch_size_px = sampling.patch_size_px
    resolution_um = sampling.resolution_um
    patch_size_um = patch_size_px * resolution_um

    scratch_dir = tempfile.mkdtemp(prefix="og_grading_")
    slide_obj = None

    try:
        gcs_uri_original = resolve_slide_raw_uri(case_id, slide) or slide.gcs_uri_original or f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
        raw_bucket_name, blob_name = parse_gcs_uri(gcs_uri_original)
        ext = os.path.splitext(blob_name)[1] or ".svs"
        local_slide_path = os.path.join(scratch_dir, f"slide{ext}")

        # 2. Open Slide and Prepare Tissue Mask
        try:
            download_blob_to_filename(raw_bucket_name, blob_name, local_slide_path)
            with OPENSLIDE_GLOBAL_LOCK:
                slide_obj = openslide.OpenSlide(local_slide_path)
        except (openslide.OpenSlideError, OSError) as exc:
            raise SlideReadError(f"could not open slide {gcs_uri_original} for case {case_id}: {exc}") from exc

        slide_w, slide_h = slide_obj.dimensions
        slide_dims_um = (float(slide_w * base_mpp), float(slide_h * float(slide.mpp_y)))

        tissue_mask = None
        try:
            mask_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/preprocess/tissue_mask.png")
            mask_img = Image.open(io.BytesIO(mask_bytes)).convert("L")
            tissue_mask = np.array(mask_img) > 10
            print(f"[Worker Stage 5: Grading] Loaded preprocess tissue mask ({tissue_mask.shape[1]}x{tissue_mask.shape[0]})")
        except Exception as me:
            print(f"[Worker Stage 5: Grading Note] Could not load preprocess tissue_mask from GCS: {me}")

        if tissue_mask is None:
            with OPENSLIDE_GLOBAL_LOCK:
                thumb = slide_obj.get_thumbnail((512, 512)).convert("RGB")
            arr = np.array(thumb).astype(float)
            r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
            tissue_mask = ~((r > 215) & (g > 215) & (b > 215))

        # 3. Maximum-Density Hotspot Patch Selection (Guarantees closest to hotspot & max tissue density)
        candidate_patches = select_max_density_hotspot_patches(
            hotspots=hotspots,
            tissue_mask=tissue_mask,
            slide_dims_um=slide_dims_um,
            base_mpp=base_mpp,
            case_id=case_id,
            n_patches=n_patches,
            patch_size_um=patch_size_um,
            checksum_sha256=getattr(slide, "checksum_sha256", None),
            mpp_y=float(slide.mpp_y) if getattr(slide, "mpp_y", None) else None
        )

        normalizer = MacenkoNormalizer()
        extracted_patches = []
        patch_images = []

        for p_meta in candidate_patches:
            patch_id = p_meta["id"]
            raw_img = extract_10x_patch(
                slide_obj=slide_obj,
                center_x=p_meta["center_x_px"],
                center_y=p_meta["center_y_px"],
                patch_size_px=patch_size_px,
                target_mpp=resolution_um,
                base_mpp=base_mpp
            )

            # Macenko normalization. The spec records whether it happened (M3 replaces this call site).
            color = "normalized"
            try:
                norm_np = normalizer.transform(np.array(raw_img))
            except Exception:
                norm_np = np.array(raw_img)
                color = "raw"
            norm_img = Image.fromarray(norm_np)

            img_buf = io.BytesIO()
            norm_img.save(img_buf, format="PNG")
            img_bytes = img_buf.getvalue()

            # Upload patch directly to GCS artifacts bucket
            upload_blob_from_bytes(
                settings.GCS_ARTIFACTS_BUCKET,
                f"cases/{case_id}/grading_patches/{patch_id}.png",
                img_bytes,
                "image/png"
            )

            patch_images.append(ImageInput(
                img_bytes,
                InputSpec(mpp=resolution_um, size_px=(patch_size_px, patch_size_px), color=color, format="png"),
            ))
            extracted_patches.append({
                "id": patch_id,
                "index": p_meta["index"],
                "hotspot_id": p_meta.get("hotspot_id"),
                "tissue_density": p_meta.get("tissue_density"),
                "source": p_meta.get("source"),
                "center_um": p_meta.get("center_um"),
                "center_x_px": p_meta["center_x_px"],
                "center_y_px": p_meta["center_y_px"],
                "tumor_probability": round(p_meta["tumor_probability"], 4),
                "image_filename": f"{patch_id}.png",
                "image_url": f"/api/v1/stages/grading/{case_id}/patches/{patch_id}/image"
            })

        print(f"[Worker Stage 5: Grading] Successfully extracted and normalized {len(extracted_patches)} evidence patches.")

        # 4. VLM estimates through the gateway: tubule and pleomorphism per patch, type over the first patches.
        def _estimate(job):
            task, prompt_id, images, entity, output_model = job
            return gateway.invoke_or_fallback(
                task, estimators.producer, ModelInputs(images=images, prompt_id=prompt_id), ctx, entity, output_model
            )

        jobs = []
        for image, p in zip(patch_images, extracted_patches):
            entity = EntityRef(EntityType.PATCH, p["id"])
            jobs.append((Task.TUBULE_PATCH, estimators.tubule_prompt, (image,), entity, TubuleEstimate))
            jobs.append((Task.PLEO_FIELD, estimators.pleo_prompt, (image,), EntityRef(EntityType.FIELD, p["id"]), PleoEstimate))
        jobs.append((
            Task.HISTOTYPE,
            estimators.histotype_prompt,
            tuple(patch_images[:estimators.histotype_images]),
            EntityRef(EntityType.SLIDE, slide_id),
            HistotypeVerdict,
        ))
        with ThreadPoolExecutor(max_workers=ESTIMATOR_THREADS) as pool:
            results = list(pool.map(_estimate, jobs))
        type_result = results.pop()
        tubule_results, pleo_results = results[0::2], results[1::2]

        # Map patch-level results. A patch whose estimate fell back has no value and needs review.
        patches_output = []
        failed_patches = []
        for p, t_res, p_res in zip(extracted_patches, tubule_results, pleo_results):
            t_failed, p_failed = isinstance(t_res, FallbackResult), isinstance(p_res, FallbackResult)
            if t_failed or p_failed:
                failed_patches.append(p["id"])
            tubule = {"tubule_percent": None, "tumor_present": None, "score": None, "rationale": None}
            if not t_failed:
                tubule = {
                    "tubule_percent": t_res.output.tubule_percent,
                    "tumor_present": t_res.output.tumor_present,
                    "score": calculate_tubule_score(t_res.output.tubule_percent, scoring),
                    "rationale": t_res.output.rationale,
                }
            tubule.update({"producer_id": estimators.producer, "record_id": str(t_res.record_id)})
            pleo = {"pleomorphism_score": None, "rationale": None}
            if not p_failed:
                pleo = {"pleomorphism_score": p_res.output.pleomorphism_score, "rationale": p_res.output.rationale}
            pleo.update({"producer_id": estimators.producer, "record_id": str(p_res.record_id)})
            patches_output.append({
                "id": p["id"],
                "index": p["index"],
                "hotspot_id": p.get("hotspot_id"),
                "tissue_density": p.get("tissue_density"),
                "source": p.get("source"),
                "center_um": p.get("center_um"),
                "center_x_px": p["center_x_px"],
                "center_y_px": p["center_y_px"],
                "tumor_probability": p["tumor_probability"],
                "image_url": p["image_url"],
                "tubule": tubule,
                "pleo": pleo,
                "review_status": "needs_review" if (t_failed or p_failed) else "suggested"
            })

        if isinstance(type_result, FallbackResult):
            histologic_type = None
            type_output = None
        else:
            histologic_type = type_result.output.type
            type_output = {
                **type_result.output.model_dump(mode="json"),
                "producer_id": estimators.producer,
                "record_id": str(type_result.record_id),
            }

        # Format HPF sites for Stage 5 dual-level review
        hpfs_output = []
        for h in hpf_sites:
            hpf_area_mm2 = math.pi * (h.radius_um / 1000.0) ** 2
            hpfs_output.append({
                "seq": h.seq,
                "center_um": h.center_um,
                "radius_um": h.radius_um,
                "mitotic_count": h.mitotic_count,
                "density_mm2": round(h.mitotic_count / hpf_area_mm2, 1),
                "review_status": "suggested"
            })

        # 5. Deterministic Pure Zero-LLM Aggregation (failed estimates are left out)
        aggregate_res = aggregate_grading_findings(
            tubule_responses=[p["tubule"] for p in patches_output],
            pleo_responses=[p["pleo"] for p in patches_output],
            mitotic_score=mitotic_score,
            cfg=scoring
        )
        needs_human_flag = bool(failed_patches) or histologic_type is None or bool(aggregate_res.get("needs_human"))

        model_versions = {estimators.producer: config.models.version_of(estimators.producer)}

        # 6. Assemble Full Output JSON
        output_payload = {
            "case_id": case_id,
            "slide_id": slide_id,
            "patches": patches_output,
            "hpfs": hpfs_output,
            "evidence": {"morphometry": None},
            "aggregate": aggregate_res,
            "histologic_type": type_output,
            "narrative": None,
            "model_versions": model_versions,
            "needs_human": needs_human_flag,
            "schema_failed_patches": failed_patches,
            "generated_at": datetime.now(timezone.utc).isoformat()
        }

        # Save output artifact directly to GCS
        upload_blob_from_bytes(
            settings.GCS_ARTIFACTS_BUCKET,
            f"cases/{case_id}/grading_output.json",
            json.dumps(output_payload, indent=2).encode("utf-8"),
            "application/json"
        )

        output_uri = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/grading_output.json"

        # 7. Persist into Database gradings table
        stmt_existing = select(Grading).where(Grading.case_id == stage_exec.case_id)
        existing_grading = db.scalars(stmt_existing).first()

        if existing_grading:
            existing_grading.tubule_percent = aggregate_res["tubule_percent"]
            existing_grading.tubule_score = aggregate_res["tubule_score"]
            existing_grading.pleo_score = aggregate_res["pleo_score"]
            existing_grading.mitotic_score = aggregate_res["mitotic_score"]
            existing_grading.nottingham_sum = aggregate_res["nottingham_sum"]
            existing_grading.grade = aggregate_res["grade"]
            existing_grading.histologic_type = histologic_type
            existing_grading.machine = output_payload
            # Issue #143: Re-running grading must clear stale overrides and unconfirm type
            existing_grading.overrides = {}
            existing_grading.type_confirmed_by = "unconfirmed"
        else:
            new_grading = Grading(
                case_id=stage_exec.case_id,
                tubule_percent=aggregate_res["tubule_percent"],
                tubule_score=aggregate_res["tubule_score"],
                pleo_score=aggregate_res["pleo_score"],
                mitotic_score=aggregate_res["mitotic_score"],
                nottingham_sum=aggregate_res["nottingham_sum"],
                grade=aggregate_res["grade"],
                histologic_type=histologic_type,
                type_confirmed_by="unconfirmed",
                machine=output_payload,
                overrides={}
            )
            db.add(new_grading)

        # Record Audit Event
        audit_evt = AuditEvent(
            case_id=str(stage_exec.case_id),
            actor=settings.DEFAULT_MOCK_USER_ID,
            event_type="stage_5_grading_generated",
            stage="grading",
            payload={
                "nottingham_sum": aggregate_res["nottingham_sum"],
                "grade": aggregate_res["grade"],
                "tubule_score": aggregate_res["tubule_score"],
                "pleo_score": aggregate_res["pleo_score"],
                "mitotic_score": aggregate_res["mitotic_score"],
                "total_mitoses": total_mitoses,
                "histologic_type": histologic_type,
                "flags": aggregate_res["flags"],
                "needs_human": needs_human_flag,
                "schema_failed_patches": failed_patches
            }
        )
        db.add(audit_evt)
        db.commit()

        stage_exec.status = "awaiting_review"
        print(f"[Worker Stage 5: Grading] Completed for case {case_id}. Nottingham Grade {aggregate_res['grade']} (Sum {aggregate_res['nottingham_sum']}/9). needs_human={needs_human_flag}.")

        return output_uri, model_versions

    finally:
        if slide_obj is not None:
            with OPENSLIDE_GLOBAL_LOCK:
                slide_obj.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
