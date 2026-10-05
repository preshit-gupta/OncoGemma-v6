"""
Stage 4 worker: mitosis candidate detection, decision, virtual HPF selection and the count.

The detector runs through the gateway (SPEC-01 §3.4): it sweeps the confirmed hotspots in its
512 px input patches, and every call is a DecisionRecord. The baseline decides (SPEC-06 arm A1,
D17/D19): a candidate at or above ``det_threshold`` is ``final_decision = 'mitosis'`` with
``decision_path = 'A'``. The optional referee (off in production) turns EQUIVOCAL into
``equivocal``, never into ``mitosis``; an allowed referee outage leaves the candidate
``equivocal`` for a pathologist (SPEC-01 §3.6, §3.9). One NMS runs after the decisions, HPFs
come from the counted candidates only, and the count is written as a ``mitosis_count`` record.

Every read goes through read_region_at_mpp: detector tiles are resampled to the detector's
resolution (a 20x scan is upsampled and reported as such), tissue comes from the registered
mask and colour from the slide's persisted stain profile (SPEC-04).
"""
import io
import json
import math
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Tuple
import numpy as np
from PIL import Image
from shapely.geometry import box
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.slide_source import download_slide
from app.core.gcs import (
    upload_blob_from_bytes,
    resolve_slide_raw_uri,
)
from app.core.stain_profiles import usable_stain_transform
from app.core.run_context import RunMode
from app.core.tasks import EntityType, Task
from app.core.tissue_mask_store import load_tissue_mask
from app.inference.gateway import EntityRef, FallbackResult, ModelInputs
from app.inference.records import mitosis_count_record
from app.inference.schemas import MitosisVerdict
from app.models.case import Case
from app.models.slide import Slide
from app.models.hotspot import Hotspot
from app.models.detection import Detection
from app.models.hpf_site import HpfSite
from app.models.audit import AuditEvent
from pipeline.detect import apply_global_nms, candidate_review_crops, hotspot_geometry, hotspot_region_um, review_crop_blob
from pipeline.mitosis_detect import detect_region, make_detect_batch
from pipeline.errors import DegenerateStainProfileError, SlideReadError
from pipeline.verify import mitosis_referee_images
from pipeline.hpf import SiteWithoutCentreError, hpf_seq_of, hpfs_from_sites
from pipeline.mitosis_gate import load_tumor_gate
from pipeline.scoring import is_counted, summarize_stage4
from pipeline.slide_io import SlideReader, centered_origin_um, normalize_region, read_region_at_mpp, require_mpp
from pipeline.stain import StainTransform
from worker.runtime import StageRuntime

# final_decision for each referee verdict. EQUIVOCAL is never counted (SPEC-06 §5.6).
DECISION_FOR_VERDICT = {
    "MITOTIC_FIGURE": "mitosis",
    "NOT_MITOTIC_FIGURE": "not_mitosis",
    "EQUIVOCAL": "equivocal",
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

    # Hotspots come from the pathologist-confirmed triage in the database only (SPEC-06 §9).
    hotspot_rows = db.scalars(
        select(Hotspot).where(
            Hotspot.case_id == case_obj.id,
            Hotspot.excluded == False
        )
    ).all()
    # The HPFs are these sites' circles: the model sites by rank, then the pinned ones in id order.
    hotspots = [
        {
            "id": r.id,
            "polygon_um": r.polygon_um,
            "center_um": r.center_um,
            "area_mm2": r.area_mm2,
            "source": r.source,
            "rank": r.rank,
        }
        for r in sorted(hotspot_rows, key=lambda r: (r.rank is None, r.rank or 0, r.id))
    ]
    for h in hotspots:
        if h["center_um"] is None:  # confirmed before HPF sites: refuse before any model call
            raise SiteWithoutCentreError(f"confirmed hotspot {h['id']!r} has no center_um; run triage again for this case")
    if not hotspots:
        raise ValueError(
            f"No confirmed tumor hotspots found for case {case_id}. "
            "Stage 3 Triage must be confirmed by a pathologist before running Stage 4 Mitosis detection."
        )

    profile = config.specimen_profiles.for_type(case_obj.specimen_type)
    site_cfg = profile.hotspots
    hpf_target = site_cfg.k_max
    od_beta = profile.stain_fit.od_beta
    tissue = load_tissue_mask(case_id)  # TissueMaskMissingError: run preprocess again
    print(f"[Worker:Mitosis] Loaded registered tissue mask ({tissue.width_px}x{tissue.height_px} px at {tissue.mpp} um/px, {tissue.area_mm2:.1f} mm2)")
    # The triage tumour mask: the tumour-cell gate (SPEC-06 §5.5) and the HPFs' tumour fraction (§5.8).
    gate_cfg = mitosis_cfg.tumor_gate
    if not gate_cfg.enabled and ctx.run_mode is RunMode.CLINICAL:
        raise ValueError("mitosis.tumor_gate.enabled is false: the gate may be switched off only in an eval ablation run")
    tumor = load_tumor_gate(case_id, gate_cfg.dilation_tiles)  # TumorMaskMissingError: run triage again

    scratch_dir = tempfile.mkdtemp(prefix="og_mitosis_")
    reader = None

    try:

        print(f"[Worker:Mitosis] {len(hotspots)} confirmed HPF sites: {[h['id'] for h in hotspots]}")

        # Download raw slide from GCS to transient scratch file for tile & crop sampling
        gcs_uri_original = resolve_slide_raw_uri(case_id, slide_obj) or slide_obj.gcs_uri_original or f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"

        try:
            local_slide_path = download_slide(gcs_uri_original, scratch_dir)
        except OSError as exc:
            raise SlideReadError(f"could not download slide {gcs_uri_original} for case {case_id}: {exc}") from exc
        reader = SlideReader.from_slide_row(local_slide_path, slide_obj)

        # The persisted stain transform; None when the slide's fit is degenerate. A model that must see
        # normalised colour cannot be run without it (checked where the referee runs).
        stain = _stain_transform(db, slide_obj.id, od_beta)

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

        # Rows a pathologist decided are kept across re-runs: their labels and added figures (#464).
        preserved = list(db.scalars(
            select(Detection).where(
                Detection.case_id == case_obj.id,
                (Detection.review_label.is_not(None)) | (Detection.decision_path == "human"),
            )
        ).all())
        used_ids = {d.id for d in preserved}
        id_counter = iter(range(1, 10 ** 9))

        def next_id():
            while True:
                cid = f"m_{next(id_counter):04d}"
                if cid not in used_ids:
                    used_ids.add(cid)
                    return cid

        # Decision (SPEC-06 §5.6, arm A1): the detector's probability as returned (no calibration, D19);
        # a candidate at or above det_threshold is a mitosis, the rest stay only in stage_a.json.
        candidates = []
        for hs_id, point in stage_a:
            if point.prob < det_cfg.det_threshold:
                continue
            candidates.append({
                "id": next_id(),
                "hotspot_id": hs_id,
                "centroid_um": [point.x_um, point.y_um],
                "p_a": point.prob,
                "p_b": None,
                "vlm": None,
                "rule_override": False,
                "in_tumor": None,  # set by the tumour-cell gate after NMS
                "final_decision": "mitosis",
                "decision_path": "A",
                "review_label": None,
                "record_ids": [str(point.record_id)],
            })

        if referee_cfg.enabled:
            # Arm A2 (off in production since the MIDOG++ baseline; referee v2 is WP-7.5).
            if referee_cfg.color == "normalized" and stain is None:
                raise DegenerateStainProfileError(
                    f"the mitosis referee is configured for normalized colour but slide {slide_id}'s stain fit is degenerate"
                )
            referee_images = {
                cand["id"]: mitosis_referee_images(reader, *cand["centroid_um"], referee_cfg, stain) for cand in candidates
            }

            def _adjudicate(cand):
                images = referee_images[cand["id"]]
                result = gateway.invoke_or_fallback(
                    Task.MITOSIS_REFEREE,
                    referee_cfg.producer,
                    ModelInputs(images=(images.focus, images.context), prompt_id=referee_cfg.prompt),
                    ctx,
                    EntityRef(EntityType.CANDIDATE, cand["id"]),
                    MitosisVerdict,
                )
                cand["record_ids"].append(str(result.record_id))
                if isinstance(result, FallbackResult):
                    # An allowed referee outage leaves the candidate for a pathologist (SPEC-01 §3.6).
                    cand["final_decision"] = "equivocal"
                else:
                    cand["final_decision"] = DECISION_FOR_VERDICT[result.output.verdict]
                    cand["vlm"] = {**result.output.model_dump(mode="json"), "rule_override": False}

            print(f"[Worker:Mitosis] Adjudicating {len(candidates)} candidates via {referee_cfg.producer} with {MODEL_CALL_THREADS} worker threads...")
            with ThreadPoolExecutor(max_workers=MODEL_CALL_THREADS) as pool:
                list(pool.map(_adjudicate, candidates))

        # One NMS, after the decisions, by p_b ?? p_a (SPEC-06 §5.7). A figure a pathologist already
        # decided stands for its neighbourhood, so a new candidate next to it is suppressed as well.
        n_decided = len(candidates)
        candidates = apply_global_nms(candidates, nms_radius_um=det_cfg.nms_radius_um)
        candidates = [
            c for c in candidates
            if all(math.dist(c["centroid_um"], d.centroid_um) >= det_cfg.nms_radius_um for d in preserved)
        ]
        print(f"[Worker:Mitosis] {n_decided} candidates >= {det_cfg.det_threshold} -> {len(candidates)} after {det_cfg.nms_radius_um}um NMS.")

        # Tumour-cell gate (SPEC-06 §5.5) on every candidate, the kept pathologist rows included; a
        # pathologist's review_label still decides those. In an eval ablation in_tumor stays null.
        for cand in candidates:
            cand["in_tumor"] = tumor.in_tumor(*cand["centroid_um"]) if gate_cfg.enabled else None
        for d in preserved:
            d.in_tumor = tumor.in_tumor(*d.centroid_um) if gate_cfg.enabled else None
        print(f"[Worker:Mitosis] Tumour gate: {sum(1 for c in candidates if c['in_tumor'])} of {len(candidates)} candidates in tumour.")

        # Review images of every persisted candidate, raw colour (contract mitosis_v6).
        crop_uploads = []
        for cand in candidates:
            crops = candidate_review_crops(reader, *cand["centroid_um"], mitosis_cfg.review_crops)
            crop_uploads += [(review_crop_blob(case_id, cand["id"], "crop"), crops.crop_png),
                             (review_crop_blob(case_id, cand["id"], "context"), crops.context_png)]

        def _upload_png(item):
            upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, item[0], item[1], "image/png")

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(_upload_png, crop_uploads))

        all_candidates = candidates + [
            {
                "id": d.id, "hotspot_id": d.hotspot_id, "centroid_um": d.centroid_um, "p_a": d.p_a, "p_b": d.p_b,
                "vlm": d.vlm, "rule_override": d.rule_override, "in_tumor": d.in_tumor,
                "final_decision": d.final_decision, "decision_path": d.decision_path,
                "review_label": d.review_label, "record_ids": d.record_ids,
            }
            for d in preserved
        ]
        for cand in all_candidates:
            cand["counted"] = is_counted(cand["review_label"], cand["final_decision"], cand["in_tumor"])

        # HPFs: the circles of the confirmed sites, where they are (SPEC-06 §5.8, D22). A candidate
        # carries the circle that contains it; one in a frame's padding only has none.
        hpfs = hpfs_from_sites(hotspots, all_candidates, tissue=tissue, tumor=tumor, diameter_um=site_cfg.hpf_diameter_um)
        hpfs, scoring_summary = summarize_stage4(
            all_candidates, hpfs, scoring=mitosis_cfg.scoring, hpf_count=hpf_target
        )
        for cand in all_candidates:
            cand["hpf_seq"] = hpf_seq_of(cand["centroid_um"], hpfs)

        # Pre-render and upload all HPF review images (10x, 20x, 40x, as scanned and normalised) to GCS.
        # A degenerate stain fit leaves no normalised variant; it is never replaced by the raw image.
        hpf_uploads = []
        review_px = hpf_cfg.review_px

        # The review image covers the site's frame (circle plus padding), centred on the circle.
        for hpf in hpfs:
            hpf_seq = hpf["seq"]
            h_cx_um, h_cy_um = hpf["center_um"]
            field_um = site_cfg.frame_um
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

        # Persist: pathologist-decided rows stay; the previous model rows and HPFs are replaced.
        db.execute(
            delete(Detection).where(
                Detection.case_id == case_obj.id,
                Detection.review_label.is_(None),
                Detection.decision_path != "human",
            )
        )
        db.execute(delete(HpfSite).where(HpfSite.case_id == case_obj.id))

        for cand in candidates:
            db.add(Detection(
                id=cand["id"],
                case_id=case_obj.id,
                hotspot_id=cand["hotspot_id"],
                centroid_um=cand["centroid_um"],
                p_a=cand["p_a"],
                p_b=cand["p_b"],
                vlm=cand["vlm"],
                rule_override=cand["rule_override"],
                in_tumor=cand["in_tumor"],
                final_decision=cand["final_decision"],
                decision_path=cand["decision_path"],
                review_label=cand["review_label"],
                record_ids=cand["record_ids"],
            ))

        for hpf in hpfs:
            db.add(HpfSite(
                case_id=case_obj.id,
                seq=hpf["seq"],
                center_um=hpf["center_um"],
                radius_um=hpf["radius_um"],
                mitotic_count=hpf["count"],
                tissue_coverage=hpf["tissue_coverage"],
                tumor_fraction=hpf["tumor_fraction"],
                source=hpf.get("source", "model"),
                image_patch_uri=None
            ))

        # The count itself is a decision, linked to the detections it counted (SPEC-06 AC8).
        db.add(mitosis_count_record(
            case_id=ctx.case_id,
            stage_execution_id=ctx.stage_execution_id,
            run_id=ctx.run_id,
            run_mode=ctx.run_mode.value,
            config_hash=ctx.config_hash,
            slide_id=slide_id,
            candidates=all_candidates,
            hpfs=hpfs,
            summary=scoring_summary,
            thresholds=mitosis_cfg.scoring.thresholds.model_dump(),
        ))

        producers = (det_cfg.producer, referee_cfg.producer) if referee_cfg.enabled else (det_cfg.producer,)
        model_versions = {key: registry.version_of(key) for key in producers}

        output_payload = {
            "case_id": case_id,
            "stage_execution_id": str(stage_exec.id),
            "candidates": all_candidates,
            "hpfs": hpfs,
            "summary": scoring_summary,
            "hpf_sites": {"hpf_target": hpf_target, "n_sites": len(hotspots),
                          "accepted_fewer": bool((stage_exec.input_ref or {}).get("accept_fewer_hpfs", False))},
            "stain_normalization": "unavailable" if stain is None else "available",
            "tumor_gate": tumor.summary() if gate_cfg.enabled else {"applied": False, "reason": "eval ablation"},
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
