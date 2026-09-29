"""
Unit tests for TCGABRCAAdapter (offline, using recorded JSON fixtures and mock HTTP).
SPEC-02 §3.1 and WP-5.2.
"""
import hashlib
import json
import re
import sys
import types
from pathlib import Path
import httpx
import pandas as pd
import pytest

from eval.datasets.base import FetchedFile, FetchIntegrityError, SlideMetadataError
from eval.datasets.storage import LocalStorage
from eval.datasets.tcga import TCGABRCAAdapter


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def gdc_files_json(fixtures_dir: Path) -> dict:
    with open(fixtures_dir / "gdc_files_response.json", "r", encoding="utf-8") as f:
        return json.load(f)


def test_tcga_discover_filters_ts_slides(gdc_files_json: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/files")
        return httpx.Response(200, json=gdc_files_json)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = TCGABRCAAdapter(http_client=client)

    df = adapter.discover(page_size=10)

    # Fixture had 3 hits: 2 DX slides and 1 TS slide
    assert len(df) == 2
    assert "ts-file-003" not in df["file_id"].values
    assert set(df["file_id"].values) == {"dx-file-001", "dx-file-002"}

    row1 = df[df["file_id"] == "dx-file-001"].iloc[0]
    assert row1["patient_id"] == "TCGA-A7-A0DC"
    assert row1["tss"] == "A7"
    assert row1["file_size"] == 1024

    row2 = df[df["file_id"] == "dx-file-002"].iloc[0]
    assert row2["patient_id"] == "TCGA-BH-A0BQ"
    assert row2["tss"] == "BH"
    assert row2["file_size"] == 2048


def test_tcga_fetch_streaming_integrity(tmp_path: Path):
    payload = b"A" * 1024
    expected_md5 = hashlib.md5(payload).hexdigest()
    expected_sha256 = hashlib.sha256(payload).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/data/dx-file-001"):
            return httpx.Response(200, content=payload)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = TCGABRCAAdapter(http_client=client)
    storage = LocalStorage(tmp_path)

    row = pd.Series({
        "file_id": "dx-file-001",
        "file_name": "test.svs",
        "md5sum": expected_md5,
        "file_size": 1024,
    })

    fetched = adapter.fetch(row, storage)
    assert fetched.slide_id == "dx-file-001"
    assert fetched.md5 == expected_md5
    assert fetched.sha256 == expected_sha256
    assert fetched.size_bytes == 1024
    assert storage.exists("tcga-brca/dx/dx-file-001.svs")


def test_tcga_fetch_md5_mismatch_raises(tmp_path: Path):
    payload = b"Wrong content"
    wrong_md5 = "bad00000000000000000000000000000"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = TCGABRCAAdapter(http_client=client)
    storage = LocalStorage(tmp_path)

    row = pd.Series({
        "file_id": "dx-bad-md5",
        "file_name": "test.svs",
        "md5sum": wrong_md5,
        "file_size": len(payload),
    })

    with pytest.raises(FetchIntegrityError, match="MD5 mismatch"):
        adapter.fetch(row, storage)


def test_tcga_fetch_resume_partial(tmp_path: Path):
    full_payload = b"Hello, World! Resumed stream."
    full_md5 = hashlib.md5(full_payload).hexdigest()
    full_sha256 = hashlib.sha256(full_payload).hexdigest()

    # Pre-write partial file
    storage = LocalStorage(tmp_path)
    relpath = "tcga-brca/dx/dx-resume.svs"
    partial_bytes = b"Hello, "
    with storage.open_write(relpath) as f:
        f.write(partial_bytes)

    assert storage.get_size(relpath) == len(partial_bytes)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Range" in request.headers
        assert request.headers["Range"] == f"bytes={len(partial_bytes)}-"
        remaining_payload = full_payload[len(partial_bytes):]
        return httpx.Response(206, content=remaining_payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = TCGABRCAAdapter(http_client=client)

    row = pd.Series({
        "file_id": "dx-resume",
        "file_name": "dx-resume.svs",
        "md5sum": full_md5,
        "file_size": len(full_payload),
    })

    fetched = adapter.fetch(row, storage)
    assert fetched.md5 == full_md5
    assert fetched.sha256 == full_sha256
    assert fetched.size_bytes == len(full_payload)

    # Check complete content on disk
    with storage.open_read(relpath) as f:
        assert f.read() == full_payload


def test_tcga_fetch_many_confirm_bytes_guard(tmp_path: Path):
    adapter = TCGABRCAAdapter()
    storage = LocalStorage(tmp_path)

    rows = pd.DataFrame([
        {"file_id": "f1", "file_size": 1000},
        {"file_id": "f2", "file_size": 2000},
    ])

    # Less than 3000 bytes confirmed
    with pytest.raises(ValueError, match="confirm_bytes"):
        adapter.fetch_many(rows, storage, confirm_bytes=2999)


def test_tcga_slide_meta_injected_reader():
    custom_reader = lambda row, path: {
        "specimen_type": "resection",
        "mpp_override": 0.252,
        "mpp_source": "file",
        "native_mag": 40.0,
        "scanner": "Aperio ScanScope",
        "tss": "A7",
    }
    adapter = TCGABRCAAdapter(slide_reader=custom_reader)
    row = pd.Series({"file_id": "f1", "submitter_id": "TCGA-A7-A0DC"})
    meta = adapter.slide_meta(row)
    assert meta["mpp_override"] == 0.252
    assert meta["native_mag"] == 40.0
    assert meta["scanner"] == "Aperio ScanScope"


def test_tcga_to_manifest_passes_validation():
    adapter = TCGABRCAAdapter()
    discovered = pd.DataFrame([
        {
            "file_id": "f1",
            "file_name": "TCGA-A7-A0DC-01Z-00-DX1.svs",
            "submitter_id": "TCGA-A7-A0DC",
            "patient_id": "TCGA-A7-A0DC",
            "tss": "A7",
        }
    ])
    fetched = {
        "f1": FetchedFile(
            slide_id="f1",
            uri="file:///path/to/tcga-brca/dx/f1.svs",
            sha256="b" * 64,
            md5="a" * 32,
            size_bytes=5000,
        )
    }

    manifest_df = adapter.to_manifest(discovered, fetched)
    assert len(manifest_df) == 1
    assert manifest_df.iloc[0]["dataset"] == "tcga_brca_dx"
    assert manifest_df.iloc[0]["sha256"] == "b" * 64


class _FakeOpenSlideError(Exception):
    pass


class _FakeOpenSlideUnsupportedFormatError(_FakeOpenSlideError):
    pass


class _FakeSlide:
    def __init__(self, properties: dict[str, str]) -> None:
        self.properties = properties

    def __enter__(self) -> "_FakeSlide":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


def _fake_openslide(open_slide) -> types.ModuleType:
    """Stand-in `openslide` module; mirrors the real error hierarchy, OpenSlide(path) calls open_slide."""
    module = types.ModuleType("openslide")
    module.OpenSlideError = _FakeOpenSlideError
    module.OpenSlideUnsupportedFormatError = _FakeOpenSlideUnsupportedFormatError
    module.OpenSlide = open_slide
    return module


def test_tcga_slide_meta_reads_openslide_properties(monkeypatch, tmp_path: Path):
    properties = {"openslide.mpp-x": "0.2527", "aperio.AppMag": "40", "openslide.vendor": "aperio"}
    monkeypatch.setitem(sys.modules, "openslide", _fake_openslide(lambda path: _FakeSlide(properties)))
    adapter = TCGABRCAAdapter()
    row = pd.Series({"file_id": "f1", "submitter_id": "TCGA-A7-A0DC"})

    meta = adapter.slide_meta(row, str(tmp_path / "f1.svs"))

    assert meta["mpp_override"] == 0.2527
    assert meta["mpp_source"] == "file"
    assert meta["native_mag"] == 40.0
    assert meta["scanner"] == "aperio"
    assert meta["tss"] == "A7"


@pytest.mark.parametrize(
    "error",
    [
        _FakeOpenSlideError("Not a TIFF file"),
        _FakeOpenSlideUnsupportedFormatError("Unsupported or missing image file"),
        FileNotFoundError(2, "No such file or directory"),
    ],
    ids=["openslide_error", "unsupported_format", "file_not_found"],
)
def test_tcga_slide_meta_unreadable_slide_raises(monkeypatch, tmp_path: Path, error: Exception):
    def open_slide(path: str):
        raise error

    monkeypatch.setitem(sys.modules, "openslide", _fake_openslide(open_slide))
    adapter = TCGABRCAAdapter()
    row = pd.Series({"file_id": "dx-corrupt", "submitter_id": "TCGA-A7-A0DC"})
    # Never created on disk, so this also checks a missing file is not skipped silently.
    slide_path = str(tmp_path / "dx-corrupt.svs")

    with pytest.raises(SlideMetadataError, match=re.escape(slide_path)) as excinfo:
        adapter.slide_meta(row, slide_path)

    assert excinfo.value.slide_path == slide_path
    assert excinfo.value.__cause__ is error


def test_tcga_to_manifest_unreadable_slide_raises(monkeypatch, tmp_path: Path):
    def open_slide(path: str):
        raise _FakeOpenSlideUnsupportedFormatError("Unsupported or missing image file")

    monkeypatch.setitem(sys.modules, "openslide", _fake_openslide(open_slide))
    adapter = TCGABRCAAdapter()
    discovered = pd.DataFrame([
        {
            "file_id": "f1",
            "file_name": "TCGA-A7-A0DC-01Z-00-DX1.svs",
            "submitter_id": "TCGA-A7-A0DC",
            "patient_id": "TCGA-A7-A0DC",
            "tss": "A7",
        }
    ])
    fetched = {
        "f1": FetchedFile(
            slide_id="f1",
            uri="file:///path/to/tcga-brca/dx/f1.svs",
            sha256="b" * 64,
            md5="a" * 32,
            size_bytes=5000,
        )
    }
    slide_path = str(tmp_path / "f1.svs")

    with pytest.raises(SlideMetadataError, match=re.escape(slide_path)):
        adapter.to_manifest(discovered, fetched, local_slide_paths={"f1": slide_path})
