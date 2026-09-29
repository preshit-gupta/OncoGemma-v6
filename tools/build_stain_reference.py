"""Build a stain colour reference (SPEC-04 §3.3): configs/stain_refs/<name>@v<version>.json.

A reference is the colour standard every slide's stain is mapped to. Two ways to build one:

  patch   Fit one image (the v5 reference patch). Reproduces v5_patch@v1:

    python tools/build_stain_reference.py patch --patch configs/stain_reference.png \\
        --specimen resection --reference-id v5_patch@v1 --out configs/stain_refs/v5_patch@v1.json

  median  The component-wise median over Stage-2 fits of 100 slides sampled from the TCGA train
          split, stratified by tissue source site (TSS). --fits is a JSON list of
          {"slide_id", "tss", "w_src", "maxc_src"}: the 'fitted' stain_profiles rows of train
          slides. Only train slides may be listed; the reference must not see the evaluation splits.

    python tools/build_stain_reference.py median --fits fits.json --reference-id tcga_train_median@v1 \\
        --out configs/stain_refs/tcga_train_median@v1.json

The fit parameters of the patch mode come from configs/specimen_profiles.yaml (stain_fit of --specimen).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.core.pipeline_config import SpecimenProfilesConfig, StainReference  # noqa: E402
from pipeline.stain import FITTER_VERSION, reference_from_patch  # noqa: E402
from PIL import Image  # noqa: E402

DEFAULT_SLIDES = 100
DEFAULT_SEED = 0


def stratified_sample(records: list[dict], n: int, seed: int) -> list[dict]:
    """Up to ``n`` records drawn round-robin across TSS groups, in a seeded order.

    Every group contributes one record per round until ``n`` are chosen or all are used, so a
    large site cannot dominate the reference.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for record in sorted(records, key=lambda r: r["slide_id"]):
        groups[record["tss"]].append(record)
    rng = random.Random(seed)
    for members in groups.values():
        rng.shuffle(members)
    order = sorted(groups)
    rng.shuffle(order)
    chosen: list[dict] = []
    while len(chosen) < n and any(groups.values()):
        for tss in order:
            if groups[tss] and len(chosen) < n:
                chosen.append(groups[tss].pop())
    return chosen


def median_reference(fits: list[dict], reference_id: str, source: str) -> StainReference:
    """Component-wise median of the fits' unit stain vectors and maximum concentrations."""
    if not fits:
        raise ValueError("no slide fits to take a median of")
    w = np.array([fit["w_src"] for fit in fits], dtype=np.float64)  # slides x 2 x 3
    w = w / np.linalg.norm(w, axis=2, keepdims=True)
    median_w = np.median(w, axis=0)
    median_w = median_w / np.linalg.norm(median_w, axis=1, keepdims=True)
    median_c = np.median(np.array([fit["maxc_src"] for fit in fits], dtype=np.float64), axis=0)
    ids = "\n".join(sorted(fit["slide_id"] for fit in fits))
    return StainReference(
        reference_id=reference_id,
        w_tgt=median_w.tolist(),
        maxc_tgt=median_c.tolist(),
        fitter_version=FITTER_VERSION,
        n_slides=len(fits),
        slide_ids_sha256=hashlib.sha256(ids.encode("utf-8")).hexdigest(),
        source=source,
    )


def write_reference(reference: StainReference, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(reference.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    out.write_bytes(text.encode("utf-8"))


def _cmd_patch(args: argparse.Namespace) -> None:
    profiles = SpecimenProfilesConfig.model_validate(yaml.safe_load(Path(args.specimen_profiles).read_text(encoding="utf-8")))
    cfg = profiles.profiles[args.specimen].stain_fit
    rgb = np.array(Image.open(args.patch).convert("RGB"), dtype=np.uint8)
    reference = reference_from_patch(rgb, cfg, args.reference_id, source=f"{Path(args.patch).as_posix()} (single patch)")
    write_reference(reference, Path(args.out))
    print(f"wrote {args.out}: w_tgt={reference.w_tgt} maxc_tgt={reference.maxc_tgt}")


def _cmd_median(args: argparse.Namespace) -> None:
    records = json.loads(Path(args.fits).read_text(encoding="utf-8"))
    chosen = stratified_sample(records, args.n, args.seed)
    if len(chosen) < args.n:
        print(f"warning: only {len(chosen)} slides available for the requested {args.n}", file=sys.stderr)
    reference = median_reference(
        chosen, args.reference_id, source=f"median of {len(chosen)} train slides (seed {args.seed}, TSS-stratified)"
    )
    write_reference(reference, Path(args.out))
    print(f"wrote {args.out}: {reference.n_slides} slides")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    patch = sub.add_parser("patch", help="fit one image")
    patch.add_argument("--patch", required=True)
    patch.add_argument("--specimen", required=True, choices=["resection", "core_biopsy"])
    patch.add_argument("--specimen-profiles", default="configs/specimen_profiles.yaml")
    patch.add_argument("--reference-id", required=True)
    patch.add_argument("--out", required=True)
    patch.set_defaults(run=_cmd_patch)

    median = sub.add_parser("median", help="median over train-slide fits")
    median.add_argument("--fits", required=True)
    median.add_argument("--n", type=int, default=DEFAULT_SLIDES)
    median.add_argument("--seed", type=int, default=DEFAULT_SEED)
    median.add_argument("--reference-id", required=True)
    median.add_argument("--out", required=True)
    median.set_defaults(run=_cmd_median)

    args = parser.parse_args(argv)
    args.run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
