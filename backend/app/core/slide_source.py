"""
Where a slide's pixels come from: one GCS object, or a DICOM WSI series under a GCS prefix.

A slide URI is either a single object (``gs://bucket/path/slide.svs``) or, when it ends with
``/``, the prefix of a DICOM WSI series (``gs://idc-open-data/<crdc_series_uuid>/``): every
``.dcm`` instance under it is one pyramid level, label or overview of the same slide. NCI's
Imaging Data Commons hosts TCGA-BRCA this way in a public bucket, so the harness reads those
slides in place (owner decisions 2026-09-30: no bulk copy; 2026-10-04: IDC DICOM).

``download_slide`` puts the slide in a scratch directory and returns the path OpenSlide opens:
the object itself, or any instance of the series (OpenSlide 4 finds the rest of the series in the
same directory). Every stage reads slides through it, so a series and a single file behave alike.
"""
from __future__ import annotations

import hashlib
import os

from app.core.gcs import download_blob_to_filename, get_bucket, parse_gcs_uri

DICOM_SUFFIX = ".dcm"
SERIES_DIRNAME = "slide_series"
_CHUNK = 1024 * 1024


class SlideSourceMissingError(FileNotFoundError):
    """The slide URI names nothing in GCS (no object, or a series prefix with no instance)."""


def is_series_uri(uri: str) -> bool:
    """A ``gs://`` prefix ending in ``/`` is a DICOM WSI series."""
    return uri.startswith("gs://") and uri.endswith("/")


def series_instances(uri: str, bucket=None) -> list:
    """The series' ``.dcm`` blobs, sorted by name. Raises ``SlideSourceMissingError`` when there are none.

    ``bucket`` lists through another client (an anonymous one for a public bucket); default: the app's.
    """
    bucket_name, prefix = parse_gcs_uri(uri)
    prefix = prefix.rstrip("/") + "/"
    bucket = get_bucket(bucket_name) if bucket is None else bucket
    blobs = [b for b in bucket.list_blobs(prefix=prefix) if b.name.lower().endswith(DICOM_SUFFIX)]
    if not blobs:
        raise SlideSourceMissingError(f"{uri} has no DICOM instance ({DICOM_SUFFIX})")
    return sorted(blobs, key=lambda b: b.name)


def slide_size_bytes(uri: str) -> int:
    """Bytes a download of the slide takes: the object, or the sum of the series' instances."""
    bucket_name, name = parse_gcs_uri(uri)
    if is_series_uri(uri):
        return sum(int(b.size) for b in series_instances(uri))
    blob = get_bucket(bucket_name).get_blob(name)
    if blob is None:
        raise SlideSourceMissingError(f"{uri} does not exist")
    return int(blob.size)


def download_slide(uri: str, scratch_dir: str) -> str:
    """Downloads the slide into ``scratch_dir`` and returns the path to open with OpenSlide."""
    bucket_name, name = parse_gcs_uri(uri)
    if not is_series_uri(uri):
        ext = os.path.splitext(name)[1]
        local = os.path.join(scratch_dir, f"slide{ext if len(ext) >= 2 else '.svs'}")
        download_blob_to_filename(bucket_name, name, local)
        return local
    series_dir = os.path.join(scratch_dir, SERIES_DIRNAME)
    os.makedirs(series_dir, exist_ok=True)
    paths = []
    for blob in series_instances(uri):
        local = os.path.join(series_dir, os.path.basename(blob.name))
        download_blob_to_filename(bucket_name, blob.name, local)
        paths.append(local)
    return paths[0]


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def series_sha256(uri: str, bucket=None) -> str:
    """A DICOM series' fingerprint: SHA-256 of ``<instance> <md5>\\n`` lines, instances sorted by name.

    The MD5s are the ones GCS stores for each object, and the storage client checks every download
    against them, so the fingerprint stands for the bytes a stage reads without hashing a whole
    series locally. It changes when any instance's bytes or the set of instances change. A
    manifest records it before any download (``eval/datasets/idc.py``); ingest recomputes it.
    """
    lines = []
    for blob in series_instances(uri, bucket):
        if not blob.md5_hash:
            raise SlideSourceMissingError(f"gs://.../{blob.name} has no stored MD5, so the series cannot be fingerprinted")
        lines.append(f"{os.path.basename(blob.name)} {blob.md5_hash}\n")
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def local_slide_sha256(local_path: str, uri: str) -> str:
    """The slide's SHA-256: of the downloaded file, or a series' fingerprint (``series_sha256``)."""
    return series_sha256(uri) if is_series_uri(uri) else _file_sha256(local_path)
