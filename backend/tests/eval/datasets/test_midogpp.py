"""
Unit tests for MIDOGppAdapter against a fixture shaped like the published MIDOG++.json.
SPEC-02 §3.3 and WP-5.2.

The published file (figshare 6615571) has [x0, y0, x1, y1] boxes, the categories
"mitotic figure" / "not mitotic figure", and no scanner or resolution: resolution comes
from each TIFF's tags.
"""
import json
from pathlib import Path

import pandas as pd
import pytest
from PIL import Image

from eval.datasets.midogpp import MIDOGppAdapter, MIDOGppError, bbox_centre, image_mpp
from eval.datasets.storage import LocalStorage

PX_PER_INCH_023 = 25400 / 0.23   # 110434.8 px/inch = 0.23 um/px (094.tiff: 110508 = 0.2298)
PX_PER_INCH_025 = 25400 / 0.25


@pytest.fixture
def coco() -> dict:
    return json.loads((Path(__file__).parent / "fixtures" / "midogpp_coco.json").read_text(encoding="utf-8"))


def write_tiff(path: Path, px_per_inch: float | None) -> None:
    kwargs = {} if px_per_inch is None else {"dpi": (px_per_inch, px_per_inch)}
    Image.new("RGB", (16, 16), (230, 200, 220)).save(path, format="TIFF", **kwargs)


@pytest.fixture
def images_dir(tmp_path: Path) -> Path:
    write_tiff(tmp_path / "001.tiff", PX_PER_INCH_023)
    write_tiff(tmp_path / "002.tiff", PX_PER_INCH_025)
    return tmp_path


def test_discovery_reads_the_resolution_from_each_tiff(coco, images_dir):
    df = MIDOGppAdapter(key="midogpp_breast").discover(coco, images_dir)
    assert set(df["image_id"]) == {1, 2}  # the canine image is another subset
    by_id = df.set_index("image_id")
    assert by_id.loc[1, "mpp"] == pytest.approx(0.23, abs=1e-4)
    assert by_id.loc[2, "mpp"] == pytest.approx(0.25, abs=1e-4)
    assert by_id["scanner"].isna().all()  # MIDOG++.json names no scanner, and none is assumed


def test_an_image_without_resolution_tags_or_not_downloaded_is_an_error(coco, tmp_path):
    write_tiff(tmp_path / "001.tiff", None)
    write_tiff(tmp_path / "002.tiff", PX_PER_INCH_025)
    with pytest.raises(MIDOGppError, match="no TIFF resolution tags"):
        image_mpp(tmp_path / "001.tiff")
    with pytest.raises(MIDOGppError):
        MIDOGppAdapter().discover(coco, tmp_path)
    (tmp_path / "001.tiff").unlink()
    with pytest.raises(FileNotFoundError, match="not downloaded"):
        MIDOGppAdapter().discover(coco, tmp_path)


def test_boxes_are_corner_pairs():
    assert bbox_centre([1311, 903, 1361, 953]) == (1336.0, 928.0)  # the first 094.tiff label
    with pytest.raises(MIDOGppError):
        bbox_centre([100, 200, 50, 60])  # an [x, y, w, h] box is refused, not misread


def test_ground_truth_points_and_geojson(coco, images_dir, tmp_path):
    adapter = MIDOGppAdapter(key="midogpp_breast")
    df, annotations, categories = adapter.discover_from_coco(coco, images_dir)
    row1 = df[df["image_id"] == 1].iloc[0]

    points = adapter.ground_truth_points(annotations[1], categories, row1["mpp"])
    # [100, 200, 150, 250] -> centre (125, 225) px; [300, 400, 350, 450] -> (325, 425) px
    assert points["MF"] == [pytest.approx((125 * row1["mpp"], 225 * row1["mpp"]))]
    assert points["imposter"] == [pytest.approx((325 * row1["mpp"], 425 * row1["mpp"]))]

    geojson = adapter.create_ground_truth_geojson(row1, annotations[1], categories)
    assert [f["properties"]["class"] for f in geojson["features"]] == ["MF", "imposter"]
    storage = LocalStorage(tmp_path / "out")
    assert adapter.write_geojson("1", geojson, storage).startswith("file://")
    assert storage.exists("midogpp/annotations/1.geojson")


def test_an_unknown_category_is_an_error_not_a_mitotic_figure(coco, images_dir):
    """v5's fallback made any name containing "mitotic" without "non" an MF, imposters included."""
    coco["categories"].append({"id": 3, "name": "mitotic-like figure"})
    with pytest.raises(MIDOGppError, match="unknown MIDOG\\+\\+ category"):
        MIDOGppAdapter().discover_from_coco(coco, images_dir)


def test_fetch_records_real_hashes_and_the_manifest_the_file_resolution(coco, images_dir):
    adapter = MIDOGppAdapter(key="midogpp_breast")
    df = adapter.discover(coco, images_dir)
    fetched = {str(row["image_id"]): adapter.fetch(row, None) for _, row in df.iterrows()}
    assert all(len(f.sha256) == 64 and f.sha256 != "0" * 64 and f.size_bytes > 0 for f in fetched.values())

    manifest = adapter.to_manifest(df, fetched, regions_uris={"1": "file:///gt/1.geojson"})
    assert len(manifest) == 2
    assert (manifest["mpp_source"] == "file").all()
    assert manifest.set_index("slide_id").loc["2", "mpp_override"] == pytest.approx(0.25, abs=1e-4)
    assert manifest.iloc[0]["regions_uri"] == "file:///gt/1.geojson"
    assert pd.isna(manifest.iloc[0]["scanner"])
