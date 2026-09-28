"""
Unit tests for eval.datasets CLI entry point (__main__.py).
SPEC-02 §5.5 and WP-5.2.
"""
from pathlib import Path
import pandas as pd
import pytest

from eval.datasets.__main__ import main
from eval.datasets.manifest import empty_manifest


def test_cli_discover(tmp_path: Path, monkeypatch):
    out_file = tmp_path / "discovered.parquet"

    # Run discover on bcss
    exit_code = main(["bcss", "discover", "--out", str(out_file)])
    assert exit_code == 0
    assert out_file.exists()
    df = pd.read_parquet(out_file)
    assert isinstance(df, pd.DataFrame)


def test_cli_fetch(tmp_path: Path):
    manifest_file = tmp_path / "manifest.parquet"
    test_df = pd.DataFrame([
        {
            "slide_barcode": "TCGA-TEST-0001",
            "file_size": 100,
            "mask_uri": "file:///fake/path.png",
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
