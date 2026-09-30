"""
TCGA-BRCA diagnostic slide adapter, GDC discovery, streaming download, and manifest generation.
SPEC-02 §3.1 and WP-5.2.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable
import httpx
import pandas as pd

from .base import DatasetAdapter, FetchedFile, FetchIntegrityError, SlideMetadataError, load_config
from .manifest import empty_manifest, validate_manifest
from .storage import LocalStorage, Storage

GDC_API_URL = "https://api.gdc.cancer.gov"
CHUNK_SIZE_BYTES = 8 * 1024 * 1024  # 8 MiB per SPEC-02 §3.1


class TCGABRCAAdapter(DatasetAdapter):
    key = "tcga_brca_dx"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        http_client: httpx.Client | None = None,
        slide_reader: Callable[[pd.Series, str | None], dict[str, Any]] | None = None,
    ) -> None:
        self.config = config or load_config().get("tcga_brca_dx", {})
        self.api_url = self.config.get("api_url", GDC_API_URL)
        self.chunk_size = self.config.get("chunk_size_bytes", CHUNK_SIZE_BYTES)
        self.timeout = self.config.get("timeout_seconds", 60.0)
        self._client = http_client
        self.slide_reader = slide_reader

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def discover(self, page_size: int = 1000) -> pd.DataFrame:
        """
        Discover TCGA-BRCA diagnostic slide images via GDC POST /files API.
        Filters for open diagnostic slide images, paginated with 'size' and 'from'.
        Keeps only file names containing '-DX'.
        """
        endpoint = f"{self.api_url.rstrip('/')}/files"
        filters = {
            "op": "and",
            "content": [
                {"op": "in", "content": {"field": "cases.project.project_id", "value": ["TCGA-BRCA"]}},
                {"op": "in", "content": {"field": "files.data_type", "value": ["Slide Image"]}},
                {"op": "in", "content": {"field": "files.experimental_strategy", "value": ["Diagnostic Slide"]}},
                {"op": "in", "content": {"field": "files.access", "value": ["open"]}},
            ],
        }

        all_hits: list[dict[str, Any]] = []
        from_idx = 0

        while True:
            payload = {
                "filters": filters,
                "fields": "file_id,file_name,md5sum,file_size,cases.submitter_id",
                "format": "JSON",
                "size": page_size,
                "from": from_idx,
            }
            resp = self.client.post(endpoint, json=payload)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            hits = data.get("hits", [])
            if not hits:
                break
            all_hits.extend(hits)
            pagination = data.get("pagination", {})
            total = pagination.get("total", 0)
            from_idx += len(hits)
            if from_idx >= total or len(hits) < page_size:
                break

        # Process and filter hits
        records = []
        for hit in all_hits:
            file_name = hit.get("file_name", "")
            # Filter: only *-DX* diagnostic slides, exclude *-TS* and *-BS*
            if "-DX" not in file_name:
                continue

            cases = hit.get("cases", [])
            submitter_id = cases[0].get("submitter_id") if cases else None
            if not submitter_id and file_name.startswith("TCGA-"):
                # Extract barcode from file_name if not in cases
                parts = file_name.split(".")
                submitter_id = parts[0][:12]

            patient_id = submitter_id[:12] if submitter_id else ""
            tss = submitter_id[5:7] if submitter_id and len(submitter_id) >= 7 else ""

            records.append({
                "file_id": hit.get("file_id") or hit.get("id"),
                "file_name": file_name,
                "md5sum": hit.get("md5sum"),
                "file_size": hit.get("file_size"),
                "submitter_id": submitter_id,
                "patient_id": patient_id,
                "tss": tss,
            })

        return pd.DataFrame(records)

    def discover_reports(self, page_size: int = 1000) -> pd.DataFrame:
        """
        Discover pathology reports for TCGA-BRCA via GDC POST /files API.
        Filters for open access Pathology Reports.
        """
        endpoint = f"{self.api_url.rstrip('/')}/files"
        filters = {
            "op": "and",
            "content": [
                {"op": "in", "content": {"field": "cases.project.project_id", "value": ["TCGA-BRCA"]}},
                {"op": "in", "content": {"field": "files.data_type", "value": ["Pathology Report"]}},
                {"op": "in", "content": {"field": "files.access", "value": ["open"]}},
            ],
        }

        all_hits: list[dict[str, Any]] = []
        from_idx = 0

        while True:
            payload = {
                "filters": filters,
                "fields": "file_id,file_name,md5sum,file_size,cases.submitter_id",
                "format": "JSON",
                "size": page_size,
                "from": from_idx,
            }
            resp = self.client.post(endpoint, json=payload)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            hits = data.get("hits", [])
            if not hits:
                break
            all_hits.extend(hits)
            pagination = data.get("pagination", {})
            total = pagination.get("total", 0)
            from_idx += len(hits)
            if from_idx >= total or len(hits) < page_size:
                break

        records = []
        for hit in all_hits:
            file_name = hit.get("file_name", "")
            cases = hit.get("cases", [])
            submitter_id = cases[0].get("submitter_id") if cases else None
            patient_id = submitter_id[:12] if submitter_id else ""
            tss = submitter_id[5:7] if submitter_id and len(submitter_id) >= 7 else ""

            records.append({
                "file_id": hit.get("file_id") or hit.get("id"),
                "file_name": file_name,
                "md5sum": hit.get("md5sum"),
                "file_size": hit.get("file_size"),
                "submitter_id": submitter_id,
                "patient_id": patient_id,
                "tss": tss,
            })

        return pd.DataFrame(records)

    def fetch(self, row: pd.Series, dest: Storage) -> FetchedFile:
        """
        Download slide payload with 8 MiB chunk streaming, hash verification,
        and resume support via HTTP Range when partial files exist.
        """
        file_id = str(row["file_id"])
        expected_md5 = str(row["md5sum"]).strip().lower() if pd.notna(row.get("md5sum")) else None
        expected_size = int(row["file_size"]) if pd.notna(row.get("file_size")) else None
        relpath = f"tcga-brca/dx/{file_id}.svs"

        url = f"{self.api_url.rstrip('/')}/data/{file_id}"
        headers: dict[str, str] = {}
        existing_bytes = 0
        md5_hasher = hashlib.md5()
        sha256_hasher = hashlib.sha256()

        # Check for resumable partial download in LocalStorage
        is_local = isinstance(dest, LocalStorage)
        if is_local and dest.exists(relpath):
            existing_bytes = dest.get_size(relpath)
            if expected_size is not None and existing_bytes == expected_size:
                # Already fully downloaded: verify hash
                with dest.open_read(relpath) as f:
                    while chunk := f.read(self.chunk_size):
                        md5_hasher.update(chunk)
                        sha256_hasher.update(chunk)
                computed_md5 = md5_hasher.hexdigest().lower()
                computed_sha256 = sha256_hasher.hexdigest().lower()
                if expected_md5 is None or computed_md5 == expected_md5:
                    return FetchedFile(
                        slide_id=file_id,
                        uri=dest.uri(relpath),
                        sha256=computed_sha256,
                        md5=computed_md5,
                        size_bytes=existing_bytes,
                    )
                # Hash mismatch on existing file: redownload from zero
                existing_bytes = 0
                md5_hasher = hashlib.md5()
                sha256_hasher = hashlib.sha256()

            elif expected_size is not None and existing_bytes < expected_size:
                # Partial file: read existing bytes to prime hashers
                with dest.open_read(relpath) as f:
                    while chunk := f.read(self.chunk_size):
                        md5_hasher.update(chunk)
                        sha256_hasher.update(chunk)
                headers["Range"] = f"bytes={existing_bytes}-"
            else:
                existing_bytes = 0

        # Execute streaming request
        total_written = existing_bytes
        with self.client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 206:
                # Resumed partial content
                out_writer = dest.open_append(relpath) if is_local else dest.open_write(relpath)
            elif resp.status_code in (200, 201):
                # Full content received
                if existing_bytes > 0:
                    # Reset hashers if server provided full stream instead of range
                    md5_hasher = hashlib.md5()
                    sha256_hasher = hashlib.sha256()
                    total_written = 0
                out_writer = dest.open_write(relpath)
            elif resp.status_code == 416:
                # Range not satisfiable, retry full download
                md5_hasher = hashlib.md5()
                sha256_hasher = hashlib.sha256()
                total_written = 0
                with self.client.stream("GET", url) as retry_resp:
                    retry_resp.raise_for_status()
                    with dest.open_write(relpath) as w:
                        for chunk in retry_resp.iter_bytes(chunk_size=self.chunk_size):
                            if chunk:
                                w.write(chunk)
                                md5_hasher.update(chunk)
                                sha256_hasher.update(chunk)
                                total_written += len(chunk)
                out_writer = None
            else:
                resp.raise_for_status()
                out_writer = dest.open_write(relpath)

            if out_writer is not None:
                with out_writer as w:
                    for chunk in resp.iter_bytes(chunk_size=self.chunk_size):
                        if chunk:
                            w.write(chunk)
                            md5_hasher.update(chunk)
                            sha256_hasher.update(chunk)
                            total_written += len(chunk)

        computed_md5 = md5_hasher.hexdigest().lower()
        computed_sha256 = sha256_hasher.hexdigest().lower()

        # Integrity verification
        if expected_md5 and computed_md5 != expected_md5:
            raise FetchIntegrityError(
                f"MD5 mismatch for file {file_id}: expected {expected_md5}, got {computed_md5}"
            )
        if expected_size is not None and total_written != expected_size:
            raise FetchIntegrityError(
                f"Size mismatch for file {file_id}: expected {expected_size} bytes, wrote {total_written} bytes"
            )

        return FetchedFile(
            slide_id=file_id,
            uri=dest.uri(relpath),
            sha256=computed_sha256,
            md5=computed_md5,
            size_bytes=total_written,
        )

    def fetch_many(self, rows: pd.DataFrame, dest: Storage, confirm_bytes: int) -> dict[str, FetchedFile]:
        """
        Fetch multiple slides guarded by confirmation bytes threshold.
        Raises ValueError unless confirm_bytes >= sum(file_size).
        """
        total_size = int(rows["file_size"].sum()) if "file_size" in rows.columns and not rows.empty else 0
        if confirm_bytes < total_size:
            raise ValueError(
                f"confirm_bytes ({confirm_bytes}) is less than total required download bytes ({total_size}). "
                f"Confirm download by providing --confirm-bytes {total_size} or greater."
            )

        fetched_files: dict[str, FetchedFile] = {}
        for _, row in rows.iterrows():
            f_file = self.fetch(row, dest)
            fetched_files[str(row["file_id"])] = f_file

        return fetched_files

    def slide_meta(self, row: pd.Series, slide_path: str | None = None) -> dict[str, Any]:
        """
        Read slide metadata (mpp, native_mag, scanner, tss).
        Injectable via self.slide_reader or reads OpenSlide properties from slide_path.
        Raises SlideMetadataError when slide_path is given but OpenSlide cannot open it.
        """
        if self.slide_reader is not None:
            return self.slide_reader(row, slide_path)

        mpp: float | None = None
        native_mag: float | None = None
        scanner: str | None = None

        if slide_path:
            import openslide  # type: ignore
            try:
                with openslide.OpenSlide(str(slide_path)) as slide:
                    props = dict(slide.properties)
            except (
                openslide.OpenSlideError,
                openslide.OpenSlideUnsupportedFormatError,
                FileNotFoundError,
            ) as exc:
                raise SlideMetadataError(str(slide_path), f"{type(exc).__name__}: {exc}") from exc
            mpp_val = props.get("openslide.mpp-x") or props.get("aperio.MPP")
            if mpp_val:
                mpp = float(mpp_val)
            mag_val = props.get("aperio.AppMag")
            if mag_val:
                native_mag = float(mag_val)
            scanner = props.get("openslide.vendor") or props.get("aperio.Scanner")

        tss = row.get("tss")
        if not tss and "submitter_id" in row and pd.notna(row["submitter_id"]):
            parts = str(row["submitter_id"]).split("-")
            if len(parts) >= 2:
                tss = parts[1]

        return {
            "specimen_type": self.config.get("specimen_type", "resection"),
            "mpp_override": mpp,
            "mpp_source": self.config.get("default_mpp_source", "file") if mpp is not None else self.config.get("fallback_mpp_source", "dataset_doc"),
            "native_mag": native_mag,
            "scanner": scanner or self.config.get("default_scanner"),
            "tss": str(tss) if tss else None,
        }

    def labels(self, label_path: Path | str | None = None) -> pd.DataFrame:
        """
        Load ground truth labels for TCGA-BRCA slides (histologic type and grades).
        SPEC-02 §3.1.
        """
        path = Path(label_path) if label_path else Path(__file__).parent / "labels" / "tcga_histotype.csv"
        if not path.exists():
            raise FileNotFoundError(f"TCGA ground-truth histologic type file not found: {path}")
        return pd.read_csv(path)

    def to_manifest(
        self,
        discovered: pd.DataFrame,
        fetched: dict[str, FetchedFile],
        local_slide_paths: dict[str, str] | None = None,
    ) -> pd.DataFrame:
        """
        Convert discovered rows and fetched files into a validated manifest DataFrame (SPEC-02 §5.1).
        """
        if discovered.empty or not fetched:
            return empty_manifest()

        # Subset to fetched slides
        fetched_ids = set(fetched.keys())
        target_df = discovered[discovered["file_id"].astype(str).isin(fetched_ids)].copy()

        local_paths = local_slide_paths or {}
        rows = []
        for _, row in target_df.iterrows():
            fid = str(row["file_id"])
            fetched_file = fetched[fid]
            slide_local = local_paths.get(fid)
            meta = self.slide_meta(row, slide_local)

            manifest_row = {
                "dataset": self.key,
                "patient_id": str(row["patient_id"]),
                "slide_id": fid,
                "uri": fetched_file.uri,
                "sha256": fetched_file.sha256,
                "specimen_type": meta["specimen_type"],
                "mpp_override": meta["mpp_override"],
                "mpp_source": meta["mpp_source"],
                "native_mag": meta["native_mag"],
                "scanner": meta["scanner"],
                "tss": meta["tss"],
                "split": None,  # Null prior to split generation
                "gt_grade": None,
                "gt_total": None,
                "gt_tubule": None,
                "gt_pleo": None,
                "gt_mitoses": None,
                "gt_histotype": None,
                "gt_label_source": None,
                "gt_label_confidence": None,
                "regions_uri": None,
            }
            rows.append(manifest_row)

        manifest_df = pd.DataFrame(rows)
        validate_manifest(manifest_df)
        return manifest_df


class TUPAC16Adapter(DatasetAdapter):
    """Optional adapter for TUPAC16 benchmark dataset (SPEC-00 §3, SPEC-02 §3.4)."""
    key = "tupac16"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    def discover(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["patient_id", "slide_id", "mitotic_score"])

    def fetch(self, row: pd.Series, dest: Storage) -> FetchedFile:
        raise NotImplementedError("TUPAC16 download availability is optional and unverified (SPEC-00 §3.1)")

    def labels(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["patient_id", "slide_id", "gt_mitoses"])

    def to_manifest(self, discovered: pd.DataFrame, fetched: dict[str, FetchedFile]) -> pd.DataFrame:
        return empty_manifest()
