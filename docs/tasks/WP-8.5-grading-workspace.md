# WP-8.5 — Grading review workspace rewrite

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | M | SPEC-07 §10 | WP-9.3 merged | B |

## Goal

Rewrite `GradingReviewWorkspace` against the [`grading_v6`](../contracts/grading_v6.md) contract:
- tubule samples at 10× and pleomorphism fields at 40×;
- a read-only mitotic panel;
- histologic-type confirmation;
- component overrides with reasons;
- a boundary warning.

The v5 file is about 1,950 lines. **Write a new, smaller component tree** instead of editing it in place.

## Read first (only these)

- `docs/contracts/grading_v6.md`, `docs/contracts/README.md`
- `docs/specs/07-stage5-nottingham-grading.md` §5.1, §6.1, §7.1
- `AGENTS.md`

## Files you may touch

- **Replace** `frontend/components/viewer/GradingReviewWorkspace.tsx`. You may split it into `frontend/components/grading/*.tsx`.
- **Create:**
  - `frontend/lib/api/grading.ts`
  - `frontend/lib/mock/grading.json`
  - `frontend/public/mock/t*.png` and `p*.png` (placeholder images)
- `frontend/app/cases/[id]/page.tsx`: only the import and props wiring of the new component
- `frontend/lib/labels.ts`: add keys only
- `frontend/lib/api.ts`: delete the old grading types and functions **only after** the new client replaces every use

## Tasks

1. **`lib/api/grading.ts`.** Add `getGrading`, `reviewSample`, `overrideComponent`, `confirmHistotype` and `confirmGrading`, typed exactly as the contract. The mock recomputes `percent` (area-weighted mean over samples with `tumor_present`) and `score`. It also recomputes `total`, `grade` and `near_grade_boundary` from the component scores. **Only the mock does this maths.**
2. **Layout:**
   - A header with **Grade**, **Total** and the three component chips (T, P, M), each showing its estimator on hover.
   - A banner for `needs_human` / `near_grade_boundary`.
   - A **Tubule** tab: a grid of samples showing image, estimate and review controls (tumour present, tubule %). A failed estimate (`null`) shows a "Needs review" state.
   - A **Pleomorphism** tab: a grid of fields at 40×, showing image, estimate (1/2/3), nuclei summary (n, median area µm², CV) and review control.
   - A **Mitoses** panel: read-only numbers, plus a link that switches to the Mitoses stage.
   - A **Histologic type** card: proposed type, rationale, a confirm button and an override select.
   - **Component overrides**: a modal with a value and a reason (≥ 10 characters, validated client-side too).
3. **Confirm.** Call `confirmGrading`, and map `histotype_unconfirmed` and `missing_component` to messages. On success, show that the case is done. There is no report step.
4. **Delete v5 concepts:** "Gate 1/2/3", the narrative panel, CAP/report references and confidence badges from VLMs.
5. **Provenance.** Add the popover.

## Acceptance (run these)

```powershell
cd frontend
$env:NEXT_PUBLIC_API_MOCK = "1"; npm run build
npx tsc --noEmit; npm run lint:labels
git grep -n -i "gate 1\|narrative\|cap report" -- frontend/components     # no output
```

Screenshots, in mock mode:
- both tabs;
- the failed-sample state;
- the override modal;
- the boundary banner;
- the confirm success.

## Out of scope — do not do

- Backend changes.
- Any client-side Nottingham arithmetic outside the mock.

## Done checklist

- [ ] New component tree + typed client + mock; old types removed after migration
- [ ] Both grids, mitotic panel, histotype card, overrides, confirm handling
- [ ] v5 concepts removed
- [ ] Build, type-check and label lint pass; screenshots attached
