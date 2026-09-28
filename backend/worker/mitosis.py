"""
Stage 4 worker: mitosis candidate detection, VLM adjudication and virtual HPF selection.

Both models run through the gateway (SPEC-01 §3.4): the detector sweeps 1024 px tiles over
the confirmed hotspots in its 512 px input patches, and the configured VLM adjudicates every
candidate with a strict MitosisVerdict. Each call is a DecisionRecord, and each detection's
label names the producer that decided it. A detector or referee failure fails the stage
unless configs/fallbacks.yaml allows the referee's in a clinical run; then the candidate
stays unreviewed and needs a human (SPEC-01 §3.6, §3.9).
"""
import os
import io
import json
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Tuple
import numpy as np
import openslide
from PIL import Image
from sqlalchemy import select, delete, not_
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
from app.inference.batching import plan_batches
from app.inference.gateway import EntityRef, FallbackResult, ImageInput, InputSpec, ModelInputs
from app.inference.outputs import DetectionList
from app.inference.schemas import MitosisVerdict
from app.models.case import Case
from app.models.slide import Slide
from app.models.hotspot import Hotspot
from app.models.detection import Detection
from app.models.hpf_site import HpfSite
from app.models.audit import AuditEvent
from pipeline.detect import apply_global_nms, enumerate_hotspot_tiles
from pipeline.errors import SlideReadError
from pipeline.verify import mitosis_referee_images
from pipeline.hpf import generate_mitosis_density_map, greedy_place_hpfs
from pipeline.scoring import calculate_hpf_mitosis_counts, compute_nottingham_mitotic_score
from worker.runtime import StageRuntime

# Detection label for each referee verdict. EQUIVOCAL is never counted (SPEC-06 §5.6).
LABEL_FOR_VERDICT = {
    "MITOTIC_FIGURE": "mitosis",
    "NOT_MITOTIC_FIGURE": "not_mitosis",
    "EQUIVOCAL": "unreviewed",
}
# Concurrent gateway calls for the tile sweep and the referee, as v5 ran them.
MODEL_CALL_THREADS = 4


def _read_rgb(openslide_slide, location, size) -> Image.Image:
    try:
        with OPENSLIDE_GLOBAL_LOCK:
            return openslide_slide.read_region(location, 0, size).convert("RGB")
    except openslide.OpenSlideError as exc:
        raise SlideReadError(f"could not read {size} px at {location}: {exc}") from exc


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def run_mitosis(stage_exec: Any, db: Session, runtime: StageRuntime) -> Tuple[str, Dict[str, str]]:
    """
    Executes Stage 4 (Mitosis Detection & Virtual HPF Selection).
    """
    config = runtime.config
    mitosis_cfg = config.mitosis
    det_cfg, referee_cfg, hpf_cfg = mitosis_cfg.detector, mitosis_cfg.referee, mitosis_cfg.hpf
    registry = config.models
    gateway, ctx = runtime.gateway, runtime.ctx
    detector_entry = registry.models[det_cfg.producer]

    raw_case_id = stage_exec.case_id
    case_id = str(raw_case_id)
    print(f"[Worker:Mitosis] Starting Stage 4 for case {case_id}...")

    case_obj = db.get(Case, raw_case_id)
    if not case_obj:
        raise ValueError(f"Case {case_id} not found in database.")

    stmt = select(Slide).where(Slide.case_id == case_obj.id).limit(1)
    slide_obj = db.scalars(stmt).first()
    if not slide_obj:
        raise ValueError(f"No slide found for case {case_id}")

    slide_id = str(slide_obj.id)
    if not getattr(slide_obj, "mpp_x", None) or slide_obj.mpp_x <= 0 or not getattr(slide_obj, "mpp_y", None) or slide_obj.mpp_y <= 0:
        raise ValueError(f"Slide {slide_id} is missing valid MPP (status='needs_mpp'). Cannot execute mitosis stage.")
    if not slide_obj.width_px or not slide_obj.height_px:
        raise ValueError(f"Slide {slide_id} has no pixel dimensions; ingest must record them before mitosis.")

    mpp_x = float(slide_obj.mpp_x)
    mpp_y = float(slide_obj.mpp_y)
    width_px = int(slide_obj.width_px)
    height_px = int(slide_obj.height_px)

    tile_size_px = det_cfg.tile_size_px
    stride_px = det_cfg.stride_px
    patch_px = detector_entry.input.size_px[0]
    radius_um = hpf_cfg.radius_um
    hpf_count = hpf_cfg.count

    # Hotspots come from the pathologist-confirmed triage in the database only (SPEC-06 §9).
    hotspot_rows = db.scalars(
        select(Hotspot).where(
            Hotspot.case_id == case_obj.id,
            Hotspot.excluded == False
        )
    ).all()
    hotspots = [
        {
            "id": r.id,
            "polygon_um": r.polygon_um,
            "area_mm2": r.area_mm2,
            "prob_mean": r.prob_mean,
            "prob_max": r.prob_max,
            "source": r.source
        }
        for r in hotspot_rows
    ]
    if not hotspots:
        raise ValueError(
            f"No confirmed tumor hotspots found for case {case_id}. "
            "Stage 3 Triage must be confirmed by a pathologist before running Stage 4 Mitosis detection."
        )

    scratch_dir = tempfile.mkdtemp(prefix="og_mitosis_")
    openslide_slide = None

    try:

        # Download preprocess tissue mask from GCS
        slide_dimensions_um = (float(width_px * mpp_x), float(height_px * mpp_y))
        tissue_mask = None
        try:
            mask_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/preprocess/tissue_mask.png")
            mask_img = Image.open(io.BytesIO(mask_bytes)).convert("L")
            tissue_mask = np.array(mask_img) > 10
            print(f"[Worker:Mitosis] Successfully loaded preprocess tissue mask ({tissue_mask.shape[1]}x{tissue_mask.shape[0]}, {tissue_mask.sum()} tissue px)")
        except Exception as tme:
            print(f"[Worker:Mitosis Note] Could not load preprocess tissue_mask from GCS: {tme}")

        # Prioritize confirmed hotspots by tumor cellularity & tissue density (prob_mean descending)
        hotspots.sort(key=lambda h: (h.get("prob_mean") or 0.0), reverse=True)
        print(f"[Worker:Mitosis] Prioritized {len(hotspots)} confirmed hotspots by cellular density: {[h['id'] for h in hotspots]}")

        # Download raw slide from GCS to transient scratch file for tile & crop sampling
        gcs_uri_original = resolve_slide_raw_uri(case_id, slide_obj) or slide_obj.gcs_uri_original or f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
        raw_bucket_name, blob_name = parse_gcs_uri(gcs_uri_original)
        ext = os.path.splitext(blob_name)[1] or ".svs"
        local_slide_path = os.path.join(scratch_dir, f"slide{ext}")

        try:
            download_blob_to_filename(raw_bucket_name, blob_name, local_slide_path)
            with OPENSLIDE_GLOBAL_LOCK:
                openslide_slide = openslide.OpenSlide(local_slide_path)
        except (openslide.OpenSlideError, OSError) as exc:
            raise SlideReadError(f"could not open slide {gcs_uri_original} for case {case_id}: {exc}") from exc

        # Fallback to OpenSlide thumbnail tissue mask if GCS mask was not found
        if tissue_mask is None:
            try:
                with OPENSLIDE_GLOBAL_LOCK:
                    thumb = openslide_slide.get_thumbnail((512, 512)).convert("RGB")
                arr = np.array(thumb).astype(float)
                r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
                tissue_mask = ~((r > 215) & (g > 215) & (b > 215))
                print(f"[Worker:Mitosis] Generated fallback tissue mask from OpenSlide thumbnail ({tissue_mask.sum()} tissue px)")
            except Exception as te:
                print(f"[Worker:Mitosis Note] Thumbnail fallback note: {te}")

        # Enumerate all candidate tiles across confirmed hotspots, skipping empty glass
        all_tiles_to_sweep = []
        for hs in hotspots:
            hs_tiles = enumerate_hotspot_tiles(
                hs["polygon_um"],
                tile_size_px=tile_size_px,
                mpp=mpp_x,
                stride_px=stride_px,
                tissue_mask=tissue_mask,
                slide_dimensions_um=slide_dimensions_um,
                min_tissue_ratio=0.20
            )
            for t in hs_tiles:
                t["hotspot_id"] = hs["id"]
            all_tiles_to_sweep.extend(hs_tiles)

        print(f"[Worker:Mitosis] Sweeping {len(all_tiles_to_sweep)} tiles across {len(hotspots)} hotspots with {MODEL_CALL_THREADS} workers...")

        # Tiles are read at the slide's own resolution; the gateway refuses one outside the
        # detector's input contract (SPEC-01 AC5) instead of sending it unresampled.
        patch_spec = InputSpec(mpp=mpp_x, size_px=(patch_px, patch_px), color="raw", format="png")
        limits = detector_entry.limits

        def _sweep_single_tile(n_tile_and_tile):
            n_tile, tile = n_tile_and_tile
            tx_um, ty_um = tile["origin_um"]
            tx_px, ty_px = tile["origin_px"]
            tile_img = _read_rgb(openslide_slide, (tx_px, ty_px), (tile_size_px, tile_size_px))
            offsets = [(ox, oy) for oy in range(0, tile_size_px, patch_px) for ox in range(0, tile_size_px, patch_px)]
            patches = [
                ImageInput(_png(tile_img.crop((ox, oy, ox + patch_px, oy + patch_px))), patch_spec)
                for ox, oy in offsets
            ]
            patch_ids = [f"t{n_tile:04d}_{ox}_{oy}" for ox, oy in offsets]
            found = []
            for batch in plan_batches([len(p.data) for p in patches], limits.max_batch, limits.max_request_bytes):
                result = gateway.invoke(
                    Task.MITOSIS_DETECT,
                    det_cfg.producer,
                    ModelInputs(images=tuple(patches[i] for i in batch)),
                    ctx,
                    EntityRef(EntityType.TILE_BATCH, f"{patch_ids[batch[0]]}_b{len(batch)}", ids=tuple(patch_ids[i] for i in batch)),
                    DetectionList,
                    params={"min_prob": det_cfg.det_threshold},
                )
                if len(result.output.detections) != len(batch):
                    raise ValueError(f"{det_cfg.producer} returned {len(result.output.detections)} point lists for {len(batch)} patches")
                for i, points in zip(batch, result.output.detections):
                    ox, oy = offsets[i]
                    for point in points:
                        if point.prob < det_cfg.det_threshold:
                            continue
                        found.append((
                            tx_um + (ox + point.x) * mpp_x,
                            ty_um + (oy + point.y) * mpp_y,
                            float(point.prob),
                            tile["hotspot_id"],
                            str(result.record_id),
                        ))
            return found

        with ThreadPoolExecutor(max_workers=MODEL_CALL_THREADS) as pool:
            sweep_results = list(pool.map(_sweep_single_tile, enumerate(all_tiles_to_sweep)))

        raw_candidates = []
        cand_seq = 1
        for tile_cands in sweep_results:
            for cand_cx_um, cand_cy_um, det_conf, hs_id, record_id in tile_cands:
                raw_candidates.append({
                    "id": f"m_{cand_seq:04d}",
                    "hotspot_id": hs_id,
                    "centroid_um": [float(cand_cx_um), float(cand_cy_um)],
                    "det_conf": float(det_conf),
                    "det_record_id": record_id,
                    "ver_conf": None,
                    "label": "unreviewed",
                    "label_source": "model"
                })
                cand_seq += 1

        # Cross-tile Global Physical NMS
        candidates = apply_global_nms(raw_candidates, nms_radius_um=det_cfg.nms_radius_um)
        print(f"[Worker:Mitosis] Detected {len(raw_candidates)} candidates -> {len(candidates)} after {det_cfg.nms_radius_um}um NMS.")

        # Referee inputs are read sequentially under the OpenSlide lock; the calls run concurrently.
        for cand in candidates:
            cx_um, cy_um = cand["centroid_um"]
            focus, context = mitosis_referee_images(
                openslide_slide, int(cx_um / mpp_x), int(cy_um / mpp_y), mpp_x,
                referee_cfg.focus_px, referee_cfg.context_um, referee_cfg.context_px,
            )
            cand["_referee_images"] = (focus, context)
            cand["crop_uri"] = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/mitosis/crops/{cand['id']}.png"
            cand["crop_orig_uri"] = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/mitosis/crops/{cand['id']}_orig.png"

        def _adjudicate(cand):
            result = gateway.invoke_or_fallback(
                Task.MITOSIS_REFEREE,
                referee_cfg.producer,
                ModelInputs(images=cand["_referee_images"], prompt_id=referee_cfg.prompt),
                ctx,
                EntityRef(EntityType.CANDIDATE, cand["id"]),
                MitosisVerdict,
            )
            cand["referee_record_id"] = str(result.record_id)
            if isinstance(result, FallbackResult):
                cand["label"] = "unreviewed"
                cand["label_source"] = "referee_unavailable"
                cand["needs_human"] = True
                cand["vlm"] = None
                cand["medgemma_verdict"] = None
                cand["medgemma_rationale"] = None
            else:
                verdict = result.output
                cand["label"] = LABEL_FOR_VERDICT[verdict.verdict]
                cand["label_source"] = f"referee:{result.producer_id}"
                cand["needs_human"] = False
                cand["vlm"] = {**verdict.model_dump(mode="json"), "rule_override": False}
                cand["medgemma_verdict"] = verdict.verdict
                cand["medgemma_rationale"] = verdict.rationale
            cand["medgemma_confidence"] = None

        if candidates:
            print(f"[Worker:Mitosis] Adjudicating {len(candidates)} candidates via {referee_cfg.producer} with {MODEL_CALL_THREADS} worker threads...")
            with ThreadPoolExecutor(max_workers=MODEL_CALL_THREADS) as pool:
                list(pool.map(_adjudicate, candidates))

        # Post-referee physical NMS to eliminate any residual coinciding/overlapping detections
        candidates = apply_global_nms(candidates, nms_radius_um=det_cfg.nms_radius_um)
        print(f"[Worker:Mitosis] Retained {len(candidates)} spatially distinct candidates after refereeing and {det_cfg.nms_radius_um}um NMS.")

        # Upload each candidate's focus crop
        def _upload_single_crop(c_item):
            focus, _ = c_item.pop("_referee_images")
            for suffix in ("", "_orig"):
                upload_blob_from_bytes(
                    settings.GCS_ARTIFACTS_BUCKET,
                    f"cases/{case_id}/mitosis/crops/{c_item['id']}{suffix}.png",
                    focus.data,
                    "image/png"
                )

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(_upload_single_crop, candidates))

        # Compute bounding box for density map
        all_xs = [c["centroid_um"][0] for c in candidates] or [0.0, float(width_px * mpp_x)]
        all_ys = [c["centroid_um"][1] for c in candidates] or [0.0, float(height_px * mpp_y)]
        bbox_um = (min(all_xs), min(all_ys), max(all_xs), max(all_ys))

        # Spatial FFT Density Convolution
        density_map, grid_meta = generate_mitosis_density_map(
            candidates,
            bounding_box_um=bbox_um,
            grid_res_um=hpf_cfg.density_grid_res_um,
            radius_um=radius_um
        )

        # Greedy 10-HPF Placement with Overlap Relaxation Fallback & Strict Tissue Density Gating
        hotspot_polys = [h["polygon_um"] for h in hotspots]
        hotspot_prios = [float(h.get("prob_mean") or 0.0) for h in hotspots]
        hpfs = greedy_place_hpfs(
            density_map,
            grid_meta,
            hotspot_polygons_um=hotspot_polys,
            count=hpf_count,
            radius_um=radius_um,
            min_separation_um=hpf_cfg.min_separation_um,
            relaxed_min_separation_um=hpf_cfg.relaxed_min_separation_um,
            tissue_mask=tissue_mask,
            slide_dimensions_um=slide_dimensions_um,
            min_tissue_coverage=hpf_cfg.min_tissue_coverage,
            hotspot_priorities=hotspot_prios
        )

        # Pre-render and upload all 10 HPF patch variants (10x, 20x, 40x @ norm/orig) to GCS
        stain_normalizer = None
        try:
            from pipeline.stain import PureNumpyMacenkoNormalizer
            sp_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/preprocess/stain_params.json")
            sp_data = json.loads(sp_bytes.decode("utf-8"))
            if "stain_matrix" in sp_data and "max_concentrations" in sp_data:
                stain_normalizer = PureNumpyMacenkoNormalizer()
                stain_normalizer.stain_matrix_target = np.array(sp_data["stain_matrix"], dtype=float)
                stain_normalizer.max_conc_target = np.array(sp_data["max_concentrations"], dtype=float)
        except Exception as se:
            print(f"[Worker:Mitosis Note] Failed to load stain normalizer: {se}")

        hpf_uploads = []
        dim_w, dim_h = openslide_slide.dimensions

        # Reticle optical patch calibration:
        # HPF radius is 262.0 um. The viewer displays a 520x520 px canvas with reticle radius = 236 px.
        # For candidate pins (px = 260 + dx/radius * 236) to perfectly match the underlying patch imagery:
        # 40x patch field width must be 520 * (262.0 / 236.0) = 577.29 um!
        for hpf in hpfs:
            hpf_seq = hpf["seq"]
            h_cx_um, h_cy_um = hpf["center_um"]
            h_cx_px = int(h_cx_um / mpp_x)
            h_cy_px = int(h_cy_um / mpp_y)

            field_um = 577.29 # Standard HPF review field (r=262 um -> width=577.29 um)
            crop_w_px = max(1, int(round(field_um / mpp_x)))
            crop_h_px = max(1, int(round(field_um / mpp_y)))

            x0 = max(0, min(dim_w - crop_w_px, h_cx_px - crop_w_px // 2))
            y0 = max(0, min(dim_h - crop_h_px, h_cy_px - crop_h_px // 2))
            patch_orig_raw = _read_rgb(openslide_slide, (x0, y0), (crop_w_px, crop_h_px))

            # 40x base patch (2048x2048, 0.28 um/px)
            patch_40x_orig = patch_orig_raw.resize((2048, 2048), Image.Resampling.BILINEAR) if patch_orig_raw.size != (2048, 2048) else patch_orig_raw

            # Stain normalize 40x ONCE (downsampled versions inherit normalized palette)
            patch_40x_norm = patch_40x_orig
            if stain_normalizer:
                try:
                    norm_arr = stain_normalizer.transform(np.array(patch_40x_orig))
                    patch_40x_norm = Image.fromarray(norm_arr)
                except Exception:
                    patch_40x_norm = patch_40x_orig

            # Generate multi-resolution hierarchy (40x, 20x, 10x) efficiently in memory
            for mag_name, target_dim in (("40x", 2048), ("20x", 1024), ("10x", 512)):
                if target_dim == 2048:
                    p_orig = patch_40x_orig
                    p_norm = patch_40x_norm
                else:
                    p_orig = patch_40x_orig.resize((target_dim, target_dim), Image.Resampling.BILINEAR)
                    p_norm = patch_40x_norm.resize((target_dim, target_dim), Image.Resampling.BILINEAR)

                buf_o = io.BytesIO()
                if mag_name == "40x":
                    p_orig.save(buf_o, "JPEG", quality=94)
                else:
                    p_orig.save(buf_o, "PNG")
                orig_bytes = buf_o.getvalue()

                buf_n = io.BytesIO()
                if mag_name == "40x":
                    p_norm.save(buf_n, "JPEG", quality=94)
                else:
                    p_norm.save(buf_n, "PNG")
                norm_bytes = buf_n.getvalue()

                hpf_uploads.append((f"cases/{case_id}/mitosis/hpfs/hpf_{hpf_seq}_{mag_name}_orig.png", orig_bytes))
                hpf_uploads.append((f"cases/{case_id}/mitosis/hpfs/hpf_{hpf_seq}_{mag_name}_norm.png", norm_bytes))

        def _upload_hpf_item(item):
            b_path, b_data = item
            upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, b_path, b_data, "image/png")

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(_upload_hpf_item, hpf_uploads))

        # Fetch existing pathologist detections before re-populating to preserve reviews & additions (#464)
        existing_pathologist_dets = list(
            db.scalars(
                select(Detection).where(
                    Detection.case_id == case_obj.id,
                    (Detection.label_source == "pathologist") | (Detection.label_source.startswith("pathologist"))
                )
            ).all()
        )
        existing_pathologist_ids = {d.id for d in existing_pathologist_dets}

        # Combine model candidates and preserved pathologist detections
        all_candidates = []
        for cand in candidates:
            if cand["id"] not in existing_pathologist_ids:
                all_candidates.append(cand)
        for pd in existing_pathologist_dets:
            all_candidates.append({
                "id": pd.id,
                "case_id": str(case_obj.id),
                "hotspot_id": pd.hotspot_id,
                "centroid_um": pd.centroid_um,
                "det_conf": pd.det_conf,
                "ver_conf": pd.ver_conf,
                "label": pd.label,
                "label_source": pd.label_source,
                "crop_uri": pd.crop_uri,
                "crop_orig_uri": pd.crop_orig_uri
            })

        # Calculate HPF Mitotic Containment Counts
        hpfs, total_mitoses_in_hpfs = calculate_hpf_mitosis_counts(all_candidates, hpfs)

        # Calculate Nottingham Mitotic Score
        scoring_summary = compute_nottingham_mitotic_score(
            count_total=total_mitoses_in_hpfs,
            n_hpf=len(hpfs),
            radius_um=radius_um,
            config_dict={"scoring": mitosis_cfg.scoring.model_dump(mode="json")},
        )

        # Persist to Database: strictly preserve all pathologist annotations, delete previous model/referee detections (#464)
        db.execute(
            delete(Detection).where(
                Detection.case_id == case_obj.id,
                Detection.label_source != "pathologist",
                not_(Detection.label_source.startswith("pathologist"))
            )
        )
        db.execute(delete(HpfSite).where(HpfSite.case_id == case_obj.id))

        for cand in candidates:
            if cand["id"] in existing_pathologist_ids:
                continue
            det_row = Detection(
                id=cand["id"],
                case_id=case_obj.id,
                hotspot_id=cand.get("hotspot_id"),
                centroid_um=cand["centroid_um"],
                det_conf=cand.get("det_conf"),
                ver_conf=cand.get("ver_conf"),
                label=cand["label"],
                label_source=cand["label_source"],
                medgemma_verdict=cand.get("medgemma_verdict"),
                medgemma_rationale=cand.get("medgemma_rationale"),
                medgemma_confidence=cand.get("medgemma_confidence"),
                crop_uri=cand.get("crop_uri"),
                crop_orig_uri=cand.get("crop_orig_uri")
            )
            db.add(det_row)

        for hpf in hpfs:
            hpf_row = HpfSite(
                case_id=case_obj.id,
                seq=hpf["seq"],
                center_um=hpf["center_um"],
                radius_um=hpf["radius_um"],
                mitotic_count=hpf["count"],
                source=hpf.get("source", "model"),
                image_patch_uri=None
            )
            db.add(hpf_row)

        model_versions = {key: registry.version_of(key) for key in (det_cfg.producer, referee_cfg.producer)}

        output_payload = {
            "case_id": case_id,
            "stage_execution_id": str(stage_exec.id),
            "candidates": all_candidates,
            "hpfs": hpfs,
            "summary": scoring_summary,
            "grid": grid_meta,
            "model_versions": model_versions
        }

        # Upload output.json directly to GCS artifacts bucket
        upload_blob_from_bytes(
            settings.GCS_ARTIFACTS_BUCKET,
            f"cases/{case_id}/mitosis/output.json",
            json.dumps(output_payload, indent=2).encode("utf-8"),
            "application/json"
        )

        output_uri = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/mitosis/output.json"
        stage_exec.status = "awaiting_review"
        stage_exec.output_ref = output_uri
        stage_exec.model_versions = model_versions

        # Record Audit Event
        audit = AuditEvent(
            case_id=case_id,
            actor="system",
            event_type="stage_output",
            stage="mitosis",
            payload={
                "candidates_count": len(candidates),
                "hpfs_count": len(hpfs),
                "summary": scoring_summary
            }
        )
        db.add(audit)
        db.commit()

        print(f"[Worker:Mitosis] Completed Stage 4 for case {case_id}: {len(candidates)} candidates, {len(hpfs)} HPFs, Mitotic Score {scoring_summary['mitotic_score']} ({scoring_summary['per_mm2']} mitoses/mm²).")

        return output_uri, model_versions

    finally:
        if openslide_slide is not None:
            with OPENSLIDE_GLOBAL_LOCK:
                openslide_slide.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
