# WP-7.8 — Validate the mitosis baseline on MIDOG++ breast

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | M | SPEC-02 §5, §7; SPEC-06 §3.1, §6.1 | WP-5.5 (merged) | Claude (`backend/eval/**`) |

## Goal

Show that the production baseline works beyond the single image it was checked on. The baseline is KongNet at 0.25 µm/px, τ 0.75, NMS 7.5 µm, referee off (D17, D19).

Run it, **with its settings fixed**, through the production code path on the MIDOG++ breast images. Report NS-M, precision, recall and count error with confidence intervals.

This is validation only (D19):
- no calibration;
- no threshold or NMS tuning;
- no new arms;
- no training.

KongNet is pretrained. The labelled images are used only to score its predictions.

## Read first (only these)

- `docs/IMPLEMENTATION_PLAN.md` §2.2
- `docs/specs/06-stage4-mitosis.md` §3.1 (dividing-cell rule for ground truth) and §6.1
- `backend/eval/mitosis_baseline.py` (the one-image run; this WP extends it to many images)
- `backend/eval/datasets/midogpp.py`, `backend/eval/splits.py`, `backend/eval/harness/documents.py` (`S4Metrics`, `Headline`)

## Files you may touch

- `backend/eval/mitosis_baseline.py` (extend it to a list of images and pooled metrics), `backend/eval/cli.py`
- `backend/eval/datasets/midogpp.py`, `backend/eval/datasets/registry.yaml`, `backend/eval/splits.py`
- Tests under `backend/tests/eval/`
- **Create** `reports/mitosis/baseline_midogpp_breast.md`

## Tasks

1. **Split.** Make a case-level val/test split of the MIDOG++ breast images with `eval/splits.py` and lock it. The next iteration tunes on val only and needs a test set it has never seen. Report val now. Report test once, for the validated baseline, after the owner approves.
2. **Ground-truth check** (§3.1). Establish from the MIDOG++ paper or labelling protocol whether a dividing cell is labelled once or twice. Cite the source in `registry.yaml`.
   - If twice, merge pairs closer than the documented distance to their midpoint before matching, and report how many were merged.
   - Otherwise record "no harmonisation" with the source.
3. **Run** each image through `detect_region` in `run_mode=eval`, against the deployed detector, with the gateway cache under the work directory. Apply exactly `configs/mitosis.yaml` (τ, NMS). Match with `eval.metrics` (Hungarian, 7.5 µm).
4. **Report** pooled and per-image results:
   - NS-M, P, R with bootstrap CIs;
   - count MAE and signed count error per 2 mm² (ROI area from image size × mpp);
   - per scanner, if a documented scanner source exists. `MIDOG++.json` has none; do not infer scanners.

   Compare with the 094 result. If the baseline falls clearly below 094 (for example NS-M < 0.70, the SPEC-00 floor), stop and report to the owner. Do not tune.

## Owner gates

- **Live run.** About 150 images means roughly 10⁴ detector tile calls; reruns are cached. Ask before the first live run.
- **Test split.** Read once, after the owner approves the val result.

## Acceptance (run these)

```powershell
python -m pytest backend/tests/eval -q -p no:cacheprovider
```

Tests, with a fake detector and synthetic images:
- the split is case-level and locked;
- pooled metrics equal per-image sums;
- settings are read from `configs/mitosis.yaml` and not overridden;
- a merged ground-truth pair counts once (if harmonisation applies).

## Out of scope — do not do

Everything deferred by D19:
- calibration, `τ_A`/`τ_A*`, NMS tuning;
- the referee, classifier B, the attribution study;
- the research `/curves/mitosis` and `/errors/mitosis` endpoints;
- any change to `configs/mitosis.yaml`.

## Done checklist

- [ ] Case-level split locked
- [ ] Ground-truth convention established from the dataset's documentation
- [ ] Baseline run on val with fixed settings; report committed
- [ ] Test run once after owner approval, or listed as pending
- [ ] `docs/STATUS.md` updated
