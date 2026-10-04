"""
Stage 5 worker: Nottingham grading (SPEC-07 §4-7; WP-8.6, contract grading_v6).

1. Samples: stratified inside the confirmed Stage 3 hotspot windows (``pipeline/grading_sampling.py``):
   ``tubule_patches`` tubule samples (512 µm @ 1.0 µm/px, may overlap) and ``pleo_fields``
   pleomorphism fields (128 µm @ 0.25 µm/px, non-overlapping), counts from the specimen profile.
2. Estimates through the model gateway with the configured producer and prompts: one tubule call
   per tubule sample, one pleomorphism call per field, the histologic type over the first tubule
   samples. A failed estimate is never replaced by a value (SPEC-01 §3.9): it fails the stage
   unless configs/fallbacks.yaml allows it in a clinical run; then that estimate is null and the
   grading needs a human.
3. M from the confirmed Stage 4 rows through ``pipeline/scoring.py`` (the single implementation).
4. Aggregation in deterministic code (``pipeline/grading.py::stage5_result``).

The machine output (``gradings.machine`` and ``grading/output.json``) is never edited later;
reviews and overrides go in ``gradings.overrides``.
"""

import io
import json
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from google.api_core.exceptions import NotFound
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.slide_source import download_slide
from app.core.gcs import (
    download_blob_as_bytes,
    resolve_slide_raw_uri,
    upload_blob_from_bytes,
)
from app.core.stain_profiles import usable_stain_transform
from app.core.tasks import EntityType, Task
from app.inference.gateway import EntityRef, FallbackResult, ImageInput, InputSpec, ModelInputs
from app.inference.schemas import HistotypeVerdict, PleoScore, TubuleEstimate
from app.models.audit import AuditEvent
from app.models.case import Case
from app.models.detection import Detection
from app.models.grading import Grading
from app.models.hotspot import Hotspot
from app.models.hpf_site import HpfSite
from app.models.stage_execution import StageExecution
from pipeline.errors import DegenerateStainProfileError, SlideReadError
from pipeline.grading import MACHINE_SCHEMA, sample_blob, stage5_result
from pipeline.grading_sampling import (
    HotspotFrame,
    Sample,
    SamplingFrameEmptyError,
    parse_tumor_tiles,
    plan_samples,
    seed_from_sha256,
)
from pipeline.scoring import summarize_stage4
from pipeline.slide_io import SlideReader, centered_origin_um, read_region_at_mpp, require_mpp
from pipeline.stain import StainTransform
from worker.runtime import StageRuntime

# Concurrent VLM calls, as v5 limited them.
ESTIMATOR_THREADS = 4

class TriageOutputMissingError(RuntimeError):
    """Stage 3 left no ``triage/tiles.parquet``: confirm triage again before grading."""


def grading_blob(case_id: str, name: str) -> str:
    return f"cases/{case_id}/grading/{name}"


def estimator_label(arm: str, producer: str, prompt_id: str) -> str:
    """``<arm>:<producer>@<prompt>``: what actually produced the component (contract ``estimator``)."""
    return f"{arm}:{producer}@{prompt_id.removesuffix('.md')}"


def load_tumor_tiles(case_id: str):
    blob = f"cases/{case_id}/triage/tiles.parquet"
    try:
        data = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, blob)
    except (NotFound, FileNotFoundError) as exc:
        raise TriageOutputMissingError(f"case {case_id} has no {blob}; run and confirm triage again") from exc
    return parse_tumor_tiles(data)


def confirmed_frames(db: Session, case: Case) -> List[HotspotFrame]:
    """The non-excluded hotspots of a confirmed Stage 3, best first."""
    triage = db.scalars(
        select(StageExecution).where(StageExecution.case_id == case.id, StageExecution.stage == "triage")
        .order_by(StageExecution.attempt.desc()).limit(1)
    ).first()
    if triage is None or triage.status != "confirmed":
        raise SamplingFrameEmptyError(f"case {case.id} has no confirmed Stage 3 (triage), so no confirmed hotspots")
    rows = db.scalars(select(Hotspot).where(Hotspot.case_id == case.id, Hotspot.excluded == False)).all()  # noqa: E712
    # Ranked model windows first, in rank order; then pathologist-added ones (no rank) by id.
    ranked = sorted((h for h in rows if h.rank is not None), key=lambda h: (h.rank, h.id))
    unranked = sorted((h for h in rows if h.rank is None), key=lambda h: h.id)
    return [HotspotFrame(id=h.id, polygon_um=h.polygon_um) for h in ranked + unranked]


def stage4_mitotic(db: Session, case: Case, config) -> Dict[str, Any]:
    """The contract's ``mitotic`` block from the confirmed Stage 4 rows (``pipeline/scoring.py``)."""
    detections = db.scalars(select(Detection).where(Detection.case_id == case.id).order_by(Detection.id)).all()
    hpf_rows = db.scalars(select(HpfSite).where(HpfSite.case_id == case.id).order_by(HpfSite.seq.asc())).all()
    candidates = [
        {"id": d.id, "centroid_um": d.centroid_um, "counted": bool(d.counted),
         "final_decision": d.final_decision, "review_label": d.review_label}
        for d in detections
    ]
    hpfs = [{"seq": h.seq, "center_um": h.center_um, "radius_um": h.radius_um} for h in hpf_rows]
    _, summary = summarize_stage4(candidates, hpfs, scoring=config.mitosis.scoring, hpf_count=config.mitosis.hpf.count)
    return {
        "score": summary["mitotic_score"],
        "count_total": summary["count_total"],
        "n_hpf": summary["n_hpf"],
        "area_mm2": summary["area_mm2"],
        "per_mm2": summary["per_mm2"],
        "flags": summary["flags"],
    }


def read_sample(reader: SlideReader, sample: Sample, stain: StainTransform | None):
    """The sample's square at its own resolution, normalised with ``stain`` when one is given."""
    x_um, y_um = centered_origin_um(reader, sample.center_um[0], sample.center_um[1], sample.size_um, sample.size_um)
    return read_region_at_mpp(
        reader, x_um, y_um, sample.size_um, sample.size_um, sample.mpp,
        color="raw" if stain is None else "normalized", stain=stain,
    )


def _png(rgb) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG")
    return buf.getvalue()


def _sample_dict(sample: Sample) -> Dict[str, Any]:
    return {
        "id": sample.id, "center_um": list(sample.center_um), "size_um": sample.size_um, "mpp": sample.mpp,
        "stratum": sample.stratum, "hotspot_id": sample.hotspot_id, "tumor_area_um2": sample.tumor_area_um2,
    }


def run_grading(stage_exec: StageExecution, db: Session, runtime: StageRuntime) -> Tuple[str, Dict[str, Any]]:
    config = runtime.config
    sampling = config.scoring.grading
    estimators = sampling.estimators
    gateway, ctx = runtime.gateway, runtime.ctx

    case_id = str(stage_exec.case_id)
    case = db.get(Case, stage_exec.case_id)
    if not case or not case.slides:
        raise ValueError(f"Case {case_id} has no valid slide records.")
    slide = case.slides[0]
    slide_id = str(slide.id)
    require_mpp(slide)
    profile = config.specimen_profiles.for_type(case.specimen_type)

    # Fail fast, before the slide is downloaded: the frame, the tumour tiles and the Stage 4 score.
    frames = confirmed_frames(db, case)
    tiles = load_tumor_tiles(case_id)
    extent_um = (slide.width_px * float(slide.mpp_x), slide.height_px * float(slide.mpp_y))
    plan = plan_samples(
        tiles, frames, extent_um,
        n_tubule=profile.grading.tubule_patches, n_pleo=profile.grading.pleo_fields,
        tubule_size_um=sampling.tubule_sample.size_um, tubule_mpp=sampling.tubule_sample.mpp,
        pleo_size_um=sampling.pleo_field.size_um, pleo_mpp=sampling.pleo_field.mpp,
        subdivisions=sampling.subdivisions, n_init=sampling.kmeans_n_init,
        seed=seed_from_sha256(slide.checksum_sha256),
    )
    mitotic = stage4_mitotic(db, case, config)
    print(f"[Worker Stage 5: Grading] case {case_id}: {len(plan.tubule)} tubule samples, {len(plan.pleo)} of "
          f"{profile.grading.pleo_fields} pleomorphism fields from {plan.n_candidates} candidate centres in {len(frames)} hotspots")

    scratch_dir = tempfile.mkdtemp(prefix="og_grading_")
    reader = None
    try:
        gcs_uri_original = resolve_slide_raw_uri(case_id, slide) or slide.gcs_uri_original or f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
        try:
            local_slide_path = download_slide(gcs_uri_original, scratch_dir)
        except OSError as exc:
            raise SlideReadError(f"could not download slide {gcs_uri_original} for case {case_id}: {exc}") from exc
        reader = SlideReader.from_slide_row(local_slide_path, slide)

        # The colour the estimators are shown. "normalized" needs the slide's persisted stain transform;
        # a degenerate fit cannot provide it, and the raw image never stands in for it.
        stain = None
        if estimators.color == "normalized":
            stain = usable_stain_transform(db, slide.id, od_beta=profile.stain_fit.od_beta)
            if stain is None:
                raise DegenerateStainProfileError(
                    f"the estimators are configured for normalized colour but slide {slide_id}'s stain fit is degenerate"
                )

        def _image(kind: str, sample: Sample) -> ImageInput:
            region = read_sample(reader, sample, stain)
            data = _png(region.rgb)
            upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, sample_blob(case_id, kind, sample.id), data, "image/png")
            size_px = region.rgb.shape[1], region.rgb.shape[0]
            return ImageInput(data, InputSpec(mpp=region.target_mpp, size_px=size_px, color=region.color, format="png"))

        tubule_images = [_image("tubule", s) for s in plan.tubule]
        pleo_images = [_image("pleo", s) for s in plan.pleo]

        def _estimate(job):
            task, prompt_id, images, entity, output_model = job
            return gateway.invoke_or_fallback(
                task, estimators.producer, ModelInputs(images=images, prompt_id=prompt_id), ctx, entity, output_model
            )

        jobs = [(Task.TUBULE_PATCH, estimators.tubule_prompt, (img,), EntityRef(EntityType.PATCH, s.id), TubuleEstimate)
                for s, img in zip(plan.tubule, tubule_images)]
        jobs += [(Task.PLEO_FIELD, estimators.pleo_prompt, (img,), EntityRef(EntityType.FIELD, s.id), PleoScore)
                 for s, img in zip(plan.pleo, pleo_images)]
        jobs.append((Task.HISTOTYPE, estimators.histotype_prompt, tuple(tubule_images[:estimators.histotype_images]),
                     EntityRef(EntityType.SLIDE, slide_id), HistotypeVerdict))
        with ThreadPoolExecutor(max_workers=ESTIMATOR_THREADS) as pool:
            results = list(pool.map(_estimate, jobs))
        type_result = results.pop()
        t_results, p_results = results[:len(plan.tubule)], results[len(plan.tubule):]
    finally:
        if reader is not None:
            reader.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)

    samples_out = []
    for s, res in zip(plan.tubule, t_results):
        failed = isinstance(res, FallbackResult)
        samples_out.append({
            **_sample_dict(s),
            "estimate": None if failed else {"tumor_present": res.output.tumor_present, "tubule_percent": res.output.tubule_percent},
            "rationale": None if failed else res.output.rationale,
            "record_id": str(res.record_id),
        })
    fields_out = []
    for s, res in zip(plan.pleo, p_results):
        failed = isinstance(res, FallbackResult)
        fields_out.append({
            **_sample_dict(s),
            "estimate": None if failed else {"pleomorphism_score": res.output.pleomorphism_score},
            "nuclei": None,  # nuclear segmentation is WP-8.3 (deferred, D21)
            "record_id": str(res.record_id),
        })
    histotype = None
    if not isinstance(type_result, FallbackResult):
        histotype = {"type": type_result.output.type, "rationale": type_result.output.rationale,
                     "record_id": str(type_result.record_id)}

    machine_flags = []
    shortfall = {
        "tubule": profile.grading.tubule_patches - len(plan.tubule),
        "pleo": profile.grading.pleo_fields - len(plan.pleo),
    }
    if shortfall["tubule"] > 0 or shortfall["pleo"] > 0 or histotype is None:
        machine_flags.append("needs_human")

    model_versions = {estimators.producer: config.models.version_of(estimators.producer)}
    machine = {
        "schema": MACHINE_SCHEMA,
        "case_id": case_id,
        "slide_id": slide_id,
        "frame": {"hotspot_ids": [f.id for f in frames], "n_candidates": plan.n_candidates},
        "tubule": {"samples": samples_out, "requested": profile.grading.tubule_patches,
                   "estimator": estimator_label("T1", estimators.producer, estimators.tubule_prompt)},
        "pleomorphism": {"fields": fields_out, "requested": profile.grading.pleo_fields, "aggregation": "mode",
                         "estimator": estimator_label("P1", estimators.producer, estimators.pleo_prompt)},
        "histotype": histotype,
        "histotype_estimator": estimator_label("H1", estimators.producer, estimators.histotype_prompt),
        "mitotic": mitotic,
        "shortfall": shortfall,
        "flags": machine_flags,
        "model_versions": model_versions,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = stage5_result(machine, {}, config.scoring)
    machine["result"] = result
    # Read by the eval harness (eval/harness/collect.py): the machine output needs a pathologist.
    machine["needs_human"] = "needs_human" in result["flags"]

    upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, grading_blob(case_id, "output.json"),
                           json.dumps(machine, indent=2).encode("utf-8"), "application/json")
    output_uri = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/{grading_blob(case_id, 'output.json')}"

    columns = {
        "tubule_percent": result["tubule_percent"],
        "tubule_score": result["tubule_score"],
        "pleo_score": result["pleo_score"],
        "mitotic_score": result["mitotic_score"],
        "nottingham_sum": result["total"],
        "grade": result["grade"],
        "histologic_type": histotype["type"] if histotype else None,
    }
    grading = db.get(Grading, stage_exec.case_id)
    if grading is None:
        grading = Grading(case_id=stage_exec.case_id)
        db.add(grading)
    for key, value in columns.items():
        setattr(grading, key, value)
    # A re-run starts a fresh review: earlier reviews refer to samples that no longer exist.
    grading.machine = machine
    grading.overrides = {}
    grading.type_confirmed_by = "unconfirmed"

    db.add(AuditEvent(
        case_id=case_id, actor="worker_grading", event_type="stage_5_grading_generated", stage="grading",
        payload={**columns, "flags": result["flags"], "n_tubule": len(plan.tubule), "n_pleo": len(plan.pleo),
                 "shortfall": shortfall},
    ))
    db.commit()
    stage_exec.status = "awaiting_review"
    print(f"[Worker Stage 5: Grading] Completed for case {case_id}: grade {result['grade']} (sum {result['total']}), flags {result['flags']}")
    return output_uri, model_versions
