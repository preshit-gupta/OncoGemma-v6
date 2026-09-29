"""
Stage 4 referee inputs: the two images the VLM sees for each mitosis candidate.

The v5 morphometric "HoVer-Net" verifier moved to pipeline/heuristics/morph_verifier.py and
is no longer part of the stage (SPEC-06 §9). SPEC-06 §5.4 (WP-7.5) replaces this geometry.
"""
import io

import openslide
from PIL import Image

from app.core.openslide_lock import OPENSLIDE_GLOBAL_LOCK
from app.inference.gateway import ImageInput, InputSpec
from pipeline.errors import SlideReadError

# JPEG quality of the context image, as v5 sent it.
CONTEXT_JPEG_QUALITY = 85


def _read(slide_obj, location, level, size) -> Image.Image:
    try:
        with OPENSLIDE_GLOBAL_LOCK:
            return slide_obj.read_region(location, level, size).convert("RGB")
    except openslide.OpenSlideError as exc:
        raise SlideReadError(f"could not read {size} px at {location} (level {level}): {exc}") from exc


def mitosis_referee_images(
    slide_obj,
    center_x: int,
    center_y: int,
    mpp_x: float,
    focus_px: int,
    context_um: float,
    context_px: int,
) -> tuple[ImageInput, ImageInput]:
    """
    Two views of the candidate at level-0 pixel (center_x, center_y):
    1. Focus: focus_px x focus_px at the slide's own resolution (PNG).
    2. Context: context_um x context_um around it, resampled to context_px (JPEG).
    Each ImageInput's spec states the resolution actually sent.
    """
    x0 = max(0, int(center_x - focus_px // 2))
    y0 = max(0, int(center_y - focus_px // 2))
    focus = _read(slide_obj, (x0, y0), 0, (focus_px, focus_px))
    buf_focus = io.BytesIO()
    focus.save(buf_focus, format="PNG")
    focus_image = ImageInput(
        buf_focus.getvalue(),
        InputSpec(mpp=mpp_x, size_px=(focus_px, focus_px), color="raw", format="png"),
    )

    downsample = context_um / context_px / mpp_x
    l0_side = int(context_um / mpp_x)
    cx0 = max(0, int(center_x - l0_side // 2))
    cy0 = max(0, int(center_y - l0_side // 2))
    level, read_side = 0, l0_side
    if hasattr(slide_obj, "get_best_level_for_downsample"):
        # Read from a pyramid level no coarser than the target, when one exists.
        candidate = slide_obj.get_best_level_for_downsample(downsample)
        candidate_downsample = slide_obj.level_downsamples[candidate]
        if candidate_downsample <= downsample * 1.25:
            level, read_side = candidate, max(1, int(round(l0_side / candidate_downsample)))
    context = _read(slide_obj, (cx0, cy0), level, (read_side, read_side)).resize(
        (context_px, context_px), Image.Resampling.BILINEAR
    )
    buf_context = io.BytesIO()
    context.save(buf_context, format="JPEG", quality=CONTEXT_JPEG_QUALITY)
    context_image = ImageInput(
        buf_context.getvalue(),
        InputSpec(mpp=l0_side * mpp_x / context_px, size_px=(context_px, context_px), color="raw", format="jpeg"),
    )
    return focus_image, context_image
