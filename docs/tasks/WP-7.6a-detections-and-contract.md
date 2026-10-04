# WP-7.6a — Detections migration, the `mitosis_v6` API and HPF fixes (baseline end to end)

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | M | SPEC-06 §5.6–5.8, §7 AC8, AC10; contract `mitosis_v6` | WP-7.2 (merged) | Claude (`backend/**`, `configs/**`) |

## Goal

Make the deployed Stage 4 work end to end with the baseline: KongNet at `det_threshold`, referee off (D17, D19). Nothing here changes what the model predicts.

**Why this is urgent.** The WP-7.7 viewer calls `/api/v1/stages/mitosis/{review,add,replace-hpfs,confirm}` and reads `p_a`, `final_decision` and `counted`. The backend still serves the v5 shape (`det_conf`, `label`, `medgemma_*`) and the v5 routes (`/recompute`, `/add_candidate`, `/bulk_action`, `/re_place_hpfs`). Outside mock mode, the Stage 4 review screen cannot load or save.

**Stopgap on `main` since this card was written (PR #44, 2026-10-03).** `routers/mitosis.py::_add_v6_candidate_fields` adds the v6 fields to the v5 `GET` payload so the review screen loads: `p_a = det_conf`, `p_b = ver_conf`, `in_tumor = True`, every unreviewed model candidate shown as `equivocal`, `counted` derived from `label`, `crop_url` = the 128 px referee crop (`?stain=norm`) and `context_url` = the same crop in original colour (`?stain=orig`). Saving still fails (the v5 routes). This WP deletes the shim and serves real columns. Note what it got wrong so the tests catch it: an unreviewed candidate above τ is `mitosis` (not `equivocal`) in the baseline, `in_tumor` is `null` while the gate is off, and `context_url` must be the 256 µm context, not the crop.

Split from WP-7.6 (plan §2.2). The tumour-cell gate is WP-7.6b. Classifier B, the referee and the Stage-A cache are deferred (D19).

## Read first (only these)

- `docs/contracts/mitosis_v6.md` (all of it)
- `docs/specs/06-stage4-mitosis.md` §5.6, §5.7, §5.8, §7 (AC8, AC10)
- `backend/worker/mitosis.py` (from the line after Stage A to the end), `backend/app/models/detection.py`, `backend/pipeline/detect.py::apply_global_nms`, `backend/pipeline/hpf.py::greedy_place_hpfs`, `backend/pipeline/scoring.py`
- `backend/app/routers/mitosis.py`: read by route, not whole
- Callers of `Detection.label`, `label_source`, `det_conf` and `ver_conf`: `app/routers/grading.py`, `app/services/stages.py`, `app/core/rehydrate.py`

## Files you may touch

- `backend/alembic/versions/0016_detections_v6.py` (create; `0015` is the latest on `main`. If another PR has taken `0016` by the time you start, use the next free number), `backend/app/models/detection.py`
- `backend/worker/mitosis.py`, `backend/pipeline/detect.py`, `backend/pipeline/hpf.py`, `backend/pipeline/scoring.py`
- `backend/app/routers/mitosis.py`, `backend/app/routers/grading.py`, `backend/app/services/stages.py`, `backend/app/core/rehydrate.py`
- `backend/app/core/pipeline_config.py` and `configs/mitosis.yaml`: the `mitosis` section only
- `backend/app/inference/records.py`: only a non-model `DecisionRecord` for `Task.MITOSIS_COUNT`
- `docs/contracts/mitosis_v6.md`: only the changes listed under "Contract changes"
- Tests: `backend/tests/test_mitosis_worker.py`, `test_mitosis_api.py`, `test_nms.py`, `test_hpf.py`, and new `backend/tests/test_mitosis_v6_contract.py`. Delete or rewrite the v5 tests `test_batch9_mitosis_pipeline.py`, `test_batch15_tiles_and_hpf.py` and `test_batch16_mitosis_pipeline.py` where they assert removed behaviour, and list each one in the PR

## Tasks

1. **Migration `0016`** (SPEC-06 §5.6; `0015` is WP-6.3's hotspot columns). Add `p_a`, `p_b`, `vlm` (JSON, the full `VlmVerdict` or null), `rule_override` (the contract has them; `p_b`, `vlm` and `rule_override` stay null/false in this iteration), `in_tumor` (nullable, see the contract changes), `final_decision`, `decision_path`, `review_label`, `record_ids` (JSON: the DecisionRecord ids of the chain) and `counted`.
   - `counted` is a generated column: `COALESCE(review_label = 'mitosis', final_decision = 'mitosis' AND COALESCE(in_tumor, TRUE))`. `in_tumor` is NULL only while `mitosis.tumor_gate` is off. It must work on Postgres 16 and on the SQLite used by tests.
   - Map existing rows deterministically:
     - `det_conf` → `p_a`;
     - `label_source` starting with `pathologist` → `review_label = label`, and `decision_path = 'human'` when `det_conf` is null;
     - otherwise `label` `mitosis` / `not_mitosis` / `unreviewed` → `final_decision` `mitosis` / `not_mitosis` / `equivocal`, with `decision_path = 'A'` and `vlm = NULL`. The contract's paths have no "referee without B" value, and a v5 verdict has no criteria to fill a `VlmVerdict`, so the old `medgemma_verdict` is not carried over.
   - Record the mapping in the migration docstring.
   - Then drop `det_conf`, `ver_conf`, `label`, `label_source` and `medgemma_*`. Avoid `batch_alter_table` on root tables (STATUS findings); `detections` is a child table.
2. **Worker** writes the v6 columns for the baseline:
   - `p_a` = the detector's probability as returned (no calibration; D19);
   - `final_decision = 'mitosis'` if `p_a ≥ det_threshold`, else the candidate is not persisted (raw points stay in `stage_a.json`);
   - `decision_path = 'A'`, `in_tumor = NULL` (gate off), `record_ids = [detect record id]`.
   - The referee stays off (D17, D19). Do not extend its code path. Stop computing referee images when it is off. If `referee.enabled` were set to true, `EQUIVOCAL` must map to `final_decision = 'equivocal'`, never to `'mitosis'`.
3. **Crops for the contract.** Write `crop` = 64 µm at 0.25 µm/px (256 px) and `context` = 256 µm at 1.0 µm/px (256 px), both PNG, raw colour, for every persisted candidate. They are independent of the referee's inputs. Serve them as `crop_url` / `context_url` the frontend can load with its session cookie.
   - The current `GET /{case_id}/candidates/{candidate_id}/crop` serves the referee crop and its `?stain=norm|orig` variants, and silently extracts a crop from the slide when the stored PNG is missing or under 1,000 bytes. Replace it: serve the stored contract crop and context, and answer `404` when one is missing (no on-demand substitute; AGENTS rule 4).
   - `detections.crop_uri` / `crop_orig_uri` become the contract crop and context URIs (rename them in `0016`, or drop them and derive the blob names from the candidate id; say which in the PR).
4. **NMS** (§5.7). Run it once, after decisions, ordered by `p_b ?? p_a` descending, with no label rank. Add `test_nms.py::test_dividing_cell_counts_once`: two detections 6 µm apart on one synthetic telophase figure, with `r_nms` 7.5 µm, yield one. If WP-7.8 finds that MIDOG++ documents a different daughter-group separation, the test follows it.
5. **HPFs** (§5.8):
   - build the density map from `counted` candidates only, with no `unreviewed` weighting and no 0.5 cut-off;
   - delete the relaxed passes (393 µm, then `radius_um`) and `relaxed_min_separation_um`;
   - if fewer than `count` fields fit, `n_hpf < 10`, `area = n_hpf · π r²`, flag `hpf_count_lt_10`;
   - each `Hpf` reports `tissue_coverage`. `tumor_fraction` is null until WP-7.6b (see the contract changes).
   - Add a property test: placed HPFs never overlap (AC10).
6. **API** = `mitosis_v6`: `GET /{case_id}`, `POST /review`, `/add`, `/replace-hpfs` and `/confirm`, with the contract's errors.
   - Delete `/recompute`, `/add_candidate`, `/bulk_action` and `/re_place_hpfs`.
   - Every recompute goes through `pipeline/scoring.py` (AC10). Add a grep test that no other backend file compares against the score thresholds.
   - `/confirm` refuses with `409 equivocal_unreviewed` while an `equivocal` candidate inside an HPF has no `review_label`. This replaces `review.gate_min_conf`; delete it.
   - Remove the invented slide size: today `GET` falls back to `width_px`/`height_px` = 20000 when there is no slide. Return the contract error instead.
7. **Downstream readers.** Grading counts `counted`, not `label == 'mitosis'`. Update `services/stages.py` and `core/rehydrate.py` to the new columns.
8. **`mitosis_count` DecisionRecord** (AC8). One record per Stage 4 run and per recompute, with the summary, the candidate ids and the `scoring.py` thresholds as params, linked to the detect records. Add the record writer to `records.py` if no non-model record exists yet. `Task.MITOSIS_COUNT` is already in `app/core/tasks.py`; WP-6.3's `hotspot_select` record is the pattern for a non-model record.

## Contract changes (update `docs/contracts/mitosis_v6.md` in this PR)

- `Candidate.in_tumor: boolean | null`. Null means the tumour-cell gate did not run (`mitosis.tumor_gate: false`), and the candidate is eligible.
- `Hpf.tumor_fraction: number | null`, null until the tumour mask exists (WP-6.2 / 7.6b).
- `MitosisSummary.mitotic_score: 1 | 2 | 3 | null`, null when `n_hpf = 0`. This was a STATUS open item.
- The `p_a` comment reads "detector probability" (not "calibrated"); calibration is deferred (D19).

## Acceptance (run these)

```powershell
python -m pytest backend/tests/test_mitosis_v6_contract.py backend/tests/test_mitosis_worker.py backend/tests/test_mitosis_api.py backend/tests/test_nms.py backend/tests/test_hpf.py -q -p no:cacheprovider
python -m pytest backend/tests -q -p no:cacheprovider            # full suite: milestone end
python -m pytest tools/tests -q -p no:cacheprovider
alembic -c backend/alembic.ini upgrade head                       # against a scratch Postgres if available
```

Contract test: `GET` returns a payload that validates against the TS types of `docs/contracts/mitosis_v6.md` (a Pydantic mirror), the mock fixture example included.

## Out of scope — do not do

- The tumour-cell gate, HPF tumour fraction and the "disk ⊂ hotspot window" constraint (WP-7.6b).
- Classifier B, referee v2, `p_A` calibration and any τ or NMS tuning (deferred, D19). `det_threshold` stays 0.75 and `nms_radius_um` 7.5.
- Frontend changes (WP-7.7b).

## Owner decision to raise in the PR

Detections written before 2026-09-29 came from the transposed-coordinate detector (plan §2.2). Re-run Stage 4 for open cases after deploying? The migration preserves pathologist rows either way.

## Done checklist

- [ ] `0016` migrates and maps existing rows; the v5 columns are gone
- [ ] The worker persists v6 columns for the baseline; contract crops are written
- [ ] One NMS by `p_b ?? p_a`; dividing-cell test
- [ ] HPF density from `counted`, no overlap (property test), `hpf_count_lt_10`
- [ ] `mitosis_v6` routes; v5 routes deleted; equivocal confirm gate; recompute only in `scoring.py` (grep test)
- [ ] Grading reads `counted`; `mitosis_count` DecisionRecord
- [ ] Contract file updated with the four changes
- [ ] `docs/STATUS.md` updated; owner told that WP-7.7b can start
