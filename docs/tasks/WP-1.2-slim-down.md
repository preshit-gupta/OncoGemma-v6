# WP-1.2 — Slim-down: remove Stage 6 (CAP report) and dead code (includes WP-1.1)

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate (the Stage-6 part gets a Claude review) | M | SPEC-08 §2, SPEC-11 §2 | — | A |

## Goal

Delete the Stage-6 CAP report stack and the dead code listed below. Keep every non-report behaviour working. After this WP, the case pipeline has **six** stages: `ingest, preprocess, qc, triage, mitosis, grading`. Confirming grading sets the case status to `done`.

## Read first (only these)

- `docs/specs/08-research-view.md` §2
- `docs/specs/11-lightweight-code-and-audit.md` §2
- `AGENTS.md`

## Files you may touch

- **Delete:** every file in the "Delete" lists below.
- **Edit only to remove report references:**
  - `backend/app/routers/__init__.py`, `backend/app/main.py`, `backend/app/routers/cases.py`, `backend/app/routers/grading.py`
  - `backend/app/models/__init__.py`, `backend/app/models/case.py`, `backend/app/models/stage_execution.py` (comment only)
  - `backend/app/core/rehydrate.py`, `backend/app/core/cloud_tasks.py`
  - `backend/worker/main.py`, `backend/worker/cloud_job_entry.py`, `backend/worker/grading.py` (narrative lines only)
  - `backend/pipeline/medgemma.py` (the narrative functions only)
  - `frontend/lib/api.ts`, `frontend/components/viewer/StageRail.tsx`, `frontend/app/cases/[id]/page.tsx`, `frontend/components/viewer/GradingReviewWorkspace.tsx` (navigation to report only)
  - Test files that reference reports (see below)
- **Create:**
  - `docs/archive/OncoGemmav5audit.md` (moved)
  - `docs/archive/README_v5.md` (the old README, verbatim)
  - a new `README.md`

## Tasks

### A. Delete the Stage-6 stack

**Backend files:**
- `backend/app/routers/report.py`
- `backend/worker/report.py`
- `backend/pipeline/report_pdf.py`
- `backend/pipeline/templates/` (whole directory)
- `backend/pipeline/staging.py`
- `backend/app/models/report.py`

**Config:**
- `configs/cap_elements.yaml`
- `configs/staging.yaml`
- `configs/prompts/cap_report@v1.md`
- `configs/prompts/findings_narrative@v1.md`

**Frontend:** `frontend/components/viewer/ReportWorkspace.tsx`

**Narrative LLM code:**
- In `backend/pipeline/medgemma.py`, delete `generate_findings_narrative`, `generate_cap_report_narrative`, `FindingsNarrativeResponse` and `CapReportNarrativeResponse`.
- In `backend/worker/grading.py`, delete the narrative prompt load (around lines 469, 477) and the narrative generation block (around line 611). Remove the `narrative` key from the grading output, or set it to `null` if the frontend still reads it.

### B. Rewire to six stages

1. `backend/app/routers/cases.py`:
   - `KNOWN_STAGES` becomes `("ingest", "preprocess", "qc", "triage", "mitosis", "grading")`.
   - In `next_stage_map`, `grading` maps to `None`.
   - Remove the `report` lookups (around line 310).
2. `backend/app/routers/grading.py` confirm endpoint (around lines 1087–1155):
   - Stop creating a `report` stage execution.
   - Set `case.status = "done"`.
   - Return `"next_stage": None`.
3. `backend/worker/main.py`: remove the `report` import and handler.
   - `backend/app/core/rehydrate.py`: remove the report entry.
   - `backend/app/core/cloud_tasks.py`: docstring only.
   - `backend/worker/cloud_job_entry.py`: remove any `report` reference.
4. `backend/app/main.py`:
   - Remove `report_router`.
   - Remove **only** the report-table DDL statements in the startup block (around lines 77–165). Leave the other DDL untouched.
5. `backend/app/models/case.py`: remove the `reports` relationship. `backend/app/models/__init__.py`: remove the `Report` import.
   - **Do not** write a DB migration. Alembic is introduced by Claude in WP-2.1.
6. Frontend:
   - Remove the report step from `StageRail.tsx`.
   - Remove the `ReportWorkspace` import, tab and routing from `app/cases/[id]/page.tsx`. Update the confirmation messages that mention "report" to drop that word.
   - Remove `CapReportData`, `fetchReportData`, `updateReportData`, `regenerateReportNarrative`, `signReport` and `amendReport` from `lib/api.ts`.
   - In `GradingReviewWorkspace.tsx`, remove the `onAdvanceToReport` / navigate-to-report behaviour. Leave its wording for WP-9.3.

### C. Delete dead code and unsupported documents

- `backend/tools/regrade_case_9d16e702.py`
- `backend/tools/generate_report_9d16e702.py`
- `backend/tools/diagnostics.py`
- `backend/pipeline/diagnose_case.py`, `backend/pipeline/diagnose_system.py`
- `backend/cli/` (whole directory; the validation harness is rebuilt in WP-5.5)
- `models/detector/EVAL.md`
- `ops/audit_evaluator.py`, `ops/all_findings_detailed.json`

Before deleting each file, grep for its module name. If anything outside the deleted set imports it, stop and report that in the PR.

### D. Documents

- Move `OncoGemmav5audit.md` to `docs/archive/OncoGemmav5audit.md`, and add this first line: `> Archived v5 audit (2026-09-04). Describes v5 at commit 899d0e8, not the current code.`
- If `OncoGemmav5audit.md.pdf` is tracked, delete it.
- Copy the current `README.md` verbatim to `docs/archive/README_v5.md`.
- Write a new `README.md` of at most 60 lines containing:
  - project one-liner
  - "v6 status: in progress; accuracy not yet measured", with a link to `docs/IMPLEMENTATION_PLAN.md`
  - links to `docs/specs/00-program-overview.md` and `AGENTS.md`
  - local dev commands (the PowerShell forms from `AGENTS.md`)
  - licence line

  **No** accuracy numbers, test-count badges or "findings resolved" claims.

### E. Tests

- **Delete entirely:**
  - `backend/tests/test_cap_reporting.py`
  - `backend/tests/test_batch7_report_signing.py`
  - `backend/tests/test_batch12_reporting_and_validation.py` (its validation-CLI test goes with `cli/`)
- **`backend/tests/test_batch18_reporting_pdf_audit.py`:** keep any test that checks **non-report** behaviour, such as audit-event ordering, by moving it to `backend/tests/test_audit_ordering.py`. Then delete the file.
- **Other test files that mention reports** (`test_batch14`, `test_batch17`, `test_batch19_21`, `test_batch8`, `test_grading_api`, `test_ingest_fixes`, `test_midog_vertex_gemini`, `test_mitosis_api`): remove only the report-specific test functions or assertions. For example, "signed report makes mitosis immutable" goes, because signing no longer exists. Update the expected stage lists to six stages. Do not weaken unrelated assertions.

## Acceptance (run these)

```powershell
python -m pytest backend/tests -q -p no:cacheprovider            # all pass, 0 errors
git grep -n -i "report_router\|ReportWorkspace\|run_report\|report_pdf\|cap_elements\|generate_findings_narrative\|generate_cap_report_narrative" -- backend frontend configs   # no output
git grep -n "\"report\"" -- backend/app backend/worker frontend   # no output (the stage name is gone)
cd frontend; npx tsc --noEmit                                       # passes
```

## Out of scope — do not do

- Do not change `backend/requirements.txt`. Dependency pruning is WP-1.3.
- Do not add Alembic or any migration.
- Do not rename UI labels beyond removing report mentions. That is WP-9.3.
- Do not refactor anything that is not a report reference.

## Done checklist

- [ ] Stage-6 files deleted, and the pipeline has 6 stages
- [ ] Grading confirm sets `case.status = "done"` and returns `next_stage: null`
- [ ] Dead code and documents deleted or archived. New README written
- [ ] Report tests deleted; the non-report tests from test_batch18 preserved in `test_audit_ordering.py`
- [ ] All acceptance commands pass (output pasted in the PR)
