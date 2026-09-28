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

**Frontend lane order.** Run the frontend cards one after another, in the order listed, to avoid conflicts in shared files (`lib/api.ts`, `app/cases/[id]/page.tsx`). Cards 6.4, 7.7, 8.5 and 9.2 touch different components and may run in parallel **only** if each keeps its `api.ts` changes in a separate file (`lib/api/<area>.ts`), as their cards instruct.

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
