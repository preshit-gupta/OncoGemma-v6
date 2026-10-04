"""Reading a case's slide from the API (SPEC-04 §3.1, §3.4).

The routers read regions through ``read_region_at_mpp``, like the stages. A request downloads the
raw slide once (cached in the temp dir), opens a ``SlideReader`` for it and closes it again.
Colour is the slide's persisted stain transform; a slide that has none (or a degenerate fit)
cannot serve normalised pixels, and the router says so instead of serving raw ones as normalised.
"""
import os
import tempfile

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import download_blob_to_filename, parse_gcs_uri, resolve_slide_raw_uri
from app.core.slide_source import download_slide, is_series_uri
from app.core.pipeline_config import get_pipeline_config
from app.core.stain_profiles import stain_transform_for_slide
from pipeline.errors import (
    MissingMppError,
    SpecimenTypeRequired,
    StainError,
    TissueMaskError,
)
from pipeline.slide_io import Region, SlideReader, read_region_at_mpp

# Errors that say a slide or case lacks something a stage produces or a user states (a resolution, a
# specimen type, a stain profile, a tissue mask). The router answers 409; the fix is not a retry.
PRECONDITION_ERRORS = (MissingMppError, SpecimenTypeRequired, StainError, TissueMaskError)


def cached_slide_path(case_id, slide) -> str:
    """The raw slide file, downloaded once and kept in the temp dir."""
    gcs_uri = (
        resolve_slide_raw_uri(str(case_id), slide)
        or slide.gcs_uri_original
        or f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide.id}.svs"
    )
    bucket_name, blob_name = parse_gcs_uri(gcs_uri)
    cache_dir = os.path.join(tempfile.gettempdir(), "oncogemma_slides")
    os.makedirs(cache_dir, exist_ok=True)
    if is_series_uri(gcs_uri):
        # A DICOM series: its own directory, with a marker naming the instance to open once complete.
        series_dir = os.path.join(cache_dir, f"{bucket_name}_{blob_name.strip('/').replace('/', '_')}")
        marker = os.path.join(series_dir, "open_path.txt")
        if not os.path.exists(marker):
            path = download_slide(gcs_uri, series_dir)
            with open(marker, "w", encoding="utf-8") as f:
                f.write(path)
        with open(marker, encoding="utf-8") as f:
            return f.read()
    target_path = os.path.join(cache_dir, blob_name.replace("/", "_"))
    if not os.path.exists(target_path) or os.path.getsize(target_path) == 0:
        download_blob_to_filename(bucket_name, blob_name, target_path)
    return target_path


def open_case_slide(case_id, slide) -> SlideReader:
    return SlideReader.from_slide_row(cached_slide_path(case_id, slide), slide)


def slide_stain_transform(db: Session, slide, case=None):
    """The slide's persisted stain transform. ``case`` defaults to the slide's own case."""
    case = slide.case if case is None else case
    od_beta = get_pipeline_config().specimen_profiles.for_type(case.specimen_type).stain_fit.od_beta
    return stain_transform_for_slide(db, slide.id, od_beta=od_beta)


def read_case_region(
    db: Session, case_id, slide, x_um: float, y_um: float, w_um: float, h_um: float, target_mpp: float, *, normalized: bool
) -> Region:
    """One region of the case's slide at ``target_mpp``, normalised with the slide's stain profile when asked."""
    stain = slide_stain_transform(db, slide) if normalized else None
    with open_case_slide(case_id, slide) as reader:
        return read_region_at_mpp(
            reader, x_um, y_um, w_um, h_um, target_mpp, color="normalized" if normalized else "raw", stain=stain
        )
