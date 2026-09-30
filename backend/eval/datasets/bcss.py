"""
BCSS (Breast Cancer Semantic Segmentation) dataset adapter.
Parses TCGA ROI masks, extracts slide barcodes, ROI offsets, and mask scales.
SPEC-02 §3.4, SPEC-05 §4.1, and WP-5.2.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any
import pandas as pd

from .base import DatasetAdapter, FetchedFile, load_config
from .manifest import empty_manifest, validate_manifest
from .storage import Storage

# Regex for parsing BCSS filenames:
# E.g. TCGA-A1-A0SK-01Z-00-DX1_1000_2000_3000_4000.png or TCGA-A1-A0SK-01Z-00-DX1_roi1.png
BCSS_BBOX_REGEX = re.compile(
    r"^(?P<barcode>TCGA-[A-Z0-9]{2}-[A-Z0-9]{4}(?:-[A-Za-z0-9]+)*)[_](?P<xmin>\d+)[_](?P<ymin>\d+)[_](?P<xmax>\d+)[_](?P<ymax>\d+)"
)
TCGA_BARCODE_REGEX = re.compile(
    r"(?P<barcode>TCGA-[A-Z0-9]{2}-[A-Z0-9]{4}(?:-[A-Za-z0-9]+)*)"
)


class BCSSAdapter(DatasetAdapter):
    key = "bcss"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_config().get("bcss", {})
        class_map = self.config.get("class_map")
        if not class_map:
            raise ValueError("eval/datasets/config.yaml bcss.class_map is missing or empty")
        self.class_map: dict[int, str] = {int(k): str(v) for k, v in class_map.items()}

        default_mpp = self.config.get("default_mpp")
        if default_mpp is None:
            raise ValueError("eval/datasets/config.yaml bcss.default_mpp is missing")
        self.default_mpp: float = float(default_mpp)

        self.default_scanner: str | None = self.config.get("default_scanner")
        default_mag = self.config.get("default_native_mag")
        self.default_native_mag: float | None = float(default_mag) if default_mag is not None else None
        self.specimen_type: str = str(self.config.get("specimen_type", "resection"))
        self.mpp_source: str = str(self.config.get("mpp_source", "dataset_doc"))
        self.gt_label_source: str = str(self.config.get("gt_label_source", "expert_consensus"))
        self.gt_label_confidence: str = str(self.config.get("gt_label_confidence", "high"))

    def parse_mask_filename(self, filename_or_path: str | Path) -> dict[str, Any]:
        """
        Parse mask file name to extract TCGA slide barcode, patient_id, and ROI bounding box.
        """
        name = Path(filename_or_path).stem

        # Match bounding box pattern
        match = BCSS_BBOX_REGEX.match(name)
        if match:
            barcode = match.group("barcode")
            xmin = int(match.group("xmin"))
            ymin = int(match.group("ymin"))
            xmax = int(match.group("xmax"))
            ymax = int(match.group("ymax"))
            bbox_px: tuple[int, int, int, int] | None = (xmin, ymin, xmax, ymax)
            bbox_um: tuple[float, float, float, float] | None = tuple(
                round(coord * self.default_mpp, 4) for coord in bbox_px
            )
        else:
            bc_match = TCGA_BARCODE_REGEX.search(name)
            barcode = bc_match.group("barcode") if bc_match else name
            bbox_px = None
            bbox_um = None

        # Standardize patient_id (TCGA-XX-YYYY)
        patient_id = barcode[:12] if barcode.startswith("TCGA-") and len(barcode) >= 12 else barcode

        return {
            "slide_barcode": barcode,
            "patient_id": patient_id,
            "roi_bbox_px": bbox_px,
            "roi_bbox_um": bbox_um,
            "mask_mpp": self.default_mpp,
        }

    def discover_from_directory(self, mask_dir: str | Path) -> pd.DataFrame:
        """
        Scan directory of mask files and emit ROI rows:
        (patient_id, slide_barcode, roi_bbox_um, mask_uri, mask_mpp)
        """
        p = Path(mask_dir)
        if not p.exists():
            return pd.DataFrame(columns=["patient_id", "slide_barcode", "roi_bbox_um", "mask_uri", "mask_mpp", "file_name"])

        rows = []
        for file_path in sorted(p.glob("*.*")):
            if file_path.suffix.lower() not in (".png", ".tif", ".tiff", ".jpg", ".jpeg"):
                continue
            meta = self.parse_mask_filename(file_path)
            rows.append({
                "patient_id": meta["patient_id"],
                "slide_barcode": meta["slide_barcode"],
                "roi_bbox_um": meta["roi_bbox_um"],
                "mask_uri": file_path.resolve().as_uri(),
                "mask_mpp": meta["mask_mpp"],
                "file_name": file_path.name,
            })

        return pd.DataFrame(rows)

    def discover(self, mask_dir_or_files: str | Path | list[str] | None = None) -> pd.DataFrame:
        if mask_dir_or_files is None:
            return pd.DataFrame(columns=["patient_id", "slide_barcode", "roi_bbox_um", "mask_uri", "mask_mpp", "file_name"])
        if isinstance(mask_dir_or_files, (str, Path)):
            return self.discover_from_directory(mask_dir_or_files)

        rows = []
        for f in mask_dir_or_files:
            meta = self.parse_mask_filename(f)
            path_obj = Path(f)
            rows.append({
                "patient_id": meta["patient_id"],
                "slide_barcode": meta["slide_barcode"],
                "roi_bbox_um": meta["roi_bbox_um"],
                "mask_uri": path_obj.resolve().as_uri() if path_obj.exists() else f"file:///{f}",
                "mask_mpp": meta["mask_mpp"],
                "file_name": path_obj.name,
            })
        return pd.DataFrame(rows)

    def fetch(self, row: pd.Series, dest: Storage) -> FetchedFile:
        """
        Stream mask payload to destination storage with real SHA-256 and MD5 hash computation.
        Raises FileNotFoundError if the source mask file cannot be found.
        """
        slide_id = str(row.get("slide_barcode", ""))
        if not slide_id or slide_id == "None":
            raise ValueError("Row missing valid 'slide_barcode'")

        mask_uri = str(row.get("mask_uri", ""))
        source_path: Path | None = None
        if mask_uri.startswith("file:///"):
            path_str = mask_uri[8:]
            if path_str.startswith("/") and len(path_str) > 2 and path_str[2] == ":":
                path_str = path_str[1:]
            source_path = Path(path_str)
        elif mask_uri and not mask_uri.startswith("gs://"):
            source_path = Path(mask_uri)

        if source_path is None or not source_path.exists():
            raise FileNotFoundError(f"BCSS source mask file not found: {mask_uri}")

        relpath = f"bcss/masks/{source_path.name}"
        sha256_hasher = hashlib.sha256()
        md5_hasher = hashlib.md5()
        total_bytes = 0
        chunk_size = 64 * 1024  # 64 KiB

        with open(source_path, "rb") as src, dest.open_write(relpath) as writer:
            while chunk := src.read(chunk_size):
                writer.write(chunk)
                sha256_hasher.update(chunk)
                md5_hasher.update(chunk)
                total_bytes += len(chunk)

        return FetchedFile(
            slide_id=slide_id,
            uri=dest.uri(relpath),
            sha256=sha256_hasher.hexdigest().lower(),
            md5=md5_hasher.hexdigest().lower(),
            size_bytes=total_bytes,
        )

    def labels(self) -> pd.DataFrame:
        """Ground-truth class definitions from configuration."""
        rows = [{"class_code": code, "class_name": name} for code, name in sorted(self.class_map.items())]
        return pd.DataFrame(rows)

    def to_manifest(self, discovered: pd.DataFrame, fetched: dict[str, FetchedFile]) -> pd.DataFrame:
        """
        Emit validated manifest conforming to MANIFEST_COLUMNS (SPEC-02 §5.1).
        """
        if discovered.empty or not fetched:
            return empty_manifest()

        rows = []
        for idx, row in discovered.iterrows():
            slide_barcode = str(row["slide_barcode"])
            if slide_barcode not in fetched:
                continue

            fetched_file = fetched[slide_barcode]
            mpp = float(row.get("mask_mpp", self.default_mpp))

            patient_id = str(row["patient_id"])
            parts = patient_id.split("-")
            tss = parts[1] if (patient_id.startswith("TCGA-") and len(parts) >= 2) else None

            manifest_row = {
                "dataset": self.key,
                "patient_id": patient_id,
                "slide_id": slide_barcode,
                "uri": fetched_file.uri,
                "sha256": fetched_file.sha256,
                "specimen_type": self.specimen_type,
                "mpp_override": mpp,
                "mpp_source": self.mpp_source,
                "native_mag": self.default_native_mag,
                "scanner": self.default_scanner,
                "tss": tss,
                "split": None,
                "gt_grade": None,
                "gt_total": None,
                "gt_tubule": None,
                "gt_pleo": None,
                "gt_mitoses": None,
                "gt_histotype": None,
                "gt_label_source": self.gt_label_source,
                "gt_label_confidence": self.gt_label_confidence,
                "regions_uri": str(row.get("mask_uri")),
            }
            rows.append(manifest_row)

        manifest_df = pd.DataFrame(rows)
        validate_manifest(manifest_df)
        return manifest_df
