"""Mitosis baseline on labelled MIDOG++ images, through the production Stage-A code path.

Runs each image through ``pipeline.mitosis_detect.detect_region`` against the deployed
detector in ``run_mode=eval``, then scores it with ``eval.metrics`` (Hungarian matching,
7.5 µm; SPEC-00 §2.1):

- A1: KongNet alone, every threshold from the raw Stage-A points, NMS radius in {5, 7.5,
  10, 12.5, 20} µm (SPEC-06 §5.2, §5.7).
- A2 (``--referee``): A1's candidates at the recall threshold (largest τ with recall ≥
  0.95), each judged by the production referee (same prompt, schema and images as
  worker/mitosis.py); only MITOTIC_FIGURE counts, as in production.

Stage-A points and gateway blobs are cached under ``--work``, so reruns cost nothing.

    VERTEX_MITOSIS_ENDPOINT_ID=6276949705008087040 python -m eval.mitosis_baseline \\
        --image 094.tiff --labels MIDOG++.json --work out/baseline --report report.md --referee

With ``--split val`` (WP-7.8) it runs every image of that split of the locked MIDOG++ breast split
with the production settings only (τ and NMS radius from ``configs/mitosis.yaml``; nothing here can
override them) and reports pooled and per-scanner NS-M, P, R and count error with bootstrap CIs.
KongNet-Det was trained on ~90% of MIDOG++, so this is an in-distribution regression check of the
pipeline, not a validation of the detector. The test split needs ``--owner-approved-test``.

    python -m eval.mitosis_baseline --split val --images-dir midogpp/images --labels MIDOG++.json \\
        --work out/midogpp_val --report ../reports/mitosis/baseline_midogpp_breast.md
"""
import argparse
import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from PIL import Image

from app.core.pipeline_config import get_config_hash, init_pipeline_config
from app.core.run_context import DecisionContext, RunMode
from app.core.tasks import EntityType, Task
from app.inference.adapters.registry import production_adapters
from app.inference.blobs import LocalBlobStore
from app.inference.gateway import EntityRef, ModelGateway, ModelInputs
from app.inference.records import DecisionLog
from app.inference.schemas import MitosisVerdict
from eval.datasets.midogpp import MIDOGppAdapter, image_mpp
from eval.metrics import (CaseMitosis, RegionPoints, bootstrap, mcnemar_exact, match_points, mitosis_f1,
                          mitosis_pr_curve)
from eval.splits import verify_lock
from pipeline.detect import apply_global_nms
from pipeline.mitosis_detect import detect_region, make_detect_batch
from pipeline.slide_io import SlideReader, read_region_at_mpp
from pipeline.verify import mitosis_referee_images

Image.MAX_IMAGE_PIXELS = None
MATCH_RADIUS_UM = 7.5            # SPEC-00 §2.1
NMS_RADII_UM = (5.0, 7.5, 10.0, 12.5, 20.0)   # SPEC-06 §5.7 candidates, plus production's 20
RECALL_TARGET = 0.95             # SPEC-06 §5.2 τ_A
THREADS = 4
NS_M_FLOOR = 0.70                # SPEC-00 floor: a pooled NS-M below it stops the iteration (WP-7.8)
COUNT_AREA_MM2 = 2.0             # count error per 2 mm² (SPEC-06 §6.1)
BOOTSTRAP_B = 2000               # SPEC-00 §2.4
BOOTSTRAP_SEED = 20260928
SPLITS_DIR = Path(__file__).parent / "splits"
SPLIT_FILE = SPLITS_DIR / "midogpp_breast.parquet"
LOCK_ROOT = Path(__file__).parents[1]  # lock paths are relative to backend/
REGRESSION_NOTE = (
    "In-distribution regression check, not validation: KongNet-Det was trained on about 90% of MIDOG++ "
    "(arXiv 2510.23559; the held-out list is unpublished), so most of these images were in its training data. "
    "A good result shows the production pipeline (tiling, resampling, τ, NMS) reproduces the detector; "
    "it says nothing about how the detector generalises."
)


def openslide_copy(image_path: Path, work: Path) -> Path:
    """A tiled TIFF of the same pixels that OpenSlide reads (MIDOG++ TIFFs are stripped).

    The worker reads slides through SlideReader (OpenSlide); so does the baseline. Needs tifffile.
    """
    import tifffile

    target = work / f"{image_path.stem}_tiled.tiff"
    if not target.is_file():
        with Image.open(image_path) as source:
            tags = source.tag_v2
            rgb = np.asarray(source.convert("RGB"))
        tifffile.imwrite(target, rgb, tile=(256, 256), photometric="rgb", compression="zlib",
                         resolution=(float(tags[282]), float(tags[283])), resolutionunit=int(tags[296]))
    return target


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ground_truth(data: dict, file_name: str, mpp: float) -> dict[str, np.ndarray]:
    """Labelled points in µm by class ("MF", "imposter") for one image of the parsed ``MIDOG++.json``."""
    image = [i for i in data["images"] if i["file_name"] == file_name]
    if not image:
        raise SystemExit(f"{file_name} is not in the MIDOG++ labels")
    categories = {c["id"]: c["name"] for c in data["categories"]}
    annotations = [a for a in data["annotations"] if a["image_id"] == image[0]["id"]]
    points = MIDOGppAdapter().ground_truth_points(annotations, categories, mpp)
    return {k: np.array(points.get(k, []), dtype=float).reshape(-1, 2) for k in ("MF", "imposter")}


def score(gt: np.ndarray, candidates: list[dict], case_id: str):
    pred = np.array([c["centroid_um"] for c in candidates], dtype=float).reshape(-1, 2)
    return mitosis_f1([CaseMitosis(case_id, [RegionPoints(gt, pred)])], radius_um=MATCH_RADIUS_UM)


def hits(gt: np.ndarray, candidates: list[dict]) -> list[bool]:
    """Per ground-truth figure: matched by a candidate (Hungarian, 7.5 µm)."""
    pred = np.array([c["centroid_um"] for c in candidates], dtype=float).reshape(-1, 2)
    matched = {i for i, _ in match_points(gt, pred, MATCH_RADIUS_UM).pairs}
    return [i in matched for i in range(len(gt))]


def as_candidates(points: list[dict], threshold: float) -> list[dict]:
    """Stage-A points at or above ``threshold`` as v6 candidates (NMS orders by ``p_b ?? p_a``; no classifier here)."""
    return [
        {"id": f"m_{i:05d}", "centroid_um": [p["x_um"], p["y_um"]], "p_a": p["prob"], "p_b": None}
        for i, p in enumerate(points) if p["prob"] >= threshold
    ]


def production_candidates(points: list[dict], det_cfg) -> list[dict]:
    """The production Stage-A output: τ, then NMS at the configured radius (configs/mitosis.yaml, D17/D19)."""
    return apply_global_nms(as_candidates(points, det_cfg.det_threshold), nms_radius_um=det_cfg.nms_radius_um)


@dataclass(frozen=True)
class ImageResult:
    """One image's matching counts at the production settings."""
    file_name: str
    scanner: str
    area_mm2: float
    n_gt: int
    n_pred: int
    tp: int
    fp: int
    fn: int

    @property
    def f1(self) -> float:
        denom = 2 * self.tp + self.fp + self.fn
        return 2 * self.tp / denom if denom else float("nan")

    @property
    def count_error(self) -> float:
        """Signed (detected - labelled) mitoses per COUNT_AREA_MM2 of the image."""
        return (self.n_pred - self.n_gt) * COUNT_AREA_MM2 / self.area_mm2


def image_result(file_name: str, scanner: str, gt_mf: np.ndarray, candidates: list[dict], area_mm2: float) -> ImageResult:
    prf = score(gt_mf, candidates, file_name)
    return ImageResult(file_name, scanner, area_mm2, len(gt_mf), len(candidates), prf.tp, prf.fp, prf.fn)


def pooled(results: list[ImageResult]) -> dict[str, float]:
    """Micro-pooled P, R and NS-M (F1) from summed per-image counts, as ``eval.metrics.mitosis_f1`` pools regions."""
    tp, fp, fn = (sum(getattr(r, k) for r in results) for k in ("tp", "fp", "fn"))
    nan = float("nan")
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else nan,
        "recall": tp / (tp + fn) if tp + fn else nan,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else nan,
    }


def summarise(results: list[ImageResult]) -> dict:
    """Pooled metrics with image-level percentile bootstrap CIs (SPEC-00 §2.4)."""
    out: dict = {**pooled(results), "n_images": len(results),
                 "n_gt": sum(r.n_gt for r in results), "n_pred": sum(r.n_pred for r in results)}
    for name in ("precision", "recall", "f1"):
        out[f"{name}_ci"] = bootstrap(lambda rs, n=name: pooled(rs)[n], results, B=BOOTSTRAP_B, seed=BOOTSTRAP_SEED)
    out["count_mae"] = bootstrap(lambda rs: float(np.mean([abs(r.count_error) for r in rs])), results,
                                 B=BOOTSTRAP_B, seed=BOOTSTRAP_SEED)
    out["count_signed"] = bootstrap(lambda rs: float(np.mean([r.count_error for r in rs])), results,
                                    B=BOOTSTRAP_B, seed=BOOTSTRAP_SEED)
    return out


def split_images(split: str, owner_approved_test: bool, split_file: Path = SPLIT_FILE,
                 lock_path: Path = SPLITS_DIR / "SPLITS.lock", lock_root: Path = LOCK_ROOT) -> pd.DataFrame:
    """The images of one split of the locked MIDOG++ breast split; the test split only after owner approval."""
    if split == "test" and not owner_approved_test:
        raise SystemExit("the test split is read once, after the owner approves the val result (WP-7.8); "
                         "pass --owner-approved-test only then")
    verify_lock(lock_path, lock_root)
    if sha256_file(split_file) not in json.loads(lock_path.read_text(encoding="utf-8")).values():
        raise SystemExit(f"{split_file} is not in {lock_path}")
    frame = pd.read_parquet(split_file)
    chosen = frame[frame["split"] == split].sort_values("image_id").reset_index(drop=True)
    if chosen.empty:
        raise SystemExit(f"{split_file} has no {split!r} images")
    return chosen


def run_validation(frame: pd.DataFrame, images_dir: Path, labels: dict,
                   detect_points: Callable[[Path], list[dict]], det_cfg) -> list[ImageResult]:
    """Every image of ``frame`` through Stage A (``detect_points``) and the production τ/NMS, scored per image."""
    paths = {row.file_name: images_dir / row.file_name for row in frame.itertuples()}
    missing = sorted(name for name, path in paths.items() if not path.is_file())
    if missing:
        raise FileNotFoundError(f"{len(missing)} images of the split are not in {images_dir}, e.g. {missing[:3]}")
    results = []
    for row in frame.itertuples():
        path = paths[row.file_name]
        with Image.open(path) as image:
            if image.size != (row.width_px, row.height_px):
                raise ValueError(f"{path} is {image.size}, the labels say {(row.width_px, row.height_px)}")
        mpp_x, mpp_y = image_mpp(path)
        gt = ground_truth(labels, row.file_name, mpp_x)["MF"]
        area_mm2 = row.width_px * row.height_px * mpp_x * mpp_y / 1e6
        cands = production_candidates(detect_points(path), det_cfg)
        results.append(image_result(row.file_name, row.scanner, gt, cands, area_mm2))
    return results


def _ci(ci, digits: int = 3) -> str:
    return f"{ci.point:.{digits}f} ({ci.low:.{digits}f}–{ci.high:.{digits}f})"


def validation_report(results: list[ImageResult], split: str, meta: list[str]) -> list[str]:
    """Markdown: pooled and per-scanner metrics with CIs, the SPEC-00 floor check, and per-image rows."""
    scanners = sorted({r.scanner for r in results})
    groups = [("All", results)] + [(s, [r for r in results if r.scanner == s]) for s in scanners]
    lines = [
        f"# Mitosis baseline: MIDOG++ breast, {split} split",
        "",
        f"> {REGRESSION_NOTE}",
        "",
        *meta,
        "",
        "## Pooled and per scanner",
        "",
        "| Group | Images | Labelled MF | Detections | TP | FP | FN | P (95% CI) | R (95% CI) | NS-M (95% CI) "
        f"| Count MAE per {COUNT_AREA_MM2:g} mm² (95% CI) | Signed count error per {COUNT_AREA_MM2:g} mm² (95% CI) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    summaries = {}
    for name, rs in groups:
        m = summaries[name] = summarise(rs)
        lines.append(f"| {name} | {m['n_images']} | {m['n_gt']} | {m['n_pred']} | {m['tp']} | {m['fp']} | {m['fn']} | "
                     f"{_ci(m['precision_ci'])} | {_ci(m['recall_ci'])} | **{_ci(m['f1_ci'])}** | "
                     f"{_ci(m['count_mae'], 2)} | {_ci(m['count_signed'], 2)} |")
    f1 = summaries["All"]["f1"]
    lines += ["", f"CIs: percentile bootstrap over images, B {BOOTSTRAP_B}, seed {BOOTSTRAP_SEED}.", ""]
    if not f1 >= NS_M_FLOOR:  # also stops on NaN
        lines.append(f"**STOP: pooled NS-M {f1:.3f} is below the SPEC-00 floor {NS_M_FLOOR}.** Report to the owner; "
                     "do not tune (D19).")
    else:
        lines.append(f"Pooled NS-M {f1:.3f} is at or above the SPEC-00 floor {NS_M_FLOOR}.")
    lines += [
        "Compare with the one-image check on 094 (`reports/baseline/mitosis_midogpp_094.md`); 094 is in val and "
        "has its own row below.",
        "",
        "## Per image",
        "",
        f"| Image | Scanner | Area (mm²) | Labelled MF | Detections | TP | FP | FN | F1 "
        f"| Count error per {COUNT_AREA_MM2:g} mm² |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [f"| {r.file_name} | {r.scanner} | {r.area_mm2:.3f} | {r.n_gt} | {r.n_pred} | {r.tp} | {r.fp} | {r.fn} | "
              f"{r.f1:.3f} | {r.count_error:+.2f} |" for r in results]
    return lines


def stage_a(image_path: Path, work: Path, config, gateway, ctx) -> tuple[list[dict], float, str]:
    """Raw Stage-A points in µm for the whole image (cached per image, detector and config)."""
    det_cfg = config.mitosis.detector
    entry = config.models.models[det_cfg.producer]
    mpp_x, mpp_y = image_mpp(image_path)
    image_sha = sha256_file(image_path)
    key = hashlib.sha256(json.dumps(
        [image_sha, entry.version, entry.weights_sha256, det_cfg.model_dump(mode="json")], sort_keys=True
    ).encode()).hexdigest()[:16]
    cache = work / f"stage_a_{image_path.stem}_{key}.json"
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))["points"], mpp_x, image_sha

    started = time.time()
    with SlideReader(str(openslide_copy(image_path, work)), mpp_x, mpp_y, "generic-tiff") as reader:
        tile_um = det_cfg.tile_size_um
        points = detect_region(
            lambda x_um, y_um: read_region_at_mpp(reader, x_um, y_um, tile_um, tile_um, det_cfg.mpp).rgb,
            (0.0, 0.0, *reader.extent_um()),
            det_cfg,
            make_detect_batch(gateway, ctx, det_cfg, entry),
            batch_size=entry.limits.max_batch,
            threads=THREADS,
            tile_prefix=image_path.stem,
        )
    rows = [{"x_um": p.x_um, "y_um": p.y_um, "prob": p.prob, "tile_id": p.tile_id, "record_id": p.record_id} for p in points]
    cache.write_text(json.dumps({
        "image": image_path.name, "image_sha256": image_sha, "mpp": [mpp_x, mpp_y],
        "detector_version": entry.version, "weights_sha256": entry.weights_sha256,
        "seconds": round(time.time() - started, 1), "points": rows,
    }), encoding="utf-8")
    return rows, mpp_x, image_sha


def referee(candidates: list[dict], image_path: Path, work: Path, config, gateway, ctx) -> dict[str, str]:
    """The production referee's verdict per candidate id (same images as worker/mitosis.py)."""
    ref_cfg = config.mitosis.referee
    if ref_cfg.color != "raw":
        raise SystemExit(f"the referee is configured for {ref_cfg.color} colour, which needs the slide's stain "
                         "profile; a MIDOG++ image has none")
    mpp_x, mpp_y = image_mpp(image_path)
    with SlideReader(str(openslide_copy(image_path, work)), mpp_x, mpp_y, "generic-tiff") as reader:
        inputs = {
            c["id"]: mitosis_referee_images(reader, c["centroid_um"][0], c["centroid_um"][1], ref_cfg, None)
            for c in candidates
        }

    def judge(cand):
        images = inputs[cand["id"]]
        result = gateway.invoke(
            Task.MITOSIS_REFEREE, ref_cfg.producer, ModelInputs(images=(images.focus, images.context), prompt_id=ref_cfg.prompt),
            ctx, EntityRef(EntityType.CANDIDATE, cand["id"]), MitosisVerdict,
        )
        return cand["id"], result.output.verdict

    with ThreadPoolExecutor(THREADS) as pool:
        return dict(pool.map(judge, candidates))


def build_parser() -> argparse.ArgumentParser:
    """No option sets τ or the NMS radius: a run uses configs/mitosis.yaml as deployed (D19)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--image", type=Path, help="one image: the A1 sweep (and A2 with --referee)")
    target.add_argument("--split", choices=("val", "test"), help="every image of this MIDOG++ breast split (WP-7.8)")
    parser.add_argument("--images-dir", type=Path, help="with --split: where the MIDOG++ images are")
    parser.add_argument("--owner-approved-test", action="store_true", help="with --split test: the owner approved it")
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--referee", action="store_true", help="with --image: also run arm A2 (one VLM call per candidate)")
    return parser


def eval_context(image_name: str) -> DecisionContext:
    case_id = uuid.uuid5(uuid.NAMESPACE_URL, f"midogpp/{image_name}")
    return DecisionContext(case_id=case_id, stage_execution_id=uuid.uuid4(), stage="mitosis",
                           run_mode=RunMode.EVAL, run_id=None, config_hash=get_config_hash())


def validate(args, config, gateway, log) -> int:
    """``--split``: every image of the split at the production settings, pooled and per scanner (WP-7.8)."""
    if args.images_dir is None:
        raise SystemExit("--split needs --images-dir")
    if args.referee:
        raise SystemExit("--split runs the baseline only; the referee is off (D19)")
    frame = split_images(args.split, args.owner_approved_test)
    labels = json.loads(args.labels.read_text(encoding="utf-8"))
    det_cfg = config.mitosis.detector
    entry = config.models.models[det_cfg.producer]
    results = run_validation(
        frame, args.images_dir, labels,
        lambda path: stage_a(path, args.work, config, gateway, eval_context(path.name))[0], det_cfg,
    )
    records = log.pending()
    meta = [
        f"- Split `{SPLIT_FILE.name}` sha256 `{sha256_file(SPLIT_FILE)}` (SPLITS.lock), {args.split}: {len(frame)} images.",
        f"- Labels `{args.labels.name}` sha256 `{sha256_file(args.labels)}`.",
        f"- Detector `{det_cfg.producer}` {entry.version}, weights `{entry.weights_sha256}`; run_mode eval, "
        f"config_hash `{get_config_hash()}`.",
        f"- Production settings from configs/mitosis.yaml: τ {det_cfg.det_threshold}, NMS {det_cfg.nms_radius_um} µm; "
        f"tiles {det_cfg.tile_size_px} px at {det_cfg.mpp} µm/px, stride {det_cfg.stride_px}. Referee off.",
        f"- Matching: Hungarian, {MATCH_RADIUS_UM} µm (eval.metrics). Ground truth: one label per dividing cell, "
        "no harmonisation (registry.yaml, midogpp_breast).",
        f"- DecisionRecords this run: {len(records)} ({sum(1 for r in records if r['cache_hit'])} cache hits).",
    ]
    lines = validation_report(results, args.split, meta)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (args.work / f"results_{args.split}.json").write_text(json.dumps([asdict(r) for r in results], indent=1),
                                                          encoding="utf-8")
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.work.mkdir(parents=True, exist_ok=True)

    config = init_pipeline_config()
    log = DecisionLog()
    gateway = ModelGateway(config, production_adapters(), log, LocalBlobStore(args.work / "blobs"))
    if args.split is not None:
        return validate(args, config, gateway, log)
    ctx = eval_context(args.image.name)

    points, mpp, image_sha = stage_a(args.image, args.work, config, gateway, ctx)
    gt = ground_truth(json.loads(args.labels.read_text(encoding="utf-8")), args.image.name, mpp)
    mf = gt["MF"]
    det_cfg = config.mitosis.detector
    entry = config.models.models[det_cfg.producer]

    lines = [
        f"# Mitosis baseline: MIDOG++ {args.image.name}",
        "",
        f"- Image sha256 `{image_sha}`, {mpp:.4f} µm/px (TIFF tags); {len(mf)} mitotic figures, {len(gt['imposter'])} imposters.",
        f"- Labels `{args.labels.name}` sha256 `{sha256_file(args.labels)}`.",
        f"- Detector `{det_cfg.producer}` {entry.version}, weights `{entry.weights_sha256}`; run_mode eval, config_hash `{get_config_hash()}`.",
        f"- Stage A: {len(points)} raw points ≥ min_prob {det_cfg.min_prob}; tiles {det_cfg.tile_size_px} px at {det_cfg.mpp} µm/px, stride {det_cfg.stride_px}, ownership.",
        f"- Matching: Hungarian, {MATCH_RADIUS_UM} µm (eval.metrics). One image, one scanner: a baseline and regression check, not evidence of generalisation.",
        "",
        "## A1: KongNet alone",
        "",
        "| NMS radius (µm) | AP | best τ | P | R | F1 at best τ | τ at R ≥ 0.95 | F1 there |",
        "|---|---|---|---|---|---|---|---|",
    ]
    best = None
    for radius in NMS_RADII_UM:
        kept = apply_global_nms(as_candidates(points, det_cfg.min_prob), nms_radius_um=radius)
        pred = np.array([c["centroid_um"] for c in kept], dtype=float).reshape(-1, 2)
        scores = np.array([c["p_a"] for c in kept], dtype=float)
        curve = mitosis_pr_curve([CaseMitosis(args.image.stem, [RegionPoints(mf, pred, scores)])], MATCH_RADIUS_UM)
        i = int(np.nanargmax(curve.f1))
        recall_ok = [j for j, r in enumerate(curve.recall) if r >= RECALL_TARGET]
        tau_recall = float(curve.thresholds[recall_ok[0]]) if recall_ok else det_cfg.min_prob
        f1_recall = float(curve.f1[recall_ok[0]]) if recall_ok else float("nan")
        lines.append(f"| {radius} | {curve.ap:.3f} | {curve.thresholds[i]:.3f} | {curve.precision[i]:.3f} | "
                     f"{curve.recall[i]:.3f} | **{curve.f1[i]:.3f}** | {tau_recall:.3f} | {f1_recall:.3f} |")
        row = {"radius": radius, "tau": float(curve.thresholds[i]), "f1": float(curve.f1[i]), "tau_recall": tau_recall}
        if best is None or row["f1"] > best["f1"]:
            best = row
    current = score(mf, production_candidates(points, det_cfg), args.image.stem)
    lines += [
        "",
        f"Current production setting (τ {det_cfg.det_threshold}, NMS {det_cfg.nms_radius_um} µm): "
        f"P {current.precision:.3f}, R {current.recall:.3f}, F1 {current.f1:.3f} (TP {current.tp}, FP {current.fp}, FN {current.fn}).",
        f"Best A1: NMS {best['radius']} µm, τ {best['tau']:.3f}, F1 {best['f1']:.3f}.",
    ]
    summary = {"a1_best": best, "a1_current": current.__dict__}

    if args.referee:
        a1 = apply_global_nms(as_candidates(points, best["tau"]), nms_radius_um=best["radius"])
        pool = apply_global_nms(as_candidates(points, best["tau_recall"]), nms_radius_um=best["radius"])
        verdicts = referee(pool, args.image, args.work, config, gateway, ctx)
        a2 = [c for c in pool if verdicts[c["id"]] == "MITOTIC_FIGURE"]
        s1, s2 = score(mf, a1, args.image.stem), score(mf, a2, args.image.stem)
        counts = {v: sum(1 for x in verdicts.values() if x == v) for v in ("MITOTIC_FIGURE", "NOT_MITOTIC_FIGURE", "EQUIVOCAL")}
        p_value = mcnemar_exact(hits(mf, a1), hits(mf, a2))
        lines += [
            "",
            "## A2: A1 at the recall threshold + the production Gemini referee",
            "",
            f"{len(pool)} candidates at τ {best['tau_recall']:.3f} (NMS {best['radius']} µm) sent to the referee; verdicts {counts}.",
            "",
            "| Arm | Detections | TP | FP | FN | P | R | F1 |",
            "|---|---|---|---|---|---|---|---|",
            f"| A1 (τ {best['tau']:.3f}) | {len(a1)} | {s1.tp} | {s1.fp} | {s1.fn} | {s1.precision:.3f} | {s1.recall:.3f} | {s1.f1:.3f} |",
            f"| A2 (referee MITOTIC_FIGURE) | {len(a2)} | {s2.tp} | {s2.fp} | {s2.fn} | {s2.precision:.3f} | {s2.recall:.3f} | {s2.f1:.3f} |",
            "",
            f"Paired on the {len(mf)} labelled figures (exact McNemar on found/missed): p = {p_value:.3f}.",
        ]
        summary["a2"] = {"a1": s1.__dict__, "a2": s2.__dict__, "verdicts": counts, "mcnemar_p": p_value}

    records = log.pending()
    lines += ["", f"DecisionRecords this run: {len(records)} "
              f"({sum(1 for r in records if r['cache_hit'])} cache hits), all status {sorted({r['status'] for r in records})}."]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (args.work / "summary.json").write_text(json.dumps(summary, indent=1, default=float), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
