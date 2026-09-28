# SPEC-00 — OncoGemma v6 Program: Metric Framework, Datasets, Issue Register, Sequencing

| Field | Value |
|---|---|
| Spec ID | SPEC-00 |
| Status | Draft for approval |
| Scope | Whole v6 program |
| Baseline | `v5.0.0-baseline` (= v5 `main` @ `2b87ab7`) |
| North-star metric | **F1** (definitions in §2 are normative for every other spec) |

## 1. Purpose and program rules

v6 turns OncoGemma from a demonstrable copilot into a **measured** one. The program has three phases:

| Phase | Name | Exit condition |
|---|---|---|
| **P1** | Fix | Every issue in §6 marked P0/P1 is closed per its spec's acceptance criteria. No locked-test evaluation happens in P1. |
| **P2** | Measure | One locked evaluation on the test splits (§4) for every north-star metric, reported with 95% CIs. |
| **P3** | Improve | Iterate using the issue register (§7) and the self-improvement loop (SPEC-09). Every change is judged on validation F1 and promoted via the gate in §3.4. |

Rules that apply to every spec:

1. **Development vs validation.** Train/val splits may be used freely during P1 for training, threshold selection and ablations. The **test split is locked** (SPEC-02 §5). It is evaluated once per released configuration, and every access is audit-logged.
2. **Intention-to-diagnose.** A case whose pipeline fails or returns no prediction is **never dropped** from a metric's denominator. It counts as a wrong prediction for its true class. Coverage (fraction of cases with a prediction) is reported alongside every metric.
3. **Provenance before numbers.** A metric is only valid if 100% of the decisions behind it have a `DecisionRecord` (SPEC-01) naming the component that actually produced them, and if the run had zero silent fallbacks.
4. **Single source of metric code.** All metrics are computed by `eval/metrics.py` (SPEC-02 §7). The CLI, the Research view (SPEC-08) and the promotion gate (SPEC-09) all import it; nothing re-implements a metric.
5. **No unsupported claims.** Accuracy statements in README, docs or UI must link to a generated evaluation report (a run ID plus `metrics.json`).

## 2. Metric definitions (normative)

### 2.1 NS-M — Mitotic-figure detection F1 (primary north star)

Stage 4 is the stage the Build Notes call "most critical". Its object-level F1 is therefore the primary north star.

**Matching.** For each evaluation region *r* (an annotated ROI or HPF), let *G_r* be the ground-truth points and *P_r* the predicted points after the final decision. All coordinates are in micrometres. Build the distance matrix *D_ij = ‖g_i − p_j‖₂*. Solve a 1:1 assignment with `scipy.optimize.linear_sum_assignment` on the cost

```
C_ij = D_ij        if D_ij ≤ R
C_ij = 1e6         otherwise
R    = 7.5 µm      (MIDOG convention: ≈ average mitotic-figure size)
```

Count only pairs with *D_ij ≤ R* as matches:

```
TP_r = |{(i,j) matched : D_ij ≤ R}|
FP_r = |P_r| − TP_r
FN_r = |G_r| − TP_r
```

Predictions outside the annotated region are discarded before matching. Predictions within *R* of the region boundary are kept only if they match a ground-truth point, which prevents boundary false-positive inflation.

**Aggregation.** The F1 is micro-averaged over all regions of all cases in the split:

```
F1 = 2·ΣTP / (2·ΣTP + ΣFP + ΣFN)
Precision = ΣTP / (ΣTP + ΣFP)
Recall    = ΣTP / (ΣTP + ΣFN)
```

**MIDOG compatibility mode.** `metrics.mitosis_f1(..., midog_compat=True)` reproduces the MIDOG reference rule: several detections within *R* of one ground-truth point count as a single TP, and the extra detections are not counted as FP. This mode exists only to compare with published MIDOG numbers. The 1:1 mode is the program metric.

**Also reported:**
- Average precision (AP): the area under the precision–recall curve from a sweep of the final score threshold (101 points, 1:1 matching at each point).
- Count MAE per 2 mm².
- Per-scanner F1 and per-magnification F1 (20× vs 40× native).

### 2.2 NS-G — Nottingham grade macro-F1 (co-north-star)

For classes *c ∈ {G1, G2, G3}*:

```
F1_c = 2·TP_c / (2·TP_c + FP_c + FN_c)
macro-F1 = (F1_G1 + F1_G2 + F1_G3) / 3
```

A missing prediction (a failed pipeline or `needs_human` with no grade) is a fourth predicted label, `none`. It adds to *FN_c* for the true class and is never a TP.

**Component macro-F1s**, computed the same way over scores {1, 2, 3}:
- `F1_T` (tubule formation)
- `F1_P` (nuclear pleomorphism)
- `F1_M` (mitotic score)

**Band metrics.** These encode the pathologists' requests in the Build Notes. They apply to cases where the ground-truth Nottingham sum is known.

| Metric | Definition |
|---|---|
| `F1_high` | Binary F1 for "sum ∈ {8, 9}" (predicted vs ground truth) over all cases with a ground-truth sum. |
| `macroF1_LM` | Among cases with ground-truth sum ∈ {3..6}, the macro-F1 over the classes present in that band (G1 = sum 3–5, G2 = sum 6). A prediction of G3 (or `none`) is a miss for the true class and not a FP for any class in the band. |
| `sum_MAE` | Mean absolute error of the predicted Nottingham sum (3–9). |

**Secondary:** quadratic-weighted Cohen's κ (QWK), the 3×3 confusion matrix, and coverage.

### 2.3 Supporting metrics (per stage)

| ID | Metric | Definition | Spec |
|---|---|---|---|
| S3-F1 | Tumor-tile F1 | Binary F1 (invasive tumor = positive) over 224 µm tiles. The label is the majority class of ground-truth annotation in the tile. Tiles with less than 50% annotated area are excluded. | 05 |
| S3-P@K | Hotspot precision@K | Fraction of the *K* selected hotspots with ≥ 50% of their area inside annotated invasive tumor. Computed only for hotspots that lie inside annotated extents. | 05 |
| S3-COV | Heatmap coverage | (Tissue tiles with a defined probability) / (tissue tiles). **Must equal 1.0.** | 05 |
| S5-HT | Histologic-type macro-F1 | Macro-F1 over {IDC-NST, ILC, mixed, other special type}. | 07 |
| INT-PROV | Provenance coverage | (Decisions with a DecisionRecord) / (decisions). **Must equal 1.0.** | 01 |
| INT-FALL | Silent-fallback count | Number of eval-mode decisions produced by a non-declared path. **Must equal 0.** | 01 |

### 2.4 Uncertainty and comparisons

- **CIs.** Use a case-level (patient-level for TCGA) non-parametric bootstrap: *B = 2000* resamples, a fixed seed recorded in `metrics.json`, and percentile 95% intervals. Mitosis bootstraps resample *cases* and carry all of a case's regions along.
- **Paired comparison of two configurations A and B.** Resample the same case indices for both configurations. Report *ΔF1 = F1_B − F1_A* and its 95% CI. For classification, also report a McNemar exact test on per-case correctness.
- **Precision of headline numbers.** A headline metric is reported as **final** only if its 95% CI half-width is ≤ 0.05. Otherwise it is labelled **provisional** and the report states the sample size needed. That size is estimated from the bootstrap variance scaled by *n*.

### 2.5 Gates

| Gate | Condition |
|---|---|
| **Measurement-validity gate (every run)** | INT-PROV = 1.0 · INT-FALL = 0 · zero patients shared between train/val and test (checked from the manifest) · config hash, model registry hash and split-file hash recorded. |
| **Promotion gate (P3 changes and SPEC-09)** | The lower bound of the paired-bootstrap ΔF1 on validation is > 0 for the target metric. No pre-registered slice drops by more than δ = 0.03 F1. The measurement-validity gate passes. |
| **Performance targets** | *Accepted by the program owner on 2026-09-28, as below.* |

| Metric | Floor | Rationale |
|---|---|---|
| NS-M F1 (MIDOG++ breast test, 1:1) | ≥ 0.70 | Carries forward the v5 "clinical floor". It sits within the range reported by MIDOG challenge participants. |
| NS-G macro-F1 (TCGA test) | set from the P2 baseline | There is no public component-sum baseline on these labels yet. |
| `F1_high` | set from the P2 baseline | Pathologist request: extremes must be identified clearly. |
| `macroF1_LM` | set from the P2 baseline | Pathologist request: consistent on low/medium cases. |

## 3. Datasets (registry)

All datasets are registered in `eval/datasets/registry.yaml`. Each entry carries source, licence, version or date retrieved, file-list hash, and the adapter class (SPEC-02 §3).

| Key | Dataset | Specimen | Role | Ground truth used | Notes / verification items |
|---|---|---|---|---|---|
| `tcga_brca_dx` | TCGA-BRCA diagnostic slides (GDC, open access, `.svs`) | Resection | **Primary** end-to-end benchmark (NS-G; slide-level `F1_M`; S3 via BCSS subset) | Nottingham grade and components parsed from TCGA pathology reports (SPEC-02 §4). Histologic type from the expert-committee review of TCGA-BRCA (Thennavan et al., *Cell Genomics* 2021; ≈1,058 annotated samples). | Only DX (FFPE) slides; frozen TS slides are excluded. Scans are a mix of ≈0.25 µm/px (40×) and ≈0.50 µm/px (20×), so resampling is mandatory (SPEC-04). Published precedent: ≈521 TCGA slides with report-derived Nottingham scores (Kim et al., *Breast Cancer Res.* 2025), so the expected labelled n is about 500. |
| `bcnb` | BCNB — Early Breast Cancer Core-Needle Biopsy WSI | Core biopsy | Secondary end-to-end benchmark (NS-G for CNB). S3 (tumor polygons). | Clinical field "histological grading". Pathologist tumor-region polygons (two pathologists). | 1,058 WSIs from 1,058 patients, iScan Coreo scanner. Official split 630/210/218 is **adopted as-is**. Images are **JPG** (not OpenSlide-readable) and must be converted at ingest (SPEC-02 §3.2). MPP, scan magnification, grade-field semantics and licence **will be supplied by the program owner**. The adapter is fully parameterised and refuses to run until they are set (SPEC-02 §3.2). |
| `midogpp_breast` | MIDOG++ (human breast carcinoma subset) | Resection ROIs | **Primary NS-M** object-level benchmark; training data for the Stage-4 classifier (SPEC-06). | Mitotic figures **and imposters (hard negatives)**, pathologist consensus. | 150 human breast cases across 3 scanners (Hamamatsu XR, Hamamatsu S360, Leica CS2), 0.23/0.25 µm/px, CC BY 4.0. KongNet is treated as externally validated (§8 R1). |
| `midogpp_other` | MIDOG++ (other tumor types) | — | Training data only (domain diversity) | Same as above | Never used for NS-M. |
| `bcss` | Breast Cancer Semantic Segmentation (TCGA-BRCA ROIs) | Resection | S3-F1 training and evaluation | Region masks (tumor, stroma, inflammatory, necrosis, …) | **Verify** the class map and the ROI→slide mapping. Split membership must follow the TCGA patient split (§4). |
| `tupac16` | TUPAC16 (500 TCGA-BRCA WSIs, mitotic score 1–3; 73-case auxiliary mitosis set) | Resection | Optional slide-level `F1_M` cross-check; the auxiliary set is an optional extra NS-M test set. | Mitotic score; mitosis points (auxiliary) | Optional; see §3.1. |
| `tcga_gt_mitosis` | Program-created TCGA HPF mitosis annotations (optional) | Resection | Optional TCGA-native NS-M slice | Point annotations by two annotators (SPEC-08 §6) | Created only if pathologist capacity becomes available. |

### 3.1 Licence and availability register (options; decisions pending where marked)

*Engineering summary, not legal advice. Confirm with counsel before any commercial deployment.*

| Asset | What is known | Options | Recommendation |
|---|---|---|---|
| **HoVer-Net weights trained on PanNuke** (SPEC-07 pleomorphism) | PanNuke is CC BY-NC-SA 4.0, and the HoVer-Net authors state that weights trained on it carry the same non-commercial licence ([hover_net README](https://github.com/vqdang/hover_net)) | (a) Use for the v6 research validation only, with registry `license_scope: research`, and replace before any commercial use. (b) Use a permissively licensed segmenter: StarDist `2D_versatile_he` (weights stated as CC BY 4.0; trained on MoNuSeg 2018 and TNBC). It has no cell types, so neoplastic nuclei are selected by the SPEC-05 tumour mask plus a size/shape filter. (c) Train our own segmenter on NuInsSeg (CC BY 4.0) plus program annotations. (d) Ask the PanNuke authors for a commercial licence | **(b) as the default segmenter; (a) as a research-only comparison arm.** This keeps the product path licence-clean from day one. (c) is a P3 option if (b) underperforms |
| **TUPAC16** (optional mitotic-score labels on 821 TCGA WSIs; auxiliary mitosis set) | Published challenge data. Current download availability is unverified. Alternative auxiliary-set labels are public ([DeepMicroscopy/TUPAC16_AlternativeLabels](https://github.com/DeepMicroscopy/TUPAC16_AlternativeLabels)), but its images come from the original release | (a) Try the challenge site once; if unavailable, email the organisers. (b) Skip it; TCGA report-derived mitotic scores (SPEC-02 §4) remain the primary slide-level label | **(a) once, time-boxed; otherwise (b). Not blocking** |
| **MITOS-ATYPIA-14** (optional field-level atypia scores for pleomorphism) | Download page on Grand Challenge; licence terms not confirmed | (a) Read the terms on the download page; use for internal validation only if permitted. (b) Skip; TCGA report components are the primary pleomorphism labels | **(a) if the terms allow research use; otherwise (b). Not blocking** |
| **BCSS** | CC0 1.0 ([license page](https://bcsegmentation.grand-challenge.org/License/)) | — | Use |
| **MIDOG++** | CC BY 4.0 | — | Use, with attribution |
| **TCGA-BRCA** | GDC open-access tier | Review GDC/TCGA data-use and publication guidelines | Use for research validation |
| **BCNB** | Terms to be supplied by the program owner | — | Pending |
| **Path Foundation, MedGemma** | Google Health AI Developer Foundations terms | Review the terms for the intended deployment | Record in `docs/licenses/` (SPEC-01 §3.7 `license_ref`) |

Every registry model and dataset entry carries `license_ref` and `license_scope ∈ {commercial_ok, research, pending}`. The harness writes the set of scopes used into `metrics.json`. A run that uses any `research`-scoped component is labelled "research-only" in the Research view.

## 4. Splits (summary; full rules in SPEC-02 §5)

- **Unit:** patient. TCGA patient = barcode `TCGA-XX-YYYY`; tissue source site (TSS) = `XX`.
- **TCGA split:** 60/20/20 train/val/test, stratified by (grade, TSS group, native magnification). A patient's DX slides all go to one split. Any BCSS ROI inherits its patient's split.
- **BCNB:** the official split.
- **MIDOG++ breast:** case-level 60/20/20, stratified by scanner. Leave-one-scanner-out (LOSO) runs are also reported as a domain-shift diagnostic.
- Split files are committed (`eval/splits/*.parquet`) with SHA-256 recorded in `eval/splits/SPLITS.lock`.

## 5. Source requirements (restated from the v6 Vision Doc and Build Notes)

The source PDFs are kept outside the repository. Each requirement is restated here with an ID so that specs can reference it.

**Vision Doc (V):**
- **V1** Create a CLI harness with one-shot results. The system must run without assistance, using the logic used to build it.
- **V2** Enable batch processing of slides.
- **V3** Add or replace Stage 6 (CAP Report) with a validation-data analyser.
- **V4** During improvement, categorise every issue as **Biological** (interaction with biomedical models; fix via architecture, platform design or programmable logic against hallucination), **Model** (simpler ML models; fix via training data, schema fixes, multiple models or split traffic), **Staging** (stage completion; close visual inspection; multi-part updates) or **Technical** (code failures, data handling, design, library or vCPU issues; fix via a full audit).
- Datasets: TCGA-BRCA (resection) and BCNB (core needle biopsy). Metric: F1.

**Build Notes (N):**
- **N1** Create Researcher and Admin roles.
- **N2** Enable batch processing of WSI slides.
- **N3** Replace Stage 6 with a Research view for data analysis and validation, with all relevant metrics.
- **N4** Add an auth screen.
- **N5** Clean labels: frontend labels are verbose or carry redundant descriptions.
- **N6** Stage 2 staining: check whether parameters target resection or CNB and take specimen type as input. Normalisation must happen once, in Stage 2 (it also happens in Stage 3).
- **N7** Stage 3: parts of the tissue show no hotspot mask. The mask must cover the entire tissue region. Suggestions for finding the best hotspots are welcome (keep the model; the architecture or logic may change).
- **N8** Stage 3: hotspots overlap, and they must never overlap.
- **N9** Stage 4 is the most critical stage. Examine it with utmost importance. The underlying models, architecture or logic may change if the evidence is strong.
- **N10** Stage 4: pathologists found the MedGemma/referee definition of a mitotic figure partial, so mitotic counts are overblown.
- **N11** Stage 5: precision/recall problems on tubule formation and pleomorphism. The system must identify extremes (sum 8/9) clearly and perform consistently on low/medium cases (sum 3–6).
- **N12** Self-improvement loops: corrections should retrain the specific model so that the same errors don't repeat.
- **N13** Architectural safety: robustness against prompt injection, rogue actions and "stupid hardcoded values".
- **N14** Stupidity check: hardcoded values can produce stupid outcomes.
- **N15** Lightweight code (confirmed interpretation: remove dead or duplicated code, unused dependencies and one-off tools; measured by LOC and image size).
- **N16** Code audit after all of the above (confirmed interpretation: a full multi-pass audit; exit with zero open critical/high findings).

**v5 analysis (A)** — findings from the 2026-09-28 code analysis:
- **A1** The Stage-3 tumor probe `probe_v1` was trained on 500 random Gaussian vectors, and the committed weights are bit-identical to `train_default_probe()`.
- **A2** Provenance is mislabelled and there are silent fallbacks:
  - Heuristic candidates are stamped `vertex_ai_midog`.
  - The referee `label_source` is derived from settings.
  - Vision prompts are retried text-only.
  - Morphometric fallbacks emit clinical-sounding rationales.
- **A3** The MedGemma "doer" receives no image, and its prompt pre-fills the heuristic's answer.
- **A4** Grading defaults are hardcoded and silent: a missing `base64` import leads to tubule 20% and pleomorphism 2, and there is an IDC-NST default.
- **A5** Resolution handling is unsafe: mitosis is not resampled to 0.25 µm/px, and pleomorphism is judged at 1.0 µm/px.
- **A6** Grading patches are sampled from the densest tissue, which biases tubule scores towards 3.
- **A7** Accuracy and audit claims are unsupported (`models/detector/EVAL.md`, README "462/462").
- **A8** The validation harness is non-functional: no slide upload, mpp hardcoded to 0.25, stage exceptions swallowed, and a test that only asserts that files exist.
- **A9** Triage pads referee-rejected regions back into the top-10 hotspots.
- **A10** Stain normalisation is fragmented across 7 call sites with 3 parameterisations.

## 6. Issue register

Priority key:
- **P0** — blocks valid measurement.
- **P1** — needed for accuracy or requirements.
- **P2** — quality.

| ID | Issue | Category (V4) | Pri | Spec(s) |
|---|---|---|---|---|
| A1 | Synthetic tumor probe | Model | P0 | 05 |
| A2 | Mislabelled provenance, silent fallbacks | Technical | P0 | 01, 06 |
| A3 | MedGemma doer receives no image | Biological | P1 | 07 |
| A4 | Hardcoded silent grading defaults | Technical | P0 | 01, 07 |
| A5 | No resolution normalisation | Staging | P0 | 04, 06, 07 |
| A6 | Density-biased grading patch sampling | Biological | P1 | 07 |
| A7 | Unsupported accuracy/audit claims | Technical | P1 | 01, 11 |
| A8 | Non-functional validation harness | Technical | P0 | 02 |
| A9 | Rejected regions padded into hotspots | Biological | P1 | 05 |
| A10 | Fragmented stain normalisation | Staging | P1 | 04 |
| N1 | Researcher and Admin roles | Technical | P1 | 03 |
| N2 | Batch processing | Technical | P0 | 02 |
| N3 | Research view replacing Stage 6 | Technical | P0 | 08 |
| N4 | Auth screen | Technical | P1 | 03 |
| N5 | Label clean-up | Staging | P2 | 10 |
| N6 | Staining once, specimen-aware | Staging | P1 | 04 |
| N7 | Heatmap gaps; hotspot finding | Model / Staging | P0 | 05 |
| N8 | Hotspot overlap | Staging | P1 | 05 |
| N9 | Stage 4 critical review | Biological / Model | P0 | 06 |
| N10 | Partial mitosis definition, overcount | Biological | P0 | 06 |
| N11 | Grading low/medium vs extremes | Biological / Model | P0 | 07 |
| N12 | Self-improvement loops | Model | P2 (P3 phase) | 09 |
| N13 | Architectural safety | Technical | P1 | 03 |
| N14 | Hardcoded values | Technical | P0 | 01 |
| N15 | Lightweight code | Technical | P2 | 11 |
| N16 | Final code audit | Technical | P2 | 11 |
| V1 | CLI harness with one-shot results | Technical | P0 | 02 |
| V2 | Batch processing | Technical | P0 | 02 |
| V3 | Validation-data analyser | Technical | P0 | 08 |
| V4 | Issue categorisation workflow | Process | P1 | 08 §5, this §7 |

## 7. Issue workflow for P3 (V4)

Every issue found during measurement is recorded in the `issues` table (SPEC-08 §5) with:
- category ∈ {Biological, Model, Staging, Technical}
- severity
- linked evidence (run ID, case, decision records)
- `metric_impact` (the metric and slice it degrades)

The category decides the default remedy class:

| Category | Default remedy class (from the Vision Doc) | Typical owner spec |
|---|---|---|
| Biological | Architectural change to the stage; platform design change; programmable logic that constrains hallucination (schemas, gating, deterministic post-processing) | 06, 07 |
| Model | Additional or corrected training data; fine-tuning; schema fixes; multiple models or split traffic (shadow / A-B via SPEC-09) | 05, 06, 07, 09 |
| Staging | Visual-inspection-driven fixes to stage logic or geometry; multi-part updates | 04, 05, 10 |
| Technical | Code, data-handling or library fixes; resolved via audit | 01, 02, 03, 11 |

## 8. Program-level risks

| ID | Risk | Mitigation |
|---|---|---|
| R1 | **KongNet is treated as an externally validated component** (program-owner decision, 2026-09-28). v6 does no separate KongNet-only validation and no training-overlap audit. | NS-M still measures the **whole Stage-4 pipeline** (resampling, tiling, classifier, referee, NMS, tumour gate), which v6 changes. Paired comparisons between arms share KongNet and are unaffected. Known limitation, recorded in reports: if KongNet saw MIDOG++ breast cases during training, the absolute NS-M on MIDOG++ may read slightly high. |
| R2 | **Pretraining exposure.** Path Foundation, Gemini and MedGemma may have seen TCGA images or reports during pretraining. | Documented as a limitation. BCNB (non-TCGA, different continent and scanner) is the external check. |
| R3 | **Label noise in report-derived TCGA grades.** | Evidence-span extraction, consistency checks and dual QA (SPEC-02 §4). Report metrics on the high-confidence label subset as well as the full set. |
| R4 | **Small n per class** (G1 is typically the minority). | Stratified splits. CI-width rule (§2.4). Label a result provisional rather than over-claim. |
| R5 | **Cost and quota** of VLM calls on about 1,100 WSIs. | Cache every model output keyed by input hash (SPEC-01 §3). Run ablations on cached detections. Use per-endpoint rate limits (SPEC-02 §6). |
| R6 | **20× scans upsampled to 0.25 µm/px** degrade mitosis detection. | Report per-magnification slices. Consider excluding 20× from NS-M if F1 differs by more than 0.10. |

## 9. Sequencing and dependencies

```
SPEC-01 Measurement integrity (DecisionRecord, RunMode, config, Alembic)
   │
   ├─► SPEC-04 Staining / specimen profiles / resolution (read_region_at_mpp, StainProfile, tissue mask)
   │      │
   │      ├─► SPEC-05 Triage & hotspots (tumor head, full coverage, non-overlap)
   │      │      │
   │      │      └─► SPEC-06 Mitosis (resampled sweep, classifier, arms)
   │      │             │
   │      │             └─► SPEC-07 Grading (tubule/pleomorphism redesign, arms)
   │      │
   ├─► SPEC-03 Auth / RBAC / safety ─────────────┐
   │                                             ▼
   └─► SPEC-02 Harness, datasets, batch ──► SPEC-08 Research view (replaces Stage 6)
                                                 │
SPEC-10 Label clean-up (parallel, after 08 routes are known)
SPEC-11 Lightweight code (after 05–08) ──► P2 Measure ──► P3 Improve (SPEC-09 loop) ──► SPEC-11 Final audit
```

The recommended P1 build order is 01 → 04 → 03 → 02 (adapters and splits first, because 05–07 need training data) → 05 → 06 → 07 → 08 → 10 → 11 (lightweight part).

## 10. Spec index

| Spec | Title |
|---|---|
| [01](01-measurement-integrity.md) | Measurement integrity: provenance, fail-loud, hardcoded values, model registry |
| [02](02-validation-harness-and-batch.md) | Validation harness, dataset adapters, labels, splits, batch processing |
| [03](03-auth-rbac-safety.md) | Google Workspace SSO, RBAC (Admin/Researcher), architectural safety |
| [04](04-stage2-staining-specimen-resolution.md) | Stage 2: single stain authority, specimen profiles, resolution, tissue mask |
| [05](05-stage3-triage-hotspots.md) | Stage 3: tumor head, full-coverage heatmap, non-overlapping hotspots |
| [06](06-stage4-mitosis.md) | Stage 4: mitosis detection redesign (north-star stage) |
| [07](07-stage5-nottingham-grading.md) | Stage 5: Nottingham grading redesign |
| [08](08-research-view.md) | Research view (replaces Stage 6 CAP report) |
| [09](09-self-improvement-loop.md) | Self-improvement loop |
| [10](10-frontend-label-cleanup.md) | Frontend label clean-up |
| [11](11-lightweight-code-and-audit.md) | Lightweight code and final audit |
