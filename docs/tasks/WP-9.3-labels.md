# WP-9.3 — Frontend label clean-up and label lint

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate | M | SPEC-10 (all) | WP-1.2 merged (the Stage-6 UI is gone) | B (first in lane) |

## Goal

Move every user-visible string into one dictionary, `frontend/lib/labels.ts`, rewrite the strings to the SPEC-10 copy rules, and add a lint that stops new literal strings from appearing.

## Read first (only these)

- `docs/specs/10-frontend-label-cleanup.md`, all of it. §2 has the rules, and §4 is the rewrite table: apply it exactly where a row matches.
- `AGENTS.md`

## Files you may touch

- `frontend/app/**`, `frontend/components/**`
- Create `frontend/lib/labels.ts`, `frontend/components/Provenance.tsx` and `frontend/scripts/lint-labels.mjs`
- `frontend/package.json`: add a script only

## Tasks

1. **Inventory.** List every JSX text node and every `title`, `placeholder`, `aria-label`, `label` and `alt` string literal in `frontend/app/**` and `frontend/components/**`. Put the before/after counts in the PR. The baseline before WP-1.2 was 393 unique strings, 102 of them with five or more words.
2. **Create `lib/labels.ts`.** It exports a single `as const` object `L`, namespaced as follows:
   - `stage.*`
   - `action.*` (buttons: ≤ 3 words)
   - `heading.*` (≤ 4 words)
   - `help.*` (≤ 15 words)
   - `status.*`
   - `error.*` (≤ 20 words)
   - `field.*` (form labels)
   - `unit.*`

   It also exports small helpers for interpolation, e.g. `L.fmt.mitosesPerArea(n, mm2)` returning `"12 mitoses / 2.16 mm²"`.
3. **Replace every literal** in the components with `L.*` references, rewriting the text per SPEC-10 §2 and §4. Where a string exists only to name a model or vendor, delete it; provenance moves to the popover (step 4).
4. **Build `components/Provenance.tsx`**, an "ⓘ" button that opens a small popover listing `model_versions` (key → value), `config_hash` (first 8 characters) and `run_mode`. Use it once per stage view. The data comes from the stage payload's `provenance`, or from the current `model_versions` field where v5 payloads still have it.
5. **Hotkeys.** Remove hotkey hints from labels and show them with `<kbd>` in tooltips.
6. **Write `scripts/lint-labels.mjs`** using the `typescript` compiler API, which is already a devDependency. The script:
   - fails on any `JsxText` containing letters;
   - fails on any string-literal value of `title`, `placeholder`, `aria-label`, `label` or `alt` outside `lib/labels.ts`;
   - fails on dictionary entries that break the word limits by namespace;
   - fails on the forbidden-term regex `/(MedGemma|Gemini|Vertex|Path Foundation|KongNet|YOLO|v4\.\d|Step \d|Stage \d|Gate \d)/`;
   - allows pure punctuation, symbols and numbers.

   Add `"lint:labels": "node scripts/lint-labels.mjs"` to `package.json`.

## Acceptance (run these)

```powershell
cd frontend
npm run lint:labels          # exit 0
npx tsc --noEmit             # passes
npm run build                # passes
```

Attach screenshots of the case list, each stage view and an error state to the PR.

## Out of scope — do not do

- Do not change behaviour, API calls or layout, beyond what shorter text implies.
- Do not add ESLint (WP-1.3).

## Done checklist

- [ ] `labels.ts` created; every UI string is referenced from it
- [ ] Provenance popover shown in every stage view
- [ ] Lint script passes; before/after string counts in the PR
- [ ] Screenshots attached
