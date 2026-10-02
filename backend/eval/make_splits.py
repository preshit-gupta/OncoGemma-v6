"""Make the committed patient-level splits and SPLITS.lock (SPEC-02 §5.2; WP-6.2, decision D20).

This version is built for TCGA-BRCA only (D20). The TCGA split is made here; BCSS (ROIs on
TCGA-BRCA slides) inherits it per patient. The outputs are committed and never remade with
another seed: a new split would put test patients into training.

    cd backend
    python -m eval.make_splits native-mag --dx gdc_brca_dx.parquet --out native_mag.parquet
    python -m eval.make_splits make --dx gdc_brca_dx.parquet --native-mag native_mag.parquet \\
        --grades eval/datasets/labels/tcga_grade.parquet --bcss-roi-bounds roiBounds_BaseMagnification.csv \\
        --out-dir eval/splits --seed 20260928

``--dx`` is the GDC listing of open-access TCGA-BRCA diagnostic slides (columns ``file_id``,
``file_name``, ``patient_id``, ``tss``). Strata are ``(gt_grade, tss_group, native_mag)``:
``gt_grade`` is the report label (WP-5.4; missing grades are their own stratum value),
``tss_group`` merges tissue source sites with fewer than ``--min-tss`` patients into ``other``,
and ``native_mag`` is the scan magnification from the slide's TIFF header (``AppMag``, else ``MPP``,
else ``unknown``), read by one ranged request per slide because GDC does not report it.
"""
from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from eval.splits import check_disjoint, inherit_splits, make_splits, write_lock

APPMAG = re.compile(r"\|\s*AppMag\s*=\s*([0-9.]+)")
MPP_FIELD = re.compile(r"\|\s*MPP\s*=\s*([0-9.]+)")
NOMINAL_MPP = {"40": 0.25, "20": 0.5}
NOMINAL_MPP_TOLERANCE = 0.1  # relative; scanners report 0.2268-0.2527 for 40x
DX_SLIDE = re.compile(r"^(TCGA-[A-Z0-9]{2}-[A-Z0-9]{4})-\d{2}[A-Z]-\d{2}-(DX\d+)\.")
MIN_TSS_PATIENTS = 10  # SPEC-02 §5.2
HEADER_BLOCK_BYTES = 1 << 16
SPLIT_FILES = ("tcga_brca_dx.parquet", "bcss.parquet")


class MissingMagnificationError(ValueError):
    """A slide's native magnification cannot be read or is not 20x/40x."""


def short_barcode(file_name: str) -> str:
    """``TCGA-XX-YYYY-DXn`` (the BCSS slide name) from a GDC file name ``TCGA-XX-YYYY-01Z-00-DXn.<uuid>.svs``."""
    match = DX_SLIDE.match(file_name)
    if match is None:
        raise ValueError(f"not a TCGA diagnostic slide file name: {file_name!r}")
    return f"{match.group(1)}-{match.group(2)}"


def native_mag_from_description(description: str) -> tuple[str, str]:
    """``(native_mag, source)`` from an Aperio description: its ``AppMag``, else its ``MPP``, else unknown.

    ``MPP`` maps to 40x or 20x only within ``NOMINAL_MPP_TOLERANCE`` of 0.25 or 0.5 µm/px; anything
    else raises. A header with neither field (some JPEG 2000 TCGA slides) is the stratum ``unknown``.
    """
    text = "|" + description
    match = APPMAG.search(text)
    if match is not None:
        return f"{float(match.group(1)):g}", "appmag"
    match = MPP_FIELD.search(text)
    if match is not None:
        mpp = float(match.group(1))
        for mag, nominal in NOMINAL_MPP.items():
            if abs(mpp - nominal) <= NOMINAL_MPP_TOLERANCE * nominal:
                return mag, "mpp"
        raise MissingMagnificationError(f"MPP {mpp} is neither 40x nor 20x in {description[:120]!r}")
    return "unknown", "none"


def fetch_native_mag(dx: pd.DataFrame, workers: int, progress: Path | None = None) -> pd.DataFrame:
    """``file_id, native_mag, mag_source`` for every slide in ``dx``, from each slide's first TIFF page.

    With ``progress``, each answer is appended to that JSON-lines file as it arrives and slides
    already in it are not read again, so an interrupted run resumes.
    """
    import json
    import threading

    from eval.datasets.remote_slide import gdc_file_url, read_first_page_description

    done: dict[str, tuple[str, str]] = {}
    if progress is not None and progress.is_file():
        for line in progress.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            done[row["file_id"]] = (row["native_mag"], row["mag_source"])
    lock = threading.Lock()

    def one(file_id: str) -> None:
        description = read_first_page_description(gdc_file_url(file_id), block_bytes=HEADER_BLOCK_BYTES)
        mag, source = native_mag_from_description(description)
        with lock:
            done[file_id] = (mag, source)
            if progress is not None:
                with open(progress, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"file_id": file_id, "native_mag": mag, "mag_source": source}) + "\n")

    todo = [fid for fid in dx["file_id"].tolist() if fid not in done]
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, todo))
    rows = [(fid, *done[fid]) for fid in dx["file_id"].tolist()]
    return pd.DataFrame(rows, columns=["file_id", "native_mag", "mag_source"])


def tcga_split_frame(dx: pd.DataFrame, native_mag: pd.DataFrame, grades: pd.DataFrame, min_tss: int) -> pd.DataFrame:
    """One row per diagnostic slide with the stratum columns ``make_splits`` uses."""
    frame = dx[["file_id", "file_name", "patient_id", "tss"]].copy()
    frame["slide_id"] = frame["file_name"].map(short_barcode)
    frame = frame.merge(native_mag[["file_id", "native_mag", "mag_source"]], on="file_id", how="left", validate="one_to_one")
    if frame["native_mag"].isna().any():
        missing = frame.loc[frame["native_mag"].isna(), "file_id"].tolist()
        raise MissingMagnificationError(f"{len(missing)} slides have no native magnification, e.g. {missing[:3]}")
    labels = grades.set_index("patient_id")["grade"]
    frame["gt_grade"] = [("none" if pd.isna(g) else str(int(g))) for g in frame["patient_id"].map(labels)]
    patients_per_tss = frame.drop_duplicates("patient_id")["tss"].value_counts()
    small = set(patients_per_tss[patients_per_tss < min_tss].index)
    frame["tss_group"] = frame["tss"].where(~frame["tss"].isin(small), "other")
    return frame.sort_values(["patient_id", "slide_id"]).reset_index(drop=True)


def bcss_frame(roi_bounds: Path, exclude: frozenset[str] = frozenset()) -> pd.DataFrame:
    """One row per BCSS ROI not in ``exclude``: slide barcode and patient (the BCSS ROI bounds CSV)."""
    rois = pd.read_csv(roi_bounds, index_col=0)
    unknown = sorted(exclude - set(rois.index))
    if unknown:
        raise KeyError(f"excluded slides are not BCSS ROIs: {unknown}")
    frame = pd.DataFrame({"slide_id": [s for s in rois.index.astype(str) if s not in exclude]})
    frame["patient_id"] = frame["slide_id"].str.slice(0, 12)
    return frame.sort_values("slide_id").reset_index(drop=True)


def make(args) -> int:
    dx = pd.read_parquet(args.dx)
    tcga = tcga_split_frame(dx, pd.read_parquet(args.native_mag), pd.read_parquet(args.grades), args.min_tss)
    tcga = make_splits(tcga, seed=args.seed, strata_cols=("gt_grade", "tss_group", "native_mag"))
    tcga["seed"] = args.seed
    bcss = inherit_splits(bcss_frame(args.bcss_roi_bounds, frozenset(args.exclude_bcss or [])), tcga)
    check_disjoint({"tcga_brca_dx": tcga, "bcss": bcss})
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / name for name in SPLIT_FILES]
    tcga.to_parquet(paths[0], index=False)
    bcss.to_parquet(paths[1], index=False)
    lock = write_lock(paths, out / "SPLITS.lock", args.lock_root)
    per_patient = tcga.drop_duplicates("patient_id")["split"].value_counts().to_dict()
    print(f"tcga_brca_dx patients {per_patient}; bcss ROIs {bcss['split'].value_counts().to_dict()}")
    print(f"SPLITS.lock: {lock}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.make_splits", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    mag = sub.add_parser("native-mag", help="read each slide's AppMag from its TIFF header (ranged GDC reads)")
    mag.add_argument("--dx", type=Path, required=True)
    mag.add_argument("--out", type=Path, required=True)
    mag.add_argument("--workers", type=int, default=8)
    mk = sub.add_parser("make", help="make the TCGA split, inherit it for BCSS and write SPLITS.lock")
    mk.add_argument("--dx", type=Path, required=True)
    mk.add_argument("--native-mag", type=Path, required=True)
    mk.add_argument("--grades", type=Path, required=True)
    mk.add_argument("--bcss-roi-bounds", type=Path, required=True)
    mk.add_argument("--out-dir", type=Path, required=True)
    mk.add_argument("--lock-root", type=Path, default=Path("."), help="lock paths are relative to this (backend/)")
    mk.add_argument("--seed", type=int, required=True)
    mk.add_argument("--exclude-bcss", nargs="*", help="BCSS slide ids left out (e.g. no open-access GDC slide)")
    mk.add_argument("--min-tss", type=int, default=MIN_TSS_PATIENTS)
    args = parser.parse_args(argv)
    if args.command == "native-mag":
        dx = pd.read_parquet(args.dx)
        fetch_native_mag(dx, args.workers, args.out.with_suffix(".progress.jsonl")).to_parquet(args.out, index=False)
        return 0
    return make(args)


if __name__ == "__main__":
    sys.exit(main())
