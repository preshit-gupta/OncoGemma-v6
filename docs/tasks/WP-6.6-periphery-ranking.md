# WP-6.6 — HPF sites at the tumour periphery first

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | M | SPEC-05 §5.2 (new arm H1P, D22) | WP-6.5 | Claude (`backend/**`, `configs/**`) |

## Goal

Nottingham practice counts mitoses at the periphery of the tumour, its invasive front. The owner asks that the 10 HPF sites lie there **preferably**. Sites near the invasive front rank first. When the front cannot hold 10 sites, interior sites fill the rest, so the HPF count stays at 10 wherever the tissue allows. This card fixes owner review issue S3-2 (plan §2.4).

**Owner decision (2026-10-05, D22):**
- The periphery band is **1 mm** wide, measured inward from the invasive front.
- Tumour edges that end at the section border (glass) are not the invasive front.
- This is the owner's clinical choice. It is not an arm selected by SPEC-05 §5.2's F1_M criterion. WP-8.7 measures the pipeline with it.

## Read first (only these)

- `docs/IMPLEMENTATION_PLAN.md` §2.4
- `docs/specs/05-stage3-triage-hotspots.md` §5.2–5.3
- `backend/pipeline/hotspots_v6.py` (after WP-6.5) and `backend/worker/triage.py` (where `is_tumor_raster`, `p_raster` and the tissue mask are built)

## Files you may touch

- `configs/specimen_profiles.yaml`, `backend/app/core/pipeline_config.py` (`ranking_arm` gains `H1P`; `periphery_band_um`)
- `backend/pipeline/hotspots_v6.py`, **create** `backend/pipeline/tumor_front.py`
- `backend/worker/triage.py`
- `docs/contracts/triage_v6.md` (`ScoreKind`, two hotspot fields)
- Tests: **create** `backend/tests/test_tumor_front.py`; extend `backend/tests/test_hpf_sites.py`

## Tasks

1. **Invasive front** (`pipeline/tumor_front.py`), on the triage tile raster.
   - **Section:** the tissue raster with holes filled (`scipy.ndimage.binary_fill_holes`). Fat and lumina inside the section count as inside; positions outside it are glass.
   - **Non-tumour tissue:** positions inside the section that are not tumour.
   - **`front_distance_um`** for each position: the Euclidean distance transform to the nearest non-tumour tissue position (`distance_transform_edt`, sampling = tile µm).
   - A tumour edge that only meets glass is not front, so the tumour beside it gets a large distance.
   - A section with no non-tumour tissue has no front: every distance is `inf`.
2. **Arm H1P** (`hotspots_v6.py`).
   - A site is `at_periphery` when the front distance at its centre is ≤ `periphery_band_um`. Interpolate bilinearly between tile centres.
   - Rank key: `(at_periphery, mean p_tumor_cal over the circle, tumour fraction)`, descending.
   - The greedy selection, the circle non-overlap rule and the referee loop are unchanged (WP-6.5).
   - Model sites use `score_kind: "periphery_then_tumor"`. Every site carries `at_periphery` and `front_distance_um`. Both are null for pinned sites, and `front_distance_um` is null when the slide has no front.
   - Record in the triage output how many selected sites are at the periphery.
3. **Config.**
   - Both profiles: `ranking_arm: H1P` and `periphery_band_um: 1000.0`, commented "owner, 2026-10-05, D22".
   - `H1` stays selectable for eval ablations.
4. **Contract** (`triage_v6.md`):
   - `ScoreKind` gains `"periphery_then_tumor"`;
   - `Hotspot` gains `at_periphery: boolean | null` and `front_distance_um: number | null`.

## Acceptance (run these)

```powershell
python -m pytest backend/tests/test_tumor_front.py backend/tests/test_hpf_sites.py backend/tests/test_hotspots_v6.py -q -p no:cacheprovider
python -m pytest backend/tests -q -p no:cacheprovider
```

Tests that must exist (synthetic rasters):
- **Round tumour in stroma** (radius 4 mm), with `p_tumor_cal` highest in the core so that H1 alone would pick the core. All 10 selected centres are within 1 mm of the tumour edge, and none is in the core.
- **Glass edge.** The tumour reaches the glass on its left side and meets stroma on its right. No site is selected along the glass edge while the stroma side has room.
- **Narrow front.** The band holds 6 sites. Ranks 1–6 are `at_periphery`, ranks 7–10 are interior, and 10 sites are selected.
- **No front.** The whole section is tumour. Every `at_periphery` is false, the order equals H1, and the selection is unchanged.
- **Determinism.** The same inputs give the same sites. `H1` gives exactly the WP-6.5 result.

## Out of scope — do not do

- Drawing the front or band on the heatmap. A follow-up, if the owner wants it.
- Mitotic prescan arms H2/H3. The DCIS edge is also out: the tumour head has no in-situ class.
- Tuning the band on data.

## Done checklist

- [ ] `tumor_front.py` with glass edges excluded
- [ ] H1P ranking in production; `at_periphery`, `front_distance_um` served
- [ ] Contract updated; tests above pass; full suite passes
- [ ] `docs/STATUS.md` updated
