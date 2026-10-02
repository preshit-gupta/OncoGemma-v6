# WP-7.6b — Tumour-cell gate and HPF tumour constraints

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | M | SPEC-06 §5.5, §5.8 | WP-6.2 (#41) and WP-6.3 (#42), both merged; WP-7.6a | Claude (`backend/worker/**`, `backend/pipeline/**`, `configs/**`) |

## Goal

Count only mitoses in invasive tumour cells, and place HPFs in tumour (SPEC-06 §5.5, §5.8). Both need the SPEC-05 tumour mask, which WP-6.2 now writes per slide (`tiles.parquet` with `p_tumor_cal`, `tumor_mask.png/json`, threshold `tumor_head.threshold` 0.5; `tumor_head@1.0.0`). The detector itself does not change (D19).

**Two facts from WP-6 that shape this card:**
- `tumor_head@1.0.0` predicts invasive / stroma / inflammatory / necrosis only. BCSS had 1 DCIS tile, so there is no trained in-situ class, and SPEC-06 §5.5's `argmax ≠ in_situ` cannot exclude anything yet. Implement the gate as "in the invasive tumour mask, dilated by one tile". Record in the stage output that in-situ exclusion is not available.
- Hotspots are now 600 µm square windows (`specimen_profiles.<type>.hotspots`, `k_max` 10, WP-6.3). An HPF disk (r 262 µm, 524 µm across) fits inside a window only if its centre is within 38 µm of the window centre, so "disk ⊂ a hotspot window" gives at most one HPF per window. **Ask the owner** whether that is intended before implementing it. The alternative is disk ⊂ the union of windows.

Classifier B, the referee and the Stage-A cache are deferred to the next iteration (D19).

## Read first (only these)

- `docs/specs/06-stage4-mitosis.md` §5.5, §5.8
- `docs/specs/05-stage3-triage-hotspots.md` §4.3 and §5 (tumour mask and windows); `backend/pipeline/hotspots_v6.py` (how the mask is read)
- `backend/worker/mitosis.py`, `backend/pipeline/hpf.py::greedy_place_hpfs`

## Files you may touch

- `backend/worker/mitosis.py`, `backend/pipeline/hpf.py`
- **Create** `backend/pipeline/mitosis_gate.py`
- `backend/app/core/pipeline_config.py`, `configs/mitosis.yaml`: `tumor_gate`, HPF `min_tumor_fraction`
- `docs/contracts/mitosis_v6.md`: make `in_tumor` and `tumor_fraction` non-null again once the gate always runs
- Tests: `backend/tests/test_mitosis_gate.py`, `test_mitosis_worker.py`, `test_hpf.py`

## Tasks

1. **Gate** (§5.5). A candidate is eligible only if its tile in the tumour mask (`p_tumor_cal ≥ tumor_head.threshold`), dilated by 1 tile (224 µm), is tumour. The `≠ in_situ` clause waits for a model with an in-situ class (see above). Read the mask the way `hotspots_v6.py` does; do not reimplement thresholds.

   `in_tumor` is set for every candidate when `tumor_gate: true`. A pathologist's `review_label = 'mitosis'` still counts.
2. **HPFs** (§5.8). Each centre must satisfy: disk inside the hotspot geometry the owner confirms (see above), tissue coverage ≥ 0.70, tumour fraction ≥ 0.50 (both from the profile's existing values). Report `tumor_fraction` per HPF.
3. **Check on real slides.** After the WP-6.x deployment, run the gate on and off on a few TCGA slides with the owner, and record the count difference in `docs/STATUS.md`.

## Acceptance (run these)

```powershell
python -m pytest backend/tests/test_mitosis_gate.py backend/tests/test_mitosis_worker.py backend/tests/test_hpf.py -q -p no:cacheprovider
python -m pytest backend/tests -q -p no:cacheprovider
```

Tests:
- a candidate in stroma or in situ is not counted, and is counted once a pathologist sets `review_label = 'mitosis'`;
- an HPF is never placed with tumour fraction < 0.5.

## Out of scope — do not do

Everything deferred by D19: classifier B, the referee, calibration, threshold tuning, the Stage-A cache.

## Done checklist

- [ ] Gate on in production; contract nullability tightened
- [ ] HPF tumour constraints
- [ ] Gate on/off difference recorded on real slides
- [ ] `docs/STATUS.md` updated
