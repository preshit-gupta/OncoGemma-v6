# WP-8.8 — Histologic type from evidence: per-patch votes, IDC-NST default, no consensus is not a guess

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | M | SPEC-07 §7.3 (arm H1), SPEC-01 §3.5, §3.9 | WP-8.6 (merged, #49) | Claude (`backend/**`, `configs/**`, `docs/contracts/grading_v6.md`) |

## Goal

Owner report, 2026-10-05, case `7bf347e5`: a TCGA slide that is IDC was proposed as **ILC** in Stage 5. Other high-grade IDC slides were typed correctly, so this is a weakness in how the type is proposed, not a systemic failure.

What the case's records and images show (read-only pull, 2026-10-05):

1. **The model described features that are not in the images.** The six patches it saw (`grading/tubule/t_01`–`t_06`, 512 µm @ 1.0 µm/px) are solid sheets of cohesive cells with large round nuclei, divided by thin septa. The answer said "discohesive… single files… targetoid", at temperature 0.
2. **The prompt makes ILC the easy answer.** `histologic_type@v1.md` lists ILC's features and asks for a type. A tumour with no tubules (T = 3) and no other cue drifts to ILC. It also tells the model the images are "Top 8 high-confidence tumor patches from the active tumor front", which is false, and its list of types omits two the schema allows (`mixed_ductal_lobular`, `micropapillary`).
3. **One call over six arbitrary patches.** The patches are the first six tubule samples, so they are not spread across the tumour. A single call has no consistency check.
4. **The doubt is thrown away.** The prompt asks for `confidence` and `differential`, but `HistotypeVerdict` has neither, and the stored raw output is just `{type, rationale}`. Nobody can see how unsure the call was.

This card changes how the proposal is made. It is still a proposal: the pathologist confirms or overrides it (unchanged).

## Read first (only these)

- `docs/specs/07-stage5-nottingham-grading.md` §7.3 (H1 is "multi-sample", strict, no default)
- `backend/worker/grading.py` (the `jobs` list and the `histotype` block), `backend/pipeline/grading.py`
- `backend/app/inference/schemas.py` (`HistotypeVerdict`), `configs/scoring.yaml` (`grading.estimators`), `configs/prompts/histologic_type@v1.md`
- `docs/contracts/grading_v6.md` (`histotype`)

## Files you may touch

- `backend/app/inference/schemas.py` (new `HistotypePatchVerdict`; `HistotypeVerdict` itself does not change)
- `backend/app/core/pipeline_config.py`, `configs/scoring.yaml`
- **Create** `configs/prompts/histologic_type@v2.md` (v1 stays on disk: records refer to it)
- `backend/worker/grading.py`, `backend/pipeline/grading.py`, `backend/app/routers/grading.py`
- `docs/contracts/grading_v6.md`, `frontend/lib/mock/grading.json` (the fixture gains the new fields; the contract test validates it)
- Tests: **create** `backend/tests/test_histotype_votes.py`; adjust `backend/tests/test_grading_worker.py`, `backend/tests/test_grading_api.py`, `backend/tests/test_fallback_paths_eval.py`, `backend/tests/test_grading_v6_contract.py`; `backend/tests/inference/test_strict_schemas.py` and `backend/tests/core/test_pipeline_config.py` (additions only)
- `docs/tasks/README.md`, `docs/STATUS.md`

## Tasks

1. **Prompt `histologic_type@v2.md`, one patch per call.**
   - Describe the input honestly: one 512 µm square at 1.0 µm/px from inside the tumour, one of several. No claim that it is a top or tumour-front patch.
   - **IDC-NST is the default.** A special type needs its defining feature to be clearly visible *in this image*.
   - State that missing tubules is not evidence for ILC (a solid grade 3 IDC has none), and that ILC needs discohesive cells in single files or a targetoid pattern.
   - List all nine types the schema allows.
   - Say what resolution allows: at 1.0 µm/px cohesion can be hard to judge, and then `cohesion` and `architecture` are `not_assessable`, never guessed.
2. **Schema.** `HistotypePatchVerdict(HistotypeVerdict)` adds required `architecture` (`solid_sheets`, `cohesive_nests`, `glands_or_tubules`, `trabeculae_or_cords`, `single_files`, `targetoid`, `papillary`, `mucin_pools`, `other`, `not_assessable`), `cohesion` (`cohesive`, `discohesive`, `mixed`, `not_assessable`) and `confidence` (`low`, `medium`, `high`). Strict, no extra fields, values normalised like the other schemas. `HistotypeVerdict` stays `{type, rationale}` so the WP-2.4 tests and the router's list of valid types are untouched. The model's `confidence` is recorded for the reviewer and **never used in any computation** (SPEC-01 §3.5: uncalibrated).
3. **Patches.** `scoring.yaml grading.estimators.histotype_images` (6) now counts patches **voted on**, one call each (`Task.HISTOTYPE`, entity `PATCH`, the sample id). They are spread evenly over the tubule samples in stratum order (`pipeline/grading.py::spread_indices`: index `i * n_available // n`), because the strata are spatial clusters. They are not the first N. The existing check that the count does not exceed `tubule_patches` stays.
4. **Aggregation** `pipeline/grading.py::aggregate_histotype(votes, min_agreement)`, deterministic, no model:
   - Votes are the patches whose call succeeded. `n_votes = 0` → no proposal (`type: null`, as today for an outage).
   - The winning type is the **strict plurality**. A tie for first → `type: null`.
   - `agreement = votes for the winner / n_votes`. Below `scoring.yaml grading.estimators.histotype_min_agreement` → `type: null`.
   - `type: null` with votes present means "the patches disagree"; it is never replaced by IDC-NST or any value (SPEC-01 §3.9). The grading gets `needs_human` (existing flag), `gradings.histologic_type` is null and the pathologist chooses.
   - Config: `histotype_min_agreement: 0.66` (4 of 6). Validated as `0 < x <= 1`. **It is an unvalidated default**; WP-8.7 and the Thennavan labels (S5-HT) tune it, and the owner may change it.
5. **Worker.** Run the per-patch jobs on the existing thread pool. A failed patch call follows the existing rule: a clinical `fallbacks.yaml` entry for `histotype` makes it a missing vote (its fallback DecisionRecord is written), otherwise the stage fails; EVAL runs always fail loud. `machine["histotype"]` is `null` with no votes, else:
   ```
   { type, agreement, n_votes, n_requested, counts: {<type>: n}, rationale,
     votes: [{ sample_id, type, architecture, cohesion, confidence, rationale, record_id } …] }
   ```
   `rationale` is the rationale of the earliest winning patch (the model's confidence is never used), `""` when `type` is null. A failed patch call appears in `votes` with `type: null` and its `record_id`. `record_id` (single) is replaced by the per-vote ids. `histotype_estimator` keeps its label (`H1:<producer>@histologic_type@v2`).
6. **API.** `GET` serves `histotype.agreement`, `votes` and `n_requested` (contract below). A grading written before this card has no votes: it serves `agreement: null`, `votes: []`, `n_requested: 0`, never invented values.

## Interfaces / contract

```ts
histotype: {
  type: string | null; estimator: string; rationale: string; confirmed: boolean; confirmed_by: string | null;
  agreement: number | null;      // winner's share of the successful votes; null with no votes
  n_requested: number;           // patches asked (0 for a grading made before WP-8.8)
  votes: HistotypeVote[];        // [] before WP-8.8
}
interface HistotypeVote {
  sample_id: string;             // a tubule sample id (t_xx)
  type: string | null;           // null = the call failed
  architecture: string | null; cohesion: string | null; confidence: "low" | "medium" | "high" | null;
  rationale: string | null;
}
```

UI rule (added to the contract; the UI itself is a separate card): when `type` is null and `votes` is non-empty, say the patches disagree and show the votes; never pre-select a type. When `agreement` is below 1, show it ("4 of 6 patches"). No model name appears (AGENTS rule 6).

## Existing tests whose assertions change (authorised by this card)

The single multi-image call becomes one call per patch, so these pre-existing assertions can no longer hold; they are changed in the same PR, each explained there:

- `test_grading_worker.py::test_grading_estimates_come_from_the_gateway…`: one histotype row → `HISTOTYPE_IMAGES` rows, each with one image and entity type `patch`; prompt `histologic_type@v2.md`; `machine["histotype"]["record_id"]` → per-vote `record_id`s. `ANSWERS` gets a `HistotypePatchVerdict` answer in place of the `HistotypeVerdict` one.
- `test_grading_worker.py::test_allowed_outages…`: the fallback records for `histotype` are one per patch.
- `test_fallback_paths_eval.py` row `worker/grading.py:519-528`: the injected failure selects `HistotypePatchVerdict` (the schema the worker now asks for).
- `test_grading_api.py::test_get_aggregates_from_machine_output`: the served `histotype` gains `agreement: null`, `n_requested: 0`, `votes: []` (its machine output predates this card, which is the "served empty" case).
- `test_grading_v6_contract.py`: the `Histotype` contract model gains the new fields (the contract example and the mock fixture gain them too).

No other assertion changes. `test_strict_schemas.py` and `test_schema_properties.py` keep covering `HistotypeVerdict` unchanged.

## Acceptance (run these)

```powershell
python -m pytest backend/tests/test_histotype_votes.py backend/tests/test_grading_worker.py backend/tests/test_grading_v6_contract.py backend/tests/test_fallback_paths_eval.py backend/tests/inference backend/tests/core/test_pipeline_config.py -q -p no:cacheprovider
python -m pytest backend/tests -q -p no:cacheprovider
```

Tests that must exist (`test_histotype_votes.py` unless noted):
- aggregation: unanimous, 4–2 with `min_agreement` 0.66 passes, 3–3 tie gives `null`, 3–2–1 below the threshold gives `null`, all failed gives no proposal; the confidence value changes no result (property test);
- `spread_indices` returns `n` distinct, increasing, in-range indices that include 0, for every `n ≤ n_available` (Hypothesis);
- worker: the votes are spread patches (not the first N), one call each with one image; a failed patch is a missing vote and the stage still completes when `fallbacks.yaml` allows it; split votes leave `histologic_type` null and `needs_human` set, with no default;
- the stored v1-style record (no votes) is served with `votes: []`;
- schema: missing `architecture`/`cohesion`/`confidence` is refused, an invented extra field is refused (`test_strict_schemas.py`, additions);
- config: `histotype_min_agreement` of 0, above 1, or missing is refused (`test_pipeline_config.py`, additions).

## Out of scope — do not do

- Frontend. The Stage 5 panel does not show votes or agreement until a UI card (proposed WP-8.9, lane B) builds against the contract above. The new fields are additive, so the current UI keeps working.
- Higher-resolution histotype inputs (a different sample geometry), a second model as a check, per-type thresholds, any use of the model's confidence.
- Tuning against labels. That is WP-8.7 and the S5-HT labels; this card sets defaults only.
- Mitosis (a separate discussion with the pathologist's annotations).
- Re-running existing cases: the owner re-runs grading after the deploy.

## Done checklist

- [ ] `histologic_type@v2.md`; `HistotypePatchVerdict`; `histotype_images` / `histotype_min_agreement` in config
- [ ] Per-patch votes in the worker, deterministic aggregation, no default on disagreement
- [ ] API and contract serve `agreement`, `votes`, `n_requested`; old gradings serve empty values
- [ ] The listed existing assertions updated with a reason; no other test changed
- [ ] Full suite passes
- [ ] `docs/STATUS.md`: re-run grading for open cases; the 0.66 default and the missing UI are noted
