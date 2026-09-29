"""
Stage 4 referee inputs: the two images the VLM sees for each mitosis candidate.

The v5 morphometric "HoVer-Net" verifier moved to pipeline/heuristics/morph_verifier.py and
is no longer part of the stage (SPEC-06 §9). SPEC-06 §5.4 (WP-7.5) replaces this geometry.
"""
import io
from dataclasses import dataclass

from PIL import Image

from app.core.pipeline_config import MitosisRefereeConfig
from app.inference.gateway import ImageInput, InputSpec
from pipeline.slide_io import Region, SlideReader, StainApplier, normalize_region, read_region_at_mpp

# JPEG quality of the context image, as v5 sent it.
CONTEXT_JPEG_QUALITY = 85


@dataclass(frozen=True)
class RefereeImages:
    focus: ImageInput
    context: ImageInput
    # The focus crop as scanned, for the review UI, whichever colour the referee was shown.
    focus_raw_png: bytes


def _png(rgb) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(rgb, quality: int) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def _spec(region: Region, image_format: str) -> InputSpec:
    height_px, width_px = region.rgb.shape[:2]
    return InputSpec(mpp=region.target_mpp, size_px=(width_px, height_px), color=region.color, format=image_format)


def mitosis_referee_images(
    reader: SlideReader,
    center_x_um: float,
    center_y_um: float,
    cfg: MitosisRefereeConfig,
    stain: StainApplier | None,
) -> RefereeImages:
    """
    Two views of the candidate at (center_x_um, center_y_um), in the colour ``cfg.color`` (PNG focus, JPEG context):
    1. Focus: ``cfg.focus_px`` square at ``cfg.focus_mpp``.
    2. Context: ``cfg.context_um`` square around it, resampled to ``cfg.context_px``.
    A view overhanging the slide edge is padded with white, so the candidate stays centred.
    ``stain`` is the slide's persisted stain transform; it is required for ``color="normalized"``.
    """
    normalized = cfg.color == "normalized"
    if normalized and stain is None:
        raise ValueError("the referee is configured for normalized colour but no stain transform was given")

    focus_um = cfg.focus_px * cfg.focus_mpp
    focus_raw = read_region_at_mpp(
        reader, center_x_um - focus_um / 2, center_y_um - focus_um / 2, focus_um, focus_um, cfg.focus_mpp
    )
    focus = normalize_region(focus_raw, stain) if normalized else focus_raw

    context = read_region_at_mpp(
        reader, center_x_um - cfg.context_um / 2, center_y_um - cfg.context_um / 2, cfg.context_um, cfg.context_um,
        cfg.context_um / cfg.context_px, color=cfg.color, stain=stain if normalized else None,
    )
    return RefereeImages(
        focus=ImageInput(_png(focus.rgb), _spec(focus, "png")),
        context=ImageInput(_jpeg(context.rgb, CONTEXT_JPEG_QUALITY), _spec(context, "jpeg")),
        focus_raw_png=_png(focus_raw.rgb),
    )
