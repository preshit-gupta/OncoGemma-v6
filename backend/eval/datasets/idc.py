"""
TCGA-BRCA diagnostic slides read in place from NCI Imaging Data Commons (IDC) DICOM (WP-8.7 prerequisite).

The owner ruled out a bulk copy of TCGA into our buckets (2026-09-30) and chose IDC's public DICOM
for whole-slide runs (2026-10-04). IDC converted every TCGA-BRCA slide to a DICOM WSI series in the
public, not requester-pays bucket ``gs://idc-open-data/<crdc_series_uuid>/``; the series'
``ContainerIdentifier`` is the slide barcode (``TCGA-3C-AALI-01Z-00-DX1``), which is the GDC file
name up to the first dot. All 1,133 locked TCGA-BRCA DX slides have exactly one IDC series (idc-index
v24, checked 2026-10-04).

Two steps, from ``backend/``:

    pip install idc-index    # only for the first step
    python -m eval.datasets.idc series --out eval/datasets/idc/tcga_brca_dx_series.parquet
    python -m eval.datasets.idc manifest --split val --out eval/manifests/tcga_brca_idc_val.parquet

``series`` writes the barcode -> series map (committed, so nothing later needs idc-index).
``manifest`` builds a SPEC-02 §5.1 manifest for one locked split: ``uri`` is the series prefix,
``sha256`` the series fingerprint (``app/core/slide_source.py::series_sha256``, from GCS's stored
MD5s, listed anonymously), the split from ``eval/splits/tcga_brca_dx.parquet`` and the ground truth
from ``eval/datasets/labels/tcga_grade.parquet`` (accepted rows only; labels are pre-QA).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

import pandas as pd

from app.core.slide_source import series_sha256
from eval.datasets.manifest import MANIFEST_COLUMNS, validate_manifest

IDC_BUCKET = "idc-open-data"
IDC_COLLECTION = "tcga_brca"
DATASET_KEY = "tcga_brca_dx"
LABEL_SOURCE = "tcga_report_pre_qa"
UNKNOWN_MAG = "unknown"  # eval.make_splits: the slide header states no magnification
HERE = Path(__file__).resolve().parent
SERIES_MAP = HERE / "idc" / "tcga_brca_dx_series.parquet"
SPLITS = HERE.parent / "splits" / "tcga_brca_dx.parquet"
LABELS = HERE / "labels" / "tcga_grade.parquet"
SERIES_COLUMNS = ["container_id", "crdc_series_uuid", "series_instance_uid", "idc_version"]


class IdcMappingError(ValueError):
    """A slide has no IDC series, or more than one."""


def series_uri(crdc_series_uuid: str) -> str:
    return f"gs://{IDC_BUCKET}/{crdc_series_uuid}/"


def container_id(file_name: str) -> str:
    """The slide barcode from a GDC file name: ``TCGA-3C-AALI-01Z-00-DX1.<uuid>.svs`` -> ``TCGA-3C-AALI-01Z-00-DX1``."""
    return file_name.split(".", 1)[0]


def native_mag(value) -> float | None:
    """The splits' native magnification; ``unknown`` (no magnification in the slide header) is null."""
    if pd.isna(value) or value == UNKNOWN_MAG:
        return None
    return float(value)


def dx_series(index: pd.DataFrame, idc_version: str) -> pd.DataFrame:
    """The TCGA-BRCA diagnostic-slide series from IDC's index (``index`` joined with ``sm_index``)."""
    rows = index[(index["collection_id"] == IDC_COLLECTION) & (index["Modality"] == "SM")
                 & index["ContainerIdentifier"].str.contains("-DX", regex=False)]
    dups = rows[rows["ContainerIdentifier"].duplicated(keep=False)]
    if not dups.empty:
        raise IdcMappingError(f"slides with several IDC series: {sorted(dups['ContainerIdentifier'].unique())[:10]}")
    out = pd.DataFrame({
        "container_id": rows["ContainerIdentifier"].astype(str),
        "crdc_series_uuid": rows["crdc_series_uuid"].astype(str),
        "series_instance_uid": rows["SeriesInstanceUID"].astype(str),
        "idc_version": idc_version,
    })
    return out.sort_values("container_id").reset_index(drop=True)


def build_manifest(
    splits: pd.DataFrame,
    labels: pd.DataFrame,
    series: pd.DataFrame,
    split: str,
    fingerprint: Callable[[str], str],
    specimen_type: str,
    mpp_source: str,
) -> pd.DataFrame:
    """The manifest of one split, every slide read from its IDC series. Raises when a slide has no series."""
    rows = splits[splits["split"] == split].copy()
    if rows.empty:
        raise IdcMappingError(f"the splits have no {split!r} row")
    rows["container_id"] = rows["file_name"].map(container_id)
    rows = rows.merge(series[["container_id", "crdc_series_uuid"]], on="container_id", how="left")
    missing = rows[rows["crdc_series_uuid"].isna()]
    if not missing.empty:
        raise IdcMappingError(f"{len(missing)} slides have no IDC series: {missing['container_id'].tolist()[:10]}")
    accepted = labels[labels["excluded_reason"].isna()].set_index("patient_id")

    out = []
    for _, r in rows.iterrows():
        uri = series_uri(r["crdc_series_uuid"])
        gt = accepted.loc[r["patient_id"]] if r["patient_id"] in accepted.index else None
        value = (lambda col: gt[col] if gt is not None and not pd.isna(gt[col]) else None)
        out.append({
            "dataset": DATASET_KEY,
            "patient_id": r["patient_id"],
            "slide_id": r["file_id"],
            "uri": uri,
            "sha256": fingerprint(uri),
            "specimen_type": specimen_type,
            "mpp_override": None,
            "mpp_source": mpp_source,
            "native_mag": native_mag(r["native_mag"]),
            "scanner": None,
            "tss": r["tss"],
            "split": split,
            "gt_grade": value("grade"),
            "gt_total": value("total"),
            "gt_tubule": value("tubule"),
            "gt_pleo": value("pleo"),
            "gt_mitoses": value("mitoses"),
            "gt_histotype": None,
            "gt_label_source": LABEL_SOURCE if gt is not None else None,
            "gt_label_confidence": value("label_confidence"),
            "regions_uri": None,
        })
    manifest = pd.DataFrame(out)
    manifest = manifest.astype({col: dtype for col, dtype in MANIFEST_COLUMNS.items()})[list(MANIFEST_COLUMNS)]
    validate_manifest(manifest)
    return manifest


def _load_idc_index() -> tuple[pd.DataFrame, str]:
    try:
        from idc_index import IDCClient
    except ImportError as exc:
        raise SystemExit("the series step needs idc-index: pip install idc-index") from exc
    client = IDCClient()
    client.fetch_index("sm_index")
    return client.index.merge(client.sm_index, on="SeriesInstanceUID"), str(client.get_idc_version())


def _anonymous_bucket():
    from google.cloud import storage

    return storage.Client.create_anonymous_client().bucket(IDC_BUCKET)


def main(argv: list[str] | None = None) -> int:
    from eval.datasets.tcga import TCGABRCAAdapter

    parser = argparse.ArgumentParser(prog="python -m eval.datasets.idc", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    s = commands.add_parser("series", help="write the slide barcode -> IDC series map (needs idc-index)")
    s.add_argument("--out", default=str(SERIES_MAP))
    m = commands.add_parser("manifest", help="build one split's manifest read from IDC")
    m.add_argument("--split", required=True, choices=["train", "val", "test"])
    m.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    if args.command == "series":
        index, version = _load_idc_index()
        table = dx_series(index, version)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        table.to_parquet(args.out, index=False)
        print(f"wrote {len(table)} TCGA-BRCA DX series (IDC {version}) to {args.out}")
        return 0

    config = TCGABRCAAdapter().config
    bucket = _anonymous_bucket()
    manifest = build_manifest(
        pd.read_parquet(SPLITS), pd.read_parquet(LABELS), pd.read_parquet(SERIES_MAP), args.split,
        fingerprint=lambda uri: series_sha256(uri, bucket),
        specimen_type=config["specimen_type"], mpp_source=config["mpp_source_with_file_mpp"],
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    manifest.to_parquet(args.out, index=False)
    print(f"wrote {len(manifest)} {args.split} slides ({int(manifest['gt_grade'].notna().sum())} with a grade) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
