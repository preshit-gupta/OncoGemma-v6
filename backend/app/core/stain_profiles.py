"""Persist and load a slide's stain profile (SPEC-04 §3.4)."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.stain_profile import StainProfile
from pipeline.errors import StainProfileMissingError
from pipeline.stain import StainFit, StainTransform


def save_stain_profile(session: Session, slide_id: uuid.UUID | str, fit: StainFit) -> StainProfile:
    """Add the fit as the slide's newest profile. The caller commits."""
    slide_uuid = slide_id if isinstance(slide_id, uuid.UUID) else uuid.UUID(str(slide_id))
    # "Newest wins" must not depend on the clock: a coarse tick or a skewed worker could tie or reverse
    # two fits, so a profile is always stamped after the slide's previous one.
    created_at = datetime.now(timezone.utc)
    previous = session.scalars(
        select(StainProfile.created_at).where(StainProfile.slide_id == slide_uuid).order_by(StainProfile.created_at.desc())
    ).first()
    if previous is not None:
        previous = previous if previous.tzinfo else previous.replace(tzinfo=timezone.utc)
        created_at = max(created_at, previous + timedelta(microseconds=1))
    row = StainProfile(
        created_at=created_at,
        slide_id=slide_uuid,
        fitter_version=fit.fitter_version,
        reference_id=fit.reference_id,
        w_src=fit.w_src,
        maxc_src=fit.maxc_src,
        w_tgt=fit.w_tgt,
        maxc_tgt=fit.maxc_tgt,
        fit_status=fit.fit_status,
        n_patches=fit.n_patches,
        mosaic_sha256=fit.mosaic_sha256,
    )
    session.add(row)
    session.flush()
    return row


def latest_stain_profile(session: Session, slide_id: uuid.UUID | str) -> StainProfile:
    """The slide's newest profile. A slide preprocessed before v6 has none: run preprocess again."""
    slide_uuid = slide_id if isinstance(slide_id, uuid.UUID) else uuid.UUID(str(slide_id))
    row = session.scalars(
        select(StainProfile)
        .where(StainProfile.slide_id == slide_uuid)
        .order_by(StainProfile.created_at.desc(), StainProfile.id.desc())
    ).first()
    if row is None:
        raise StainProfileMissingError(f"slide {slide_uuid} has no stain profile; run the preprocess stage for it")
    return row


def transform_of_profile(profile: StainProfile, *, od_beta: float) -> StainTransform:
    """The transform of a stain profile row. A degenerate profile is refused."""
    return StainTransform.from_profile(profile, od_beta=od_beta)


def stain_transform_for_slide(session: Session, slide_id: uuid.UUID | str, *, od_beta: float) -> StainTransform:
    """The slide's persisted stain transform. Raises for a missing or degenerate profile."""
    return transform_of_profile(latest_stain_profile(session, slide_id), od_beta=od_beta)


def usable_stain_transform(session: Session, slide_id: uuid.UUID | str, *, od_beta: float) -> StainTransform | None:
    """The slide's stain transform, or None when its fit is degenerate and colour cannot be normalised.

    A slide without any profile still raises (StainProfileMissingError): its preprocess stage has to run.
    """
    profile = latest_stain_profile(session, slide_id)
    if profile.fit_status == "degenerate":
        return None
    return transform_of_profile(profile, od_beta=od_beta)
