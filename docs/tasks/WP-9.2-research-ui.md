# WP-9.2 — Research view UI

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | L (2–3 sessions; submit as up to 3 PRs: 9.2a dashboard, 9.2b errors/compare/issues, 9.2c annotation/QA/batches) | SPEC-08 §3–8 | WP-9.3 merged; WP-4.2 merged (uses `AuthProvider.can`) | B |

## Goal

Build the Research view, which replaces Stage 6, entirely against the [`research_v1`](../contracts/research_v1.md) contract, using mock data. The backend comes later (WP-5.5, WP-9.1).

## Read first (only these)

- `docs/contracts/research_v1.md`, `docs/contracts/README.md`
- `docs/specs/08-research-view.md` §3–6 and §8
- `docs/specs/00-program-overview.md` §2.4–2.5 (what "final/provisional/invalid" and the gate mean)
- `AGENTS.md`

## Files you may touch

- **Create:**
  - `frontend/app/research/**` (pages below)
  - `frontend/components/research/**`
  - `frontend/lib/api/research.ts`
  - `frontend/lib/mock/research.json`
  - `frontend/public/mock/err_*.png`
- `frontend/package.json`: add `recharts` and `react-window` (and their types) only
- `frontend/lib/labels.ts`: add keys only
- `frontend/app/layout.tsx`: add a "Research" nav entry guarded by `can("research:read")`

## Pages and components

| Route | PR | Content |
|---|---|---|
| `/research` | 9.2a | Runs table (name, dataset, split, arm, status, locked-test badge, headline NS-M / NS-G with CI and status badge, gate chip). Filters. "New batch" button (`batch:create`) opens the batch form (9.2c) |
| `/research/runs/[id]` | 9.2a | Header (hashes copyable, licence-scope badge "research-only" if any scope is `research`). **Headline tiles**: value, `[ci_low, ci_high]`, `n`, status badge. **If the gate is invalid, tiles show "invalid" and no number emphasis.** Tabs: Stages, Confusion, Mitosis curves, Calibration, Slices, Items, Errors, Cost |
| `/research/runs/[id]/items/[slideId]` | 9.2a | Ground truth vs prediction table. Collapsible decision tree (`DecisionNode`), with producer/version/status per node and JSON output in a code block. Links to the stage viewers |
| `/research/runs/[id]` → Errors tab | 9.2b | Virtualised gallery (`react-window`) of `MitosisErrorCard`s, 100 per page: crop with ground-truth ○ and prediction × markers (positions from `*_points_um` relative to the crop centre, 64 µm field), p_a / p_b / VLM verdict / rule-override chip. **"Log issue"** button pre-fills the issue form with evidence |
| `/research/compare` | 9.2b | Pick runs A/B. Δ table with CI (green only when `delta_low > 0`, red only when `delta_high < 0`, otherwise neutral). Slice Δ table. Flips list |
| `/research/issues` | 9.2b | Table with filters, create/edit form (category select shows the default remedy class from SPEC-00 §7 as helper text), per-category counts. Resolving without `resolved_in` shows the `resolution_requires_run` error |
| `/research/annotate` | 9.2c | Task list, and the annotation viewer: reuse `OpenSeadragonViewer` at 40×, draw task regions, hotkeys M (MF point) / X (imposter point) / Del / Space. Definition side panel (reuse `lib/definitions/mitotic_figure.ts` if WP-7.7 is merged; otherwise create it). **With `blind: true`, no model-output request may be made** |
| `/research/labels-qa` | 9.2c | Report text with evidence quotes highlighted, regex vs LLM values side by side, accept / edit (reason) / exclude (reason) |
| Batch form + progress | 9.2c | Form per contract. Progress via `EventSource` on `/api/v1/batches/{id}/events` (the mock emits scripted events every second). Cancel and retry-failed |

## Charting rules

- Use `recharts`, with one shared colour-blind-safe palette in `components/research/palette.ts`.
- **CIs are always drawn as error bars.**
- Every chart shows `n`.
- Never show a bare percentage without `n`.
- Chart types:
  - confusion matrices as tables with shaded cells (not charts);
  - PR and F1-vs-τ as line charts;
  - reliability diagrams as a scatter plus diagonal, with the ECE shown.

## Acceptance (run these, per PR)

```powershell
cd frontend
$env:NEXT_PUBLIC_API_MOCK = "1"; npm run build
npx tsc --noEmit; npm run lint:labels
```

- **Dashboard values (9.2a):** add `frontend/scripts/check-research-fixture.mjs`, which asserts that every number rendered in the headline tiles equals `mock/research.json`. Keep it simple: import the fixture and the formatting helper and compare the strings.
- **Screenshots:** every page, the invalid-gate run and a blind annotation task.

## Out of scope — do not do

- Backend changes.
- Computing any metric in the browser. Display fixture or API values only; the what-if curve values come from the API.

## Done checklist

- [ ] 9.2a / 9.2b / 9.2c merged in order
- [ ] All routes permission-guarded via `can()`
- [ ] Blind annotation makes no model-output requests (verify in the browser network tab; screenshot)
- [ ] Build, type-check and label lint pass; screenshots attached
