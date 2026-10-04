# WP-8.7 — Validate the Stage 5 grading baseline on TCGA-BRCA val

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | M | SPEC-02 §5, §7; SPEC-07 §7.1, §8.1–8.2 (metrics only), §9 AC2/AC3 (measured, not gated) | WP-8.6, WP-7.6a; whole-slide TCGA reads in place (see "Prerequisite") | Claude (`backend/eval/**`) |

## Goal

Measure the grading baseline that WP-8.6 ships, with its settings fixed, on the locked TCGA-BRCA val split through the production code path (the WP-5.5 harness, `run_mode=eval`). Report NS-G and the SPEC-07 band metrics with confidence intervals, and the per-band **mean signed error** of each component and of the sum. The signed error is what tests the upward-bias hypothesis of SPEC-07 §1.

The baseline:
- Stage 3: tumour head and hotspots as merged in WP-6.2/6.3.
- Stage 4: KongNet at τ 0.75, NMS 7.5 µm, referee off (D17).
- Stage 5 (WP-8.6): stratified samples inside the confirmed hotspots, the existing `tubule@v1` / `pleo@v1` / `histologic_type@v1` prompts with `estimators.producer`, area-weighted T%, mode P, no confidence weights.

This is validation only (D19 spirit, as for WP-7.8):
- no new estimator arms;
- no prompt changes;
- no cut-point calibration (arm T4);
- no training;
- no tuning of any threshold.

## Prerequisite (not built yet)

The harness cannot run whole TCGA slides yet:
- the owner ruled out a bulk copy into our buckets (2026-09-30);
- the in-place source (IDC DICOM or GDC range reads in ingest and `SlideReader`) is STATUS "Claude — next" item 1.

This card starts after that lands, or after the owner approves another source for the val slides only. Do not build it under this card.

## Read first (only these)

- `docs/IMPLEMENTATION_PLAN.md` §2.2 and §2.3 (WP-8 re-plan)
- `docs/specs/07-stage5-nottingham-grading.md` §1, §7.1, §8.2, §9
- `backend/eval/harness/report.py` (the `s5` block: `ns_g`, `f1_t`, `f1_p`, `f1_m`, `f1_high`, `macro_f1_lm`, `sum_mae`, `qwk` already exist), `backend/eval/harness/collect.py`
- `backend/eval/metrics.py::band_metrics`, `bootstrap`, `qwk`
- `backend/eval/datasets/labels/README.md` and `tcga_grade_summary.json`

## Files you may touch

- **Create** `backend/eval/grading_baseline.py`. It reads a finished run's items and the labels, computes the per-band signed errors, and writes the report. It adds no field to `metrics.json` (`MetricsV1`), so no contract changes.
- `backend/eval/metrics.py`: add one function `signed_error_by_band` (pure, with tests). Do not change existing functions.
- `backend/eval/cli.py`: one sub-command that calls `grading_baseline`.
- Tests under `backend/tests/eval/`.
- **Create** `reports/baseline/grading_tcga_val.md`
- `docs/STATUS.md`

## Tasks

1. **Run.** Start a harness batch over the TCGA val split, with stages through grading, in `run_mode=eval`, using the deployed models, with no config overrides.
   - The harness confirms Stages 3 and 4 with their unreviewed machine output and never confirms grading. Check this in `one_shot.py`/`driver.py` and state it in the report.
   - Record the run id, `config_hash` and the image tags.
2. **Ground truth.** Use `tcga_grade.parquet` rows with `excluded_reason` null.
   - **The labels are pre-QA** (STATUS WP-5.4: the QA queue of 258 is unreviewed). Say so in the report title.
   - Every accepted row is `label_confidence: high` with `label_source: both` (the report grammar and the extraction agree), so the SPEC-00 R3 subset equals the full set. Say so rather than reporting a second table.
   - Val has 145 graded patients (231 slides, 216 patients). Components are stated for T 85, P 98, M 85 and the sum 87 (checked 2026-10-04). Component metrics use only the rows that state that component. Expect wide CIs and report n in every cell.
   - A patient with several DX slides: use the harness's existing case unit, and state which one it is.
3. **Metrics.**
   - Coverage: the share of graded val cases with a predicted grade. A missing grade counts as wrong (SPEC-00 rule 2).
   - From the harness report: NS-G, `F1_T`, `F1_P`, `F1_M`, `F1_high`, `macro_F1_LM`, `sum_MAE` and QWK, with bootstrap CIs.
   - From `signed_error_by_band`: the mean signed error (prediction − truth) of T, P, M and the sum, for each true band (sum 3–5 / 6–7 / 8–9), with bootstrap CIs and n per cell.
   - AC2 is reported, not gated: does the sum-3–6 signed-error CI contain 0, or lie within ±0.3?
   - The confusion matrix of grade and of each component.
   - The `needs_human` rate, and which flags caused it.
4. **Report** in `reports/baseline/grading_tcga_val.md`: the setup, the numbers, and the label caveat. If NS-G or `macro_F1_LM` is clearly poor, or the signed error is clearly positive in the low bands, **stop and report to the owner**. Do not tune. The next iteration's hypotheses (§2.3 of the plan) are tested against this result.

## Owner gates

- **Live run.** About 145 graded val cases. Each costs about 97 VLM calls for grading (48 + 48 + 1), plus the Path Foundation embeddings, the tumour referee and the KongNet tiles. That is about 1.4 × 10⁴ VLM calls in total; reruns are cached. Give the owner an estimate from one case's DecisionRecords and ask before the first full run.
- **Test split.** Locked. Not read under this card.

## Acceptance (run these)

```powershell
python -m pytest backend/tests/eval -q -p no:cacheprovider
```

Tests, with synthetic items and labels:
- `signed_error_by_band` puts a case in its **true** band, signs it as prediction − truth, and leaves out cases with no truth for that component;
- a case with no predicted grade counts as wrong in NS-G and is left out of the signed error, with the count reported;
- the report states the run id, `config_hash` and the label status;
- no config value is overridden (the run's `config_hash` equals the committed config's).

## Out of scope — do not do

- The SPEC-07 §8.2 attribution study (G0 = v5 Stage 5, cumulative fixes). Deferred with WP-8.1–8.4; this card measures only the v6 baseline.
- Histologic type S5-HT and ILC F1. The Thennavan et al. labels (`eval/datasets/labels/tcga_histotype.csv`) are not in the repo, and `report.py` marks them unavailable. Listed as an open item.
- New arms (T1 with `tubule@v2`, T1-MG, T3, T4, P1–P4, H2), MIL/ordinal training, StarDist, the direct-grade comparator.
- Any change to `configs/`.
- BCNB (D20).

## Done checklist

- [ ] Prerequisite met: TCGA val slides readable by the harness
- [ ] Owner go-ahead for the live run
- [ ] Val run with fixed settings; run id and `config_hash` recorded
- [ ] NS-G, band metrics and per-band signed errors with CIs and n; label caveat stated
- [ ] `reports/baseline/grading_tcga_val.md` committed
- [ ] `docs/STATUS.md` updated
