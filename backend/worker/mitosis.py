"""
Stage 4 worker: mitosis candidate detection, VLM adjudication and virtual HPF selection.

Both models run through the gateway (SPEC-01 §3.4): the detector sweeps 1024 px tiles over
the confirmed hotspots in its 512 px input patches, and the configured VLM adjudicates every
candidate with a strict MitosisVerdict. Each call is a DecisionRecord, and each detection's
label names the producer that decided it. A detector or referee failure fails the stage
unless configs/fallbacks.yaml allows the referee's in a clinical run; then the candidate
stays unreviewed and needs a human (SPEC-01 §3.6, §3.9).

Every read goes through read_region_at_mpp: detector tiles are resampled to the detector's
resolution (a 20x scan is upsampled and reported as such), tissue comes from the registered
mask and colour from the slide's persisted stain profile (SPEC-04).
"""
import os
import io
import json
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Tuple
import numpy as np
from PIL import Image
from shapely.geometry import box
from sqlalchemy import select, delete, not_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import (
    parse_gcs_uri,
    upload_blob_from_bytes,
    download_blob_to_filename,
    resolve_slide_raw_uri
)
from app.core.stain_profiles import usable_stain_transform
from app.core.tasks import EntityType, Task
from app.core.tissue_mask_store import load_tissue_mask
from app.inference.gateway import EntityRef, FallbackResult, ModelInputs
from app.inference.schemas import MitosisVerdict
from app.models.case import Case
from app.models.slide import Slide
from app.models.hotspot import Hotspot
from app.models.detection import Detection
from app.models.hpf_site import HpfSite
from app.models.audit import AuditEvent
from pipeline.detect import apply_global_nms, hotspot_geometry, hotspot_region_um
from pipeline.mitosis_detect import detect_region, make_detect_batch
from pipeline.errors import DegenerateStainProfileError, SlideReadError
from pipeline.verify import mitosis_referee_images
from pipeline.hpf import generate_mitosis_density_map, greedy_place_hpfs
from pipeline.scoring import calculate_hpf_mitosis_counts, compute_nottingham_mitotic_score
from pipeline.slide_io import SlideReader, centered_origin_um, normalize_region, read_region_at_mpp, require_mpp
from pipeline.stain import StainTransform
from worker.runtime import StageRuntime

# Detection label for each referee verdict. EQUIVOCAL is never counted (SPEC-06 §5.6).
LABEL_FOR_VERDICT = {
    "MITOTIC_FIGURE": "mitosis",
    "NOT_MITOTIC_FIGURE": "not_mitosis",
    "EQUIVOCAL": "unreviewed",
}
# Concurrent gateway calls for the tile sweep and the referee, as v5 ran them.
MODEL_CALL_THREADS = 4


def _png(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG")
    return buf.getvalue()


def _stain_transform(db: Session, slide_id, od_beta: float) -> StainTransform | None:
    """The slide's persisted stain transform, or None when its fit is degenerate (colour cannot be normalised)."""
    return usable_stain_transform(db, slide_id, od_beta=od_beta)  # StainProfileMissingError: run preprocess again


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
    require_mpp(slide_obj)
    if not slide_obj.width_px or not slide_obj.height_px:
        raise ValueError(f"Slide {slide_id} has no pixel dimensions; ingest must record them before mitosis.")

    mpp_x = float(slide_obj.mpp_x)
    mpp_y = float(slide_obj.mpp_y)
    width_px = int(slide_obj.width_px)
    height_px = int(slide_obj.height_px)

    tile_um = det_cfg.tile_size_um
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

    slide_dimensions_um = (float(width_px * mpp_x), float(height_px * mpp_y))
    od_beta = config.specimen_profiles.for_type(case_obj.specimen_type).stain_fit.od_beta
    tissue = load_tissue_mask(case_id)  # TissueMaskMissingError: run preprocess again
    print(f"[Worker:Mitosis] Loaded registered tissue mask ({tissue.width_px}x{tissue.height_px} px at {tissue.mpp} um/px, {tissue.area_mm2:.1f} mm2)")

    scratch_dir = tempfile.mkdtemp(prefix="og_mitosis_")
    reader = None

    try:

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
        except OSError as exc:
            raise SlideReadError(f"could not download slide {gcs_uri_original} for case {case_id}: {exc}") from exc
        reader = SlideReader.from_slide_row(local_slide_path, slide_obj)

        # The persisted stain transform; None when the slide's fit is degenerate. A model that must see
        # normalised colour cannot be run without it.
        stain = _stain_transform(db, slide_obj.id, od_beta)
        if referee_cfg.color == "normalized" and stain is None:
            raise DegenerateStainProfileError(
                f"the mitosis referee is configured for normalized colour but slide {slide_id}'s stain fit is degenerate"
            )

        # Stage A (SPEC-06 §5.1-5.2): each hotspot's region in detector-sized tiles at the detector's
        # resolution (SlideReader resamples; a 20x scan is upsampled and the output says so), with
        # ownership; raw candidates down to min_prob, thresholded here.
        detect_batch = make_detect_batch(gateway, ctx, det_cfg, detector_entry)

        def read_tile(x_um, y_um):
            return read_region_at_mpp(reader, x_um, y_um, tile_um, tile_um, det_cfg.mpp).rgb

        stage_a = []
        for hs in hotspots:
            geometry = hotspot_geometry(hs["polygon_um"])

            def include_tile(x0_um, y0_um, x1_um, y1_um, geometry=geometry):
                if not geometry.intersects(box(x0_um, y0_um, x1_um, y1_um)):
                    return False
                (fraction,) = tissue.fractions_of_boxes_um(
                    np.array([x0_um]), np.array([y0_um]), np.array([x1_um]), np.array([y1_um])
                )
                return fraction >= det_cfg.min_tissue_fraction

            points = detect_region(
                read_tile,
                hotspot_region_um(geometry, det_cfg.region_margin_um, tissue.extent_um),
                det_cfg,
                detect_batch,
                batch_size=detector_entry.limits.max_batch,
                threads=MODEL_CALL_THREADS,
                include_tile=include_tile,
                tile_prefix=hs["id"],
            )
            stage_a += [(hs["id"], p) for p in points]
        print(f"[Worker:Mitosis] Stage A: {len(stage_a)} raw candidates >= {det_cfg.min_prob} across {len(hotspots)} hotspots.")

        # Raw Stage-A candidates are kept so thresholds can be swept offline (SPEC-06 §5.2).
        upload_blob_from_bytes(
            settings.GCS_ARTIFACTS_BUCKET,
            f"cases/{case_id}/mitosis/stage_a.json",
            json.dumps({
                "detector": det_cfg.producer,
                "detector_version": detector_entry.version,
                "weights_sha256": detector_entry.weights_sha256,
                "min_prob": det_cfg.min_prob,
                "points": [
                    {"hotspot_id": hs_id, "x_um": p.x_um, "y_um": p.y_um, "prob": p.prob,
                     "tile_id": p.tile_id, "record_id": p.record_id}
                    for hs_id, p in stage_a
                ],
            }).encode("utf-8"),
            "application/json",
        )

        raw_candidates = []
        for hs_id, point in stage_a:
            if point.prob < det_cfg.det_threshold:
                continue
            raw_candidates.append({
                "id": f"m_{len(raw_candidates) + 1:04d}",
                "hotspot_id": hs_id,
                "centroid_um": [point.x_um, point.y_um],
                "det_conf": point.prob,
                "det_record_id": point.record_id,
                "ver_conf": None,
                "label": "unreviewed",
                "label_source": "model"
            })

        # Cross-tile Global Physical NMS
        candidates = apply_global_nms(raw_candidates, nms_radius_um=det_cfg.nms_radius_um)
        print(f"[Worker:Mitosis] Detected {len(raw_candidates)} candidates -> {len(candidates)} after {det_cfg.nms_radius_um}um NMS.")

        # Referee inputs are read up front; the model calls then run concurrently.
        for cand in candidates:
            cx_um, cy_um = cand["centroid_um"]
            cand["_referee_images"] = mitosis_referee_images(reader, cx_um, cy_um, referee_cfg, stain)
            cand["crop_uri"] = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/mitosis/crops/{cand['id']}.png"
            cand["crop_orig_uri"] = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/mitosis/crops/{cand['id']}_orig.png"

        def _adjudicate(cand):
            result = gateway.invoke_or_fallback(
                Task.MITOSIS_REFEREE,
                referee_cfg.producer,
                ModelInputs(
                    images=(cand["_referee_images"].focus, cand["_referee_images"].context), prompt_id=referee_cfg.prompt
                ),
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

        if referee_cfg.enabled:
            print(f"[Worker:Mitosis] Adjudicating {len(candidates)} candidates via {referee_cfg.producer} with {MODEL_CALL_THREADS} worker threads...")
            with ThreadPoolExecutor(max_workers=MODEL_CALL_THREADS) as pool:
                list(pool.map(_adjudicate, candidates))
        else:
            # SPEC-06 arm A1: the detector at det_threshold decides; no VLM call is made.
            for cand in candidates:
                cand.update({
                    "label": "mitosis", "label_source": f"detector:{det_cfg.producer}", "needs_human": False,
                    "referee_record_id": None, "vlm": None,
                    "medgemma_verdict": None, "medgemma_rationale": None, "medgemma_confidence": None,
                })

        # Post-referee physical NMS to eliminate any residual coinciding/overlapping detections
        candidates = apply_global_nms(candidates, nms_radius_um=det_cfg.nms_radius_um)
        print(f"[Worker:Mitosis] Retained {len(candidates)} spatially distinct candidates after refereeing and {det_cfg.nms_radius_um}um NMS.")

        # Upload each candidate's focus crop: what the referee saw, and as scanned
        def _upload_single_crop(c_item):
            images = c_item.pop("_referee_images")
            for suffix, data in (("", images.focus.data), ("_orig", images.focus_raw_png)):
                upload_blob_from_bytes(
                    settings.GCS_ARTIFACTS_BUCKET,
                    f"cases/{case_id}/mitosis/crops/{c_item['id']}{suffix}.png",
                    data,
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
            tissue=tissue,
            slide_dimensions_um=slide_dimensions_um,
            min_tissue_coverage=hpf_cfg.min_tissue_coverage,
            hotspot_priorities=hotspot_prios
        )

        # Pre-render and upload all HPF review images (10x, 20x, 40x, as scanned and normalised) to GCS.
        # A degenerate stain fit leaves no normalised variant; it is never replaced by the raw image.
        hpf_uploads = []
        review_px = hpf_cfg.review_px

        # Reticle optical patch calibration: the viewer canvas shows the HPF radius as a reticle, so the
        # review image spans hpf.review_field_um (canvas width * radius / reticle radius).
        for hpf in hpfs:
            hpf_seq = hpf["seq"]
            h_cx_um, h_cy_um = hpf["center_um"]
            field_um = hpf_cfg.review_field_um
            x_um, y_um = centered_origin_um(reader, h_cx_um, h_cy_um, field_um, field_um)
            review = read_region_at_mpp(reader, x_um, y_um, field_um, field_um, field_um / review_px)

            variants = {"orig": review}
            if stain is not None:
                variants["norm"] = normalize_region(review, stain)

            # Generate the multi-resolution hierarchy (40x, 20x, 10x) in memory
            for mag_name, target_dim in (("40x", review_px), ("20x", review_px // 2), ("10x", review_px // 4)):
                for stain_name, region in variants.items():
                    image = Image.fromarray(region.rgb)
                    if target_dim != review_px:
                        image = image.resize((target_dim, target_dim), Image.Resampling.BILINEAR)
                    buf = io.BytesIO()
                    if mag_name == "40x":
                        image.save(buf, "JPEG", quality=94)
                    else:
                        image.save(buf, "PNG")
                    hpf_uploads.append((f"cases/{case_id}/mitosis/hpfs/hpf_{hpf_seq}_{mag_name}_{stain_name}.png", buf.getvalue()))

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
            scoring=mitosis_cfg.scoring,
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

        producers = (det_cfg.producer, referee_cfg.producer) if referee_cfg.enabled else (det_cfg.producer,)
        model_versions = {key: registry.version_of(key) for key in producers}

        output_payload = {
            "case_id": case_id,
            "stage_execution_id": str(stage_exec.id),
            "candidates": all_candidates,
            "hpfs": hpfs,
            "summary": scoring_summary,
            "grid": grid_meta,
            "stain_normalization": "unavailable" if stain is None else "available",
            # The 20x/40x slice (SPEC-00 R6): a slide coarser than the detector's resolution is upsampled.
            "native_mpp": reader.native_mpp,
            "detector_upsampled": reader.native_mpp > det_cfg.mpp * (1 + detector_entry.input.mpp_tolerance),
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
        if reader is not None:
            reader.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
