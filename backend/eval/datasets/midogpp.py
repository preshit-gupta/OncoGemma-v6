"""
MIDOG++ dataset adapter: COCO JSON parsing, point conversion in µm, GeoJSON ground truth,
and manifest generation for breast and other tumor sets.
SPEC-02 §3.3 and WP-5.2.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import pandas as pd

from .base import DatasetAdapter, FetchedFile, load_config
from .manifest import empty_manifest, validate_manifest
from .storage import Storage


class MIDOGppAdapter(DatasetAdapter):
    def __init__(self, key: str = "midogpp_breast", config: dict[str, Any] | None = None) -> None:
        self.key = key
        self.config = config or load_config().get("midogpp", {})
        self.scanner_mpp: dict[str, float] = self.config.get(
            "scanner_mpp",
            {
                "Hamamatsu XR": 0.23,
                "Hamamatsu S360": 0.23,
                "Leica CS2": 0.25,
                "XR": 0.23,
                "S360": 0.23,
                "CS2": 0.25,
            },
        )
        self.category_map: dict[str, str] = self.config.get(
            "categories",
            {
                "mitotic figure": "MF",
                "non-mitotic figure": "imposter",
                "hard negative": "imposter",
            },
        )

    def get_scanner_mpp(self, scanner_name: str | None) -> float:
        """Resolve scanner MPP from configuration or default to 0.25."""
        if not scanner_name:
            return 0.25
        scanner_clean = str(scanner_name).strip()
        for k, mpp in self.scanner_mpp.items():
            if k.lower() in scanner_clean.lower():
                return float(mpp)
        return 0.25

    def discover_from_coco(self, coco_data: dict[str, Any]) -> tuple[pd.DataFrame, dict[int, list[dict[str, Any]]], dict[int, str]]:
        """
        Parse COCO JSON dictionary into images DataFrame, annotations grouped by image_id,
        and category lookup map.
        """
        categories = {cat["id"]: cat["name"] for cat in coco_data.get("categories", [])}

        annotations_by_image: dict[int, list[dict[str, Any]]] = {}
        for ann in coco_data.get("annotations", []):
            img_id = ann["image_id"]
            annotations_by_image.setdefault(img_id, []).append(ann)

        rows = []
        for img in coco_data.get("images", []):
            img_id = img["id"]
            file_name = img.get("file_name", f"{img_id}.png")
            scanner = img.get("scanner", "Hamamatsu XR")
            mpp = self.get_scanner_mpp(scanner)
            tumor_type = str(img.get("tumor_type", "breast")).strip().lower()

            # Filter breast vs other
            is_breast = "breast" in tumor_type
            if self.key == "midogpp_breast" and not is_breast:
                continue
            if self.key == "midogpp_other" and is_breast:
                continue

            patient_id = str(img.get("patient_id") or img.get("case_id") or f"case_{img_id:04d}")

            rows.append({
                "image_id": img_id,
                "file_name": file_name,
                "scanner": scanner,
                "mpp": mpp,
                "tumor_type": tumor_type,
                "patient_id": patient_id,
                "width": img.get("width"),
                "height": img.get("height"),
            })

        df = pd.DataFrame(rows)
        return df, annotations_by_image, categories

    def discover(self, coco_path_or_data: str | Path | dict[str, Any] | None = None) -> pd.DataFrame:
        """
        Discover images from COCO format annotations.
        """
        if coco_path_or_data is None:
            return pd.DataFrame(columns=["image_id", "file_name", "scanner", "mpp", "tumor_type", "patient_id"])

        if isinstance(coco_path_or_data, (str, Path)):
            with open(coco_path_or_data, "r", encoding="utf-8") as f:
                coco_data = json.load(f)
        else:
            coco_data = coco_path_or_data

        df, _, _ = self.discover_from_coco(coco_data)
        return df

    def create_ground_truth_geojson(
        self,
        image_row: pd.Series | dict[str, Any],
        annotations: list[dict[str, Any]],
        category_names: dict[int, str],
    ) -> dict[str, Any]:
        """
        Convert bounding-box centers to point coordinates in µm and format as GeoJSON.
        Categories are mapped to 'MF' or 'imposter'.
        """
        scanner = image_row.get("scanner")
        mpp = float(image_row.get("mpp") or self.get_scanner_mpp(scanner))

        features = []
        for ann in annotations:
            bbox = ann.get("bbox", [0, 0, 0, 0])  # [x, y, w, h]
            center_x_px = bbox[0] + (bbox[2] / 2.0)
            center_y_px = bbox[1] + (bbox[3] / 2.0)

            x_um = round(center_x_px * mpp, 4)
            y_um = round(center_y_px * mpp, 4)

            cat_id = ann.get("category_id")
            cat_name = category_names.get(cat_id, "mitotic figure")
            target_class = self.category_map.get(cat_name.lower())
            if target_class is None:
                target_class = "MF" if ("mitotic" in cat_name.lower() and "non" not in cat_name.lower()) else "imposter"

            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [x_um, y_um],
                },
                "properties": {
                    "class": target_class,
                    "category_id": cat_id,
                    "category_name": cat_name,
                    "bbox_pixels": bbox,
                },
            }
            features.append(feature)

        return {
            "type": "FeatureCollection",
            "features": features,
        }

    def write_geojson(self, slide_id: str, geojson_obj: dict[str, Any], dest: Storage) -> str:
        """Serialize ground-truth GeoJSON to destination storage and return its URI."""
        relpath = f"midogpp/annotations/{slide_id}.geojson"
        payload = json.dumps(geojson_obj, indent=2).encode("utf-8")
        with dest.open_write(relpath) as w:
            w.write(payload)
        return dest.uri(relpath)

    def fetch(self, row: pd.Series, dest: Storage) -> FetchedFile:
        """
        Fetch / record MIDOG++ ROI image payload.
        """
        slide_id = str(row.get("image_id", row.get("file_name")))
        relpath = f"midogpp/images/{row.get('file_name', f'{slide_id}.png')}"
        uri = dest.uri(relpath)
        sha256 = row.get("sha256", "0" * 64)
        return FetchedFile(
            slide_id=slide_id,
            uri=uri,
            sha256=sha256,
            md5=None,
            size_bytes=int(row.get("size_bytes", 0)),
        )

    def labels(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["patient_id", "slide_id", "gt_mitoses"])

    def to_manifest(
        self,
        discovered: pd.DataFrame,
        fetched: dict[str, FetchedFile],
        regions_uris: dict[str, str] | None = None,
    ) -> pd.DataFrame:
        """
        Emit validated manifest conforming to MANIFEST_COLUMNS (SPEC-02 §5.1).
        """
        if discovered.empty or not fetched:
            return empty_manifest()

        regions = regions_uris or {}
        rows = []
        for _, row in discovered.iterrows():
            slide_id = str(row["image_id"])
            if slide_id not in fetched:
                continue

            fetched_file = fetched[slide_id]
            mpp = float(row.get("mpp", self.get_scanner_mpp(row.get("scanner"))))

            manifest_row = {
                "dataset": self.key,
                "patient_id": str(row["patient_id"]),
                "slide_id": slide_id,
                "uri": fetched_file.uri,
                "sha256": fetched_file.sha256,
                "specimen_type": "resection",
                "mpp_override": mpp,
                "mpp_source": "dataset_doc",
                "native_mag": 40.0,
                "scanner": str(row.get("scanner", "Hamamatsu XR")),
                "tss": None,
                "split": None,
                "gt_grade": None,
                "gt_total": None,
                "gt_tubule": None,
                "gt_pleo": None,
                "gt_mitoses": None,
                "gt_histotype": None,
                "gt_label_source": "consensus",
                "gt_label_confidence": "high",
                "regions_uri": regions.get(slide_id),
            }
            rows.append(manifest_row)

        manifest_df = pd.DataFrame(rows)
        validate_manifest(manifest_df)
        return manifest_df
