"""
MIDOG++ dataset adapter: COCO JSON parsing, point conversion in µm, GeoJSON ground truth,
and manifest generation for breast and other tumor sets.
SPEC-02 §3.3 and WP-5.2.

Checked against the published ``MIDOG++.json`` (figshare 6615571, article 23531121) and
image ``094.tiff`` on 2026-09-29:
- ``bbox`` is ``[x0, y0, x1, y1]`` in image pixels (e.g. ``[1311, 903, 1361, 953]``);
- the categories are "mitotic figure" and "not mitotic figure";
- images carry no scanner or resolution field; each TIFF's resolution tags give it
  (094: 110508 px/inch = 0.2298 µm/px).
Nothing is defaulted: an unknown category, a missing image or missing resolution tags raise.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
from PIL import Image

from .base import DatasetAdapter, FetchedFile, load_config
from .manifest import empty_manifest, validate_manifest
from .storage import Storage

# TIFF tags (baseline): XResolution, YResolution, ResolutionUnit; unit 2 = inch, 3 = cm.
TIFF_X_RESOLUTION, TIFF_Y_RESOLUTION, TIFF_RESOLUTION_UNIT = 282, 283, 296
MICRONS_PER_RESOLUTION_UNIT = {2: 25400.0, 3: 10000.0}


class MIDOGppError(ValueError):
    """The MIDOG++ data does not have the shape this adapter was checked against."""


def image_mpp(path: Path) -> tuple[float, float]:
    """(mpp_x, mpp_y) in µm/px from a TIFF's resolution tags."""
    with Image.open(path) as image:
        tags = getattr(image, "tag_v2", None)
        if tags is None or not all(t in tags for t in (TIFF_X_RESOLUTION, TIFF_Y_RESOLUTION, TIFF_RESOLUTION_UNIT)):
            raise MIDOGppError(f"{path} has no TIFF resolution tags")
        unit = MICRONS_PER_RESOLUTION_UNIT.get(int(tags[TIFF_RESOLUTION_UNIT]))
        if unit is None:
            raise MIDOGppError(f"{path}: unsupported TIFF resolution unit {tags[TIFF_RESOLUTION_UNIT]}")
        return unit / float(tags[TIFF_X_RESOLUTION]), unit / float(tags[TIFF_Y_RESOLUTION])


def bbox_centre(bbox: list[float]) -> tuple[float, float]:
    """Centre of a MIDOG++ ``[x0, y0, x1, y1]`` box."""
    if len(bbox) != 4:
        raise MIDOGppError(f"bbox {bbox!r} is not [x0, y0, x1, y1]")
    x0, y0, x1, y1 = (float(v) for v in bbox)
    if not (x1 > x0 and y1 > y0):
        raise MIDOGppError(f"bbox {bbox!r} is not [x0, y0, x1, y1] with x1 > x0 and y1 > y0")
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0


class MIDOGppAdapter(DatasetAdapter):
    def __init__(self, key: str = "midogpp_breast", config: dict[str, Any] | None = None) -> None:
        self.key = key
        self.config = config or load_config().get("midogpp", {})
        categories = self.config.get("categories")
        if not categories:
            raise MIDOGppError("eval/datasets/config.yaml midogpp.categories is missing")
        self.category_map: dict[str, str] = {name.lower(): target for name, target in categories.items()}
        self.specimen_type: str = str(self.config.get("specimen_type", "resection"))
        self.mpp_source: str = str(self.config.get("mpp_source", "file"))
        self.gt_label_source: str = str(self.config.get("gt_label_source", "consensus"))
        self.gt_label_confidence: str = str(self.config.get("gt_label_confidence", "high"))

    def target_class(self, category_name: str) -> str:
        target = self.category_map.get(category_name.lower())
        if target is None:
            raise MIDOGppError(f"unknown MIDOG++ category {category_name!r}; known: {sorted(self.category_map)}")
        return target

    def discover_from_coco(
        self, coco_data: dict[str, Any], images_dir: Path
    ) -> tuple[pd.DataFrame, dict[int, list[dict[str, Any]]], dict[int, str]]:
        """
        Parse COCO JSON into an images DataFrame (resolution read from each image file),
        annotations grouped by image_id, and the category lookup map.
        """
        categories = {cat["id"]: cat["name"] for cat in coco_data.get("categories", [])}
        for name in categories.values():
            self.target_class(name)
        annotations_by_image: dict[int, list[dict[str, Any]]] = {}
        for ann in coco_data.get("annotations", []):
            annotations_by_image.setdefault(ann["image_id"], []).append(ann)

        rows = []
        for img in coco_data.get("images", []):
            is_breast = "breast" in str(img["tumor_type"]).lower()
            if (self.key == "midogpp_breast") != is_breast:
                continue
            path = Path(images_dir) / img["file_name"]
            if not path.is_file():
                raise FileNotFoundError(f"MIDOG++ image {path} is not downloaded")
            mpp_x, mpp_y = image_mpp(path)
            rows.append({
                "image_id": img["id"],
                "file_name": img["file_name"],
                "path": str(path),
                "scanner": img.get("scanner"),
                "mpp": mpp_x,
                "mpp_y": mpp_y,
                "tumor_type": str(img["tumor_type"]).strip().lower(),
                # MIDOG++ has one image per case and no patient identifier.
                "patient_id": str(img.get("patient_id") or f"midogpp_{img['id']:04d}"),
                "width": img.get("width"),
                "height": img.get("height"),
            })
        return pd.DataFrame(rows), annotations_by_image, categories

    def discover(self, coco_path_or_data: str | Path | dict[str, Any], images_dir: Path) -> pd.DataFrame:
        """Images of this subset that are present in ``images_dir``."""
        if isinstance(coco_path_or_data, (str, Path)):
            with open(coco_path_or_data, "r", encoding="utf-8") as f:
                coco_data = json.load(f)
        else:
            coco_data = coco_path_or_data
        df, _, _ = self.discover_from_coco(coco_data, images_dir)
        return df

    def ground_truth_points(
        self, annotations: list[dict[str, Any]], category_names: dict[int, str], mpp: float
    ) -> dict[str, list[tuple[float, float]]]:
        """Box centres in µm, by target class ("MF", "imposter")."""
        points: dict[str, list[tuple[float, float]]] = {}
        for ann in annotations:
            x_px, y_px = bbox_centre(ann["bbox"])
            target = self.target_class(category_names[ann["category_id"]])
            points.setdefault(target, []).append((x_px * mpp, y_px * mpp))
        return points

    def create_ground_truth_geojson(
        self,
        image_row: pd.Series | dict[str, Any],
        annotations: list[dict[str, Any]],
        category_names: dict[int, str],
    ) -> dict[str, Any]:
        """Box centres as GeoJSON points in µm, classed "MF" or "imposter"."""
        mpp = float(image_row["mpp"])
        features = []
        for ann in annotations:
            x_px, y_px = bbox_centre(ann["bbox"])
            cat_name = category_names[ann["category_id"]]
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(x_px * mpp, 4), round(y_px * mpp, 4)]},
                "properties": {
                    "class": self.target_class(cat_name),
                    "category_id": ann["category_id"],
                    "category_name": cat_name,
                    "bbox_pixels": ann["bbox"],
                },
            })
        return {"type": "FeatureCollection", "features": features}

    def write_geojson(self, slide_id: str, geojson_obj: dict[str, Any], dest: Storage) -> str:
        """Serialize ground-truth GeoJSON to destination storage and return its URI."""
        relpath = f"midogpp/annotations/{slide_id}.geojson"
        payload = json.dumps(geojson_obj, indent=2).encode("utf-8")
        with dest.open_write(relpath) as w:
            w.write(payload)
        return dest.uri(relpath)

    def fetch(self, row: pd.Series, dest: Storage | None = None) -> FetchedFile:
        """
        Record or stream a MIDOG++ image payload with real SHA-256 and MD5 hash computation.
        If dest is provided, streams the image into destination storage.
        """
        path = Path(row["path"])
        if not path.is_file():
            raise FileNotFoundError(f"MIDOG++ image {path} is not downloaded")

        slide_id = str(row["image_id"])
        relpath = f"midogpp/images/{path.name}"
        sha256_hasher = hashlib.sha256()
        md5_hasher = hashlib.md5()
        total_bytes = 0
        chunk_size = 64 * 1024  # 64 KiB streaming

        if dest is not None:
            with open(path, "rb") as src, dest.open_write(relpath) as writer:
                while chunk := src.read(chunk_size):
                    writer.write(chunk)
                    sha256_hasher.update(chunk)
                    md5_hasher.update(chunk)
                    total_bytes += len(chunk)
            uri = dest.uri(relpath)
        else:
            with open(path, "rb") as src:
                while chunk := src.read(chunk_size):
                    sha256_hasher.update(chunk)
                    md5_hasher.update(chunk)
                    total_bytes += len(chunk)
            uri = path.resolve().as_uri()

        return FetchedFile(
            slide_id=slide_id,
            uri=uri,
            sha256=sha256_hasher.hexdigest().lower(),
            md5=md5_hasher.hexdigest().lower(),
            size_bytes=total_bytes,
        )

    def labels(self) -> pd.DataFrame:
        """Ground-truth category definitions."""
        rows = [{"category_name": cat, "target_class": cls} for cat, cls in sorted(self.category_map.items())]
        return pd.DataFrame(rows)

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
            rows.append({
                "dataset": self.key,
                "patient_id": str(row["patient_id"]),
                "slide_id": slide_id,
                "uri": fetched_file.uri,
                "sha256": fetched_file.sha256,
                "specimen_type": self.specimen_type,
                "mpp_override": float(row["mpp"]),
                "mpp_source": self.mpp_source,
                "native_mag": None,
                "scanner": None if pd.isna(row.get("scanner")) else str(row["scanner"]),
                "tss": None,
                "split": None,
                "gt_grade": None,
                "gt_total": None,
                "gt_tubule": None,
                "gt_pleo": None,
                "gt_mitoses": None,
                "gt_histotype": None,
                "gt_label_source": self.gt_label_source,
                "gt_label_confidence": self.gt_label_confidence,
                "regions_uri": regions.get(slide_id),
            })

        manifest_df = pd.DataFrame(rows)
        validate_manifest(manifest_df)
        return manifest_df
