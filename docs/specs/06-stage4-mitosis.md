# SPEC-06 — Stage 4 Redesign: Mitotic-Figure Detection (North-Star Stage)

| Field | Value |
|---|---|
| Spec ID | SPEC-06 |
| Category (V4) | Biological (referee definition, gating), Model (detector calibration, trained classifier), Staging (tiling/HPF geometry) |
| Issues covered | N9, N10, A2 (Stage-4 part), A5 (Stage-4 part) |
| Depends on | SPEC-01, SPEC-02 (MIDOG++ adapter, component harness), SPEC-04 (`read_region_at_mpp`), SPEC-05 (hotspots, tumour mask, prescan) |
| Metrics owned | **NS-M** (primary north star), `F1_M`, count MAE (SPEC-00 §2.1–2.2) |

> The Build Notes say this stage decides whether the project lives: "If this stage is giving mediocre performance, the project is as good as dead." Every design choice here has to be backed by an F1 measurement (§6). Model or architecture changes are accepted only with paired-bootstrap evidence.

## 1. Root-cause analysis of the over-count (N10)

The pathologists reported "overblown" mitotic counts. Ten mechanisms in v5 bias the count, and **eight of them push it upward**.

| # | Mechanism | Evidence | Direction |
|---|---|---|---|
| RC1 | **Heuristic substitution on negative tiles.** `detect()` returns KongNet's result only `if primary_results:` (`pipeline/detect.py:265`). An empty list is falsy, so the OD heuristic runs (`:271`) and its candidates are returned (`:279-280`), up to 4 per tile (`:376`). Tiles that KongNet correctly judged mitosis-free become candidate sources. This hurts low-proliferation tumours most, which is exactly where the score should be 1. | code path | ↑↑ |
| RC2 | **Verdict parser turns negations into confirmations.** `"CONFIRM" in vu` is tested first (`pipeline/medgemma.py:212`), so "NOT CONFIRMED" and "CANNOT CONFIRM" become `CONFIRMED`. | SPEC-01 §1.2 | ↑ |
| RC3 | **Partial definition** (pathologist feedback). The prompt (`configs/prompts/mitosis_confirmation@v1.md`) has 4 criteria: envelope, spicules, apoptosis, lymphocytes. It has no explicit rules for prophase, pyknotic or hyperchromatic interphase tumour nuclei, telophase counting, atypical mitoses, crush artefact, or non-neoplastic cells. | prompt text | ↑ |
| RC4 | **Prompt/code mismatch.** The prompt says image 1 is the 512 µm context and image 2 is the focus "marked with a subtle boundary circle". The code sends `[focus, context]` (`medgemma.py:1054-1060`) and draws no circle (`pipeline/verify.py:241-252`). The context JPEG is declared `image/png` (`medgemma.py:922, 977`). | code | ? (noise) |
| RC5 | **Forced confidence.** `CONFIRMED` sets `ver_conf = max(ver_conf, 0.90)` and rejections set it to ≤ 0.10 (`worker/mitosis.py:402, 406`). There is no calibration. | code | ↑ (HPF placement) |
| RC6 | **No resampling** (A5). Tiles are read at level 0 without resampling (`worker/mitosis.py:233-241, 255`). TCGA includes 20× scans, and the heuristic's pixel-area gates (`detect.py:324`) and 80-px NMS (`:371`) assume 0.25 µm/px. | code | ? |
| RC7 | **Sub-patch seams.** 1024-px tiles are split into four **non-overlapping** 512-px sub-patches (`detect.py:139-155`), so objects on the seams are split or missed. Tile overlap is only 64 px (`configs/mitosis.yaml` stride 960). Detections are also JPEG q90 (`detect.py:131, 149`). | code | ↓ (recall) |
| RC8 | **Server-side threshold.** `det_threshold = 0.35` is uncalibrated and applied server-side (`detect.py:135, 153`), so detections below it are never returned and no threshold sweep is possible. | config | ? |
| RC9 | **Heuristic verifier survives referee failures.** `ver_c ≥ 0.70 ∧ det_c ≥ 0.50` sets `label = mitosis` (`worker/mitosis.py:339-341`). If the referee then throws, the exception is swallowed (`:410-411`) and the heuristic label is counted. | code | ↑ |
| RC10 | **Count geometry.** HPF density includes `unreviewed` candidates weighted by `ver_conf` (`pipeline/hpf.py:56-65`), so HPFs are placed where false positives cluster. The HPF relaxation passes allow **overlapping** fields at 393 µm and then 262 µm separation (`hpf.py` passes 2–3). Areas are summed per HPF while mitoses are counted uniquely, which inflates the denominator (this pushes ↓). There is no tumour-cell gate: mitoses in stroma, lymphocytes or DCIS inside a hotspot count. | code | ↑ / ↓ |

**The first deliverable is the attribution study (§6.2).** It removes RC1, RC2, … cumulatively and measures ΔF1 and Δ(mean signed count error) on MIDOG++ breast val and TCGA val. This provides the "strong evidence" the Build Notes ask for before any model or architecture change.

## 2. Goals / non-goals

**Goals**
- Maximise NS-M on held-out breast data with calibrated, resolution-correct, provenance-complete decisions.
- Make the operational definition of a mitotic figure explicit, versioned and pathologist-approved (N10).
- Remove every upward-biasing mechanism in §1.

**Non-goals**
- Atypical-mitosis subtyping (MIDOG 2025 track 2) is out of scope. Atypical figures are counted as mitoses.

## 3. Operational definition (single source)

`configs/definitions/mitotic_figure@v2.md` is the **fixed** program definition. It was decided once by the program owner (2026-09-28) and is used unchanged everywhere, with no per-run or per-evaluation approval step. The file is hashed into `config_hash`. **The same file** is embedded in:
- the VLM prompt (§5.4)
- the ground-truth annotation guideline (SPEC-08 §6)
- the ground-truth harmonisation step for evaluation data (§3.1)

The model, the annotators and the evaluation ground truth are therefore judged against identical criteria. The file only changes through a deliberate version bump (`@v3`), which changes `config_hash` and so invalidates comparisons with earlier runs.

**Definition content** (van Diest-style criteria):

- **Count** a cell as a mitotic figure when **all** of the following hold:
  1. The **nuclear membrane is absent**, i.e. the cell is beyond prophase.
  2. **Condensed chromosomes** are visible as dark, hairy or spiky projections, in one of these arrangements:
     - clotted (early metaphase / prometaphase)
     - a plate or ring (metaphase)
     - two separating groups (anaphase)
     - two separate clots at opposite poles (telophase)
  3. The cell is a **neoplastic** (invasive carcinoma) cell.
- **Atypical mitoses** (multipolar, lagging chromosomes, asymmetric) **are counted**.
- **Do not count:**
  - prophase or any figure with a visible nuclear membrane
  - hyperchromatic interphase nuclei (intact smooth contour)
  - pyknotic nuclei (small, homogeneous, round, smooth, no projections)
  - apoptotic bodies (dense round fragments, eosinophilic cytoplasm, clear halo)
  - lymphocytes and plasma cells
  - crushed or smeared nuclei
  - mitoses in non-neoplastic cells (endothelium, stroma, inflammatory cells, normal epithelium, in-situ component)
- **Dividing-cell counting rule (decided):** **one dividing cell counts as one mitosis.** This includes anaphase and telophase, where the chromosomes have separated into two groups or clots but cytokinesis is not complete. Both groups belong to the same figure and are counted **once**. The rule is encoded in three places:
  - the prompt: "count the two chromosome groups of one dividing cell as a single mitotic figure";
  - the NMS merge (§5.7);
  - the ground-truth harmonisation (§3.1).

### 3.1 Ground-truth harmonisation for evaluation data

Evaluation ground truth must follow the same one-cell-one-mitosis rule. Otherwise NS-M would penalise a correct single detection with a false negative.

- **Check.** At implementation, check how each evaluation dataset annotates anaphase/telophase figures, starting with MIDOG++ (paper and labelling protocol).
- **If a dataset annotates daughter groups separately,** apply a deterministic harmonisation to its ground truth before matching:
  - pairs of mitotic-figure points closer than `d_div` are merged into a single point at their midpoint;
  - `d_div` is set from the dataset's documented convention, or measured from its annotated anaphase/telophase examples;
  - the step is applied identically to every arm.
- **Recording.** The harmonisation and its `d_div` are recorded in `eval/datasets/registry.yaml` and in each run's `metrics.json`. The number of merged pairs per dataset is reported.
- **Program annotations** (SPEC-08 §6) use the rule natively: annotators place one point per dividing cell.

## 4. Detector service contract (repo `MIDOG-microservice`)

The v5 service (`D:\Projects\MIDOG\main.py`) runs `KongNet_Det_MIDOG_1` through TIAToolbox `NucleusDetector` in `patch_mode`. It thresholds server-side and returns fake `48×48` boxes. The v6 contract:

```
GET  /metadata  -> {"model":"KongNet_Det_MIDOG_1","tiatoolbox":"<pinned version>","weights_sha256":"<sha>",
                    "input_mpp":0.25,"patch_px":512,"output":"points","deterministic":true}
POST /predict   {"instances":[{"image_png_b64":"...","mpp":0.25}], "parameters":{"min_prob":0.01}}
            ->  {"predictions":[{"points":[{"x":f,"y":f,"prob":f}], "error":null}], "model_sha256":"<sha>"}
```

- The service **rejects** instances whose `mpp` differs from `input_mpp` by more than 1%. The client-side gateway contract (SPEC-01 §3.4) enforces the same rule.
- **Verify** KongNet's native resolution from the TIAToolbox model `ioconfig`. If it is not 0.25 µm/px, `input_mpp` and the tiling in §5.1 follow the model.
- Images are sent as **lossless PNG** (v5 sent JPEG q90).
- `min_prob` defaults to 0.01, so the client can sweep thresholds offline from cached raw detections.
- Weights are baked into the image, and their SHA-256 is reported in `/metadata` and in each response. The tiatoolbox version is pinned. Torch runs deterministic (`torch.use_deterministic_algorithms(True)`, fixed batch size).
- The `/health` route is unchanged. The fallback to `best.pt` (the 6.5 MB COCO-sized YOLO starter) is deleted.

## 5. Target pipeline

```
hotspots (SPEC-05) ─► sweep tiles @0.25µm/px (ownership) ─► Stage A: KongNet p_A ≥ τ_A
   ─► Stage B: classifier p_B (256px @0.25) ─► [Stage C: VLM tie-break if p_B ∈ band] ─► post-rules
   ─► tumour-cell gate ─► NMS(r_nms) ─► final_decision ─► HPF placement (counted only) ─► per-mm² score
```

### 5.1 Tiling and ownership

- **Sweep region.** `S = ⋃ hotspot windows ⊕ 16 µm`, restricted to tiles with tissue fraction ≥ 0.1.
- **Tiles.** 512 × 512 px at 0.25 µm/px (128 µm), stride 448 px (overlap 64 px = 16 µm, more than 2 × a 7.5 µm figure radius). Pixels come from `read_region_at_mpp(target_mpp=0.25, color=registry.kongnet.input.color)`.
- **Ownership.** Each tile owns `[32, 480)²` px, extended to the tile edge on any side facing the boundary of `S`. A detection is kept only if its centre lies in the tile's owned region. Owned regions partition `S`, so tile overlap cannot duplicate a detection.
- **Prescan reuse.** Stage-A outputs computed during the SPEC-05 prescan are reused. The cache key is `(slide_sha256, tile_origin_um, detector_sha)`.

### 5.2 Stage A: candidate generation (KongNet)

- **Output.** Points with `p_A`, stored raw at `min_prob = 0.01` in `mitosis/stage_a.parquet`.
- **Calibration.** `p_A` is calibrated with isotonic regression on MIDOG++ breast **val**. A detection is labelled positive when it matches a ground-truth mitotic figure within 7.5 µm (1:1).
- **`τ_A`.** The smallest threshold such that Stage-A recall on val is ≥ 0.95. If recall 0.95 is unreachable, `τ_A = 0.01` and the recall ceiling is reported.
- **Arm A1** (KongNet only) instead uses `τ_A* = argmax F1(val)`.

### 5.3 Stage B: trained mitotic-figure classifier

**Input.** A 64 × 64 µm crop at 0.25 µm/px (256 × 256 px), centred on the candidate. Colour is raw; stain robustness comes from augmentation.

**Training data** (MIDOG++ train cases only; test and val cases are never used):

| Class | Source | Notes |
|---|---|---|
| positive | MIDOG++ mitotic-figure labels (all human domains; breast plus others) | Centre jitter ±2 µm |
| hard negative | MIDOG++ **imposter** ("non-mitotic figure") labels | The key signal against mimics |
| mined negative | Stage-A detections with `p_A ≥ τ_A` that match no ground truth within 7.5 µm | Produced by running Stage A on train ROIs |
| easy negative | Random tumour-region crops | Capped at 10% of negatives |
| optional | Canine MIDOG++ domains | Ablation only (`B-CNN+canine`) |
| future | SPEC-09 correction examples (`review_label` on non-test cases) | Added through the promotion gate |

**Architectures (arms):**
- **B-CNN.**
  - Model: EfficientNet-B0 (timm, ImageNet init), fine-tuned.
  - Augmentation:
    - rot90 / flips
    - HED stain jitter (σ = 0.05 per channel)
    - brightness / contrast ±10%
    - Gaussian blur σ ∈ [0, 1] px
    - JPEG q ∈ [70, 100]
    - scale ±10% (0.225–0.275 µm/px)
  - Loss: focal (γ = 2, α tuned) or BCE with pos_weight ≤ 5.
  - Optimisation: AdamW, lr 3e-4 cosine, ≤ 30 epochs.
  - Selection: early stopping on the **end-to-end val NS-M** (not on crop accuracy).
- **B-FM.**
  - Frozen Path Foundation embedding of a 112 µm crop at 0.5 µm/px (224 px), then a logistic or MLP head.
  - **Verify** that PF's accepted magnifications include 0.5 µm/px.

**Calibration and threshold:**
- Temperature scaling on val.
- `τ_B = argmax_τ NS-M(val)`, with `τ_A` fixed.

**Serving:**
- ONNX export (`opset ≥ 17`), run in the worker with `onnxruntime` (CPU) as registry provider `local_onnx`, entry `mitosis_classifier`.
- The artefact SHA-256 goes in the registry.
- Expected cost: a few hundred crops per slide at tens of milliseconds each on CPU.

**Artefacts:**
- `training/mitosis_classifier/` holds the config YAML, the train script and the export script.
- Model card fields: the data snapshot and splits lock, per-scanner val F1, calibration curve, and seed.

### 5.4 Stage C: VLM tie-breaker (optional, arms A3 and A5)

- **When it runs.** Only for candidates with `p_B ∈ [τ_lo, τ_hi]`. The band is tuned on val to maximise NS-M. The starting band is `[τ_B − 0.15, τ_B + 0.15]`.
- **Inputs**, in this order and captioned in the prompt:
  - **Image 1 "FOCUS":** 64 × 64 µm at 0.25 µm/px (256 px). A 1-px ring of radius 10 µm in `#00FF00` (a colour absent from H&E) is centred on the candidate.
  - **Image 2 "CONTEXT":** 256 × 256 µm at 1.0 µm/px (256 px), with a 16 µm `#00FF00` square marking the candidate.
  - Both are PNG, `color = normalized` (SPEC-04 §3.5).
- **Prompt.** `configs/prompts/mitosis_referee@v2.md` embeds `mitotic_figure@v2.md` verbatim. Variables are typed (SPEC-03 §5.2) and there are no free-text inputs.
- **Output** (strict, SPEC-01 §3.5):
  ```
  MitosisVerdict{ verdict: MITOTIC_FIGURE|NOT_MITOTIC_FIGURE|EQUIVOCAL,
                  criteria: {membrane_absent: bool, condensed_chromosome_projections: bool,
                             phase: prometaphase|metaphase|anaphase|telophase|atypical|none,
                             neoplastic_cell: bool},
                  mimic: none|apoptotic_body|pyknotic_nucleus|hyperchromatic_interphase|lymphocyte|prophase|crush|other,
                  rationale: str }
  ```
- **Deterministic post-rule** (programmable logic against hallucination). `MITOTIC_FIGURE` is accepted only if `membrane_absent ∧ condensed_chromosome_projections ∧ neoplastic_cell ∧ mimic == none ∧ phase ≠ none`. Otherwise it becomes `EQUIVOCAL`, and the DecisionRecord gets `params.rule_override = true`.
- **Arm A2** uses the v5 prompt with the strict schema and no post-rule, as the baseline for the definition change.

### 5.5 Tumour-cell gate

A candidate is eligible for counting only if:
- its tile in the SPEC-05 tumour mask, dilated by 1 tile (224 µm), has `is_tumor`, **and**
- `argmax(p[7]) ≠ in_situ`, because in-situ mitoses are excluded from Nottingham counting.

The gate is **on** by default and **ablated** on TCGA val (`F1_M`). On MIDOG++ ROIs the gate is disabled, because the ROIs are tumour by construction.

### 5.6 Decision, labels and persistence

The `detections` table migration replaces the overloaded v5 `label` / `label_source` / `medgemma_*` columns:

| Column | Type | Meaning |
|---|---|---|
| `p_a` | real | Calibrated Stage-A probability |
| `p_b` | real | Calibrated Stage-B probability (null if the arm has no B) |
| `vlm_verdict` | text | Stage-C verdict (null if not invoked) |
| `rule_override` | bool | Post-rule downgraded the VLM |
| `in_tumor` | bool | Gate result |
| `final_decision` | text | `mitosis` \| `not_mitosis` \| `equivocal` |
| `decision_path` | text | `A` \| `AB` \| `ABC` |
| `review_label` | text null | Pathologist: `mitosis` \| `not_mitosis` (SPEC-09 source) |
| `counted` | bool (generated) | `COALESCE(review_label = 'mitosis', final_decision = 'mitosis' AND in_tumor)` |

- `equivocal` is **never counted**. `n_equivocal` is reported alongside the score, and the UI routes these candidates to review first.
- Pathologist-added figures are rows with `decision_path = 'human'` and `review_label = 'mitosis'`.

### 5.7 NMS

After decisions, a global greedy NMS with radius `r_nms` runs in µm, ordered by `p_b` (or `p_a`).
- `r_nms ∈ {5, 7.5, 10, 12.5} µm` is tuned on val for NS-M.
- It must implement the dividing-cell rule (§3): the two chromosome groups of one anaphase/telophase cell must merge into one detection.
  - The candidate set for `r_nms` is therefore restricted to values ≥ the typical daughter-group separation. That separation is measured on val from harmonised ground-truth pairs (§3.1).
  - A dedicated test (`test_nms.py::test_dividing_cell_counts_once`) asserts that two detections on one telophase figure yield one count.

### 5.8 HPFs and score

- **Density map.** Built from `counted` detections only. `unreviewed` / `equivocal` weighting is removed (`pipeline/hpf.py:56-65`).
- **HPF centres.** Must satisfy:
  - HPF disk ⊂ a hotspot window
  - tissue coverage ≥ 0.70
  - tumour fraction ≥ 0.50
- **Separation.** Fields never overlap (separation ≥ 2r = 524 µm). The relaxed passes at 393 µm and 262 µm are deleted. If fewer than 10 fields fit:
  - `n_hpf < 10`
  - area = `n_hpf · π r²`
  - flag `hpf_count_lt_10`
- **Score.** The existing per-mm² thresholds (`score2_min 3.65`, `score3_min 7.30`) in `pipeline/scoring.py` are the **single** implementation. SPEC-11 removes the duplicates in `pipeline/grading.py:210-261`, `worker/grading.py` and `frontend/components/viewer/MitosisViewer.tsx`.
- **Recompute.** Scoring recompute after review is server-side only (SPEC-03 §5.3).

## 6. Evidence protocol

### 6.1 Data

| Purpose | Data | Unit |
|---|---|---|
| NS-M development | MIDOG++ breast val (case-level split; component harness) | object |
| NS-M domain robustness | MIDOG++ breast LOSO (train on 2 scanners, evaluate on the 3rd) | object |
| `F1_M` development | TCGA val (full pipeline) | slide |
| NS-M final | MIDOG++ breast test **after the contamination check** (SPEC-00 R1). Also the TUPAC16 auxiliary set if obtained, and `tcga_gt_mitosis` if created | object |
| `F1_M` final | TCGA test (locked) | slide |

### 6.2 Attribution study (first deliverable; v5 → v6)

Start from **A0 = v5 as-is**, replayed in the component harness with the v5 tiling, JPEG input, `τ = 0.35`, heuristic substitution, heuristic verifier, v1 prompt and v5 parser. Apply the fixes cumulatively in the order below. For each row, report NS-M, P, R, mean signed count error per 2 mm², `F1_M` (TCGA val), and paired ΔF1 vs the previous row.

| Step | Change |
|---|---|
| A0 | v5 as-is |
| +RC1 | No heuristic substitution |
| +RC2 | Strict verdict parser |
| +RC9 | No heuristic verifier labels |
| +RC6/RC7 | 0.25 µm/px resampling, 512/448 tiling with ownership, PNG |
| +RC8 | Calibrated `τ_A` (becomes A1) |

The table is committed as `reports/attribution/mitosis_v5_to_v6.md`, with run IDs.

### 6.3 Arms (selection on val, confirmation on test)

| Arm | Pipeline | Model calls / candidate |
|---|---|---|
| A1 | KongNet at `τ_A*` | 0 |
| A2 | A1 tiling, `τ_A` (recall) + Gemini, v5 prompt, strict schema | 1 VLM |
| A3 | Same + Gemini with v2 definition + post-rule | 1 VLM |
| A4 | `τ_A` (recall) + classifier B (best of B-CNN / B-FM) at `τ_B` | 1 ONNX |
| A5 | A4 + Stage C in band | ONNX + VLM (band only) |

Each arm is run with the tumour-cell gate on and off for `F1_M`.

**Selection rule:**
1. Choose the arm with the highest val NS-M.
2. If its paired-bootstrap ΔF1 against a cheaper arm has a CI that includes 0, choose the cheaper arm.
3. `F1_M` (TCGA val) must not regress (lower bound of ΔF1 > −0.03).
4. Run exactly **one** locked-test evaluation of the chosen arm, plus A1 as a reference.

## 7. Metrics and acceptance criteria

| # | Criterion |
|---|---|
| AC1 | The attribution table (§6.2) exists. RC1 and RC2 removal each show precision gain with a paired CI lower bound > 0, **or** the table shows they were immaterial |
| AC2 | The chosen arm's NS-M on uncontaminated breast test is reported with CI. Proposed floor ≥ 0.70 (SPEC-00 §2.5) |
| AC3 | Over-count resolved: the mean signed count error per 2 mm² on MIDOG++ breast test has a 95% CI containing 0, or with upper bound ≤ +0.5 |
| AC4 | `F1_M` on TCGA val improves over A0 (paired ΔF1 lower bound > 0) |
| AC5 | mpp contract: every detector request carries `mpp = 0.25 ± 1%`. The service rejects 0.5 (integration test) |
| AC6 | Ownership: a synthetic field with a mocked detector that fires on every object in every overlapping tile outputs each object exactly once |
| AC7 | One fixed definition: `mitotic_figure@v2.md` is the only definition file referenced by the prompt, the annotation guideline and the ground-truth harmonisation (static check). Its hash appears in every run's `config_hash`. The dividing-cell test in §5.7 passes |
| AC8 | Every counted detection has the full DecisionRecord chain (`mitosis_detect` → `mitosis_classify` → [`mitosis_referee`] → `mitosis_count`). INT-PROV = 1.0 |
| AC9 | No `heuristic` producer appears in any EVAL DecisionRecord of the chosen arm (INT-FALL = 0) |
| AC10 | HPFs never overlap (property test). Score recompute uses `pipeline/scoring.py` only (grep test) |

## 8. Ground-truth annotation protocol for TCGA (optional; pathologist capacity TBD)

Used only if capacity exists. It adds a TCGA-native NS-M slice, and produces `tcga_gt_mitosis`.

- **Sample.** 60 TCGA slides from val and test, stratified by report mitotic score (20 per class).
- **Regions.** Chosen **independently of the model**, so there is no selection bias:
  - the annotator's own hotspot, with 10 HPFs placed by the annotator,
  - plus 2 random HPFs inside the tumour mask.
- **Annotators.** Two annotators, blinded to model output, using the SPEC-08 annotation mode and the §3 definition. They place points with class `MF` or `imposter`.
- **Adjudication.** Consensus review of disagreements, or a third annotator.
- **Reporting.** Inter-annotator F1, computed with the same 7.5 µm matching. This is the **human ceiling** reported next to NS-M.

## 9. Changes to existing code

| File | Change |
|---|---|
| `backend/pipeline/detect.py` | Replace with `pipeline/mitosis/{tiling.py, detector_client.py, nms.py}`. Move `_detect_hyperchromatic_features` to `pipeline/heuristics/od_sweep.py` (A0 only) |
| `backend/pipeline/verify.py` | Delete `HoVerNetMitosisVerifier` (a heuristic, not HoVer-Net) or move it to `heuristics/morph_verifier.py` (A0 only). Replace `create_dual_magnification_composite` with `pipeline/mitosis/referee_inputs.py` (§5.4 geometry and markers) |
| `backend/pipeline/mitosis/classifier.py` | New ONNX inference |
| `training/mitosis_classifier/` | New training code |
| `backend/pipeline/mitosis/referee.py` | v2 prompt, strict schema, post-rule |
| `backend/worker/mitosis.py` | Rewrite per §5. Delete the verifier gating (`:333-347`), forced confidences (`:402, 406`), swallowed referee errors (`:410-411`) and the triage-artefact hotspot fallback (`:122-156`). Hotspots come from the DB only, and a missing confirmed triage is an error |
| `backend/pipeline/hpf.py` | Density from `counted` only. Delete the overlapping relaxation passes |
| `configs/mitosis.yaml` | v2 schema: `tile_px`, `stride_px`, `own_margin_px`, `tau_a`, `tau_b`, `band`, `r_nms_um`, `tumor_gate`. Remove `weights_path`, `verifier`, `mock` |
| `configs/prompts/mitosis_referee@v2.md`, `configs/definitions/mitotic_figure@v2.md` | New |
| DB | `detections` migration (§5.6) |
| `frontend/components/viewer/MitosisViewer.tsx`, `MitosisGallery.tsx` | Show the decision chain (`p_a`, `p_b`, VLM verdict and criteria). Equivocal queue first. `counted` badge. Remove the client-side score computation |
| `D:\Projects\MIDOG` (separate repo) | §4 contract changes |

## 10. Risks

| Risk | Mitigation |
|---|---|
| KongNet training overlaps MIDOG++ breast (SPEC-00 R1) | Contamination check before test. Alternative test sets |
| MIDOG++ breast val is small (about 30 cases) | Use LOSO folds, report CIs, and treat results as provisional per the SPEC-00 §2.4 rule |
| TCGA scanners and staining differ from MIDOG++ | `F1_M` on TCGA val is part of selection. Stain augmentation. SPEC-09 corrections |
| An evaluation dataset annotates dividing cells as two figures | Ground-truth harmonisation (§3.1), applied identically to all arms and reported |
| Classifier B overfits imposters from non-breast domains | Per-domain ablation (`B-CNN+canine`). Breast-only val selection |
