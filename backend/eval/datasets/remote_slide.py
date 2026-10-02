"""
Read regions of a remote whole-slide image by HTTP range requests (WP-6.2, decision D20).

The BCSS release ships colour-normalised RGBs only, while Path Foundation embeds raw scanner
colour. The training pixels of the tumour head therefore come from the TCGA slides themselves.
A whole SVS is about 1 GiB and GDC serves a single stream at well under 1 MB/s, so instead of
downloading slides this module fetches only the bytes of the TIFF tiles a region needs.

``RangedHTTPFile`` is a read-only, seekable file over one URL, with a block cache. ``open_remote_slide``
wraps it in a ``tiffslide.TiffSlide``, which has OpenSlide's interface, and hands that to
``SlideReader``. Every region then goes through ``read_region_at_mpp`` (level choice, ICC to
sRGB, resampling): training sees the pixels production would see.

A server that ignores the range header, or answers with a different length, raises
``RemoteReadError``. Nothing is retried silently beyond ``max_attempts`` transport attempts.
"""
from __future__ import annotations

import io
import threading
import time
from collections import OrderedDict

import requests

from pipeline.errors import SlideReadError
from pipeline.slide_io import SlideReader

GDC_DATA_URL = "https://api.gdc.cancer.gov/data/{file_id}"

# Transport tuning, not clinical values.
DEFAULT_BLOCK_BYTES = 1 << 18  # 256 KiB per range request (fastest measured on GDC)
DEFAULT_CACHE_BLOCKS = 1024  # 256 MiB of cached blocks at most
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_ATTEMPTS = 4
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# Transport failures worth another attempt (a dropped connection mid-body included).
TRANSPORT_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)


class RemoteReadError(SlideReadError):
    """A remote slide could not be read by range requests."""


def gdc_file_url(file_id: str) -> str:
    """The GDC data endpoint of one open-access file."""
    if not file_id or "/" in file_id:
        raise ValueError(f"not a GDC file id: {file_id!r}")
    return GDC_DATA_URL.format(file_id=file_id)


def _parse_total(content_range: str | None) -> int:
    # RFC 9110 says "bytes 0-1023/2048"; GDC answers "0-1023/2048". Both are accepted.
    if not content_range or "/" not in content_range:
        raise RemoteReadError(f"no usable Content-Range header: {content_range!r}")
    total = content_range.rsplit("/", 1)[1].strip()
    if not total.isdigit():
        raise RemoteReadError(f"Content-Range has no total length: {content_range!r}")
    return int(total)


class RangedHTTPFile(io.RawIOBase):
    """A read-only, seekable file over one URL that fetches fixed-size blocks by range requests."""

    def __init__(
        self,
        url: str,
        *,
        session: requests.Session | None = None,
        block_bytes: int = DEFAULT_BLOCK_BYTES,
        cache_blocks: int = DEFAULT_CACHE_BLOCKS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        super().__init__()
        if block_bytes <= 0 or cache_blocks <= 0 or max_attempts <= 0:
            raise ValueError("block_bytes, cache_blocks and max_attempts must be positive")
        self.url = url
        self._session = session or requests.Session()
        self._block_bytes = block_bytes
        self._cache_blocks = cache_blocks
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._pos = 0
        self.bytes_fetched = 0
        self.requests_made = 0
        first = self._get_range(0, block_bytes - 1)
        self._size = self._total
        self._store(0, first)

    # -- transport -------------------------------------------------------------------

    def _get_range(self, start: int, end: int) -> bytes:
        """Bytes [start, end] of the URL; the server must answer 206 with exactly that range."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.get(
                    self.url, headers={"Range": f"bytes={start}-{end}"}, timeout=self._timeout_s
                )
                data = response.content
            except TRANSPORT_ERRORS as exc:
                if attempt == self._max_attempts:
                    raise RemoteReadError(f"{self.url}: bytes {start}-{end} failed after {attempt} attempts: {exc}") from exc
                time.sleep(2 ** (attempt - 1))
                continue
            if response.status_code in RETRY_STATUS and attempt < self._max_attempts:
                time.sleep(2 ** (attempt - 1))
                continue
            if response.status_code != 206:
                raise RemoteReadError(
                    f"{self.url}: range request for bytes {start}-{end} answered HTTP {response.status_code}, not 206"
                )
            self._total = _parse_total(response.headers.get("Content-Range"))
            expected = min(end, self._total - 1) - start + 1
            if len(data) != expected:
                raise RemoteReadError(f"{self.url}: asked for {expected} bytes at {start}, got {len(data)}")
            self.bytes_fetched += len(data)
            self.requests_made += 1
            return data
        raise AssertionError("unreachable")  # the loop returns or raises

    def _store(self, index: int, data: bytes) -> None:
        self._cache[index] = data
        self._cache.move_to_end(index)
        while len(self._cache) > self._cache_blocks:
            self._cache.popitem(last=False)

    def _block(self, index: int) -> bytes:
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached
        start = index * self._block_bytes
        data = self._get_range(start, min(start + self._block_bytes, self._size) - 1)
        self._store(index, data)
        return data

    def _read_span(self, start: int, length: int) -> bytes:
        """Bytes [start, start + length) clipped to the file; contiguous missing blocks go in one request."""
        stop = min(start + length, self._size)
        if start >= stop:
            return b""
        first, last = start // self._block_bytes, (stop - 1) // self._block_bytes
        with self._lock:
            missing = [i for i in range(first, last + 1) if i not in self._cache]
            if len(missing) > 1 and missing == list(range(missing[0], missing[-1] + 1)):
                lo = missing[0] * self._block_bytes
                hi = min((missing[-1] + 1) * self._block_bytes, self._size)
                data = self._get_range(lo, hi - 1)
                for k, index in enumerate(missing):
                    self._store(index, data[k * self._block_bytes:(k + 1) * self._block_bytes])
            parts = [self._block(i) for i in range(first, last + 1)]
        joined = b"".join(parts)
        offset = start - first * self._block_bytes
        return joined[offset:offset + (stop - start)]

    # -- io.RawIOBase ----------------------------------------------------------------

    @property
    def size(self) -> int:
        return self._size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        if pos < 0:
            raise ValueError(f"negative seek position {pos}")
        self._pos = pos
        return pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._size - self._pos
        data = self._read_span(self._pos, size)
        self._pos += len(data)
        return data

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


def open_remote_slide(
    url: str, mpp_x: float, mpp_y: float, *, source_format: str = "svs", **file_kwargs
) -> SlideReader:
    """A ``SlideReader`` over a remote TIFF-family slide, read by range requests.

    ``mpp_x``/``mpp_y`` are the slide's level-0 resolution, given by the caller as for any
    slide (SPEC-04 §3.1). Each reader thread gets its own ranged file and TiffSlide handle.
    """
    import tiffslide  # eval-only dependency (requirements/dev.in, training.in)

    def opener(_path: str):
        try:
            return tiffslide.TiffSlide(RangedHTTPFile(url, **file_kwargs))
        except RemoteReadError:
            raise
        except (OSError, ValueError, KeyError) as exc:
            raise RemoteReadError(f"could not open remote slide {url}: {exc}") from exc

    return SlideReader(url, mpp_x, mpp_y, source_format, opener=opener)


def read_first_page_description(url: str, **file_kwargs) -> str:
    """The ImageDescription of a remote TIFF's first page (Aperio puts ``AppMag``, ``MPP`` there).

    Only the header and the first IFD are fetched.
    """
    import tifffile

    handle = RangedHTTPFile(url, **file_kwargs)
    try:
        with tifffile.TiffFile(handle) as tif:
            return tif.pages.first.description or ""
    except (tifffile.TiffFileError, ValueError, KeyError) as exc:
        raise RemoteReadError(f"could not read the TIFF header of {url}: {exc}") from exc
