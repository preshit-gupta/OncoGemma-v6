"""
Storage protocols and adapters for local filesystem and Google Cloud Storage (GCS).
SPEC-02 §3 and WP-5.2.
"""
from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import urlparse


class Storage(Protocol):
    def open_write(self, relpath: str) -> BinaryIO:
        """Open a streaming binary writer for the target relative path."""
        ...

    def exists(self, relpath: str) -> bool:
        """Check whether the target relative path exists."""
        ...

    def uri(self, relpath: str) -> str:
        """Return the fully qualified URI (file:///... or gs://bucket/...)."""
        ...


class LocalStorage(Storage):
    """Local filesystem storage adapter for development and testing."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir).resolve()

    def _path(self, relpath: str) -> Path:
        clean_rel = relpath.lstrip("/\\")
        return self.base_dir / clean_rel

    def exists(self, relpath: str) -> bool:
        return self._path(relpath).exists()

    def get_size(self, relpath: str) -> int:
        p = self._path(relpath)
        return p.stat().st_size if p.exists() else 0

    def open_write(self, relpath: str) -> BinaryIO:
        p = self._path(relpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        return open(p, "wb")

    def open_read(self, relpath: str) -> BinaryIO:
        p = self._path(relpath)
        return open(p, "rb")

    def open_append(self, relpath: str) -> BinaryIO:
        p = self._path(relpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        return open(p, "ab")

    def uri(self, relpath: str) -> str:
        return self._path(relpath).as_uri()


class GcsStorage(Storage):
    """Google Cloud Storage adapter that streams directly to GCS without loading into RAM."""

    def __init__(self, bucket_name: str, prefix: str = "", client: object | None = None) -> None:
        self.bucket_name = bucket_name
        self.prefix = prefix.strip("/")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from google.cloud import storage as gcs  # type: ignore
            self._client = gcs.Client()
        return self._client

    @property
    def bucket(self):
        return self.client.bucket(self.bucket_name)

    def _blob_name(self, relpath: str) -> str:
        clean_rel = relpath.lstrip("/")
        return f"{self.prefix}/{clean_rel}" if self.prefix else clean_rel

    def exists(self, relpath: str) -> bool:
        blob = self.bucket.blob(self._blob_name(relpath))
        return bool(blob.exists())

    def open_write(self, relpath: str) -> BinaryIO:
        blob = self.bucket.blob(self._blob_name(relpath))
        return blob.open("wb")

    def open_read(self, relpath: str) -> BinaryIO:
        blob = self.bucket.blob(self._blob_name(relpath))
        return blob.open("rb")

    def uri(self, relpath: str) -> str:
        return f"gs://{self.bucket_name}/{self._blob_name(relpath)}"


def storage_from_uri(uri: str) -> Storage:
    """Factory creating LocalStorage or GcsStorage from a target URI string."""
    if uri.startswith("gs://"):
        parsed = urlparse(uri)
        bucket = parsed.netloc
        prefix = parsed.path.lstrip("/")
        return GcsStorage(bucket_name=bucket, prefix=prefix)
    if uri.startswith("file://"):
        # Strip scheme for local path
        path_str = uri[7:]
        # On Windows file:///D:/... -> D:/...
        if path_str.startswith("/") and len(path_str) > 2 and path_str[2] == ":":
            path_str = path_str[1:]
        return LocalStorage(Path(path_str))
    return LocalStorage(Path(uri))
