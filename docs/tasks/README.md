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

## WP-7 cards owned by Claude (baseline first, D19; plan §2.2)

They are not for delegates, but follow the same template so each is one bounded session.

| Card | Can start | Notes |
|---|---|---|
| [WP-7.6a Detections and `mitosis_v6` API](WP-7.6a-detections-and-contract.md) | now | The production Stage 4 review screen fails until this lands |
| [WP-7.8 Baseline validation](WP-7.8-baseline-validation.md) | now | Fixed settings on MIDOG++ breast; no tuning, no training |
| [WP-7.6b Tumour-cell gate](WP-7.6b-tumour-gate.md) | after WP-7.6a | WP-6.2 and 6.3 are merged |

Deferred to the next iteration (D19): attribution study, classifier B, referee v2, calibration.

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
