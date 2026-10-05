# Task cards

Each card is a bounded, self-contained unit of delegated work. Start by reading [`AGENTS.md`](../../AGENTS.md).

## Cards ready now

| Card | Lane | Can start | Notes |
|---|---|---|---|
| [WP-1.2 Slim-down (includes WP-1.1 Stage 6 removal)](WP-1.2-slim-down.md) | A | now | Recommended first: shrinks the code base for everyone |
| [WP-2.4 Strict output schemas](WP-2.4-strict-schemas.md) | D | now | Tests pre-written |
| [WP-2.5 Hardcoded-literal scanner](WP-2.5-literal-scanner.md) | D | now | Tests pre-written |
| [WP-5.1 Evaluation metrics](WP-5.1-metrics.md) | C | now | Tests pre-written |
| [WP-5.3 Splits and lock](WP-5.3-splits.md) | C | now | Tests pre-written |
| [WP-5.2 Dataset adapters](WP-5.2-dataset-adapters.md) | C | after 5.3 merges (shares `eval/`) | D+R, critical path |
| [WP-7.1 MIDOG service v2](WP-7.1-midog-service.md) | E | now | Separate repo |
| [WP-9.3 Label clean-up](WP-9.3-labels.md) | B | after WP-1.2 merges | First in the frontend lane |
| [WP-4.2 Frontend auth](WP-4.2-frontend-auth.md) | B | after 9.3 | Contract: `contracts/auth_v1.md` |
| [WP-6.4 Triage viewer](WP-6.4-triage-viewer.md) | B | after 9.3 | Contract: `contracts/triage_v6.md` |
| [WP-7.7 Mitosis viewer](WP-7.7-mitosis-viewer.md) | B | after 9.3 | Contract: `contracts/mitosis_v6.md` |
| [WP-8.5 Grading workspace](WP-8.5-grading-workspace.md) | B | after 9.3 | Contract: `contracts/grading_v6.md` |
| [WP-9.2 Research UI](WP-9.2-research-ui.md) | B | after 9.3 | Contract: `contracts/research_v1.md` |
| [WP-7.7b Mitosis client clean-up](WP-7.7b-mitosis-client-cleanup.md) | B | after WP-7.6a merges | Small; follows the contract changes in 7.6a |

WP-7.1 and WP-7.7 are done (status banners in their cards).

## WP-7 and WP-8 cards owned by Claude (baseline first, D19; plan §2.2, §2.3)

They are not for delegates, but follow the same template so each is one bounded session.

| Card | Can start | Notes |
|---|---|---|
| [WP-7.6a Detections and `mitosis_v6` API](WP-7.6a-detections-and-contract.md) | now | The production Stage 4 review screen fails until this lands |
| [WP-7.8 Baseline validation](WP-7.8-baseline-validation.md) | now | Fixed settings on MIDOG++ breast; no tuning, no training |
| [WP-7.6b Tumour-cell gate](WP-7.6b-tumour-gate.md) | after WP-7.6a | WP-6.2 and 6.3 are merged |
| [WP-8.6 `grading_v6` API and Stage 5 sampling](WP-8.6-grading-v6-api.md) | **done** (#49) | |
| [WP-5.6 TCGA from IDC DICOM](WP-5.6-idc-source.md) | **done** (#54) | Reads TCGA-BRCA in place for whole-slide runs; prerequisite of WP-8.7 |
| [WP-8.7 Grading baseline validation](WP-8.7-grading-baseline-validation.md) | now (owner runs the live run, approved 2026-10-04) | Fixed settings on TCGA val (`eval/manifests/tcga_brca_idc_val.parquet`) |

## Owner review of Stages 3–5 (2026-10-05, D22; plan §2.4)

The backend ships together with WP-6.7 and WP-7.10 in one release. After that, open cases re-run Stage 3, Stage 4 and grading.

| Card | Owner | Can start | Fixes |
|---|---|---|---|
| [WP-9.4 Quick fixes: Stage 5 scrolling, Stage 4 HPF chips](WP-9.4-review-quick-fixes.md) | B | now | S5-1, S4-1 |
| [WP-6.5 HPF sites: 0.5 mm circles in 0.6 mm frames](WP-6.5-hpf-sites.md) | Claude | now | S3-1, S3-3, S3-4 (backend) |
| [WP-6.6 HPF sites at the tumour periphery first](WP-6.6-periphery-ranking.md) | Claude | after 6.5 | S3-2 |
| [WP-7.9 Morphology descriptions of mitotic figures](WP-7.9-mitosis-descriptions.md) | Claude | after 6.5 | S4-2 (backend) |
| [WP-6.7 Stage 3 viewer: HPF circles, Pin HPF](WP-6.7-triage-hpf-pins.md) | B | after 6.5 and 6.6 (mock mode earlier) | S3-3, S3-4 (UI) |
| [WP-7.10 Stage 4 viewer: HPF circles, descriptions](WP-7.10-mitosis-viewer-hpfs.md) | B | after 6.5, 7.9 and 9.4 (mock mode earlier) | S3-1, S3-4, S4-2 (UI) |

Deferred to the next iteration (D19): attribution study, classifier B, referee v2, calibration. For Stage 5 (plan §2.3, proposed): WP-8.1's whole-tumour frame, WP-8.2–8.4 arms and training, the SPEC-07 attribution study.

## Card template

```markdown
# WP-x.y — Title
| Owner | Size | Spec | Depends on | Lane |
## Goal
## Read first (only these)
## Files you may touch
## Tasks
## Interfaces / contract
## Acceptance (run these)
## Out of scope — do not do
## Done checklist
```

## Reviewer checklist (Claude or program owner)

- [ ] The diff touches only the files allowed by the card.
- [ ] Acceptance commands were re-run locally and pass.
- [ ] No new `except Exception` that swallows errors or returns a default.
- [ ] No new hardcoded clinical constants. No model names in UI strings.
- [ ] Pre-written test assertions are unchanged.
- [ ] Assumptions listed in the PR are acceptable, or have been resolved.
