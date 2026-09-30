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

    # Run discover on bcss
    exit_code = main(["bcss", "discover", "--out", str(out_file)])
    assert exit_code == 0
    assert out_file.exists()
    df = pd.read_parquet(out_file)
    assert isinstance(df, pd.DataFrame)


def test_cli_fetch(tmp_path: Path):
    # Create real source mask image
    source_mask = tmp_path / "TCGA-TEST-0001_100_200_300_400.png"
    Image.new("L", (16, 16), color=1).save(source_mask)

    manifest_file = tmp_path / "manifest.parquet"
    test_df = pd.DataFrame([
        {
            "slide_barcode": "TCGA-TEST-0001",
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
