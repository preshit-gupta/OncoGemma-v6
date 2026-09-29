"""
Stage 2 (preprocess): the registered tissue mask, the slide's stain profile and the normalised
pyramid (SPEC-04 §3.4, §3.6).

Everything that later stages assume about colour and tissue is decided here, once:
1. The case's specimen type picks the profile (an unknown specimen is refused).
2. The tissue mask is computed over the full slide extent at the profile's resolution.
3. The stain profile is fitted on patches sampled over that mask and persisted.
4. The normalised DeepZoom pyramid is rendered from the persisted profile.
Every pixel is read through ``read_region_at_mpp``; a read that fails fails the stage.
"""
import os
import json
import math
import shutil
import tempfile
import numpy as np
from PIL import Image
import glob
import time
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import (
    get_gcs_client,
    parse_gcs_uri,
    upload_blob_from_bytes,
    download_blob_to_filename,
    resolve_slide_raw_uri
)
from app.core.pipeline_config import NormPyramidConfig
from app.core.stain_profiles import save_stain_profile, transform_of_profile
from app.core.tissue_mask_store import save_tissue_mask
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.audit import AuditEvent
from pipeline.slide_io import (
    DZI_TILE_PX,
    SlideReader,
    dzi_level_dimensions,
    dzi_max_level,
    read_dzi_tile,
    require_mpp,
    seed_from_checksum,
)
from pipeline.stain import FITTER_VERSION, StainTransform, fit_stain_profile
from pipeline.tissue_mask import MASK_ALGORITHM_VERSION, compute_tissue_mask
from worker.runtime import StageRuntime

# Concurrent tile uploads.
UPLOAD_THREADS = 16


def norm_pyramid_levels(width_px: int, height_px: int, mpp_x: float, cfg: NormPyramidConfig) -> list[dict]:
    """DeepZoom levels the normalised pyramid covers: the coarsest ones, down to ``cfg.max_mpp``.

    Level z shows the slide shrunk by ``2 ** (max_level - z)``, as OpenSlide's DeepZoomGenerator does.
    Levels are added from the coarsest while the cumulative tile count stays within ``cfg.max_tiles``.
    """
    max_level = dzi_max_level(width_px, height_px)
    downsample = max(1.0, cfg.max_mpp / mpp_x)
    finest = max(0, round(max_level - math.log2(downsample)))
    levels = []
    cumulative = 0
    for z in range(0, min(max_level, finest) + 1):
        scale = 1 << (max_level - z)
        level_w, level_h = dzi_level_dimensions(width_px, height_px, z)
        cols, rows = -(-level_w // DZI_TILE_PX), -(-level_h // DZI_TILE_PX)
        if cumulative + cols * rows > cfg.max_tiles and z > 0:
            break
        cumulative += cols * rows
        levels.append({"z": z, "scale": scale, "width_px": level_w, "height_px": level_h, "cols": cols, "rows": rows})
    return levels


def generate_norm_dzi_pyramid(
    reader: SlideReader, slide_id: str, stain: StainTransform, cfg: NormPyramidConfig, scratch_dir: str
) -> str:
    """
    Render the stain-normalised DeepZoom pyramid with the slide's persisted stain transform and
    stream it to the GCS pyramids bucket. Fails if a tile cannot be read or uploaded.
    """
    norm_pyramid_dir = os.path.join(scratch_dir, "norm_pyramid")
    os.makedirs(norm_pyramid_dir, exist_ok=True)

    for level in norm_pyramid_levels(*reader.dimensions, reader.mpp_x, cfg):
        norm_level_dir = os.path.join(norm_pyramid_dir, str(level["z"]))
        os.makedirs(norm_level_dir, exist_ok=True)
        for c in range(level["cols"]):
            for r in range(level["rows"]):
                tile = Image.fromarray(read_dzi_tile(reader, level["z"], c, r, color="normalized", stain=stain).rgb)
                tile.save(os.path.join(norm_level_dir, f"{c}_{r}.png"), "PNG")
                tile.save(os.path.join(norm_level_dir, f"{c}_{r}.jpg"), "JPEG", quality=85)

    # Stream normalized tiles directly to GCS Cloud Storage pyramid bucket
    client = get_gcs_client()
    bucket = client.bucket(settings.GCS_PYRAMIDS_BUCKET)
    norm_files = glob.glob(os.path.join(norm_pyramid_dir, "**", "*.*"), recursive=True)
    norm_files = [f for f in norm_files if f.lower().endswith((".jpg", ".jpeg", ".png"))]

    def upload_single_norm_tile(local_path):
        rel_path = os.path.relpath(local_path, norm_pyramid_dir)
        parts = rel_path.split(os.sep)
        if len(parts) < 2:
            return None
        z_level = parts[-2]
        filename = parts[-1]
        blob_path = f"{slide_id}/norm/{z_level}/{filename}"
        blob = bucket.blob(blob_path)
        c_type = "image/png" if filename.lower().endswith(".png") else "image/jpeg"

        last_err = None
        for attempt in range(3):
            try:
                blob.upload_from_filename(local_path, content_type=c_type, timeout=30)
                return None
            except Exception as e:
                last_err = e
                time.sleep(0.05 * (2 ** attempt))
        return f"{blob_path}: {last_err}"

    with ThreadPoolExecutor(max_workers=UPLOAD_THREADS) as executor:
        results = list(executor.map(upload_single_norm_tile, norm_files))

    failures = [r for r in results if r is not None]
    if failures:
        raise RuntimeError(f"Normalized pyramid upload failed for {len(failures)}/{len(norm_files)} tiles: {failures[:5]}")

    return f"gs://{settings.GCS_PYRAMIDS_BUCKET}/{slide_id}/norm/"


def run_preprocess(stage_execution: StageExecution, session: Session, runtime: StageRuntime) -> tuple[str, dict]:
    """
    Preprocess worker handler:
    1. Refuses a slide without MPP and a case without a specimen type.
    2. Downloads raw slide directly from GCS.
    3. Computes the registered tissue mask and persists it (PNG + JSON).
    4. Fits the slide's stain profile on tissue patches and persists it.
    5. Renders the normalized DZI pyramid with that profile and uploads it to GCS.
    6. Persists preprocess/output.json & queues the next stage ('qc').
    """
    input_ref = stage_execution.input_ref or {}
    slide_id = input_ref.get("slide_id")
    case_id = stage_execution.case_id

    if not slide_id:
        slide_obj = session.scalars(select(Slide).where(Slide.case_id == case_id)).first()
        if slide_obj:
            slide_id = str(slide_obj.id)

    if not slide_id:
        raise ValueError(f"Slide not found for preprocess stage in case {case_id}")

    slide_obj = session.get(Slide, str(slide_id))
    if not slide_obj:
        slide_obj = session.scalars(select(Slide).where(Slide.id == str(slide_id))).first()

    if not slide_obj:
        raise ValueError(f"Slide object {slide_id} not found in database")

    # Halt preprocess stage if MPP is missing per PRD 01-stage-v4.0 §2.3 step 4
    require_mpp(slide_obj)

    config = runtime.config
    case_obj = session.get(Case, case_id)
    if not case_obj:
        raise ValueError(f"Case {case_id} not found in database")
    specimen_type = case_obj.specimen_type
    profile = config.specimen_profiles.for_type(specimen_type)  # SpecimenTypeRequired for 'unknown'
    seed = seed_from_checksum(slide_obj.checksum_sha256)

    scratch_dir = tempfile.mkdtemp(prefix="og_preprocess_")
    reader = None

    try:
        gcs_uri_original = resolve_slide_raw_uri(case_id, slide_obj) or slide_obj.gcs_uri_original or f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
        raw_bucket_name, blob_name = parse_gcs_uri(gcs_uri_original)

        ext = os.path.splitext(blob_name)[1] or ".svs"
        local_slide_path = os.path.join(scratch_dir, f"slide{ext}")

        # Download directly from GCS raw bucket to transient scratch file
        download_blob_to_filename(raw_bucket_name, blob_name, local_slide_path)

        if not os.path.exists(local_slide_path):
            raise FileNotFoundError(f"Raw slide file not found in GCS for preprocess stage in case {case_id}")

        reader = SlideReader.from_slide_row(local_slide_path, slide_obj)

        # 1. Registered tissue mask over the full slide extent
        mask = compute_tissue_mask(reader, profile.tissue_mask, config.qc.pen_marks, specimen_type)
        tissue_mask_uri, tissue_mask_meta_uri = save_tissue_mask(
            case_id, mask, params=profile.tissue_mask.model_dump(mode="json")
        )

        # 2. Stain profile, fitted once on patches sampled over the mask, then persisted
        origins = mask.sample_origins_um(
            profile.stain_fit.n_candidates, profile.stain_fit.patch_um, np.random.default_rng(seed)
        )
        reference = config.stain_refs[profile.stain_target.ref]
        fit = fit_stain_profile(reader, origins, profile.stain_fit, reference)
        stain_row = save_stain_profile(session, slide_obj.id, fit)

        # 3. Normalized DZI pyramid from the persisted profile. A degenerate profile cannot normalise anything.
        norm_pyramid_uri = None
        if fit.fit_status != "degenerate":
            stain = transform_of_profile(stain_row, od_beta=profile.stain_fit.od_beta)
            norm_pyramid_uri = generate_norm_dzi_pyramid(reader, str(slide_obj.id), stain, profile.norm_pyramid, scratch_dir)

        icc_applied = reader.has_icc_profile
        native_mpp = reader.native_mpp

        # Save preprocess/output.json directly to GCS
        preprocess_output = {
            "specimen_type": specimen_type,
            "icc_applied": icc_applied,
            "native_mpp": native_mpp,
            "stain_profile_id": str(stain_row.id),
            "stain_fit_status": fit.fit_status,
            "stain_patches": fit.n_patches,
            "stain_reference": fit.reference_id,
            "norm_pyramid_uri": norm_pyramid_uri,
            "tissue_mask_uri": tissue_mask_uri,
            "tissue_mask_meta_uri": tissue_mask_meta_uri,
            "tissue_area_mm2": round(mask.area_mm2, 2),
            "model_versions": {"stain_fitter": FITTER_VERSION, "tissue_mask": MASK_ALGORITHM_VERSION}
        }

        output_ref = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/preprocess/output.json"
        upload_blob_from_bytes(
            settings.GCS_ARTIFACTS_BUCKET,
            f"cases/{case_id}/preprocess/output.json",
            json.dumps(preprocess_output, indent=2).encode("utf-8"),
            "application/json"
        )

        # Update stage execution status
        stage_execution.status = "done"

        # Emit audit event
        audit = AuditEvent(
            case_id=str(case_id),
            actor="worker_preprocess",
            event_type="stage_output",
            stage="preprocess",
            payload={
                "specimen_type": specimen_type,
                "icc_applied": icc_applied,
                "tissue_area_mm2": mask.area_mm2,
                "stain_fit_status": fit.fit_status,
                "norm_pyramid_uri": norm_pyramid_uri
            }
        )
        session.add(audit)

        # Auto-chain next stage ('qc') in queued status (monotonic attempt tracking)
        stmt_qc = (
            select(StageExecution)
            .where(
                StageExecution.case_id == case_id,
                StageExecution.stage == "qc"
            )
            .order_by(StageExecution.attempt.desc())
        )
        existing_qc = session.scalars(stmt_qc).first()
        next_qc_attempt = (existing_qc.attempt + 1) if existing_qc else 1

        next_qc_stage = StageExecution(
            case_id=case_id,
            stage="qc",
            attempt=next_qc_attempt,
            status="queued",
            input_ref={"slide_id": str(slide_id), "preprocess_output_ref": output_ref}
        )
        session.add(next_qc_stage)
        session.commit()
        session.refresh(next_qc_stage)

        from app.core.cloud_tasks import dispatch_stage_task
        dispatch_stage_task(
            case_id=str(case_id),
            stage="qc",
            stage_exec_id=str(next_qc_stage.id),
            payload={"slide_id": str(slide_id), "preprocess_output_ref": output_ref}
        )

        return output_ref, preprocess_output["model_versions"]

    finally:
        if reader is not None:
            reader.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
