"""Blob storage used by the gateway: output cache, verbatim VLM answers, entity sidecars."""
from typing import Protocol

from google.api_core.exceptions import NotFound

from app.core import gcs


class BlobStore(Protocol):
    def read(self, path: str) -> bytes | None:
        """The blob's bytes, or None when it does not exist. Any other failure raises."""
        ...

    def write(self, path: str, data: bytes, content_type: str) -> str:
        """Store ``data`` at ``path`` and return its ``gs://`` URI."""
        ...


class GcsBlobStore:
    """Blobs in one GCS bucket (the artifacts bucket in the worker)."""

    def __init__(self, bucket: str):
        self.bucket = bucket

    def read(self, path: str) -> bytes | None:
        try:
            return gcs.download_blob_as_bytes(self.bucket, path)
        except (NotFound, FileNotFoundError):  # FileNotFoundError: the local GCS mock
            return None

    def write(self, path: str, data: bytes, content_type: str) -> str:
        gcs.upload_blob_from_bytes(self.bucket, path, data, content_type)
        return f"gs://{self.bucket}/{path}"


class LocalBlobStore:
    """Raw outputs and cache entries under a local directory (evaluation runs off the cloud)."""

    def __init__(self, root):
        from pathlib import Path

        self.root = Path(root)

    def read(self, path: str) -> bytes | None:
        target = self.root / path
        return target.read_bytes() if target.is_file() else None

    def write(self, path: str, data: bytes, content_type: str) -> str:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target.resolve().as_uri()
