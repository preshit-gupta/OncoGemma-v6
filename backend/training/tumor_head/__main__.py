"""Tumour-head command line (SPEC-05 §4; WP-6.2). Run from ``backend/``.

    python -m training.tumor_head build --roi-bounds roiBounds_BaseMagnification.csv --masks masks_base/ \\
        --dx gdc_brca_dx.parquet --work work/ --out data/ --exclude TCGA-A2-A0D2-DX1
    python -m training.tumor_head train --dataset data/bcss_tiles.parquet --dataset-meta data/bcss_tiles.json \\
        --splits eval/splits/bcss.parquet --splits-lock eval/splits/SPLITS.lock --v5-probe ../models/probe/probe_v1.joblib \\
        --version 1.0.0 --out ../models/tumor_head/1.0.0

``build`` calls the Path Foundation endpoint (EVAL mode; cached under ``--work``). ``train`` is
offline. Test metrics need ``--confirm-test-access "<reason>"``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m training.tumor_head", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="label BCSS tiles and embed them (Path Foundation, EVAL mode)")
    build.add_argument("--roi-bounds", type=Path, required=True)
    build.add_argument("--masks", type=Path, required=True, help="base-magnification masks with SHA256.json")
    build.add_argument("--dx", type=Path, required=True, help="GDC listing of TCGA-BRCA diagnostic slides")
    build.add_argument("--work", type=Path, required=True, help="gateway cache")
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--exclude", nargs="*", help="BCSS slide ids to leave out (recorded)")
    build.add_argument("--workers", type=int, default=6)
    build.add_argument("--limit", type=int, default=0, help="first N ROIs only (smoke runs)")
    tr = sub.add_parser("train", help="fit, select, calibrate; write the artefacts and the model card")
    tr.add_argument("--dataset", type=Path, required=True)
    tr.add_argument("--dataset-meta", type=Path)
    tr.add_argument("--splits", type=Path, required=True, help="eval/splits/bcss.parquet (listed in the lock)")
    tr.add_argument("--splits-lock", type=Path, required=True)
    tr.add_argument("--splits-root", type=Path, default=Path("."), help="the lock's paths are relative to this")
    tr.add_argument("--v5-probe", type=Path, help="models/probe/probe_v1.joblib, for the od_fusion_v5 row (AC1)")
    tr.add_argument("--version", required=True)
    tr.add_argument("--out", type=Path, required=True)
    tr.add_argument("--confirm-test-access", help="reason for computing test metrics (SPEC-02 §5.2)")
    args = parser.parse_args(argv)
    if args.command == "build":
        from training.tumor_head.dataset import build as run_build

        return run_build(args)
    from training.tumor_head.train import run as run_train

    return run_train(args)


if __name__ == "__main__":
    sys.exit(main())
