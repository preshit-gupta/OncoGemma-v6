# WP-9.4 — Quick review-screen fixes: Stage 5 scrolling, Stage 4 HPF chips

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate or Claude | S | SPEC-07 §10, SPEC-06 §9 | — (can start now) | B (`frontend/**`) |

## Goal

Two small UI fixes that need no backend change and can ship before the rest of plan §2.4. They fix owner review issues S5-1 and S4-1.

1. **Stage 5 does not scroll; the full report cannot be read.**
   - The case page renders the workspace inside `<div className="flex-1 relative overflow-hidden …">` (`app/cases/[id]/page.tsx`, the stage content area).
   - `GradingReviewWorkspace`'s root (`<div className="flex flex-col gap-6 p-6">`) has no height and no scroll container of its own.
   - Anything below the first screen is therefore clipped. The full-height viewers of Stages 3 and 4 don't have this problem, because they scroll their own panels.
2. **Stage 4 HPF chips show tissue and tumour fractions.** The owner doesn't need them there. Each chip should read "HPF {seq} · {count}".

## Read first (only these)

- `frontend/app/cases/[id]/page.tsx`: the stage content container and the `activeStage === "grading"` branch
- `frontend/components/viewer/GradingReviewWorkspace.tsx`: the root element of its `return`
- `frontend/components/viewer/MitosisViewer.tsx`: the "Placed HPFs" chip row

## Files you may touch

- `frontend/components/viewer/GradingReviewWorkspace.tsx` (the root element only)
- `frontend/app/cases/[id]/page.tsx` (the grading branch's container only, if you scroll there instead)
- `frontend/components/viewer/MitosisViewer.tsx` (the chip row only)
- `frontend/lib/labels.ts`: remove labels left unused; `lint:labels` decides

## Tasks

1. **Scrolling.**
   - Give Stage 5 its own vertical scroll container that fills the stage area. For example, the workspace root becomes `h-full overflow-y-auto`, keeping `flex flex-col gap-6 p-6` inside.
   - Do not make the page's outer container scroll. The stage rail and the header must stay fixed.
   - Check that nothing inside the workspace uses `h-screen` or a fixed height that would create a second scroll bar.
2. **HPF chips.** Each chip shows only `{L.field.hpfLabel} {seq} · {count}`. Remove the tissue and tumour fraction parts.

## Acceptance (run these)

```powershell
cd frontend
$env:NEXT_PUBLIC_API_MOCK = "1"; npm run build
npx tsc --noEmit; npm run lint:labels
```

Visual check with the owner:
- On the deployed site, or in mock mode with the grading fixture, open Stage 5 in a window about 800 px tall and scroll to the last section of the report. The stage rail and header stay in place.
- Stage 4 chips read "HPF n · count".

## Out of scope — do not do

- Any other layout change.
- The Stage 4 overlay and descriptions (WP-7.10); the tissue-inadequacy text (WP-6.7, WP-7.10).

## Done checklist

- [ ] Stage 5 scrolls to the end of the report; rail and header fixed
- [ ] HPF chips show the count only
- [ ] Build, type check and label lint pass; owner visual check done
