# WP-7.7b — Remove the v5 mitosis client; show an ungated `in_tumor`

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | S | SPEC-06 §9; contract `mitosis_v6` | WP-7.6a merged (it changes the contract) | B |

## Goal

Two follow-ups to WP-7.7 after the backend serves `mitosis_v6` (WP-7.6a):

- delete the v5 mitosis functions still in `frontend/lib/api.ts`;
- render the contract's new nullable fields: `in_tumor`, `tumor_fraction` and `mitotic_score`.

PR #44 (2026-10-03) already guards `p_a` / `p_b` with `!= null` in `MitosisViewer.tsx` and `MitosisGallery.tsx`. Keep those guards: both fields are nullable in the contract.

## Read first (only these)

- `docs/contracts/mitosis_v6.md` (as changed by WP-7.6a)
- `AGENTS.md`
- `frontend/lib/api.ts`: the mitosis section only (search `stages/mitosis`)

## Files you may touch

- `frontend/lib/api.ts`: delete the mitosis functions that call `/stages/mitosis/recompute`, `/add_candidate`, `/bulk_action`, `/re_place_hpfs`, and the v5 `GET`/`confirm` wrappers, if nothing outside the mitosis components imports them. Otherwise list the importers in the PR.
- `frontend/lib/api/mitosis.ts`, `frontend/lib/mock/mitosis.json`
- `frontend/components/viewer/MitosisViewer.tsx`, `frontend/components/viewer/MitosisGallery.tsx`
- `frontend/lib/labels.ts`: add keys only

## Tasks

1. Delete the v5 mitosis client functions and their types. `git grep -n "stages/mitosis/\(recompute\|add_candidate\|bulk_action\|re_place_hpfs\)" -- frontend` must print nothing.
2. Types follow the contract: `in_tumor: boolean | null`, `tumor_fraction: number | null`, `mitotic_score: 1 | 2 | 3 | null`.
3. Display rules:
   - `in_tumor === null` → "Tumour gate: not applied" in the decision-chain panel;
   - `tumor_fraction === null` → "—";
   - `mitotic_score === null` → "No score (no HPFs)".
   - All strings come from `labels.ts`.
4. Mock: add one candidate with `in_tumor: null` and a summary variant with `n_hpf: 0`. The mock recompute uses `review_label ?? (final_decision == "mitosis" && (in_tumor ?? true))`.

## Acceptance (run these)

```powershell
cd frontend
$env:NEXT_PUBLIC_API_MOCK = "1"; npm run build
npx tsc --noEmit; npm run lint:labels
git grep -n "stages/mitosis/recompute\|add_candidate\|bulk_action\|re_place_hpfs" -- .    # no output
```

Screenshot, in mock mode: the `in_tumor: null` candidate's decision chain, and the zero-HPF summary.

## Out of scope — do not do

- Backend changes.
- Client-side scoring of any kind. The mock is the only place that simulates the server.

## Done checklist

- [ ] v5 mitosis client deleted; no importer left
- [ ] Nullable fields typed and displayed per the rules
- [ ] Mock covers both new cases
- [ ] Build, type-check and label lint pass; screenshots attached
