"""Remote slide reads by HTTP range requests (WP-6.2, decision D20). No network: a fake session."""
import io

import numpy as np
import pytest

pytest.importorskip("tiffslide")
tifffile = pytest.importorskip("tifffile")

from eval.datasets.remote_slide import (  # noqa: E402
    RangedHTTPFile,
    RemoteReadError,
    _parse_total,
    open_remote_slide,
    read_first_page_description,
)
from pipeline.slide_io import read_region_at_mpp  # noqa: E402

URL = "https://example.test/data/slide"


class FakeResponse:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class FakeSession:
    """Serves ``data`` with GDC's Content-Range form ("start-end/total"), counting requests."""

    def __init__(self, data: bytes, ignore_range: bool = False):
        self.data = data
        self.ignore_range = ignore_range
        self.requests = 0

    def get(self, url, headers, timeout):
        self.requests += 1
        if self.ignore_range:
            return FakeResponse(200, self.data)
        start, end = (int(v) for v in headers["Range"].removeprefix("bytes=").split("-"))
        end = min(end, len(self.data) - 1)
        return FakeResponse(206, self.data[start:end + 1], {"Content-Range": f"{start}-{end}/{len(self.data)}"})


def pyramid_tiff(width=1024, height=768) -> bytes:
    """A tiled two-level RGB TIFF with an Aperio-style description (MPP 0.5)."""
    rng = np.random.default_rng(0)
    base = rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
    buf = io.BytesIO()
    with tifffile.TiffWriter(buf) as tif:
        tif.write(base, tile=(256, 256), photometric="rgb", description="Aperio Image Library v10\n1024x768 |AppMag = 20|MPP = 0.5")
        tif.write(base[::4, ::4], tile=(256, 256), photometric="rgb", subfiletype=1)
    return buf.getvalue()


def test_parse_total_accepts_rfc_and_gdc_forms():
    assert _parse_total("bytes 0-9/100") == 100
    assert _parse_total("0-9/100") == 100
    with pytest.raises(RemoteReadError):
        _parse_total("0-9/*")


def test_ranged_file_reads_like_the_bytes():
    data = bytes(range(256)) * 40
    f = RangedHTTPFile(URL, session=FakeSession(data), block_bytes=1000)
    assert f.size == len(data)
    f.seek(995)
    assert f.read(20) == data[995:1015]
    f.seek(-5, io.SEEK_END)
    assert f.read() == data[-5:]
    assert f.read(10) == b""


def test_a_server_that_ignores_ranges_is_refused():
    with pytest.raises(RemoteReadError, match="not 206"):
        RangedHTTPFile(URL, session=FakeSession(b"x" * 100, ignore_range=True))


def test_remote_regions_equal_local_ones(tmp_path):
    import tiffslide

    from pipeline.slide_io import SlideReader

    data = pyramid_tiff()
    path = tmp_path / "slide.tiff"
    path.write_bytes(data)
    session = FakeSession(data)
    remote = open_remote_slide(URL, 0.5, 0.5, session=session, block_bytes=4096)
    local = SlideReader(str(path), 0.5, 0.5, "tiff", opener=tiffslide.TiffSlide)
    try:
        for args in [(10.0, 20.0, 200.0, 150.0, 1.0), (0.0, 0.0, 512.0, 384.0, 2.0)]:
            a = read_region_at_mpp(remote, *args)
            b = read_region_at_mpp(local, *args)
            np.testing.assert_array_equal(a.rgb, b.rgb)
            assert a.native_level == b.native_level
    finally:
        remote.close()
        local.close()
    assert session.requests > 0


def test_first_page_description():
    session = FakeSession(pyramid_tiff())
    assert "AppMag = 20" in read_first_page_description(URL, session=session, block_bytes=4096)
