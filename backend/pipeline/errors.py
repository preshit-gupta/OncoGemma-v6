"""Errors raised by the image pipeline."""


class SlideReadError(RuntimeError):
    """The slide file, or a region of it, could not be read. The stage fails (SPEC-01 §3.9)."""
