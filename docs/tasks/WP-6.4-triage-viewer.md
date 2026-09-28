# WP-6.4 — Triage viewer: tile-resolution heatmap and non-overlap editing

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | M | SPEC-05 §4.3, §5.5 | WP-9.3 merged | B |

## Goal

Update `TriageViewer` to the [`triage_v6`](../contracts/triage_v6.md) contract:
- the full-coverage tile heatmap;
- hotspot metadata;
- clear handling of the server's overlap and invalid-polygon errors.

Develop against the mock fixture. The backend implements the same contract later.

## Read first (only these)

- `docs/contracts/triage_v6.md`, `docs/contracts/README.md`
- `docs/specs/05-stage3-triage-hotspots.md` §4.3 and §5.5
- `AGENTS.md`

## Files you may touch

- `frontend/components/viewer/TriageViewer.tsx`
- `frontend/components/viewer/OpenSeadragonViewer.tsx`: overlay helpers only
- **Create:**
  - `frontend/lib/api/triage.ts` (typed client plus mock switch)
  - `frontend/lib/mock/triage.json`
  - `frontend/public/mock/heatmap_demo.png`: generate an 89×67 PNG with a smooth blob and alpha 0 in the corners
- `frontend/lib/labels.ts`: add keys only

## Tasks

1. **`lib/api/triage.ts`.** Add `getTriage(caseId)`, `postTriageEdits(caseId, edits)` and `confirmTriage(caseId, noInvasiveTumor)`, typed exactly as the contract. Mock mode keeps an in-memory copy. Its `edits` handler validates overlap with the same rule as the server: axis-aligned bounding-box intersection area > 0 after a `min_gap_um = 100` buffer, rejecting with `hotspot_overlap`.
2. **Heatmap overlay.** Place the PNG using the contract's overlay geometry, with `image-rendering: pixelated`, an opacity slider and an on/off toggle. Delete the old grid-based rendering code paths.
3. **Hotspot list and tooltip.** Show rank, `score_kind` (as a label), tumour fraction (%), expected mitoses (when present) and source. Excluded hotspots appear greyed out, with their reason.
4. **Editing.** On add or modify, call `postTriageEdits`.
   - On `422 hotspot_overlap`, highlight the conflicting pair in red and show `L.error.hotspotOverlap`. The local edit is not kept.
   - On `422 invalid_polygon`, show the reason message.
5. **Flags.**
   - `hotspots_limited_by_tissue` shows an info banner.
   - `no_invasive_tumor_detected` shows the zero-tumour confirmation path, using the existing `confirmTriageStage(…, true)` flow.
6. **Provenance.** Use the `Provenance` component with `provenance`.

## Acceptance (run these)

```powershell
cd frontend
$env:NEXT_PUBLIC_API_MOCK = "1"; npm run build
npx tsc --noEmit; npm run lint:labels
```

Put these screenshots, in mock mode, in the PR:
- the heatmap on and off;
- the hotspot tooltip;
- a rejected overlapping edit.

## Out of scope — do not do

- Backend changes.
- Changing hotspot geometry rules on the client (the server is authoritative; the mock only imitates it).

## Done checklist

- [ ] Typed client + mock; old heatmap code removed
- [ ] Overlap and invalid-polygon errors surfaced
- [ ] Flags handled; provenance popover present
- [ ] Build, type-check and label lint pass; screenshots attached
