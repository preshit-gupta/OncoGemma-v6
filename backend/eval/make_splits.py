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

The MIDOG++ breast images get a case-level val/test split (WP-7.8), stratified by scanner and added
to the same lock; the TCGA/BCSS entries are verified first and stay as they are:

    python -m eval.make_splits midogpp --labels MIDOG++.json --scanners datasets_xvalidation.csv \
        --out-dir eval/splits --seed 20260928

``datasets_xvalidation.csv`` is the MIDOG++ authors' per-slide table (figshare article 23559798,
``Slide;Dataset;Tumor;Scanner;...``); ``MIDOG++.json`` has no scanner field.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from eval.splits import check_disjoint, inherit_splits, make_splits, verify_lock, write_lock

APPMAG = re.compile(r"\|\s*AppMag\s*=\s*([0-9.]+)")
MPP_FIELD = re.compile(r"\|\s*MPP\s*=\s*([0-9.]+)")
NOMINAL_MPP = {"40": 0.25, "20": 0.5}
NOMINAL_MPP_TOLERANCE = 0.1  # relative; scanners report 0.2268-0.2527 for 40x
DX_SLIDE = re.compile(r"^(TCGA-[A-Z0-9]{2}-[A-Z0-9]{4})-\d{2}[A-Z]-\d{2}-(DX\d+)\.")
MIN_TSS_PATIENTS = 10  # SPEC-02 §5.2
HEADER_BLOCK_BYTES = 1 << 16
SPLIT_FILES = ("tcga_brca_dx.parquet", "bcss.parquet")
MIDOGPP_BREAST_FILE = "midogpp_breast.parquet"
MIDOGPP_BREAST_TUMOR = "human breast cancer"
MIDOGPP_RATIOS = (0.0, 0.5, 0.5)  # validation only (D19): no train split
# 094 chose the production τ and NMS radius (D17), so it can never be a test image.
MIDOGPP_PINNED_VAL = ("094.tiff",)


class MissingMagnificationError(ValueError):
    """A slide's native magnification cannot be read or is not 20x/40x."""


class MIDOGppSplitError(ValueError):
    """The MIDOG++ labels and the authors' scanner table do not describe the same breast images."""


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


def midogpp_breast_frame(labels: dict, scanners: pd.DataFrame) -> pd.DataFrame:
    """One row per MIDOG++ breast image in the authors' table, with its scanner and label counts.

    Images 151-200 of ``MIDOG++.json`` are breast images without annotations and are not in the
    table, so they are left out. Every breast slide of the table must be in the labels.
    """
    table = scanners[scanners["Tumor"] == MIDOGPP_BREAST_TUMOR]
    if table.empty:
        raise MIDOGppSplitError(f"the scanner table has no {MIDOGPP_BREAST_TUMOR!r} slides")
    images = {int(Path(i["file_name"]).stem): i for i in labels["images"]}
    missing = sorted(set(table["Slide"].astype(int)) - set(images))
    if missing:
        raise MIDOGppSplitError(f"{len(missing)} breast slides of the scanner table are not in the labels, e.g. {missing[:3]}")
    categories = {c["id"]: c["name"] for c in labels["categories"]}
    counts: dict[tuple[int, str], int] = {}
    for ann in labels["annotations"]:
        key = (ann["image_id"], categories[ann["category_id"]])
        counts[key] = counts.get(key, 0) + 1
    rows = []
    for _, row in table.iterrows():
        image = images[int(row["Slide"])]
        if image["tumor_type"] != MIDOGPP_BREAST_TUMOR:
            raise MIDOGppSplitError(f"slide {row['Slide']} is {image['tumor_type']!r} in the labels")
        rows.append({
            "image_id": int(image["id"]),
            "file_name": image["file_name"],
            "patient_id": f"midogpp_{int(image['id']):04d}",  # one image per case (eval.datasets.midogpp)
            "scanner": str(row["Scanner"]),
            "midogpp_dataset": str(row["Dataset"]),  # the authors' own train/test assignment, kept for reference
            "n_mf": counts.get((image["id"], "mitotic figure"), 0),
            "n_imposter": counts.get((image["id"], "not mitotic figure"), 0),
            "width_px": int(image["width"]),
            "height_px": int(image["height"]),
        })
    return pd.DataFrame(rows).sort_values("image_id").reset_index(drop=True)


def midogpp_split(frame: pd.DataFrame, seed: int, pinned_val: tuple[str, ...] = MIDOGPP_PINNED_VAL) -> pd.DataFrame:
    """Case-level val/test split stratified by scanner; ``pinned_val`` images are val and not drawn."""
    unknown = sorted(set(pinned_val) - set(frame["file_name"]))
    if unknown:
        raise MIDOGppSplitError(f"pinned val images are not in the frame: {unknown}")
    pinned = frame["file_name"].isin(pinned_val)
    drawn = make_splits(frame[~pinned], seed=seed, strata_cols=("scanner",), ratios=MIDOGPP_RATIOS)
    out = pd.concat([drawn, frame[pinned].assign(split="val")]).sort_values("image_id").reset_index(drop=True)
    out["seed"] = seed
    return out


def add_to_lock(new_paths: list[Path], lock_path: Path, lock_root: Path) -> dict[str, str]:
    """Add files to an existing SPLITS.lock after checking that every entry already in it still matches."""
    verify_lock(lock_path, lock_root)
    existing = [lock_root / rel for rel in json.loads(lock_path.read_text(encoding="utf-8"))]
    return write_lock(sorted(set(existing) | set(new_paths)), lock_path, lock_root)


def make_midogpp(args) -> int:
    labels = json.loads(args.labels.read_text(encoding="utf-8"))
    frame = midogpp_split(midogpp_breast_frame(labels, pd.read_csv(args.scanners, sep=";")), args.seed)
    check_disjoint({"midogpp_breast": frame})
    path = args.out_dir / MIDOGPP_BREAST_FILE
    frame.to_parquet(path, index=False)
    lock = add_to_lock([path], args.out_dir / "SPLITS.lock", args.lock_root)
    print(f"midogpp_breast images {frame.groupby(['scanner', 'split']).size().to_dict()}")
    print(f"SPLITS.lock: {lock}")
    return 0


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
    mp = sub.add_parser("midogpp", help="make the MIDOG++ breast val/test split and add it to SPLITS.lock")
    mp.add_argument("--labels", type=Path, required=True, help="MIDOG++.json")
    mp.add_argument("--scanners", type=Path, required=True, help="the authors' datasets_xvalidation.csv")
    mp.add_argument("--out-dir", type=Path, required=True)
    mp.add_argument("--lock-root", type=Path, default=Path("."), help="lock paths are relative to this (backend/)")
    mp.add_argument("--seed", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "midogpp":
        return make_midogpp(args)
    if args.command == "native-mag":
        dx = pd.read_parquet(args.dx)
        fetch_native_mag(dx, args.workers, args.out.with_suffix(".progress.jsonl")).to_parquet(args.out, index=False)
        return 0
    return make(args)


if __name__ == "__main__":
    sys.exit(main())
