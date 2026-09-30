"""
Unit tests for eval.datasets CLI entry point (__main__.py).
SPEC-02 §5.5 and WP-5.2.
"""
from pathlib import Path
from PIL import Image
import pandas as pd
import pytest

from eval.datasets.__main__ import main


def test_cli_discover(tmp_path: Path):
    out_file = tmp_path / "discovered.parquet"
    masks = tmp_path / "masks"
    masks.mkdir()
    Image.new("L", (16, 8), color=1).save(masks / "TCGA-A1-A0SK-DX1_xmin45749_ymin25055_MPP-0.2500.png")

    # Run discover on bcss
    exit_code = main(["bcss", "discover", "--out", str(out_file), "--source", str(masks)])
    assert exit_code == 0
    assert out_file.exists()
    df = pd.read_parquet(out_file)
    assert list(df["slide_barcode"]) == ["TCGA-A1-A0SK-DX1"]
    assert list(df["mask_mpp"]) == [0.25]


def test_cli_fetch(tmp_path: Path):
    # Create real source mask image
    source_mask = tmp_path / "TCGA-A1-A0SK-DX1_xmin45749_ymin25055_MPP-0.2500.png"
    Image.new("L", (16, 16), color=1).save(source_mask)

    manifest_file = tmp_path / "manifest.parquet"
    test_df = pd.DataFrame([
        {
            "slide_barcode": "TCGA-A1-A0SK-DX1",
            "file_size": source_mask.stat().st_size,
            "mask_uri": source_mask.resolve().as_uri(),
        }
    ])
    test_df.to_parquet(manifest_file)

    dest_dir = tmp_path / "dest"
    exit_code = main([
        "bcss",
        "fetch",
        "--manifest",
        str(manifest_file),
        "--dest",
        str(dest_dir),
        "--confirm-bytes",
        "1000",
    ])
    assert exit_code == 0
    assert dest_dir.exists()
