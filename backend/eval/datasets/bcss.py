"""
BCSS (Breast Cancer Semantic Segmentation) dataset adapter.
Parses TCGA ROI masks, extracts slide barcodes, ROI offsets, and mask scales.
SPEC-02 §3.4, SPEC-05 §4.1, and WP-5.2.
"""
from __future__ import annotations

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
    r"^(?P<barcode>TCGA-[A-Z0-9]{2}-[A-Z0-9]{4}[A-Za-z0-9\-_]*?)[_](?P<xmin>\d+)[_](?P<ymin>\d+)[_](?P<xmax>\d+)[_](?P<ymax>\d+)"
)
TCGA_BARCODE_REGEX = re.compile(r"(?P<barcode>TCGA-[A-Z0-9]{2}-[A-Z0-9]{4}[A-Za-z0-9\-_]*)")


class BCSSAdapter(DatasetAdapter):
    key = "bcss"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_config().get("bcss", {})
        self.class_map: dict[int, str] = self.config.get(
            "class_map",
            {
                0: "outside_roi",
                1: "tumor",
                2: "stroma",
                3: "inflammatory",
                4: "necrosis",
                5: "benign_epithelium",
            },
        )
        self.default_mpp: float = float(self.config.get("default_mpp", 0.25))
        self.default_scanner: str | None = self.config.get("default_scanner")

    def parse_mask_filename(self, filename_or_path: str | Path) -> dict[str, Any]:
        """
        Parse mask file name to extract TCGA slide barcode, patient_id, and ROI bounding box.
        """
        name = Path(filename_or_path).stem

        # Try matching bounding box pattern
        match = BCSS_BBOX_REGEX.match(name)
        if match:
            barcode = match.group("barcode")
            xmin = int(match.group("xmin"))
            ymin = int(match.group("ymin"))
            xmax = int(match.group("xmax"))
            ymax = int(match.group("ymax"))
            bbox_px = (xmin, ymin, xmax, ymax)
        else:
            # Fallback to barcode search
            bc_match = TCGA_BARCODE_REGEX.search(name)
            barcode = bc_match.group("barcode") if bc_match else name
            bbox_px = (0, 0, 0, 0)

        # Standardize patient_id (TCGA-XX-YYYY)
        patient_id = barcode[:12] if barcode.startswith("TCGA-") and len(barcode) >= 12 else barcode

        # Calculate bounding box in µm
        bbox_um = tuple(round(coord * self.default_mpp, 4) for coord in bbox_px)

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
            return pd.DataFrame(columns=["patient_id", "slide_barcode", "roi_bbox_um", "mask_uri", "mask_mpp"])

        rows = []
        for file_path in p.glob("*.*"):
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
            return pd.DataFrame(columns=["patient_id", "slide_barcode", "roi_bbox_um", "mask_uri", "mask_mpp"])
        if isinstance(mask_dir_or_files, (str, Path)):
            return self.discover_from_directory(mask_dir_or_files)
        # List of file paths
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
        slide_id = str(row.get("slide_barcode", "bcss_slide"))
        return FetchedFile(
            slide_id=slide_id,
            uri=row.get("mask_uri", dest.uri(f"bcss/{slide_id}.png")),
            sha256=str(row.get("sha256", "0" * 64)),
            md5=None,
            size_bytes=int(row.get("size_bytes", 0)),
        )

    def labels(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["patient_id", "slide_id", "class_map"])

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

            manifest_row = {
                "dataset": self.key,
                "patient_id": str(row["patient_id"]),
                "slide_id": slide_barcode,
                "uri": fetched_file.uri,
                "sha256": fetched_file.sha256,
                "specimen_type": "resection",
                "mpp_override": mpp,
                "mpp_source": "dataset_doc",
                "native_mag": 40.0,
                "scanner": self.default_scanner,
                "tss": row["patient_id"][5:7] if len(row["patient_id"]) >= 7 else None,
                "split": None,
                "gt_grade": None,
                "gt_total": None,
                "gt_tubule": None,
                "gt_pleo": None,
                "gt_mitoses": None,
                "gt_histotype": None,
                "gt_label_source": "expert_consensus",
                "gt_label_confidence": "high",
                "regions_uri": str(row.get("mask_uri")),
            }
            rows.append(manifest_row)

        manifest_df = pd.DataFrame(rows)
        validate_manifest(manifest_df)
        return manifest_df
