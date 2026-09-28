# SPEC-10 — Frontend Label Clean-Up

| Field | Value |
|---|---|
| Spec ID | SPEC-10 |
| Category (V4) | Staging (UX) |
| Issues covered | N5 |
| Depends on | SPEC-08 (Stage 6 removal changes which screens exist); SPEC-03 (login and admin screens) |

## 1. Problem (measured)

A scan of `frontend/app/**` and `frontend/components/**` (12 TSX files) extracted JSX text nodes and `title`, `placeholder`, `aria-label` and `label` attributes. It found **393 unique user-visible strings**, and **102 of them are five words or longer**.

| File | Strings |
|---|---|
| `components/viewer/GradingReviewWorkspace.tsx` | 100 |
| `components/viewer/ReportWorkspace.tsx` (deleted by SPEC-08) | 91 |
| `components/viewer/MitosisViewer.tsx` | 57 |
| `app/cases/[id]/page.tsx` | 52 |
| `components/viewer/TriageViewer.tsx` | 47 |
| others | 46 |

The verbosity falls into five patterns:

1. **Stage numbers and version tags inside actions.** For example "Confirm hotspots & proceed to Step 4 (v4.3 Mitosis Counting)" and "Approve slide stain quality & proceed to Step 3 (v4.2 Hotspot Triage)".
2. **Model and vendor names in primary copy.** For example "Screened via Vertex AI Path Foundation and verified by MedGemma 1.5…", "Evaluating 24 normalized tumor patches with MedGemma and synthesizing Elston-Ellis Nottingham grade scores…" and "Re-run Vertex AI Path Foundation screening and hotspot assessment". v6 changes the models (SPEC-05 to SPEC-07), so these strings would also become *wrong*.
3. **Redundant explanation of the obvious.** For example "Click on the Whole Slide Image to select a custom tumor ROI" and "Jump to patch position on whole slide viewer".
4. **Legacy concepts.** For example "Gate 1 / Gate 2 / Gate 3", "dual-level review gates", "Stage 6 (CAP Report)", "benign report queue" and "Clinical safety gate requires all detections outside reviewed fields (≥50% confidence)…".
5. **Hotkey and optics trivia in labels.** For example "40× High-Power Objective (True 0.25 µm/px Nottingham Mitosis Counting)" and "Toggle Mitotic Figure Annotations & Green Dots (Hotkey: A)".

## 2. Rules (copy standard)

| # | Rule |
|---|---|
| R1 | Buttons: **≤ 3 words**, verb first, sentence case ("Confirm hotspots") |
| R2 | Headings and tab names: **≤ 4 words**, noun phrase, sentence case |
| R3 | Helper or empty-state text: **≤ 15 words**, one sentence, only when the next action is not obvious |
| R4 | No model, vendor or version names in primary copy. Provenance goes in a single **ⓘ provenance** popover per stage, fed from `stage_executions.model_versions` (SPEC-01), so it can never be stale |
| R5 | No stage numbers in actions. The stage rail already shows position. Use "Confirm" or "Continue" |
| R6 | Units: `µm`, `mm²`, `×` (magnification). Numbers carry units, e.g. "12 mitoses / 2.16 mm²" |
| R7 | Hotkeys: shown as a `<kbd>` hint on hover or tooltip, never inside the label |
| R8 | Errors: say what happened plus the next action, ≤ 20 words, no stack or internal IDs. The detail sits behind "Details" |
| R9 | Clinical terms: standard Nottingham and WHO vocabulary ("Tubule formation", "Nuclear pleomorphism", "Mitotic count", "Histologic type") |

## 3. Implementation

1. **Label dictionary.** `frontend/lib/labels.ts` exports typed, namespaced keys, and every component imports from it. There are no string literals in JSX except punctuation.
   ```ts
   export const L = {
     stage: { ingest: "Ingest", preprocess: "Preprocess", qc: "Quality check", triage: "Hotspots", mitosis: "Mitoses", grading: "Grade" },
     action: { confirm: "Confirm", confirmHotspots: "Confirm hotspots", confirmMitoses: "Confirm mitoses", confirmGrade: "Confirm grade",
               retry: "Retry", addHotspot: "Add hotspot", excludeHotspot: "Exclude", markMitosis: "Mitosis", markNotMitosis: "Not mitosis",
               saveMpp: "Save MPP", overrideQc: "Override QC", signIn: "Sign in with Google", signOut: "Sign out" },
     status: { running: "Running…", awaitingReview: "Ready for review", failed: "Failed", done: "Done", needsHuman: "Needs review" },
     ...
   } as const;
   ```
2. **Lint.** `frontend/scripts/lint-labels.mjs` runs in CI:
   - It fails on JSX text or label attributes that are string literals outside `lib/labels.ts`, via the ESLint rule `react/jsx-no-literals` with allowed punctuation.
   - It fails on dictionary entries violating R1–R3, using word-count checks by key namespace (`action.*` ≤ 3, `heading.*` ≤ 4, `help.*` ≤ 15).
   - It fails on the forbidden-term regex from R4/R5: `/(MedGemma|Gemini|Vertex|Path Foundation|KongNet|YOLO|v4\.\d|Step \d|Stage \d|Gate \d)/`.
3. **Provenance popover.** One component, `components/Provenance.tsx`, renders `{model, version, endpoint}` rows for a stage.

## 4. Rewrite table (representative; the full table is produced by the inventory script)

| Before | After | Rule |
|---|---|---|
| Confirm Hotspots & Proceed to Step 4 | Confirm hotspots | R1, R5 |
| Confirm hotspots & proceed to Step 4 (v4.3 Mitosis Counting) | Confirm hotspots | R1, R4, R5 |
| Confirm Mitoses & Proceed to Step 5 | Confirm mitoses | R1, R5 |
| Approve Slide & Proceed to Step 3 | Confirm | R1, R5 |
| Approve slide stain quality & proceed to Step 3 (v4.2 Hotspot Triage) | Confirm | R1, R4, R5 |
| Confirm Override & Move to Step 3 | Override QC | R1, R5 |
| Confirm Nottingham Grade & Advance to CAP Report (Stage 6) | Confirm grade | R1, R5 (and Stage 6 removed) |
| Save Calibration & Queue Preprocess | Save MPP | R1 |
| Evaluating Nottingham Parameters with MedGemma 1.5... | Grading… | R4 |
| Evaluating 24 normalized tumor patches with MedGemma and synthesizing Elston-Ellis Nottingham grade scores... | Grading… | R4 |
| Re-run Vertex AI Path Foundation screening and hotspot assessment | Rerun hotspots | R1, R4 |
| Screened via Vertex AI Path Foundation and verified by MedGemma 1.5. This ROI will be transferred to… | *(removed; provenance popover)* | R4 |
| Stage v4.3: Mitosis Scoring (40× Objective / 400× Optical) | Mitoses | R2, R4 |
| 40× High-Power Objective (True 0.25 µm/px Nottingham Mitosis Counting) | 40× | R6 |
| 10× Overview (Whole HPF field of view, 1.0 µm/px) | 10× | R6 |
| Toggle Mitotic Figure Annotations & Green Dots (Hotkey: A) | Show marks (hint: A) | R1, R7 |
| All high-confidence candidate mitotic figures have been verified or rejected. Safety gate satisfied. | All candidates reviewed | R3 |
| Clinical safety gate requires all detections outside reviewed fields (≥50% confidence) to be verified or rejected before advancing to Nottingham Grading. | Review all candidates to continue. | R3 |
| Please approve all 24 image patches above (Gate 1). | Review all samples to continue. | R3, legacy |
| Please approve all 10 High-Power Fields above (Gate 2). | Review all HPFs to continue. | R3, legacy |
| All dual-level review gates satisfied. Ready to proceed to CAP Report Generation. | Ready to confirm | R3, legacy |
| CAP Histologic Subtype Classification | Histologic type | R2, R9 |
| Inspect 10× normalized evidence patches. Confirm suggested Tubule % and Nuclear Pleomorphism findings or click Edit to customize. | Review each sample; edit if needed. | R3 |
| Click on the Whole Slide Image to select a custom tumor ROI | Click the slide to place a hotspot. | R3 |
| Jump to patch position on whole slide viewer | Show on slide | R1 |
| Highlight hotspot location on slide with crosshair reticle | Show on slide | R1 |
| Re-run greedy 10-HPF placement based on confirmed mitoses | Re-place HPFs | R1 |
| No invasive tumor identified (route directly to benign report queue) | No invasive tumor | R1 (and report removed) |
| Slide failed automated QC checks. Click to provide clinical override justification & proceed to Step 3. | QC failed. Override with a reason or rescan. | R8 |
| This case does not have a slide file uploaded or the ingest pipeline stage was not initialized. | No slide yet. Upload one to start. | R8 |
| Select a Whole-Slide Image case to open the multi-stage Nottingham grading and CAP reporting workspace. | Select a case. | R3 |
| Upload your first H&E breast carcinoma WSI slide (.svs, .ndpi, .tif, .jpg, .png) to get started with OncoGemma. | Upload an H&E slide to start. | R3 |
| Please provide at least 10 characters justification for manual score overrides. | Add a reason (10+ characters). | R3 |

## 5. Acceptance criteria

| # | Criterion |
|---|---|
| AC1 | `lint-labels.mjs` and `react/jsx-no-literals` pass. No literal UI strings outside `lib/labels.ts` |
| AC2 | 0 dictionary entries violate the R1–R3 word limits. 0 forbidden-term hits (R4/R5) |
| AC3 | Every stage view shows provenance only in the popover, fed by `model_versions`. A test mutates `model_versions` and sees the popover update |
| AC4 | Inventory after the change: total unique strings and the number of strings of ≥ 5 words are reported in the PR description against the v5 baseline (393 / 102) |
| AC5 | Screenshots of every stage view, the login page and the Research view are attached to the PR for program-owner review |
