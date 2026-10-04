# Mitosis baseline on MIDOG++ breast (WP-7.8): not run

**Status (2026-10-04): code only, by owner decision.** No MIDOG++ images were downloaded and no detector calls were made. The split, the ground-truth check and the run command are ready; this file will be replaced by the run's output if the owner later approves a run.

## Why it was not run

KongNet-Det (`kongnet_det_midog_1`) was trained on about 90% of MIDOG++ (arXiv 2510.23559; the held-out list is not published). On these images a result is an **in-distribution regression check of our pipeline** (tiling, resampling to 0.25 µm/px, τ, NMS), **not a validation of the detector**. The cost if run:

| Scope | Images | Download | Tiles (512 px at 0.25 µm/px, stride 448) | Requests (4 tiles each) | Labelled mitoses |
|---|---|---|---|---|---|
| Val split | 76 | ~8 GB | ~12,500 | ~3,100 | 800 |
| All labelled breast | 150 | ~16 GB | ~24,750 | ~6,200 | 1,721 |

Each image is its own figshare file (~110–140 MB), so a run downloads only the split's images, not the 65 GB of all seven tumour types. A real validation needs labelled cases KongNet never saw (for example TCGA slides with pathologist mitosis labels).

## Split (locked)

`backend/eval/splits/midogpp_breast.parquet`, sha256 `a33c847b3ce7542341763132970674c7352e06f5bd1e3c87a5bb650477d3b16b`, in `SPLITS.lock` beside the unchanged TCGA/BCSS entries. Made with `python -m eval.make_splits midogpp ... --seed 20260928` from `MIDOG++.json` (sha256 `8c87ab42…1f47`) and the authors' `datasets_xvalidation.csv` (sha256 `211140f5…26b8`).

- One row per image (one image per case), stratified by scanner, val/test 50/50, no train split (D19: no training).
- Image 094 is pinned to val: it chose the production τ 0.75 and NMS 7.5 µm (D17), so it can never be a test image.
- Images 151–200 are breast images without annotations and are not in the authors' table; they are left out.

| Scanner (authors' spelling) | Val images | Val MF | Test images | Test MF |
|---|---|---|---|---|
| Hamammatsu XR (1–50) | 25 | 145 | 25 | 306 |
| Hamamatsu S360 (51–100) | 26 | 261 | 24 | 321 |
| Aperio CS2 (101–150) | 25 | 394 | 25 | 294 |

The authors' table also carries their own train/test assignment (column `Dataset`, kept as `midogpp_dataset`): 33 breast images are in their test set (val 20, test 13 here). If KongNet's held-out 10% were exactly those images, they would be the only unseen ones; that is not documented, so nothing here relies on it.

## Ground truth (SPEC-06 §3.1)

No harmonisation. The MIDOG++ paper (Aubreville et al., Sci Data 10:484, 2023) labels each structure with one circle at its centre, so a dividing cell is one label. In the labels, the nearest two breast mitotic figures are 38.6 px apart (≥ 8.5 µm at the scanners' 0.22–0.25 µm/px), so no pair is inside the 7.5 µm match radius. Cited in `backend/eval/datasets/registry.yaml` (`midogpp_breast`).

## Running it later (needs the owner's go-ahead)

```powershell
cd backend
python -m eval.mitosis_baseline --split val --images-dir <midogpp images> --labels MIDOG++.json `
    --work out/midogpp_val --report ../reports/mitosis/baseline_midogpp_breast.md
```

The run uses `configs/mitosis.yaml` as deployed (no option can change τ or NMS). It reports pooled and per-scanner NS-M, P, R, count MAE and signed count error per 2 mm², with image-level bootstrap CIs. It stops with a "STOP" line if pooled NS-M is below the SPEC-00 floor of 0.70. The test split refuses to run without `--owner-approved-test`.
