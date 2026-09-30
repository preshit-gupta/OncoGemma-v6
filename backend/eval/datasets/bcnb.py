"""
BCNB (Early Breast Cancer Core-Needle Biopsy) dataset adapter.
SPEC-02 §3.2 and WP-5.2.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any, Callable
import pandas as pd

from .base import DatasetAdapter, DatasetConfigMissing, FetchedFile, load_config
from .manifest import empty_manifest, validate_manifest
from .storage import Storage, copy_with_hashes

REQUIRED_CONFIG_KEYS = (
    "mpp",
    "native_mag",
    "image_glob",
    "clinical_file",
    "grade_field",
    "grade_map",
    "tumor_polygons",
    "split_source",
    "license_ref",
)
# Fixed by SPEC-02 §3.2 rather than configured: BCNB slides are core-needle biopsies, their
# µm/px comes from the dataset documentation (config ``mpp``), and grades from the clinical file.
SPECIMEN_TYPE = "core_biopsy"
MPP_SOURCE = "dataset_doc"
GT_LABEL_SOURCE = "clinical_records"
GT_LABEL_CONFIDENCE = "high"


def convert_to_pyramidal_tiff(jpg_path: str | Path, out_path: str | Path, mpp: float) -> str:
    """
    Convert a plain JPEG WSI to a generic tiled pyramidal TIFF readable by OpenSlide.
    Uses pyvips with exact parameters from SPEC-02 §3.2.
    """
    import pyvips  # type: ignore

    out_p = Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    img = pyvips.Image.new_from_file(str(jpg_path))
    # libvips takes xres/yres in pixels per mm, hence 1000.0 / mpp.
    img.tiffsave(
        str(out_path),
        tile=True,
        tile_width=512,
        tile_height=512,
        pyramid=True,
        compression="jpeg",
        Q=90,
        bigtiff=True,
        xres=1000.0 / mpp,
        yres=1000.0 / mpp,
        resunit="cm",
    )
    return str(out_path)


class BCNBAdapter(DatasetAdapter):
    key = "bcnb"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        check_config_on_init: bool = True,
        converter: Callable[[Path, Path, float], str] = convert_to_pyramidal_tiff,
    ) -> None:
        self.config = config if config is not None else load_config().get("bcnb", {})
        self.converter = converter
        if check_config_on_init:
            self.validate_config()

    def validate_config(self) -> None:
        """
        Check that all required configuration keys are present and non-null.
        Raises DatasetConfigMissing listing all missing or null keys (SPEC-02 §3.2).
        """
        missing: list[str] = []
        for key in REQUIRED_CONFIG_KEYS:
            if key not in self.config or self.config[key] is None:
                missing.append(key)
            elif key == "tumor_polygons" and isinstance(self.config[key], dict):
                poly = self.config[key]
                if poly.get("path") is None or poly.get("format") is None:
                    missing.append("tumor_polygons")
            elif key == "grade_map" and not self.config[key]:
                missing.append("grade_map")

        if missing:
            raise DatasetConfigMissing(
                missing_keys=missing,
                message=f"BCNB configuration is incomplete. Missing required keys: {', '.join(missing)}",
            )

    def map_grades(self, clinical_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """
        Map histological grades using grade_map.
        Returns:
            valid_df: rows with successfully mapped grade (gt_grade in 1, 2, 3)
            excluded_df: rows with unmapped or missing grade, recording excluded_reason
        """
        grade_field = self.config["grade_field"]
        grade_map = self.config["grade_map"]

        valid_rows = []
        excluded_rows = []

        for _, row in clinical_df.iterrows():
            row_dict = row.to_dict()
            raw_grade = row_dict.get(grade_field)

            if pd.isna(raw_grade):
                row_dict["gt_grade"] = None
                row_dict["excluded_reason"] = "Missing grade value"
                excluded_rows.append(row_dict)
                continue

            mapped = grade_map.get(raw_grade) if raw_grade is not None else None

            if mapped in (1, 2, 3):
                row_dict["gt_grade"] = int(mapped)
                row_dict["excluded_reason"] = None
                valid_rows.append(row_dict)
            else:
                row_dict["gt_grade"] = None
                row_dict["excluded_reason"] = f"Unmapped grade value: {raw_grade}"
                excluded_rows.append(row_dict)

        return pd.DataFrame(valid_rows), pd.DataFrame(excluded_rows)

    def discover(self) -> pd.DataFrame:
        """Discover BCNB WSIs matching image_glob."""
        self.validate_config()
        image_glob = self.config["image_glob"]
        files = list(Path().glob(image_glob))

        rows = []
        for f in files:
            slide_id = f.stem
            patient_id = slide_id
            rows.append({
                "patient_id": patient_id,
                "slide_id": slide_id,
                "file_path": str(f.resolve()),
                "mpp": float(self.config["mpp"]),
                "native_mag": float(self.config["native_mag"]),
            })
        return pd.DataFrame(rows)

    def fetch(self, row: pd.Series, dest: Storage) -> FetchedFile:
        """
        Convert the slide's JPEG to a pyramidal TIFF (SPEC-02 §3.2), stream the TIFF into ``dest``
        and return its hashes: the manifest's canonical slide is the converted file.
        Raises FileNotFoundError if the source JPEG is missing.
        """
        self.validate_config()
        slide_id = str(row["slide_id"])
        source_path = Path(str(row["file_path"]))
        if not source_path.is_file():
            raise FileNotFoundError(f"BCNB source slide file not found: '{source_path}'")

        relpath = f"bcnb/slides/{slide_id}.tif"
        with tempfile.TemporaryDirectory(prefix="bcnb-") as work_dir:
            tiff_path = Path(work_dir) / f"{slide_id}.tif"
            self.converter(source_path, tiff_path, float(self.config["mpp"]))
            if not tiff_path.is_file():
                raise FileNotFoundError(f"BCNB conversion of {source_path} wrote no TIFF at {tiff_path}")
            sha256, md5, size = copy_with_hashes(tiff_path, dest, relpath)
        return FetchedFile(slide_id=slide_id, uri=dest.uri(relpath), sha256=sha256, md5=md5, size_bytes=size)

    def labels(self) -> pd.DataFrame:
        """Read clinical file and return mapped ground truth labels."""
        self.validate_config()
        clinical_file = self.config["clinical_file"]
        path = Path(clinical_file)
        if not path.exists():
            raise FileNotFoundError(f"BCNB clinical file not found: {clinical_file}")

        if clinical_file.endswith(".csv"):
            df = pd.read_csv(clinical_file)
        else:
            df = pd.read_excel(clinical_file)
        valid_df, _ = self.map_grades(df)
        return valid_df

    def to_manifest(self, discovered: pd.DataFrame, fetched: dict[str, FetchedFile]) -> pd.DataFrame:
        """
        Emit validated manifest conforming to MANIFEST_COLUMNS (SPEC-02 §5.1).
        """
        self.validate_config()
        if discovered.empty or not fetched:
            return empty_manifest()

        rows = []
        for _, row in discovered.iterrows():
            slide_id = str(row["slide_id"])
            if slide_id not in fetched:
                continue

            fetched_file = fetched[slide_id]
            mpp = float(self.config["mpp"])
            native_mag = float(self.config["native_mag"])

            manifest_row = {
                "dataset": self.key,
                "patient_id": str(row["patient_id"]),
                "slide_id": slide_id,
                "uri": fetched_file.uri,
                "sha256": fetched_file.sha256,
                "specimen_type": SPECIMEN_TYPE,
                "mpp_override": mpp,
                "mpp_source": MPP_SOURCE,
                "native_mag": native_mag,
                "scanner": None,
                "tss": None,
                "split": None,
                "gt_grade": row.get("gt_grade"),
                "gt_total": None,
                "gt_tubule": None,
                "gt_pleo": None,
                "gt_mitoses": None,
                "gt_histotype": None,
                "gt_label_source": GT_LABEL_SOURCE,
                "gt_label_confidence": GT_LABEL_CONFIDENCE,
                "regions_uri": None,
            }
            rows.append(manifest_row)

        manifest_df = pd.DataFrame(rows)
        validate_manifest(manifest_df)
        return manifest_df
