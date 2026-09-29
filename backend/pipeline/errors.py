"""Errors raised by the image pipeline."""


class SlideReadError(RuntimeError):
    """The slide file, or a region of it, could not be read. The stage fails (SPEC-01 §3.9)."""


class RegionOutOfBoundsError(SlideReadError):
    """A requested region lies entirely outside the slide."""


class IccProfileError(SlideReadError):
    """The slide's embedded ICC profile cannot be converted to sRGB, so its colours cannot be trusted."""


class MissingMppError(ValueError):
    """The slide has no valid resolution (status 'needs_mpp'), so no physical region can be read."""


class StainError(RuntimeError):
    """A stain profile could not be fitted, found or applied."""


class DegenerateStainProfileError(StainError):
    """The slide's stain fit is degenerate, so its colours cannot be normalised (SPEC-04 §3.4)."""


class StainProfileMissingError(StainError):
    """The slide has no stain profile; Stage 2 (preprocess) must run for it."""
