"""SPEC-04 AC1: one place reads pixels and one place normalises colour.

A static scan of the backend source (tests excluded): a call to ``read_region`` appears only in
``pipeline/slide_io.py``, an OpenSlide handle is opened only where a slide's metadata or
readability is the point, and stain transforms are built and applied only by the stain authority.
"""
import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

SLIDE_IO = "pipeline/slide_io.py"
STAIN = "pipeline/stain.py"
# Ingest reads a slide's own metadata and builds the raw DeepZoom pyramid (the raw layer, Stage 1); the
# TCGA adapter opens a slide only to see whether it is readable.
OPENS_A_SLIDE = {SLIDE_IO, "worker/ingest.py", "eval/datasets/tcga.py"}
# StainTransform.from_profile turns a persisted row into the transform.
FROM_PROFILE = {STAIN, "app/core/stain_profiles.py"}


def source_files():
    for path in sorted(BACKEND.rglob("*.py")):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith(("tests/", "alembic/")) or "__pycache__" in relative:
            continue
        yield relative, ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)


def calls(tree):
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def callee(call: ast.Call) -> tuple[str, str]:
    """(receiver source, name) of a call: ``a.b.read_region(...)`` is ("a.b", "read_region")."""
    if isinstance(call.func, ast.Attribute):
        return ast.unparse(call.func.value), call.func.attr
    if isinstance(call.func, ast.Name):
        return "", call.func.id
    return "", ""


def where(predicate) -> set[str]:
    return {
        relative for relative, tree in source_files() if any(predicate(*callee(call)) for call in calls(tree))
    }


def test_the_scan_sees_the_backend():
    files = [relative for relative, _ in source_files()]
    assert SLIDE_IO in files and STAIN in files and "worker/triage.py" in files


def test_read_region_is_called_only_by_slide_io():
    assert where(lambda receiver, name: name == "read_region") == {SLIDE_IO}


def test_no_stage_or_router_takes_a_thumbnail_or_a_deepzoom_view_of_a_slide():
    assert where(lambda receiver, name: name == "get_thumbnail") == set()
    assert where(lambda receiver, name: name == "get_tile" and "dz" in receiver.lower()) <= {"worker/ingest.py"}


def test_an_openslide_handle_is_opened_only_where_a_slides_own_metadata_is_the_point():
    opened = where(lambda receiver, name: name == "OpenSlide")
    assert SLIDE_IO in opened
    assert opened <= OPENS_A_SLIDE, sorted(opened - OPENS_A_SLIDE)


def test_stain_transforms_are_applied_only_by_the_stain_authority():
    applied = where(lambda receiver, name: name in ("apply", "transform") and "stain" in receiver.lower())
    assert applied <= {STAIN, SLIDE_IO}, sorted(applied - {STAIN, SLIDE_IO})
    assert where(lambda receiver, name: name in ("apply", "transform") and "normalizer" in receiver.lower()) == set()


def test_stain_transforms_are_built_only_by_the_stain_authority():
    assert where(lambda receiver, name: name == "StainTransform" and receiver == "") <= {STAIN}
    assert where(lambda receiver, name: name == "from_profile" and "StainTransform" in receiver) <= FROM_PROFILE


def test_the_v5_normalisers_and_their_readers_are_gone():
    import pipeline.stain as stain
    import pipeline.tiles as tiles

    for name in ("PureNumpyMacenkoNormalizer", "MacenkoNormalizer", "fit_macenko_stain", "get_macenko_normalizer_class"):
        assert not hasattr(stain, name), name
    for name in ("read_region_srgb", "get_icc_transform", "check_icc_profile"):
        assert not hasattr(tiles, name), name
    assert not (BACKEND / "app" / "core" / "openslide_lock.py").exists()


@pytest.mark.parametrize("relative", ["worker/triage.py", "worker/mitosis.py", "worker/grading.py", "worker/preprocess.py", "worker/qc.py"])
def test_every_stage_reads_through_the_reader_and_loads_the_registered_mask_or_profile(relative):
    """Each stage that touches pixels builds a SlideReader (through from_slide_row) and never a raw OpenSlide."""
    tree = dict(source_files())[relative]
    names = {callee(call)[1] for call in calls(tree)}
    assert "from_slide_row" in names
    assert "OpenSlide" not in names
