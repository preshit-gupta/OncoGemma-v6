# WP-7.6b — Tumour-cell gate and HPF tumour constraints

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | M | SPEC-06 §5.5, §5.8 | WP-6.2 (tumour mask), WP-7.6a | Claude (`backend/worker/**`, `backend/pipeline/**`, `configs/**`) |

## Goal

Count only mitoses in invasive tumour cells, and place HPFs in tumour (SPEC-06 §5.5, §5.8). Both need the SPEC-05 tumour mask that WP-6.2 produces. **Blocked until WP-6.2 exists.** The detector itself does not change (D19).

Classifier B, the referee and the Stage-A cache are deferred to the next iteration (D19).

## Read first (only these)

- `docs/specs/06-stage4-mitosis.md` §5.5, §5.8
- `docs/specs/05-stage3-triage-hotspots.md`: the tumour mask section (WP-6.2's output)
- `backend/worker/mitosis.py`, `backend/pipeline/hpf.py::greedy_place_hpfs`

## Files you may touch

- `backend/worker/mitosis.py`, `backend/pipeline/hpf.py`
- **Create** `backend/pipeline/mitosis_gate.py`
- `backend/app/core/pipeline_config.py`, `configs/mitosis.yaml`: `tumor_gate`, HPF `min_tumor_fraction`
- `docs/contracts/mitosis_v6.md`: make `in_tumor` and `tumor_fraction` non-null again once the gate always runs
- Tests: `backend/tests/test_mitosis_gate.py`, `test_mitosis_worker.py`, `test_hpf.py`

## Tasks

1. **Gate** (§5.5). A candidate is eligible only if:
   - its tile in the tumour mask, dilated by 1 tile (224 µm), has `is_tumor`, and
   - `argmax(p[7]) ≠ in_situ`.

   `in_tumor` is set for every candidate when `tumor_gate: true`. A pathologist's `review_label = 'mitosis'` still counts.
2. **HPFs** (§5.8). Each centre must satisfy: disk ⊂ a hotspot, tissue coverage ≥ 0.70, tumour fraction ≥ 0.50. Report `tumor_fraction` per HPF.
3. **Check on real slides.** Run the gate on and off on a few TCGA slides with the owner, and record the count difference in `docs/STATUS.md`.

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
