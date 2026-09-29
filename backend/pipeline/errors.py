"""Errors raised by the image pipeline."""


class SlideReadError(RuntimeError):
    """The slide file, or a region of it, could not be read. The stage fails (SPEC-01 §3.9)."""


class RegionOutOfBoundsError(SlideReadError):
    """A requested region lies entirely outside the slide."""


class IccProfileError(SlideReadError):
    """The slide's embedded ICC profile cannot be converted to sRGB, so its colours cannot be trusted."""


class MissingMppError(ValueError):
    """The slide has no valid resolution (status 'needs_mpp'), so no physical region can be read."""
