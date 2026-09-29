# Mitosis baseline: MIDOG++ 094.tiff

- Image sha256 `08838742cc89393aed0076e9670b05ac3caa8e0381d56e378c463791d5240630`, 0.2298 µm/px (TIFF tags); 82 mitotic figures, 102 imposters.
- Labels `MIDOG++.json` sha256 `8c87ab4276eb11f6b6b5ac0d956b89dad7568a5474e0904b653d54dc68091f47`.
- Detector `kongnet_det_midog_1` models/831662348912558080@1@2026-09-29, weights `978afb3313963eca54ab9afd00c1b5febc485d6c908afeadb5b992844fab6198`; run_mode eval, config_hash `d02265f708064c7410a3876204ebc99960a6265374e29a8a33aa767b79ed31bf`.
- Stage A: 112 raw points ≥ min_prob 0.01; tiles 512 px at 0.25 µm/px, stride 448, ownership.
- Matching: Hungarian, 7.5 µm (eval.metrics). One image, one scanner: a baseline and regression check, not evidence of generalisation.

## A1: KongNet alone

| NMS radius (µm) | AP | best τ | P | R | F1 at best τ | τ at R ≥ 0.95 | F1 there |
|---|---|---|---|---|---|---|---|
| 5.0 | 0.806 | 0.751 | 0.815 | 0.915 | **0.862** | 0.253 | 0.843 |
| 7.5 | 0.806 | 0.751 | 0.815 | 0.915 | **0.862** | 0.253 | 0.843 |
| 10.0 | 0.806 | 0.751 | 0.815 | 0.915 | **0.862** | 0.253 | 0.843 |
| 12.5 | 0.806 | 0.751 | 0.815 | 0.915 | **0.862** | 0.253 | 0.843 |
| 20.0 | 0.772 | 0.751 | 0.818 | 0.878 | **0.847** | 0.010 | nan |

Current production setting (τ 0.75, NMS 7.5 µm): P 0.815, R 0.915, F1 0.862 (TP 75, FP 17, FN 7).
Best A1: NMS 5.0 µm, τ 0.751, F1 0.862.

## A2: A1 at the recall threshold + the production Gemini referee

103 candidates at τ 0.253 (NMS 5.0 µm) sent to the referee; verdicts {'MITOTIC_FIGURE': 66, 'NOT_MITOTIC_FIGURE': 28, 'EQUIVOCAL': 9}.

| Arm | Detections | TP | FP | FN | P | R | F1 |
|---|---|---|---|---|---|---|---|
| A1 (τ 0.751) | 92 | 75 | 17 | 7 | 0.815 | 0.915 | 0.862 |
| A2 (referee MITOTIC_FIGURE) | 66 | 50 | 16 | 32 | 0.758 | 0.610 | 0.676 |

Paired on the 82 labelled figures (exact McNemar on found/missed): p = 0.000.

DecisionRecords this run: 186 (186 cache hits), all status ['ok'].

## A1 again through the M3 read path (SlideReader, 2026-09-30)

After merging M3 (WP-3.1–3.4), tiles are read with `pipeline.slide_io.read_region_at_mpp` from an
OpenSlide-readable (tiled, pixel-identical) copy of 094: 111 raw points; AP 0.806; best F1 0.860
(τ 0.867, NMS 5–12.5 µm); **production setting τ 0.75 / NMS 7.5 µm: P 0.804, R 0.902, F1 0.851**
(TP 74, FP 18, FN 8). The curve is flat between τ 0.75 and 0.87, so τ stays at 0.75 rather than
being tuned to one image. A2 above was measured with the pre-M3 referee inputs (raw colour);
the M3 referee sees stain-normalised crops, which a MIDOG++ image cannot provide (no stain profile).

## A0: production before 2026-09-29 (recorded, not re-runnable)

Measured on the same image and endpoint before MIDOG-microservice #2 fixed the coordinates
server-side, with scratch scripts (legacy contract, 512 px native tiles, KongNet's own 0.99):

| Setup | Detections | TP | FP | P | R | F1 |
|---|---|---|---|---|---|---|
| Coordinates as tiatoolbox 2.0.1 returned them (every deployment until then) | 104 | 11 | 93 | 0.11 | 0.13 | 0.12 |
| Same, x/y swapped back | 90 | 75 | 15 | 0.83 | 0.91 | 0.87 |
| Downsampled to 0.5 µm/px, swapped back, best threshold | 21 | 14 | 7 | 0.67 | 0.17 | 0.27 |

## Decisions (configs/mitosis.yaml)

- `det_threshold` 0.75 and `nms_radius_um` 7.5 (A1 best; 20 µm merged true figures).
- `referee.enabled: false`: A2 is worse than A1 (SPEC-06 §6.3 rule 2, cheaper arm). Revisit with the
  v2 definition and post-rule (arm A3, WP-7.5) or a trained classifier (A4), measured the same way.
