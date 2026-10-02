# WP-7.7 — Mitosis viewer and gallery: decision chain, equivocal-first review

> **Status: done (merged with the frontend lane, #9).** It works in mock mode only, until the backend serves `mitosis_v6` (WP-7.6a). Follow-up: [WP-7.7b](WP-7.7b-mitosis-client-cleanup.md) (remove the v5 client in `lib/api.ts`, show the contract's new nullable fields).

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | M | SPEC-06 §5.6, §9 | WP-9.3 merged | B |

## Goal

Move `MitosisViewer` and `MitosisGallery` to the [`mitosis_v6`](../contracts/mitosis_v6.md) contract:
- show each candidate's decision chain;
- review equivocal candidates first;
- **remove every client-side score computation**, so the UI shows server values only.

## Read first (only these)

- `docs/contracts/mitosis_v6.md`, `docs/contracts/README.md`
- `docs/specs/06-stage4-mitosis.md` §3 (the definition shown in the side panel), §5.6
- `AGENTS.md`

## Files you may touch

- `frontend/components/viewer/MitosisViewer.tsx`, `frontend/components/viewer/MitosisGallery.tsx`
- **Create:**
  - `frontend/lib/api/mitosis.ts`
  - `frontend/lib/mock/mitosis.json`
  - `frontend/public/mock/crop_m*.png` and `ctx_m*.png` (any small placeholder images)
  - `frontend/lib/definitions/mitotic_figure.ts`: export the definition text from `docs/specs/06-stage4-mitosis.md` §3 as a string constant, for the side panel
- `frontend/lib/labels.ts`: add keys only

## Tasks

1. **`lib/api/mitosis.ts`.** Add `getMitosis`, `reviewCandidate`, `addCandidate`, `replaceHpfs` and `confirmMitosis`, typed exactly as the contract. The mock simulates the server recomputation described in the contract, and no other file may do that maths.
2. **Delete client-side scoring.** Remove every mitotic-score, density or threshold calculation from the components; the v5 file has one near line 164. `grep` for `3.65`, `7.3`, `per_mm2 =` and `mitotic_score =`. None may remain outside `lib/mock` handling.
3. **Review queue.** Order candidates per the contract. Hotkeys:
   - <kbd>M</kbd> sets `review_label="mitosis"`;
   - <kbd>X</kbd> sets `"not_mitosis"`;
   - <kbd>U</kbd> clears the label (`null`);
   - <kbd>Space</kbd> toggles between the crop and the context image.

   Show the hotkeys as `<kbd>` hints only.
4. **Decision-chain panel** for the selected candidate:
   - Detector `p_a` and Classifier `p_b`, as 2-decimal numbers;
   - the VLM verdict and the criteria checklist, with ✓/✗ for each boolean plus the phase;
   - `mimic`;
   - a "Rule override" badge when `rule_override`;
   - the final decision and its review label;
   - a **Counted** badge from `counted`.
5. **Summary header.** Show `summary` as it comes, e.g. "12 mitoses / 2.16 mm² · Score 2", plus the equivocal count and the `hpf_count_lt_10` flag.
6. **Confirm.** Call `confirmMitosis`. On `409 equivocal_unreviewed`, jump to the first unreviewed ID and show the message.
7. **Definition side panel.** A collapsible panel showing the definition text.

## Acceptance (run these)

```powershell
cd frontend
$env:NEXT_PUBLIC_API_MOCK = "1"; npm run build
npx tsc --noEmit; npm run lint:labels
git grep -n "3\.65\|7\.3\b" -- frontend/components     # no output
```

Screenshots, in mock mode:
- the equivocal candidate selected, showing the full chain;
- the counts updating after a review;
- the confirm-blocked state.

## Out of scope — do not do

- Backend changes.
- Changing the HPF geometry display beyond reading the new fields.

## Done checklist

- [ ] Typed client + mock; no client-side scoring left
- [ ] Equivocal-first queue, hotkeys, decision-chain panel, counted badge
- [ ] Confirm-gate handling; definition panel
- [ ] Build, type-check and label lint pass; screenshots attached
