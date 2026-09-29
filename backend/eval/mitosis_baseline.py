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
"""
import argparse
import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
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
from eval.metrics import CaseMitosis, RegionPoints, mcnemar_exact, match_points, mitosis_f1, mitosis_pr_curve
from pipeline.detect import apply_global_nms
from pipeline.mitosis_detect import detect_region, make_detect_batch
from pipeline.slide_io import SlideReader, read_region_at_mpp
from pipeline.verify import mitosis_referee_images

Image.MAX_IMAGE_PIXELS = None
MATCH_RADIUS_UM = 7.5            # SPEC-00 §2.1
NMS_RADII_UM = (5.0, 7.5, 10.0, 12.5, 20.0)   # SPEC-06 §5.7 candidates, plus production's 20
RECALL_TARGET = 0.95             # SPEC-06 §5.2 τ_A
THREADS = 4


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


def ground_truth(labels_path: Path, file_name: str, mpp: float) -> dict[str, np.ndarray]:
    data = json.loads(labels_path.read_text(encoding="utf-8"))
    image = [i for i in data["images"] if i["file_name"] == file_name]
    if not image:
        raise SystemExit(f"{file_name} is not in {labels_path}")
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
    return [
        {"id": f"m_{i:05d}", "centroid_um": [p["x_um"], p["y_um"]], "det_conf": p["prob"], "ver_conf": None,
         "label": "unreviewed"}
        for i, p in enumerate(points) if p["prob"] >= threshold
    ]


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--referee", action="store_true", help="also run arm A2 (one VLM call per candidate)")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)

    config = init_pipeline_config()
    log = DecisionLog()
    gateway = ModelGateway(config, production_adapters(), log, LocalBlobStore(args.work / "blobs"))
    case_id = uuid.uuid5(uuid.NAMESPACE_URL, f"midogpp/{args.image.name}")
    ctx = DecisionContext(case_id=case_id, stage_execution_id=uuid.uuid4(), stage="mitosis",
                          run_mode=RunMode.EVAL, run_id=None, config_hash=get_config_hash())

    points, mpp, image_sha = stage_a(args.image, args.work, config, gateway, ctx)
    gt = ground_truth(args.labels, args.image.name, mpp)
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
        scores = np.array([c["det_conf"] for c in kept], dtype=float)
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
    current = score(mf, apply_global_nms(as_candidates(points, det_cfg.det_threshold), det_cfg.nms_radius_um), args.image.stem)
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
