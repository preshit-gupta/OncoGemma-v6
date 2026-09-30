"""
BCSS (Breast Cancer Semantic Segmentation) dataset adapter.
Parses TCGA ROI masks, extracts slide barcodes, ROI offsets, and mask scales.
SPEC-02 §3.4, SPEC-05 §4.1, and WP-5.2.

Checked against the BCSS repository (PathologyDataScience/BCSS) on 2026-09-30:
- ``download_crowdsource_dataset.py`` names each mask ``"%s_xmin%d_ymin%d_%s.png"`` from the
  ``meta/roiBounds.csv`` slide name, the ROI's top-left corner and ``MPP-%.4f`` (default 0.25), e.g.
  ``TCGA-A1-A0SK-DX1_xmin45749_ymin25055_MPP-0.2500.png``;
- ``xmin``/``ymin`` are passed unchanged as ``left``/``top`` of the HistomicsTK region request, so they
  are in the slide's base (level-0) pixels, while the mask's own pixels are at the ``MPP`` in the name;
- the slide name is the short barcode ``TCGA-XX-YYYY-DXn``, not the GDC file name
  ``TCGA-XX-YYYY-01Z-00-DXn.<uuid>.svs``; join to TCGA on ``patient_id``;
- ``meta/gtruth_codes.tsv`` has 22 codes (0..21), kept in config.yaml ``bcss.class_map``.
A name in any other form (e.g. ``MAG-...`` downloads) raises ``BCSSNameError``.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping
import pandas as pd
from PIL import Image

from .base import DatasetAdapter, FetchedFile, load_config, require_config
from .manifest import empty_manifest, validate_manifest
from .storage import Storage, copy_with_hashes, local_path_from_uri

BCSS_MASK_NAME_REGEX = re.compile(
    r"^(?P<barcode>TCGA-[A-Z0-9]{2}-[A-Z0-9]{4}(?:-[A-Za-z0-9]+)*)"
    r"_xmin(?P<xmin>\d+)_ymin(?P<ymin>\d+)_MPP-(?P<mpp>\d+(?:\.\d+)?)$"
)
REQUIRED_KEYS = (
    "class_map",
    "specimen_type",
    "mpp_source",
    "gt_label_source",
    "gt_label_confidence",
)
DISCOVER_COLUMNS = [
    "patient_id",
    "slide_barcode",
    "roi_origin_px",
    "roi_size_px",
    "roi_bbox_um",
    "mask_uri",
    "mask_mpp",
    "file_name",
]


class BCSSNameError(ValueError):
    """A BCSS mask file name is not in the ``<slide>_xmin<X>_ymin<Y>_MPP-<mpp>`` form."""


class BCSSAdapter(DatasetAdapter):
    key = "bcss"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config if config is not None else load_config().get("bcss", {})
        require_config(self.config, REQUIRED_KEYS, "bcss")
        self.class_map: dict[int, str] = {int(k): str(v) for k, v in self.config["class_map"].items()}
        self.specimen_type = str(self.config["specimen_type"])
        self.mpp_source = str(self.config["mpp_source"])
        self.gt_label_source = str(self.config["gt_label_source"])
        self.gt_label_confidence = str(self.config["gt_label_confidence"])

    def parse_mask_filename(self, filename_or_path: str | Path) -> dict[str, Any]:
        """
        Slide barcode, patient_id, ROI top-left corner (slide base pixels) and mask µm/px from a mask name.
        """
        name = Path(filename_or_path).stem
        match = BCSS_MASK_NAME_REGEX.match(name)
        if match is None:
            raise BCSSNameError(
                f"BCSS mask name {name!r} is not '<slide>_xmin<X>_ymin<Y>_MPP-<mpp>' "
                "(the form written by BCSS download_crowdsource_dataset.py)"
            )
        barcode = match.group("barcode")
        return {
            "slide_barcode": barcode,
            "patient_id": barcode[:12],
            "roi_origin_px": (int(match.group("xmin")), int(match.group("ymin"))),
            "mask_mpp": float(match.group("mpp")),
        }

    @staticmethod
    def roi_bbox_um(
        roi_origin_px: tuple[int, int], roi_size_px: tuple[int, int], mask_mpp: float, slide_mpp: float
    ) -> tuple[float, float, float, float]:
        """ROI (x0, y0, x1, y1) in slide µm: the origin is in slide base pixels, the size in mask pixels."""
        x0 = roi_origin_px[0] * slide_mpp
        y0 = roi_origin_px[1] * slide_mpp
        return (
            round(x0, 4),
            round(y0, 4),
            round(x0 + roi_size_px[0] * mask_mpp, 4),
            round(y0 + roi_size_px[1] * mask_mpp, 4),
        )

    def discover(
        self,
        mask_dir_or_files: str | Path | list[str | Path],
        slide_mpp: Mapping[str, float] | None = None,
    ) -> pd.DataFrame:
        """
        One ROI row per mask: (patient_id, slide_barcode, roi_origin_px, roi_size_px, roi_bbox_um, mask_uri, mask_mpp).

        ``roi_bbox_um`` needs the base µm/px of the TCGA slide (``slide_mpp`` keyed by slide barcode,
        from TCGA ``slide_meta``); it is None for slides not in ``slide_mpp``.
        """
        if isinstance(mask_dir_or_files, (str, Path)):
            directory = Path(mask_dir_or_files)
            if not directory.is_dir():
                raise FileNotFoundError(f"BCSS mask directory not found: {directory}")
            files = sorted(directory.glob("*.png"))
        else:
            files = [Path(f) for f in mask_dir_or_files]

        slide_mpp = slide_mpp or {}
        rows = []
        for file_path in files:
            meta = self.parse_mask_filename(file_path)
            if not file_path.is_file():
                raise FileNotFoundError(f"BCSS mask file not found: {file_path}")
            with Image.open(file_path) as mask:
                roi_size_px = mask.size
            base_mpp = slide_mpp.get(meta["slide_barcode"])
            rows.append({
                "patient_id": meta["patient_id"],
                "slide_barcode": meta["slide_barcode"],
                "roi_origin_px": meta["roi_origin_px"],
                "roi_size_px": roi_size_px,
                "roi_bbox_um": None if base_mpp is None else self.roi_bbox_um(
                    meta["roi_origin_px"], roi_size_px, meta["mask_mpp"], float(base_mpp)
                ),
                "mask_uri": file_path.resolve().as_uri(),
                "mask_mpp": meta["mask_mpp"],
                "file_name": file_path.name,
            })
        return pd.DataFrame(rows, columns=DISCOVER_COLUMNS)

    def fetch(self, row: pd.Series, dest: Storage) -> FetchedFile:
        """
        Stream mask payload to destination storage with real SHA-256 and MD5 hash computation.
        Raises FileNotFoundError if the source mask file cannot be found.
        """
        slide_id = row.get("slide_barcode")
        if slide_id is None or pd.isna(slide_id) or not str(slide_id):
            raise ValueError("BCSS row has no 'slide_barcode'")

        mask_uri = str(row["mask_uri"])
        source_path = local_path_from_uri(mask_uri)
        if not source_path.is_file():
            raise FileNotFoundError(f"BCSS source mask file not found: {mask_uri}")

        relpath = f"bcss/masks/{source_path.name}"
        sha256, md5, size = copy_with_hashes(source_path, dest, relpath)
        return FetchedFile(slide_id=str(slide_id), uri=dest.uri(relpath), sha256=sha256, md5=md5, size_bytes=size)

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
        for _, row in discovered.iterrows():
            slide_barcode = str(row["slide_barcode"])
            if slide_barcode not in fetched:
                continue

            fetched_file = fetched[slide_barcode]
            patient_id = str(row["patient_id"])
            rows.append({
                "dataset": self.key,
                "patient_id": patient_id,
                "slide_id": slide_barcode,
                "uri": fetched_file.uri,
                "sha256": fetched_file.sha256,
                "specimen_type": self.specimen_type,
                "mpp_override": float(row["mask_mpp"]),
                "mpp_source": self.mpp_source,
                "native_mag": None,  # varies by TCGA slide (20x/40x); join tcga_brca_dx for it
                "scanner": None,
                "tss": patient_id.split("-")[1],
                "split": None,
                "gt_grade": None,
                "gt_total": None,
                "gt_tubule": None,
                "gt_pleo": None,
                "gt_mitoses": None,
                "gt_histotype": None,
                "gt_label_source": self.gt_label_source,
                "gt_label_confidence": self.gt_label_confidence,
                "regions_uri": fetched_file.uri,
            })

        manifest_df = pd.DataFrame(rows)
        validate_manifest(manifest_df)
        return manifest_df
