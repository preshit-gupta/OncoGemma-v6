# SPEC-07 — Stage 5 Redesign: Nottingham Grading (Tubule Formation, Nuclear Pleomorphism, Histologic Type)

| Field | Value |
|---|---|
| Spec ID | SPEC-07 |
| Category (V4) | Biological (sampling, magnification, VLM use), Model (trained heads, calibration) |
| Issues covered | N11, A3, A4 (Stage-5 part), A5 (pleomorphism resolution), A6 |
| Depends on | SPEC-01, SPEC-02 (TCGA labels, splits), SPEC-04, SPEC-05 (tumour mask), SPEC-06 (mitotic score) |
| Metrics owned | NS-G, `F1_T`, `F1_P`, `F1_high`, `macroF1_LM`, `sum_MAE`, S5-HT (SPEC-00 §2) |

## 1. Why v5 grades high-grade tumours well but low/medium poorly (N11)

Every component estimator in v5 has an **upward** bias. The Nottingham sum is capped at 9, so upward bias barely hurts true grade-3 cases (sum 8–9). The same bias pushes true sum-3–6 cases across the 5→6 and 7→8 boundaries. The result matches the pathologist feedback: extremes look right, while low and medium grades are unstable.

| # | Bias mechanism | Evidence | Component |
|---|---|---|---|
| B1 | Mitotic over-count | SPEC-06 §1, RC1–RC10 | M ↑ |
| B2 | **Patches come from the densest tissue inside the hotspots**, which the v5 triage already chose for darkness and cellularity (SPEC-05 §1.5). `select_max_density_hotspot_patches` (`worker/grading.py:87`) ranks candidates by a `uniform_filter` of the *tissue* mask (not tumour, not architecture). It takes each hotspot's peak and then fills by density ≥ 0.5. Solid sheets are over-sampled, tubular areas and the tumour periphery are under-sampled, and the whole-tumour denominator is ignored. | code | T ↑ (less tubule % → score 3) |
| B3 | **Tubule estimator.** The "doer" takes lumen pixels (`r>200 ∧ g>190 ∧ b>200`, components of 150–12,000 px) divided by `1.5 ×` nuclear pixels, capped at 80% (`pipeline/medgemma.py:452-472`). That is not "% of tumour area forming tubules". The Gemini verifier receives the doer's number as an anchor (`:737-750`). An unparsable percentage becomes **0%**, i.e. score 3 (`:50-61`). Aggregation is a confidence-weighted median over patches (`pipeline/grading.py:316-326`). | code | T ↑ |
| B4 | **Pleomorphism at 1.0 µm/px.** Patches are 512 µm at 512 px (`configs/scoring.yaml:27`), so a nucleus is 8–12 px and nucleoli and chromatin texture cannot be resolved. Nuclear-size CV comes from an RGB threshold mask (`medgemma.py:423-450`) that merges touching nuclei, which inflates CV and pushes towards score 3. The weighted mode resolves ties to the **max** (`pipeline/grading.py:341`). An unparsable score becomes 2. | code | P ↑ |
| B5 | **Silent defaults.** Tubule 20% (score 2), pleomorphism 2, histologic type IDC-NST (`worker/grading.py:495-528`, the `base64` NameError). | SPEC-01 | T, P, type |
| B6 | **The MedGemma doer never sees an image** (`medgemma.py:725, 809` pass `[]`). Its prompt's example JSON contains the heuristic's answer (`:719, :803`), so the "doer" echoes the heuristic. | code | T, P |

**First deliverable: the attribution study** (§8.2). It measures the **mean signed error** of each component and of the sum on TCGA val, stratified by true band (3–5 / 6–7 / 8–9). It then removes B2–B6 cumulatively. This either confirms or refutes the upward-bias hypothesis with numbers before any estimator is chosen.

## 2. Goals / non-goals

**Goals**
- Estimate each Nottingham component the way the definition prescribes:
  - **tubules:** a whole-tumour, low-power area fraction
  - **pleomorphism:** high-power nuclear features
  - **mitoses:** SPEC-06
- Choose estimators by F1, not by assumption.
- Remove every biasing default.
- Meet the pathologists' two requirements through explicit band metrics:
  - extremes (`F1_high`)
  - low/medium consistency (`macroF1_LM`)

**Non-goals**
- Changing Nottingham cut-offs in the clinical definition. Estimator calibration (§5.4, arm T4) is allowed and documented, but the reported component is still judged against the standard definition via ground truth.

## 3. Ground truth for this stage

| Label | Source | Expected coverage |
|---|---|---|
| Grade (1–3) | TCGA reports (SPEC-02 §4); BCNB clinical field (mapping supplied by the program owner; SPEC-02 §3.2) | High (≈500 TCGA; BCNB most) |
| Components T, P, M, total | TCGA reports, when stated | **Partial.** Coverage is measured in SPEC-02 §4. Component arms train and evaluate only where the label exists |
| Histologic type | Thennavan et al. 2021 TCGA expert review | ≈1,058 samples |
| Field-level atypia (auxiliary) | MITOS-ATYPIA-14 nuclear atypia scores (breast, ×20/×40 frames) | Optional and not blocking (SPEC-00 §3.1) |

## 4. Sampling frame

Every Stage-5 sample is drawn from the **invasive tumour mask** (SPEC-05 §4.3), not from the hotspots.

**Stratified spatial sampling** (`pipeline/grading/sampling.py`):
1. Take the tumour tiles with centres `x_t` and weights = tumour area.
2. Run weighted k-means with `k = n_samples` (the profile's `tubule_patches` / `pleo_fields`).
3. Take one sample per cluster, centred on the tumour tile nearest the cluster centroid.
4. Enforce a minimum separation of one sample size by greedy rejection, re-drawing from the same cluster.

The seed is the slide SHA-256 (as in v5). Every sample stores:
- its tumour area `a_i`, which is the exact intersection of the tumour mask with the sample box;
- its `stratum` (the cluster).

## 5. Tubule formation

**Definition (reference):** the percentage of the **invasive tumour area** that forms definite tubules or glands, meaning a clear lumen surrounded by polarised tumour cells.

| Tubule % | Score |
|---|---|
| > 75% | 1 |
| 10–75% | 2 |
| < 10% | 3 |

### 5.1 Samples

- `tubule_patches` samples (48 for resection, 24 for CNB).
- Each sample is 512 × 512 µm at **1.0 µm/px**. That is low power, which is correct for architecture.
- `color = normalized`.

### 5.2 Slide aggregation (all arms)

```
T% = Σ_i a_i · t_i  /  Σ_i a_i        over samples with tumor_present = true (area-weighted mean)
```

The area-weighted mean estimates "% of tumour area". It replaces the median over confidence weights.

### 5.3 Estimator arms

| Arm | Estimator | Notes |
|---|---|---|
| T0 | v5 (heuristic + anchored Gemini, density sampling) | Attribution baseline only |
| T1 | **VLM per sample** through the gateway (`task=tubule_patch`). Gemini receives the image **with no numeric anchor**, prompt `tubule@v2.md` embeds the definition. Output: strict `TubuleEstimate{tumor_present, tubule_percent}` | Image mandatory |
| T1-MG | Same as T1 with MedGemma 1.5 4B as the estimator. The image goes in an OpenAI-compatible chat-completions payload with image content parts. **Verify** the deployed container's request format (Vertex Model Garden vLLM) | Replaces the v5 "doer". Kept only if it beats T1 or improves T1 when ensembled |
| T3 | **Attention-MIL** (ABMIL, gated attention) over PF embeddings of tumour tiles (224 µm @ 1.0 µm/px, cached from SPEC-05). Ordinal 3-class output with CORAL loss. Trained on TCGA-train tubule labels, 5-fold patient CV. Output is the score directly | Needs component labels |
| T4 | T1 % with **calibrated cut-points**: `c1 < c2` fitted on val to maximise `F1_T`, replacing 10/75 | Corrects a systematic estimator bias. Documented as calibration |

### 5.4 Doer → verifier chain

The v5 MedGemma-doer → Gemini-verifier chain is **not** retained by default. An arm `T1-chain` (MedGemma estimate shown to Gemini) is evaluated. It is kept only if its paired ΔF1 against T1 has lower bound > 0. Anchoring a verifier on another model's number is a documented bias risk.

## 6. Nuclear pleomorphism

**Definition (reference):** variation in size and shape of tumour nuclei compared with normal epithelial nuclei, together with chromatin pattern and nucleoli.

| Score | Description |
|---|---|
| 1 | Small, regular, uniform nuclei |
| 2 | Moderate increase in size and variation, visible nucleoli |
| 3 | Marked variation, vesicular chromatin, prominent (often multiple) nucleoli |

### 6.1 Samples

- `pleo_fields` samples (48 for resection, 32 for CNB).
- Each field is 128 × 128 µm at **0.25 µm/px** (512 px).
- Colour is normalised for the VLM and raw for segmentation (per the SPEC-04 colour policy).

### 6.2 Nuclear instance segmentation

A pluggable `NucleiSegmenter` interface, `segment(field) -> instances[{polygon, centroid, type?}]`, has two registered implementations. The licence reasoning is in SPEC-00 §3.1.

| Segmenter | Licence scope | Cell types | Role |
|---|---|---|---|
| **StarDist `2D_versatile_he`** | `commercial_ok` (weights stated as CC BY 4.0; confirm with counsel) | None | **Default.** Neoplastic nuclei are selected as nuclei inside tumour tiles (SPEC-05 mask, `argmax = invasive_tumor`), excluding small round nuclei (area < 30 µm² and form factor > 0.85, i.e. lymphocyte-like) |
| **HoVer-Net PanNuke** (TIAToolbox `hovernet_fast-pannuke`) | `research` (CC BY-NC-SA 4.0) | Neoplastic, inflammatory, connective, dead, non-neoplastic epithelial | Research-only comparison arm (P2-HV). If it beats the default by paired ΔF1 with lower bound > 0, program options are listed in SPEC-00 §3.1 (c)/(d) |

- **Resolution.** Both run at their native resolution. **Verify** each model's expected µm/px and feed it via `read_region_at_mpp`.
- **Serving.** Add to the GPU microservice (`/segment_nuclei?model=`) with the same contract rules as SPEC-06 §4: mpp-checked, lossless PNG, weights SHA-256.

### 6.3 Features

Features are computed over neoplastic nuclei, as selected by the segmenter in use. If fewer than 200 nuclei are found across the fields, the case is flagged `insufficient_nuclei`. The relative-size feature (normal vs neoplastic epithelium) is available only with a typed segmenter. With StarDist it is imputed as missing.

- **Size:** area in µm² (mean, CV, p10, p50, p90, p90/p50).
- **Shape:** perimeter, eccentricity, solidity, form factor `4πA/P²`.
- **Chromatin:** hematoxylin OD from the slide `StainProfile` deconvolution (mean, SD, entropy) and GLCM contrast / homogeneity on the H channel inside the nucleus.
- **Nucleolar proxy:** the count of intranuclear LoG blobs at σ ≈ 0.5–1 µm per nucleus.
- **Relative size:** `p50_area(neoplastic) / p50_area(non-neoplastic epithelial)` when there are at least 30 normal nuclei. Otherwise it is missing, and imputed with a missing-indicator.

Each feature is aggregated per slide as a median across fields plus a p90 across fields, which captures the "worst area".

### 6.4 Estimator arms

| Arm | Estimator |
|---|---|
| P0 | v5 (1 µm/px, heuristic + anchored Gemini) — attribution baseline |
| P1 | VLM per field at 0.25 µm/px (`pleo@v2.md` with the definition), strict `PleoEstimate`. Slide aggregation is `mode` or `p75(field scores)`, chosen on val |
| P2 | **Morphometric ordinal model.** Ordinal logistic regression (`statsmodels` `OrderedModel`, logit) on standardised §6.3 features. L2 via a feature-selection path, trained on TCGA-train pleomorphism labels with 5-fold patient CV |
| P3 | Stacked: P2 features + P1 per-field score histogram → ordinal model |
| P4 | ABMIL over PF embeddings of 112 µm tiles at 0.5 µm/px inside the tumour mask, with CORAL loss |

MITOS-ATYPIA-14, if obtainable, is used as an **auxiliary field-level validation** of P1 and P2 (Spearman ρ, field-level macro-F1). It does not count toward NS-G.

## 7. Aggregation, grade and histologic type

### 7.1 Nottingham sum and grade

- The formula is unchanged and deterministic: `sum = T + P + M`, with bands 3–5 → G1, 6–7 → G2, 8–9 → G3 (`pipeline/grading.py:142-174`).
- **Confidence weighting is removed**, because VLM confidences are uncalibrated (SPEC-01 §3.5).
- A grade is emitted only when all three components exist. Otherwise:
  - `needs_human = true`
  - the eval prediction is `none` (counted as wrong, per SPEC-00 rule 2).
- The histologic-type column has no default (SPEC-01).

### 7.2 Direct-grade comparator (reported, not product)

- An ABMIL model over PF tumour-tile embeddings, trained on TCGA-train grades, similar to published foundation-model MIL grade predictors.
- Its NS-G is reported next to the component-sum pipeline, as a ceiling and sanity check.
- If it beats the component pipeline by paired ΔF1 with lower bound > 0, the program owner decides whether to use it as a second opinion. Product use requires a separate decision, because it is not decomposable into components.

### 7.3 Histologic type

- Classes: IDC-NST, ILC, mixed ductal-lobular, other special type. The fine-grained special types collapse into `other` for the metric.
- **Arm H1:** Gemini multi-sample (8 stratified 512 µm samples at 1.0 µm/px), strict `HistotypeVerdict`, no default.
- **Arm H2:** ABMIL over tumour-tile embeddings.
- Selection is by S5-HT (macro-F1) on TCGA val. ILC F1 is reported separately because ILC is the clinically important minority class.

## 8. Evidence protocol

### 8.1 Data

- **Development:** TCGA val for components, grade and type; BCNB val for grade only.
- **Final:** TCGA test and BCNB test, locked, one evaluation.

### 8.2 Attribution study (first deliverable)

Start from G0 = v5 Stage 5 fed with v6 Stage-4 counts, then apply fixes cumulatively:
1. strict schemas and no defaults
2. no numeric anchoring
3. tumour-mask stratified sampling
4. area-weighted tubule mean
5. pleomorphism at 0.25 µm/px

For each step, report per-component mean signed error (by true band), `F1_T`, `F1_P`, NS-G, `F1_high`, `macroF1_LM`, `sum_MAE`, and paired ΔF1.

### 8.3 Arm selection

1. Select tubule and pleomorphism estimators independently by val `F1_T` / `F1_P`.
2. Among combinations within the CI of the best, pick by `macroF1_LM`, subject to the constraint `F1_high(val) ≥ F1_high(G0)`, i.e. extremes must not get worse.
3. Prefer cheaper arms when the CIs overlap.

## 9. Metrics and acceptance criteria

| # | Criterion |
|---|---|
| AC1 | The attribution table exists with per-band signed errors. B2–B6 are each quantified |
| AC2 | **Bias removed:** mean signed Nottingham-sum error on TCGA val cases with true sum 3–6 has a 95% CI containing 0 or bounded within ±0.3 |
| AC3 | `macroF1_LM` improves over G0 (paired ΔF1 lower bound > 0). `F1_high` is not lower than G0 (lower bound of Δ > −0.03) |
| AC4 | NS-G on TCGA test and BCNB test is reported with CI, coverage and QWK, alongside the direct-grade comparator (§7.2) |
| AC5 | No defaults: a fuzz test on the stage outputs finds that no component is ever set without a DecisionRecord of status `ok` |
| AC6 | Resolution: every `pleo_field` DecisionRecord has `input_spec.mpp = 0.25`, and every `tubule_patch` record has `1.0` |
| AC7 | Sampling: 100% of samples lie inside the tumour mask (tumour fraction ≥ 0.5). Stratum coverage equals `n_samples` for tumours of ≥ n tiles |
| AC8 | Histologic type: S5-HT and ILC F1 are reported. There is no `IDC-NST` value without a DecisionRecord |

## 10. Changes to existing code

| File | Change |
|---|---|
| `backend/worker/grading.py` | Rewrite: tumour-mask stratified sampling (§4), gateway calls per arm, and no fallback blocks (`:490-528`). Hotspots are no longer the sampling frame. Delete `select_max_density_hotspot_patches` and `extract_10x_patch` (replaced by `read_region_at_mpp`) |
| `backend/pipeline/grading.py` | `aggregate_grading_findings`: area-weighted tubule mean, configurable pleomorphism aggregation, no confidence weights, and component presence required. Delete the duplicated mitotic-score helpers (`:210-261`) |
| `backend/pipeline/medgemma.py` | Split into `pipeline/vlm/{tubule.py, pleo.py, histotype.py}`, all built on the gateway. Delete `extract_morphometric_doer_assessment` (`:395-476`), the doer prompts (`:708-721`, `:793-805`) and the Stage-6 narratives |
| `backend/pipeline/nuclei/` | New: HoVer-Net client and feature extraction |
| `training/{tubule_mil, pleo_ordinal, grade_mil, histotype_mil}/` | New training code. Artefacts go to the registry |
| `configs/prompts/{tubule,pleo,histologic_type}@v2.md`, `configs/definitions/{tubule_formation,nuclear_pleomorphism}@v1.md` | New definitions, fixed once and used everywhere (same policy as SPEC-06 §3; changes only via a version bump) |
| `configs/scoring.yaml` | Remove `confidence_weights`, `n_patches`, `resolution_um` (moved to profiles / the registry). Keep the Nottingham thresholds |
| `frontend/components/viewer/GradingReviewWorkspace.tsx` | Show per-sample evidence at the right magnification (10× tubule, 40× pleomorphism), per-component estimator and provenance, and the band warning when the sum is 5/6 or 7/8 (near a boundary) |

## 11. Risks

| Risk | Mitigation |
|---|---|
| Component labels are sparse in TCGA reports | Measure coverage early (SPEC-02). MIL arms need them, while T1/P1 need none for inference and are only *selected* on labelled val. Report n per component |
| Class imbalance (G1 minority) | Stratified splits, ordinal losses, and macro-F1 as the metric (not accuracy) |
| MIL overfitting on about 300 train slides | Frozen features, 5-fold ensembles, strong regularisation. CIs decide |
| Segmenter compute and licence | The GPU service is shared with KongNet. The default segmenter is licence-clean. HoVer-Net is research-only (§6.2, SPEC-00 §3.1) |
| Report-derived grade differs from the "true" grade (inter-observer variability) | Reported as a limitation. `label_confidence=high` subset analysis (SPEC-00 R3) |
