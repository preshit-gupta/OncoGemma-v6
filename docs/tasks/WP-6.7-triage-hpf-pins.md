# WP-6.7 — Stage 3 viewer: HPF circles, Pin HPF, tissue inadequacy

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate or Claude | M | SPEC-05 §5.5 (D22); contract `triage_v6` as amended by WP-6.5 and WP-6.6 | WP-6.5 and WP-6.6 merged (or build against their contract in mock mode and merge after them) | B (`frontend/**`) |

## Goal

Show Stage 3's hotspots as what they now are: **HPF sites**. Each site is a 0.5 mm circle inside a dashed 0.6 mm frame. The pathologist can pin, move, exclude, restore and delete sites, and sees how many of the 10 are active. When the tissue cannot hold 10 sites, the screen says so and asks for an acknowledgement before confirming. This is the UI half of owner review issues S3-1, S3-3 and S3-4 (plan §2.4).

**Why "Add hotspot / Pin ROI" fails today.** WP-6.5 fixes the backend causes: edits overwrote each other, a pinned square overlapped the packed model windows, v5 fields, and restore did nothing. The frontend causes are fixed here:
- the overlap message says "keep at least 100 micrometers", but the gap is 0;
- the box and free-polygon modes send polygons, and an HPF is a circle;
- "Restore" sends `modify` with the same polygon.

## Read first (only these)

- `docs/contracts/triage_v6.md` (after WP-6.5/6.6: `Hotspot`, `EditOp`, `hpf_target`, the confirm body, the error codes)
- `frontend/components/viewer/TriageViewer.tsx`, `frontend/lib/api/triage.ts`, `frontend/lib/mock/triage.json`
- `frontend/components/viewer/OpenSeadragonViewer.tsx`: the hotspot polygon overlay and the `isAddingRoiMode` / `onAddRoiClick` click path
- `frontend/lib/labels.ts` (`error.hotspotOverlap`, `help.*`, `action.addHotspot`)

## Files you may touch

- `frontend/components/viewer/TriageViewer.tsx`, `frontend/components/viewer/OpenSeadragonViewer.tsx` (an HPF-circle overlay; keep the existing polygon overlay for frames)
- `frontend/lib/api/triage.ts`, `frontend/lib/mock/triage.json`, `frontend/lib/labels.ts`

## Tasks

1. **Types and client** (`lib/api/triage.ts`).
   - Mirror the amended contract: `Hotspot.center_um`, `hpf_diameter_um`, `tissue_fraction`, `at_periphery`, `front_distance_um`, and `TriageStageV6.hpf_target`, `n_sites_available`.
   - The new `EditOp` union is `add | move | exclude | restore | delete` with `center_um`.
   - `confirmTriage(caseId, noInvasiveTumor, acceptFewerHpfs)`.
   - The mock handler checks **circle** overlap: Euclidean centre distance < diameter. It no longer checks the AABB with a 100 µm gap.
2. **Overlay.**
   - Draw each site's circle (radius `hpf_diameter_um / 2`) with its rank or `U<n>` badge, and its frame (`polygon_um`) dashed and faint.
   - Excluded sites are dimmed.
   - Sites with `at_periphery` show a small "tumour edge" marker.
   - Sizes come from the data. Do not hardcode 500 or 600.
3. **Pin HPF.**
   - "Pin HPF" mode: a click sends `{op:"add", center_um}` at the clicked point.
   - "Move": select a site, click the new centre, and `{op:"move", id, center_um}` is sent.
   - "Restore" sends `{op:"restore", id}`.
   - Delete the box and free-polygon draw types and the vertex editing (`handleAddRoiFromClick` polygon branch, `handleFinishCustomPolygon`, `handleUpdateVertex`, `handleAddVertex`, `handleRemoveVertex`).
4. **Messages** (all in `labels.ts`; numbers come from the response):
   - `hotspot_overlap`: "HPF circles cannot overlap. Pin the new HPF further from HPF {ids}." Highlight the conflicting sites.
   - `too_many_sites`: "{hpf_target} HPFs are already active. Exclude one before pinning another."
   - `invalid_site`: "The HPF circle must lie inside the slide."
   - `triage_rerun_required`: "This case was triaged before HPF sites were introduced. Re-run Stage 3."
5. **Site count and tissue inadequacy.**
   - The header shows "HPF sites: {active} / {hpf_target}".
   - When `flags` includes `hotspots_limited_by_tissue`, a banner reads: "Tissue inadequate: only {n_sites_available} of {hpf_target} HPFs (0.5 mm circles with enough tumour) fit on this slide. The mitotic score will be based on fewer HPFs."
   - With fewer than `hpf_target` active sites, the confirm button stays disabled until the pathologist ticks "Tissue is inadequate for {hpf_target} HPFs". Confirm then sends `accept_fewer_hpfs: true`.
   - A `409 hpf_sites_lt_10` is shown as the same prompt.
6. **Mock fixture.** Ten model sites, 8 at the periphery. Add a second fixture state, or a query flag, with `hotspots_limited_by_tissue` and 7 sites, so the banner and the acknowledgement can be checked in mock mode.

## Acceptance (run these)

```powershell
cd frontend
$env:NEXT_PUBLIC_API_MOCK = "1"; npm run build
npx tsc --noEmit; npm run lint:labels
git grep -n "handleFinishCustomPolygon\|handleUpdateVertex\|checkAABBOverlap\|100 micrometers" -- frontend    # no output
```

Visual check with the owner, in mock mode:
- circles with dashed frames;
- pinning two HPFs in a row keeps both;
- pinning onto an existing circle shows the overlap message;
- the inadequate-tissue fixture shows the banner and blocks confirmation until the box is ticked.

## Out of scope — do not do

- Backend changes; the heatmap; the Stage 4 viewer (WP-7.10).
- Drawing the periphery band.
- Showing tissue or tumour fractions on Stage 4 (removed by WP-9.4).

## Done checklist

- [ ] Sites drawn as circles in frames; pin, move, restore and delete use the new ops
- [ ] Polygon and box drawing removed; overlap message fixed
- [ ] "N / 10" count; tissue-inadequacy banner and acknowledgement
- [ ] Build, type check and label lint pass; owner visual check done
