"""
Unit tests for MIDOGppAdapter using COCO JSON fixtures.
SPEC-02 §3.3 and WP-5.2.
"""
import json
from pathlib import Path
import pandas as pd
import pytest

from eval.datasets.base import FetchedFile
from eval.datasets.midogpp import MIDOGppAdapter
from eval.datasets.storage import LocalStorage


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def midog_coco_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "midogpp_coco.json"


def test_midogpp_coco_discovery(midog_coco_path: Path):
    adapter = MIDOGppAdapter(key="midogpp_breast")
    df = adapter.discover(midog_coco_path)

    # 2 breast images in fixture
    assert len(df) == 2
    assert set(df["image_id"]) == {1, 2}

    row1 = df[df["image_id"] == 1].iloc[0]
    assert row1["scanner"] == "Hamamatsu XR"
    assert row1["mpp"] == 0.23

    row2 = df[df["image_id"] == 2].iloc[0]
    assert row2["scanner"] == "Leica CS2"
    assert row2["mpp"] == 0.25


def test_midogpp_point_conversion_and_geojson(midog_coco_path: Path, tmp_path: Path):
    adapter = MIDOGppAdapter(key="midogpp_breast")
    with open(midog_coco_path, "r", encoding="utf-8") as f:
        coco_data = json.load(f)

    df, annotations_by_img, cat_names = adapter.discover_from_coco(coco_data)
    storage = LocalStorage(tmp_path)

    # Image 1 (Hamamatsu XR, mpp=0.23)
    row1 = df[df["image_id"] == 1].iloc[0]
    anns1 = annotations_by_img[1]
    assert len(anns1) == 2

    geojson1 = adapter.create_ground_truth_geojson(row1, anns1, cat_names)
    assert geojson1["type"] == "FeatureCollection"
    assert len(geojson1["features"]) == 2

    # Feature 0: mitotic figure (category 1 -> MF)
    # bbox: [100, 200, 50, 60] -> center = (125, 230) -> * 0.23 = (28.75, 52.9)
    f0 = geojson1["features"][0]
    assert f0["properties"]["class"] == "MF"
    coords0 = f0["geometry"]["coordinates"]
    assert pytest.approx(coords0[0], abs=1e-3) == 28.75
    assert pytest.approx(coords0[1], abs=1e-3) == 52.9

    # Feature 1: non-mitotic figure (category 2 -> imposter)
    # bbox: [300, 400, 40, 40] -> center = (320, 420) -> * 0.23 = (73.6, 96.6)
    f1 = geojson1["features"][1]
    assert f1["properties"]["class"] == "imposter"
    coords1 = f1["geometry"]["coordinates"]
    assert pytest.approx(coords1[0], abs=1e-3) == 73.6
    assert pytest.approx(coords1[1], abs=1e-3) == 96.6

    # Test writing to storage
    uri = adapter.write_geojson("1", geojson1, storage)
    assert uri.startswith("file://")
    assert storage.exists("midogpp/annotations/1.geojson")


def test_midogpp_manifest_generation(midog_coco_path: Path):
    adapter = MIDOGppAdapter(key="midogpp_breast")
    df = adapter.discover(midog_coco_path)

    fetched = {
        "1": FetchedFile(
            slide_id="1",
            uri="file:///path/to/midogpp/images/001.png",
            sha256="c" * 64,
            md5=None,
            size_bytes=4000,
        ),
        "2": FetchedFile(
            slide_id="2",
            uri="file:///path/to/midogpp/images/002.png",
            sha256="d" * 64,
            md5=None,
            size_bytes=4000,
        ),
    }
    regions_uris = {
        "1": "file:///path/to/midogpp/annotations/1.geojson",
        "2": "file:///path/to/midogpp/annotations/2.geojson",
    }

    manifest_df = adapter.to_manifest(df, fetched, regions_uris=regions_uris)
    assert len(manifest_df) == 2
    assert set(manifest_df["slide_id"]) == {"1", "2"}
    assert manifest_df.iloc[0]["dataset"] == "midogpp_breast"
    assert manifest_df.iloc[0]["mpp_source"] == "dataset_doc"
    assert manifest_df.iloc[0]["regions_uri"] == "file:///path/to/midogpp/annotations/1.geojson"
