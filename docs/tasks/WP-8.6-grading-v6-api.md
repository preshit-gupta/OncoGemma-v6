# WP-8.6 — `grading_v6` API and the Stage 5 sampling it needs (baseline end to end)

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | L | SPEC-07 §4, §5.1–5.2, §6.1, §7.1, §7.3; contract `grading_v6` | WP-6.2, WP-6.3, WP-8.5, WP-7.6a (all merged) | Claude (`backend/**`, `configs/**`) |

> **Refreshed 2026-10-04** against `main` after WP-7.6a merged (#47). Changes from the 2026-10-02 card:
> - WP-7.6a edited the same files (`routers/grading.py`, `worker/grading.py`, `services/stages.py`, `core/rehydrate.py`, `pipeline/scoring.py`). Grading already reads `counted`, but it still calls its own mitotic helpers.
> - **Owner decisions (2026-10-04):** the WP-8 split is confirmed (plan §2.3, D21). When pleomorphism field scores tie, P takes the **highest** tied score.
> - WP-7.6a makes `pipeline/scoring.py` the single counting implementation (SPEC-06 AC10). Detections count by the generated `counted` column, and a summary with no HPF has `mitotic_score: null`. This card takes M from that module and deletes grading's own mitotic helpers (task 3).
> - Line references are replaced by symbols. The "confirmed hotspot" definition is spelled out (task 1).
> - Dead `scoring.yaml` grading keys are removed (task 7).
> - No frontend caller of the v5 grading routes is left (checked 2026-10-04; `lib/api/grading.ts` calls only `grading_v6` routes).

## Goal

Make the deployed Stage 5 work end to end against the [`grading_v6`](../contracts/grading_v6.md) contract the WP-8.5 workspace already calls. Use the estimators that run today. Nothing here adds an estimator arm, training or tuning (D19 spirit: baseline first).

**Why this is urgent.** Outside mock mode, the grade screen cannot load or save:
- WP-8.5 rewrote the workspace against `grading_v6`. It reads `tubule.samples`, `pleomorphism.fields` and `histotype`, and calls `/review-sample`, `/override` and `/histotype/confirm`.
- The backend still serves the v5 shape (`patches`, `hpfs`, `machine`, `tubule_score`, `is_type_confirmed` and so on) and the v5 routes `/patches/review`, `/hpfs/review`, `/recompute` and `/type/confirm`.

This came up on 2026-10-02 while fixing a production run. Histotype confirm posted to a route that does not exist, and the root cause is this whole gap.

**Why the worker changes too.** The contract's samples cannot be filled honestly from today's worker output. Tubule samples are 512 µm at 1.0 µm/px and pleomorphism fields are 128 µm at 0.25 µm/px. v5 reads one hotspot patch and asks both questions of it. Relabelling a v5 patch as a 128 µm field would invent its geometry (SPEC-01 §3.9). So the minimum honest change is stratified sampling plus separate tubule and pleomorphism reads. The estimators stay as they are.

**Owner decisions (2026-10-02).**
- **Sampling frame.** Samples come from the confirmed Stage 3 hotspot windows, not the whole tumour mask. This deviates from SPEC-07 §4; propose the spec change in the PR.
- **Counts.** Use the spec counts: 48 tubule + 48 pleomorphism for resection (≈ 96 estimator calls per case); 24 + 32 for core biopsy.
- **Tubule overlap.** At most 10 windows of 600 µm fit only about 10 non-overlapping 512 µm boxes, so tubule samples may overlap.
- **Old cases.** Cases graded by the v5 worker return `404 not_found`; the owner deletes them.

## Read first (only these)

- `docs/contracts/grading_v6.md` and `docs/contracts/README.md` (Provenance, Mutations)
- `docs/specs/07-stage5-nottingham-grading.md` §4, §5.1, §5.2, §6.1, §7.1, §7.3, §10
- `backend/worker/grading.py`: read by function, not whole
- `backend/app/routers/grading.py`: read by route, not whole
- `backend/pipeline/grading.py::aggregate_grading_findings`
- `backend/pipeline/scoring.py` as WP-7.6a leaves it: `summarize_stage4`, `is_counted`, `HPF_COUNT_LT_10`
- `backend/worker/triage.py::tiles_parquet` and the `is_tumor` lines of `run_triage` (the tile grid, `p_tumor_cal`, `is_tumor`), `backend/pipeline/tile_grid.py`
- `backend/pipeline/hotspots_v6.py::HotspotWindow`, `backend/app/models/hotspot.py` (`polygon_um`, `window_um`, `excluded`)
- `backend/app/models/grading.py`
- Frontend reference (read only): `frontend/lib/api/grading.ts`, `frontend/lib/mock/grading.json`

## Files you may touch

- `backend/worker/grading.py`, `backend/pipeline/grading.py`, and `backend/pipeline/grading_sampling.py` (create, SPEC-07 §4)
- `backend/app/routers/grading.py`, `backend/app/services/stages.py`, `backend/app/core/rehydrate.py`: only their grading readers
- `backend/app/core/pipeline_config.py`, `configs/specimen_profiles.yaml` and `configs/scoring.yaml`: only the grading sample counts and sizes (`tubule_patches`, `pleo_fields`, the histotype sample count), and removing the dead `grading` keys (task 7)
- `frontend/lib/api/grading.ts`: only if a contract change below needs it
- `docs/contracts/grading_v6.md`: only the changes listed under "Contract changes"
- `backend/eval/harness/collect.py`: only if task 8 finds it broken
- Tests: `backend/tests/test_grading_worker.py`, `test_grading_api.py`, `test_grading.py`, and new `backend/tests/test_grading_v6_contract.py` and `test_grading_sampling.py`. Delete or rewrite the v5 tests `test_batch8_grading_pipeline.py` and `test_batch17_grading_staging.py` where they assert removed routes or shapes, and list each one in the PR

## Tasks

1. **Sampling (SPEC-07 §4 method, hotspot frame)** in `pipeline/grading_sampling.py`:
   - **Candidates:** tumour tiles (`is_tumor` from `triage/tiles.parquet`) whose centres lie inside a confirmed, non-excluded Stage 3 hotspot window. "Confirmed" means the Stage 3 execution is `confirmed` and the `hotspots` row has `excluded = false`. Pathologist-added or modified windows count too (`source`); the window is the row's `polygon_um`.
   - **Strata:** weighted k-means with `k = n_samples`, weights = tumour area, seeded by the slide SHA-256. One sample per cluster, at the candidate tile nearest the centroid.
   - **Pleomorphism fields** (128 µm at 0.25 µm/px): greedy minimum separation of one field size, re-drawing within the cluster. The box lies inside its hotspot window.
   - **Tubule samples** (512 µm at 1.0 µm/px): no separation constraint, so overlap is allowed (owner decision). The centre lies inside a hotspot window; the box may extend past it.
   - Each sample records `stratum`, `hotspot_id` and `tumor_area_um2`, the exact intersection of the tumour mask with the box.
   - Counts come from the specimen profile (`tubule_patches`, `pleo_fields`: 48/48 resection, 24/32 CNB), not from code.
   - Replace `select_max_density_hotspot_patches`.
   - A case with no confirmed hotspots, or no tumour tiles inside them, fails the stage with a specific error. There is no fallback to whole-slide sampling.
   - If fewer separated pleomorphism fields fit than requested, use the ones that fit and flag `needs_human`. Never pad with overlapping fields.
2. **Worker:**
   - One tubule call per tubule sample and one pleomorphism call per field, with the **existing** producers and prompts and the existing `invoke_or_fallback` handling (a failed estimate → `estimate: null`, flag `needs_human`).
   - Histotype over the first N tubule samples, as today.
   - Write the sample PNGs (`tubule/<id>.png`, `pleo/<id>.png`) and keep the sample list in `gradings.machine`.
   - `nuclei` is null: segmentation is WP-8.3.
3. **Aggregation (SPEC-07 §5.2, §7.1)** in `pipeline/grading.py`:
   - T% = area-weighted mean over samples with `tumor_present`. Reviewed values replace estimates.
   - P = the mode of field scores; `aggregation: "mode"`.
   - No confidence weights.
   - The grade only when T, P and M all exist; otherwise `null` plus `needs_human`.
   - `near_grade_boundary` when the total is in {5, 6, 7, 8}; `hpf_count_lt_10` comes from Stage 4.
   - Thresholds come from `scoring.yaml` through `pipeline/scoring.py` only.
   - **M** comes from the confirmed Stage 4 output through `pipeline/scoring.py` (`summarize_stage4` over `counted` detections and the HPFs). Grading never recounts mitoses its own way. A Stage 4 summary with `mitotic_score: null` (no HPF placed) gives no grade, with `needs_human`.
   - Write the aggregate to the `gradings` columns (`tubule_percent`, the three scores, `nottingham_sum`, `grade`, `histologic_type`), and recompute them after every review and override. The eval harness reads these columns, and `check_nottingham_grade_calc` must hold.
   - Delete `calculate_mitotic_score_from_hpfs`, `calculate_mitotic_score_from_detections_and_hpfs`, and `weighted_median` from `pipeline/grading.py` when nothing calls them (SPEC-07 §10). Replace `weighted_mode` with an unweighted mode.
   - P ties: when two or more scores tie for the mode, P is the **highest** tied score (owner, 2026-10-04). The tie-break value comes from config (`scoring.yaml`), not from code, and a test covers it. Record in `machine` that a tie occurred.
4. **API = `grading_v6`:**
   - `GET /{case_id}`, `POST /review-sample`, `/override`, `/histotype/confirm` (`{case_id, type}`, `422 invalid_value`, idempotent) and `/confirm`, with the contract's errors. Every mutation returns the full `GradingStageV6`.
   - Reviews and overrides go in `gradings.overrides` (`reviews[sample_id]`, `reasons`). `machine` is never mutated, so the machine output stays auditable.
   - Cases whose `gradings.machine` has no v6 samples (written by the v5 worker) return `404 not_found` on every route (owner decision).
   - Delete `/patches/review`, `/hpfs/review`, `/recompute`, `/type/confirm`, `/{case_id}/type/confirm` and `/{case_id}/patches/{patch_id}/image`, after checking that no frontend caller remains.
5. **Downstream readers.** Update `services/stages.py` and `core/rehydrate.py` to the new `machine` layout.
6. **Contract test.** `GET` validates against a Pydantic mirror of `GradingStageV6`, the mock fixture example included.
7. **Config clean-up (SPEC-07 §10).** Remove `scoring.yaml` `grading.confidence_weights`, `n_patches`, `patch_size_px` and `resolution_um`, with their `pipeline_config.py` fields. The counts move to the specimen profiles. The sample sizes (512 µm @ 1.0 µm/px, 128 µm @ 0.25 µm/px) go in one config place, not in code. Keep `estimators`. Remove `min_tumor_patches` and `max_disp` only if nothing reads them after the rewrite.
8. **Eval collection.** `eval/harness/collect.py` reads `gradings` columns, not `machine`, so it keeps working. Check that `needs_human` still comes from `machine["needs_human"]` and that its test passes. Touch `collect.py` only if it breaks.

## Contract changes (update `docs/contracts/grading_v6.md` in this PR)

- `PleoField.nuclei` stays `null` until WP-8.3. The type already allows it; say so in a comment.
- `tubule.estimator` / `pleomorphism.estimator` report the producer actually used (for example `T1:<producer>@<prompt>`). The example's `P2:ordinal_morph` arm does not exist yet.
- `/histotype/confirm` lists `409 stage_locked` (grading confirmed or report signed), which the backend already enforces.

## Acceptance (run these)

```powershell
python -m pytest backend/tests/test_grading_v6_contract.py backend/tests/test_grading_sampling.py backend/tests/test_grading_worker.py backend/tests/test_grading_api.py -q -p no:cacheprovider
python -m pytest backend/tests -q -p no:cacheprovider            # full suite
cd frontend; npx tsc --noEmit
```

Sampling tests:
- deterministic for a fixed SHA;
- every sample centre lies on a tumour tile inside a confirmed hotspot window;
- pleomorphism fields lie inside their window and are pairwise separated by ≥ 128 µm;
- tubule counts follow the profile even with one hotspot;
- excluded hotspots are never sampled;
- no hotspots or no tumour tiles in them → the specific error;
- a v5-shaped grading → `404 not_found`.

## Out of scope — do not do

- New estimator arms (T1-MG, T3, T4, P-arms, H2), MIL/ordinal training, StarDist (WP-8.2–8.4).
- Prompt changes (`tubule@v2`, `pleo@v2`, definitions files).
- Frontend component changes (WP-8.5 is merged; only `lib/api/grading.ts` if a contract change needs it).
- Any tuning of thresholds.

## Proposed spec change (raise in the PR; do not edit the spec)

SPEC-07 §4: the sampling frame is the confirmed hotspot windows (owner, 2026-10-02), not the whole tumour mask. Tubule samples may overlap. Overlapping tubule samples score some tissue more than once, so the area-weighted T% counts that tissue more than once. Report this in the PR.

## Proposed spec change: pleomorphism ties (raise in the PR)

SPEC-07 §1 lists the tie-to-max mode as part of bias B4. The owner keeps tie-to-max (2026-10-04), so B4 is reduced to the 1.0 µm/px resolution and the merged-nuclei CV. WP-8.7's signed error measures the effect.

## Done checklist

- [ ] Stratified sampling inside confirmed hotspots (48/48 resection, 24/32 CNB); tubule overlap allowed, pleomorphism separated
- [ ] v5-shaped gradings return `404 not_found`
- [ ] Separate tubule (512 µm @ 1.0) and pleomorphism (128 µm @ 0.25) reads with existing estimators
- [ ] Area-weighted T%, mode P, grade only with all three components, boundary flag
- [ ] M only through `pipeline/scoring.py`; grading's mitotic helpers and confidence weights deleted
- [ ] `grading_v6` routes; v5 routes deleted; reviews and overrides never mutate `machine`
- [ ] Contract test passes against the mock fixture example
- [ ] Contract file updated with the listed changes
- [ ] `docs/STATUS.md` updated
