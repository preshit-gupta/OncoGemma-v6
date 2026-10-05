# WP-6.5 — HPF sites: 0.5 mm circles in padded 0.6 mm frames

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | L | SPEC-05 §5.1, 5.3, 5.5; SPEC-06 §5.8 (both amended by D22) | — | Claude (`backend/**`, `configs/**`) |

## Goal

An HPF is a **circle 0.5 mm across** (0.196 mm²; 10 HPFs = 1.96 mm²). Its hotspot is the **0.6 mm square** around it, which adds 0.05 mm of padding on each side so that a figure on the circle's edge is seen whole. Stage 3 selects HPF sites. Stage 4 counts only inside those circles. The pathologist pins sites, and a site that is pinned is used. When fewer than 10 sites fit, the case says that the tissue is inadequate.

This card fixes owner review issues S3-1, S3-3 and S3-4 in the backend (plan §2.4).

**Owner decisions, 2026-10-05 (D22):**
- Circles never overlap. One site's padding may overlap a neighbouring site's padding. A figure in another site's padding is shown, and it is counted only in the circle that contains it.
- Fewer than 10 sites: the mitotic score is still computed per mm² over the circles examined. Stages 3–5 say that the tissue is inadequate, and the pathologist acknowledges it when confirming Stage 3.

## Findings this card fixes (verified on `main`, c7e448c)

1. **Selection uses the square, not the circle.**
   - Stage 3 qualifies 600 µm squares: tissue ≥ 0.70 and tumour ≥ 0.50 over the whole square, and no two squares may overlap (Chebyshev rule, `pipeline/hotspots_v6.py::select_hotspots`).
   - Stage 4 then fits a 524 µm disk (`mitosis.yaml hpf.radius_um` 262) inside each square.
   - So each HPF uses 0.36 mm² of qualifying tissue. Ten HPFs use 3.6 mm², the area of 18 HPFs, and small sections yield fewer than 10.
2. **The circle drifts towards figures.** Inside its window, `pipeline/hpf.py::place_hpfs` moves the disk centre on a 16 µm lattice to the position with the most counted figures. This biases the count upwards.
3. **The HPF order is arbitrary.**
   - `worker/mitosis.py` and `routers/mitosis.py::replace_hpfs` order hotspots by `prob_mean`. v6 hotspots have no `prob_mean`, so every priority is 0 and the order is the database row order.
   - With 10 model windows, a pinned ROI comes 11th and gets no HPF.
4. **Pin ROI** (`routers/triage.py`):
   - (a) Each `POST /edits` **replaces** `review_edits` with that request's ops, but the client sends one op per request. Every edit therefore discards the earlier ones.
   - (b) Added ROIs get v5 fields (`area_mm2`, `prob_mean`) and the id `user_roi_01` every time. The contract says `hs_u_<n>`.
   - (c) A 600 µm pin square next to tightly packed model windows (gap 0) overlaps one of them and returns 422.
   - (d) A free polygon defines no HPF.
   - (e) "Restore" sends `modify` with the same polygon, which never clears `excluded`.
5. **No tissue-inadequacy gate.** Stage 3 can be confirmed with any number of active hotspots. `hpf_count_lt_10` appears only in Stage 4, and Stage 5 (`grading_v6.mitotic`) carries no flag.

## Read first (only these)

- `docs/IMPLEMENTATION_PLAN.md` §2.4 (this work and D22)
- `docs/specs/05-stage3-triage-hotspots.md` §5; `docs/specs/06-stage4-mitosis.md` §5.8
- `docs/contracts/triage_v6.md`, `mitosis_v6.md` (Hpf, Candidate, endpoints), `grading_v6.md` (`mitotic`)
- `backend/pipeline/hotspots_v6.py`, `backend/pipeline/hpf.py`, `backend/pipeline/mitosis_gate.py` (`tumor_fraction_in_disk`), `backend/pipeline/tissue_mask.py` (`fraction_in_disk_um`)
- `backend/worker/triage.py` (selection call site), `backend/worker/mitosis.py` (hotspot load, `place_hpfs` call, review images)
- `backend/app/routers/triage.py` (`apply_edit_ops`, `save_triage_edits`), `backend/app/core/geometry.py`, `backend/app/services/stages.py::_confirm_triage`, `backend/eval/harness/controller.py::confirm`

## Files you may touch

- `configs/specimen_profiles.yaml`, `configs/mitosis.yaml`, `backend/app/core/pipeline_config.py`
- `backend/pipeline/hotspots_v6.py`, `backend/pipeline/hpf.py`, `backend/pipeline/scoring.py`, `backend/pipeline/tissue_mask.py` (vectorised disk fractions only)
- `backend/worker/triage.py`, `backend/worker/mitosis.py`, `backend/worker/grading.py` (pass the mitotic flags only)
- `backend/app/routers/triage.py`, `backend/app/routers/mitosis.py`, `backend/app/routers/grading.py` (mitotic flags only), `backend/app/core/geometry.py`, `backend/app/services/stages.py`, `backend/app/models/hotspot.py`, `backend/app/main.py` (error handlers)
- **Create** `backend/alembic/versions/0017_hpf_sites.py`
- `backend/eval/harness/controller.py` (the triage confirm option)
- `docs/contracts/triage_v6.md`, `mitosis_v6.md`, `grading_v6.md`
- Tests: `backend/tests/test_hotspots_v6.py`, `test_hpf.py`, `test_triage_api_v6.py`, `test_mitosis_worker.py`, `test_mitosis_api.py`, `test_mitosis_v6_contract.py`, `test_scoring.py`, plus a new `backend/tests/test_hpf_sites.py`. You may update **existing** tests whose assertions encode the old geometry (r 262, squares, polygon edits). List each one in the PR with the reason.

## Tasks

1. **Config.**
   - In `specimen_profiles.<type>.hotspots` (both profiles), replace `window_um` with:
     - `hpf_diameter_um: 500.0`
     - `frame_padding_um: 50.0`; the frame side is derived, `d + 2·padding` = 600 µm
     - `lattice_step_um: 125.0`, which is d/4
   - `min_tissue_fraction` and `min_tumor_fraction` are now measured **over the circle**.
   - `gap_um` is now the minimum distance between circle edges (stays 0).
   - `k_max` stays 10 and is the only HPF target. Delete `mitosis.yaml hpf.radius_um`, `count`, `centre_step_um` and `min_separation_um`, and their validator.
   - `hpf.review_field_um` becomes the frame side, derived. Keep `review_px`.
2. **Stage 3 site selection** (`hotspots_v6.py`, `worker/triage.py`).
   - Candidate sites are lattice centres over the tumour-mask bounding box.
   - A site is valid when:
     - its circle is inside the slide;
     - the circle's tissue fraction (tissue mask) is ≥ `min_tissue_fraction`;
     - the circle's tumour fraction (area-weighted share of tumour tiles in the undilated mask, as SPEC-05 §5.1 uses today) is ≥ `min_tumor_fraction`.
   - H1 ranks sites by the mean `p_tumor_cal` over the circle (area-weighted).
   - Greedy selection keeps a site when its Euclidean centre distance to every selected site is ≥ `d + gap`. The tumour referee loop is unchanged.
   - Compute the disk fractions vectorised, for example by convolving with a disk kernel. On sampled centres they must agree with `TissueMask.fraction_in_disk_um` and `TumorGate.tumor_fraction_in_disk` (mask only) to 1e-3.
   - Each output hotspot carries:
     - `center_um`, `hpf_diameter_um`;
     - `polygon_um`: the frame, a closed square ring;
     - `window_um`: the frame side;
     - `tissue_fraction`, `tumor_fraction`, `rank`, `rank_score`, `score_kind`, `source`, `excluded`, `exclude_reason`;
     - `area_mm2`: the circle's area.
   - The output also carries `hpf_target` (k_max) and `n_sites_available`. `hotspots_limited_by_tissue` is unchanged.
3. **Pathologist edits** (`routers/triage.py`, `core/geometry.py`). Replace the edit grammar with HPF-site ops:
   - `{op:"add", center_um}`
   - `{op:"move", id, center_um}`
   - `{op:"exclude", id, reason}`
   - `{op:"restore", id}`
   - `{op:"delete", id}`

   `modify` and `polygon_um` are removed. Rules:
   - `review_edits` **accumulates**: each request appends its ops, as the mitosis and grading routers do.
   - Pinned ids are `hs_u_<n>`, numbered over the whole edit history and stable across requests.
   - The server builds each frame from `center_um`.
   - The circle must lie inside the slide. Otherwise answer `422 {error:"invalid_site", id, reason:"out_of_bounds"}`.
   - Active circles must not overlap. Otherwise answer `422 {error:"hotspot_overlap", ids}`. Frames may overlap.
   - The number of active sites may not exceed `hpf_target`. Otherwise answer `422 {error:"too_many_sites", hpf_target}`.
   - A pinned site has no tissue or tumour minimum, because that is the pathologist's call. Its `tissue_fraction`, `tumor_fraction` and `rank` are null.
   - Stored v6.0 edits (polygon ops) are not converted. A triage output or edit list without `center_um` answers `409 {error:"triage_rerun_required"}`. The owner re-runs Stage 3 for open cases.
4. **Confirm gate** (`services/stages.py`, `routers/triage.py`).
   - The confirm body gains `accept_fewer_hpfs: boolean` (default false).
   - With fewer than `hpf_target` active sites and no acceptance, answer `409 {error:"hpf_sites_lt_10", n_active, hpf_target}`.
   - Persist the acceptance in the `stage_confirmed` audit payload and on the execution's output.
   - The harness passes `accept_fewer_hpfs=True` exactly when the effective active count is below the target. Its prediction already records the number of active hotspots.
   - Migration `0017_hpf_sites` adds `hotspots.center_um` (JSON), `hotspots.hpf_diameter_um` (REAL) and `hotspots.tissue_fraction` (REAL), all nullable for old rows.
5. **Stage 4 uses the confirmed circles** (`hpf.py`, `worker/mitosis.py`, `routers/mitosis.py`).
   - HPF *i* is the circle of active site *i*: same centre, radius `d/2`. There is no search inside the window and no count-based shift.
   - `seq` follows the model sites by rank, then the pinned sites in id order.
   - Delete `window_centres`, the `prob_mean` ordering and **`POST /replace-hpfs`**: route, contract row and tests. Sites change only in Stage 3. WP-7.10 removes the button, so deploy the two together.
   - A confirmed site without `center_um` raises a clear error and is not guessed.
   - Per-HPF `tissue_coverage` and `tumor_fraction` are still computed and stored for audit.
   - Each `Candidate` gains `hpf_seq: number | null`: the circle that contains it, computed server-side, null outside every circle.
   - Review images cover the frame (600 µm) centred on the site.
6. **Scoring** (`scoring.py`). No formula change. Add a test: 10 circles of Ø 500 µm give 1.963 mm², and with the per-mm² thresholds the counts 7 | 8 and 14 | 15 fall either side of the score 1 | 2 and 2 | 3 boundaries. This is the Elston–Ellis field-diameter table row for 0.50 mm (≤ 7, 8–14, ≥ 15), which the owner confirms.
7. **Stage 4 and 5 fields.**
   - Each `Hpf` names its `hotspot_id` and carries its `frame_um`.
   - `MitosisSummary` and `grading_v6.mitotic` carry `hpf_target`.
   - `grading_v6.mitotic` gains `flags` (`hpf_count_lt_10`), copied from `summarize_stage4`.
   - The UI then never hardcodes 10, 500 or 600.
8. **Contracts.** Apply the interface below to the three contract files and their mock fixtures' documentation. The fixtures themselves change in WP-6.7 and WP-7.10.

## Interfaces / contract

```ts
// triage_v6
interface Hotspot {            // an HPF site
  id: string;                  // "hs_01".. model (by rank), "hs_u_<n>" pinned
  center_um: [number, number];
  hpf_diameter_um: number;     // 500
  polygon_um: [number, number][]; // padded frame: square of side window_um centred on center_um, closed ring
  window_um: number;           // frame side (600)
  rank: number | null; rank_score: number | null; score_kind: ScoreKind | null;
  tissue_fraction: number | null; tumor_fraction: number | null;   // over the circle; null when pinned
  prescan_expected: number | null;
  source: "model" | "pathologist_added" | "pathologist_modified";  // modified = moved
  excluded: boolean; exclude_reason: string | null;
}
TriageStageV6 += { hpf_target: number; n_sites_available: number }
EditOp = {op:"add", center_um} | {op:"move", id, center_um} | {op:"exclude", id, reason} | {op:"restore", id} | {op:"delete", id}
POST /confirm body += { accept_fewer_hpfs?: boolean }   // 409 {error:"hpf_sites_lt_10", n_active, hpf_target}
// mitosis_v6
Candidate += { hpf_seq: number | null }
Hpf += { hotspot_id: string; frame_um: [number, number][] }   // the site it came from; its padded frame (closed ring)
MitosisSummary += { hpf_target: number }
DELETE route: POST /api/v1/stages/mitosis/replace-hpfs
// grading_v6
mitotic += { flags: ("hpf_count_lt_10")[]; hpf_target: number }
```

## Acceptance (run these)

```powershell
python -m pytest backend/tests/test_hpf_sites.py backend/tests/test_hpf.py backend/tests/test_triage_api_v6.py backend/tests/test_mitosis_worker.py -q -p no:cacheprovider
python -m pytest backend/tests -q -p no:cacheprovider
python tools/check_image_imports.py ops/docker/Dockerfile.api app.main
```

Tests that must exist:
- **Small tissue.** A tumour strip 3 tiles (672 µm) wide and 25 tiles (5,600 µm) long, all tissue, yields 10 sites. The old 600 µm-square rule fits at most 9 there: a square can extend at most 30% past the strip, so ten squares would need 6,000 µm of a 5,960 µm span.
- **Disjoint circles.** A Hypothesis test over random masks: selected circles never overlap, frames may overlap, and every model circle meets both fractions.
- **Pinning.** Pinning two sites in two requests keeps both. Ids are `hs_u_1` and `hs_u_2`. Restore clears `excluded`. A pin whose circle overlaps an active circle gets 422 `hotspot_overlap`, but a pin whose *frame* overlaps does not. An 11th active site gets 422 `too_many_sites`.
- **Confirm gate.** Confirming 9 active sites without acceptance gets 409 `hpf_sites_lt_10`. With acceptance it succeeds, and Stage 4 reports `n_hpf` 9 and `hpf_count_lt_10`.
- **Fixed circles.** Stage 4 HPF centres equal the confirmed site centres, whatever the figure positions. A pinned site always becomes an HPF.
- **`hpf_seq`.** A counted candidate outside every circle has `hpf_seq` null and is not in `count_total`.
- **Grading tests.** They pass unchanged with overlapping frames: `candidate_cells` assigns each point to one frame.

## Out of scope — do not do

- Periphery ranking (WP-6.6); frontend (WP-6.7, WP-7.10); mitosis descriptions (WP-7.9).
- Changing the score thresholds, the detector, τ, NMS or the tumour gate.
- Deleting `triage.yaml hotspot_extraction` or `pipeline/hotspots.py` (separate clean-up).

## Done checklist

- [ ] Circles of Ø 500 µm in 600 µm frames; selection, overlap and fractions on the circle
- [ ] Stage 4 counts in the confirmed circles; `replace-hpfs` deleted; `hpf_seq` served
- [ ] Edits accumulate; pin/move/restore work; confirm gate with `accept_fewer_hpfs`
- [ ] Contracts updated (triage, mitosis, grading); migration `0017`
- [ ] Full suite passes; image import check passes
- [ ] `docs/STATUS.md`: owner re-runs Stage 3 and 4 (and grading) for open cases after deploy; deploy together with WP-7.10
