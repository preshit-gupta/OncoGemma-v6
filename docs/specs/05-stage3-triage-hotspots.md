# SPEC-05 — Stage 3 Rebuild: Trained Tumour Head, Full-Coverage Heatmap, Proliferation-Ranked Non-Overlapping Hotspots

| Field | Value |
|---|---|
| Spec ID | SPEC-05 |
| Category (V4) | Model (tumour head), Staging (coverage, geometry), Biological (referee use) |
| Issues covered | A1, A9, N7, N8 |
| Depends on | SPEC-01, SPEC-02 (BCSS/BCNB adapters, splits), SPEC-04 (`read_region_at_mpp`, `TissueMask`, specimen profiles) |
| Blocks | SPEC-06 (tumour mask gate, hotspots), SPEC-07 (tumour sampling frame) |
| Metrics owned | S3-F1, S3-P@K, S3-COV (SPEC-00 §2.3); contributes to `F1_M` and NS-G |

## 1. Root-cause analysis

### 1.1 The tumour probability is not a tumour probability (A1)

- `models/probe/probe_v1.joblib` is **bit-identical** to what `train_default_probe()` produces (`pipeline/probe.py:63-92`). That function is a `LogisticRegression` fitted on `X = randn(500, 384)` with the label `y = [mean(X) > 0]`. Its coefficient vector has cosine ≈ 0.705 with the all-ones vector, so the model roughly answers "is the mean embedding component positive?". The metadata says `"dataset": "synthetic_dev"`.
- The fused score (`worker/triage.py:534`) is

  ```
  p = clip( (0.35·p_probe + 0.65·c) · (0.40 + 0.60·m) · 1.25 , 0.05, 0.98 )
  c = clip((OD_thumb − p10)/(p90 − p10), 0, 1)       # OD summed over RGB of an 80-px-wide thumbnail
  m = clip(EDT_to_tissue_edge / 2, 0.15, 1)
  ```

  Since `p_probe` is uninformative, the heatmap reduces to *stain darkness × distance from the tissue edge*.

### 1.2 Heatmap gaps (N7, Notes screenshot 1)

1. The overview grid is fixed at `nx = 80` columns (`worker/triage.py:278`), so a cell measures `W_um/80`, which is ≈ 300–500 µm on typical slides.
2. The tissue mask is the Stage-2 mask NEAREST-resized to 80×ny, intersected with `~is_glass` (`r,g,b > 215` on an 80-px thumbnail). Pale stroma and fat therefore count as glass.
3. **`binary_opening(3×3)`** (`worker/triage.py:338`) deletes any structure narrower than about 3 cells, i.e. about 0.9–1.5 mm. A core-needle core is about 1 mm wide, and a diagonal band loses most of its cells. The components-≥-25-cells filter (`:344`), about 2.4 mm² at 312 µm cells, removes small fragments.
4. `extract_hotspots` runs a second opening and a ≥ 15-cell filter (`pipeline/hotspots.py:56-63`).
5. The removed cells stay `NaN`, which renders with alpha 0 (`worker/triage.py:216`). That is the visible gap.

Sampling adds error on top:
- The budget is 2,048 patches, 80% chosen by thumbnail OD and 20% strided (`:377-427`).
- Several 224 µm patches map to the same 80-grid cell, and the last write wins (`:529-535`).
- Unsampled cells are filled by 3-NN IDW in grid space (`:537-554`).

### 1.3 Overlapping hotspots (N8, Notes screenshot 2)

- Hotspots are axis-aligned **squares** of side `s = 600 µm` (`hotspots.py:31`), but their separation is tested as the **Euclidean** distance between centres in grid cells:
  - primary pass: ≥ `max(3, round(800/stride))` cells (`:100`)
  - fill pass: ≥ 0.7 of that (`:183`)
- Two such squares overlap iff `|dx| < s` and `|dy| < s`. A Euclidean distance can therefore pass while the squares still overlap. Worked example with stride = 312 µm:
  - The fill-pass threshold is `0.7 × 3 × 312 = 656 µm`.
  - Diagonal neighbours at `(464, 464) µm` are 656 µm apart, so they pass.
  - Yet `464 < 600` on both axes, so the squares overlap.
- Diagonal cores produce exactly this diagonal chain of peaks.
- With smaller strides the overlap happens even in the primary pass. For example, stride 190 µm gives a threshold of `4 × 190 = 760 µm`, and the diagonal `(537, 537)` overlaps.
- Pathologist edits are **not validated** for overlap: `app/routers/triage.py:94` (`apply_edit_ops`) and `:556` (`save_triage_edits`).

### 1.4 Rejected regions are used anyway (A9)

The top 20 candidates are refereed (`worker/triage.py:564-613`). Then `selected = confirmed[:10]` is padded with **unconfirmed** candidates up to 10 (`:622-625`). Regions that the referee classified as stroma or adipose then drive the mitosis sweep.

### 1.5 Hotspots target the wrong property

Ranking uses `prob_mean` (`:617`), which in effect is cellularity and darkness. Mitoses are counted in the hotspots (SPEC-06), and Nottingham practice counts mitoses in the most mitotically active invasive area. v5 selects the densest-staining area instead, which is not the same area.

## 2. Goals / non-goals

**Goals**
- A tumour classifier trained on real labels, with calibrated probabilities.
- Every tissue tile scored, so heatmap coverage is exactly 1.0.
- Hotspots that never overlap, are ranked by expected proliferation, never padded, and chosen by F1 evidence.

**Non-goals**
- Replacing Path Foundation. The Notes say to keep the model; the encoder stays.

## 3. Tile grid and embeddings

- **Grid.** The grid is global and anchored at the slide origin. Tile `(i, j)` covers `[i·224, (i+1)·224) × [j·224, (j+1)·224)` µm, which is the Path Foundation input of 224 px at 1.0 µm/px.
  - A tile is included if `TissueMask.fraction_in_box_um(...) ≥ profile.triage.min_tissue_fraction` (0.25).
  - There is **no cap**. The expected tile count is `tissue_mm² / 0.050176`: about 8,000 tiles for 400 mm² of resection tissue, and about 400 tiles for 20 mm² of CNB tissue.
- **Pixels.** `read_region_at_mpp(target_mpp=1.0, color=registry.path_foundation.input.color)`.
  - Tiles are batched per `registry.limits` (`max_batch`, `max_request_bytes`) through the gateway, with one DecisionRecord per batch.
- **Embedding cache.** `gs://<artifacts>/embeddings/<slide_sha256>/<pf_version>/grid224_v1.parquet`.
  - Columns: `i:int32, j:int32, x_um:float32, y_um:float32, tissue_fraction:float32, emb:fixed_size_list<float32,384>`.
  - The cache key includes the grid version and the PF version.
  - This replaces the v5 cache (`worker/triage.py:444-468`), whose key ignored the slide checksum and the grid.
- **Cost.** Dedicated Vertex endpoints are billed per node-hour, not per patch. `configs/pricing.yaml` must model `node_hour_usd × wall_hours` for dedicated endpoints; the v5 value `$0.005 / 1k patches` has to be **verified** against actual billing. The SPEC-02 cost ledger reports cost per slide.

## 4. Tumour head

### 4.1 Label space and training data

The head has seven classes. `invasive_tumor` is the positive class for S3-F1 and for the tumour mask. `in_situ` is kept separate because Nottingham grading and mitotic counting apply only to the invasive component.

| Class | BCSS source classes (**verify class map**) | BCNB source |
|---|---|---|
| `invasive_tumor` | tumor | Inside the tumour polygon (≥ 75% overlap). **Verify** whether BCNB polygons include DCIS |
| `in_situ` | dcis | — |
| `benign_epithelium` | normal acinus/duct | — |
| `stroma` | stroma | Tissue with 0% polygon overlap and > 500 µm from the polygon edge, labelled `non_tumor` (binary loss only) |
| `inflammatory` | lymphocytic infiltrate, plasma cells, other immune | same |
| `necrosis` | necrosis/debris | same |
| `adipose_background` | fat, blood, other or undetermined | same |

- **Tile labelling rule (BCSS).** The annotated area must be ≥ 50% of the tile, and the majority class must cover ≥ 50% of the annotated area. Otherwise the tile is excluded from training and evaluation.
- **Splits.** Splits are inherited from the SPEC-02 patient-level splits, so no BCSS/TCGA patient in test is ever used for training.

### 4.2 Model

- **Features.** The PF embedding (384-d), L2-normalised (as v5), then z-scored with train statistics.
- **Primary model.** Multinomial logistic regression (`sklearn`, `lbfgs`, `class_weight='balanced'`). `C ∈ {0.01 … 10}` is chosen by 5-fold `GroupKFold` on patients within train.
- **Challenger.** An MLP (384 → 256 → 7, GELU, dropout 0.2, AdamW, early stopping on val loss). It is promoted only if the paired-bootstrap ΔF1 on val (S3-F1) has lower bound > 0.
- **Binary objective.** The BCNB `non_tumor` tiles enter through a binary cross-entropy term on `1 − p(invasive_tumor)`. For the linear model, this is implemented as sample weights on a collapsed binary logistic head that is trained jointly and ensembled with the multinomial head; the final choice is made by val F1.
- **Calibration.** Isotonic regression on val, one-vs-rest for `invasive_tumor`.
- **Spatial smoothing (ablation).** A Gaussian with σ = 1 tile on the probability grid, applied before thresholding.
- **Threshold.** `τ_tumor = argmax_τ F1(val)` for binary invasive tumour, reported separately for BCSS val and BCNB val. If the two optima differ by more than 0.1, the specimen profiles carry per-profile `τ_tumor`.
- **Artefacts.** `gs://<models>/tumor_head/<semver>/` holds `model.joblib`, `scaler.json`, `calibrator.joblib` and `card.json`. The card records the train snapshot ID (SPEC-09), the splits lock hash, class counts, val metrics and the seed. The registry entry is `tumor_head`.
- **Deletions.** `models/probe/probe_v1.*`, the OD/margin fusion, the IDW interpolation and the 80-column grid. The v5 fusion survives only as the heuristic `od_fusion_v5` for one ablation row.

### 4.3 Outputs

- `triage/tiles.parquet`: per tile `(i, j, x_um, y_um, tissue_fraction, p[7], p_tumor_cal, is_tumor)`.
- `triage/heatmap.png` (1 px per tile, `p_tumor_cal` in viridis; alpha = 0 **only** where the tile is outside the tissue mask) and `triage/heatmap.json` (`{tile_um, origin_um, nx, ny, head_version}`).
  - The frontend overlay scales it by `tile_um / slide_mpp`.
- `triage/tumor_mask.png` and `.json` on the same grid (`is_tumor` after optional smoothing). This is consumed by SPEC-06 §5.5 and SPEC-07 §4.

## 5. Hotspot selection

### 5.1 Definitions

- A hotspot is an axis-aligned square window of side `w = profile.hotspots.window_um` (600 µm). It must contain one HPF disk of r = 262 µm (diameter 524 µm).
- A candidate window is centred on a lattice with step `w/4` (150 µm) over the bounding box of the tumour mask. It is valid if:
  - `TissueMask.fraction_in_box ≥ 0.70` (matches the HPF tissue rule in `pipeline/hpf.py`), and
  - the tumour fraction, i.e. the area-weighted share of the window covered by tumour tiles, is `≥ min_tumor_fraction` (0.50).

### 5.2 Ranking (arm chosen by evidence)

| Arm | Score | Cost |
|---|---|---|
| H1 | Mean `p_tumor_cal` in the window | Free |
| H2 | **Mitotic prescan.** Expected count = Σ calibrated Stage-A probabilities (SPEC-06 §5.2) of detections inside the window | Detector sweep over the prescan area |
| H3 | H2, with ties broken by H1 | as H2 |

**Prescan area:**
- **CNB:** the entire tumour mask. It is small, typically only a few mm².
- **Resection:** up to `prescan_max_mm2` (default 40 mm²) of tumour mask, sampled as whole 128 µm tiles.
  - Sampling is stratified: tumour-mask connected components are allocated area-proportionally.
  - The count is then smoothed with a disk kernel of radius `w/2`.

The prescan detections are cached and **reused** by Stage 4 inside the selected hotspots, so there is no double sweep.

**Arm selection criterion:** the slide-level mitotic-score macro-F1 (`F1_M`) on TCGA val, computed with the full SPEC-06 pipeline. Hotspot choice exists to reproduce the pathologist's mitotic score. S3-P@K is a guard: it must be ≥ 0.9 for the selected arm.

### 5.3 Greedy selection with hard non-overlap

```python
def select_hotspots(cands: list[Window], k_max: int, w: float, gap: float) -> list[Window]:
    selected: list[Window] = []
    for c in sorted(cands, key=lambda c: (c.score, c.tumor_fraction), reverse=True):
        if all(max(abs(c.cx - s.cx), abs(c.cy - s.cy)) >= w + gap for s in selected):   # Chebyshev: exact for equal squares
            selected.append(c)
            if len(selected) == k_max:
                break
    return selected
```

- `K < k_max` is allowed and flagged `hotspots_limited_by_tissue`. **No padding**, whether with low-score windows or rejected windows.
- `K = 0` gives the stage result `no_invasive_tumor_detected`, which leads to the existing zero-tumour confirmation gate. In eval mode, the case's grade prediction is `none`, which counts as wrong under SPEC-00 rule 2.
- The rank, score kind, tumour fraction and prescan expected count are persisted. The `hotspots` table gains `rank_score REAL`, `score_kind TEXT`, `tumor_fraction REAL`, `prescan_expected REAL` and `window_um REAL`.

### 5.4 Optional VLM tumour referee (arm)

- The referee runs on the top `2·k_max` windows.
- Input: `read_region_at_mpp(target_mpp=1.0, 512×512 µm, color=normalized)`.
- Output: the strict `TumorVerdict` (SPEC-01 §3.5).
- Rejected windows are **removed**. Selection continues down the ranked list, and nothing is ever padded back.
- It is kept only if the paired-bootstrap ΔF1 (`F1_M`) and ΔS3-P@K on val are both non-negative, and at least one of them has a lower bound > 0.

### 5.5 Pathologist edits (server-validated)

On `save_triage_edits` / `confirm_triage`, the server validates every active hotspot:
- The polygon is valid (`shapely.is_valid`), closed and has ≤ 64 vertices.
- It lies within the slide extent.
- Its area is between 0.1 and 4.0 mm².
- For every pair of active hotspots `(a, b)`: `a.buffer(gap/2, join_style="mitre").intersection(b.buffer(gap/2, join_style="mitre")).area == 0`.

A violation returns `422 {"error": "hotspot_overlap", "ids": [...]}`. The frontend (`TriageViewer.tsx`) shows the conflict and blocks confirmation. Every edit writes a DecisionRecord with `producer_kind='human'` and `supersedes_id` pointing to the model's `hotspot_select` record (SPEC-01, SPEC-09).

## 6. Metrics and acceptance criteria

| # | Metric / check | Target |
|---|---|---|
| AC1 | S3-F1 (invasive tumour, binary), BCSS test and BCNB test, with 95% CI | Reported. Floor to ratify after P2. **Must beat the v5 fusion (`od_fusion_v5`) by paired ΔF1 with lower bound > 0 on val** |
| AC2 | S3-COV on every val slide in TCGA and BCNB | **= 1.0** |
| AC3 | Non-overlap property test (Hypothesis): random tumour masks including thin diagonal bands, random scores, `k_max`, `w`, `gap` | For every selected pair, Chebyshev distance ≥ w + gap. 10,000 examples |
| AC4 | Pathologist overlap edit | API returns 422. Covered by an E2E test |
| AC5 | No padding | Unit test: after rejections, returned K ≤ number of accepted windows. `hotspots.source` never marks a rejected window as selected |
| AC6 | Hotspot arm | H1/H2/H3 results on TCGA val (`F1_M`, S3-P@K) recorded in the run registry. The chosen arm is written to `specimen_profiles.yaml` with run IDs |
| AC7 | Reproducibility | Retraining the head from the same snapshot and seed gives coefficients equal within 1e-10 |
| AC8 | CNB | On 20 BCNB val slides: S3-COV = 1.0 and a visual review screenshot attached to the PR (the Notes screenshot case) |

## 7. Changes to existing code

| File | Change |
|---|---|
| `backend/worker/triage.py` | Rewrite `run_triage`: tile grid → gateway embeddings → head → tumour mask → windows → ranking → selection → persistence. Delete the overview grid, smart scout, fusion, IDW, synthetic crops, padding and per-patch stain normalisation (SPEC-04) |
| `backend/pipeline/hotspots.py` | Replace with `pipeline/hotspots_v6.py` (window lattice, Chebyshev selection, validation helpers). The old module is deleted |
| `backend/pipeline/probe.py` | Replace with `pipeline/tumor_head.py` (load, predict, calibrate), plus `training/tumor_head/train.py` |
| `backend/app/routers/triage.py` | Add edit validation (§5.5). Serve `heatmap.png` and `heatmap.json`. Delete `generate_synthetic_microscopic_patch` (SPEC-01) |
| `frontend/components/viewer/TriageViewer.tsx` | Tile-resolution overlay using `heatmap.json` geometry. Overlap feedback on edit. Show `score_kind` and the prescan expected count in the hotspot tooltip |
| `configs/triage.yaml` | Superseded by `specimen_profiles.yaml` (`triage`, `hotspots`) plus the registry. Deleted |

## 8. Risks

| Risk | Mitigation |
|---|---|
| BCSS covers ROIs from a limited number of TCGA slides, so the head may not generalise to BCNB or other sites | Combine BCSS and BCNB, report per-dataset F1, and add SPEC-09 correction data |
| Full-coverage PF cost and latency on large resections | Embedding cache. Endpoint autoscaling (max replicas in the registry). The slide time budget is tracked in SPEC-02's cost/latency report |
| Prescan adds detector time | Cap `prescan_max_mm2`. Reuse the detections in Stage 4. H1 remains the choice if H2/H3 give no F1 gain |
