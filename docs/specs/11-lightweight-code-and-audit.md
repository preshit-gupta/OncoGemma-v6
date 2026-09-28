# SPEC-11 — Lightweight Code and Final Code Audit

| Field | Value |
|---|---|
| Spec ID | SPEC-11 |
| Category (V4) | Technical |
| Issues covered | N15, N16 (A7 closure) |
| Depends on | SPEC-05 to SPEC-08 for the deletions they cause. The final audit (§4) runs after P1 and again before the P2 locked-test run |
| Interpretation | Confirmed by the program owner on 2026-09-28. **Lightweight** means removing dead or duplicated code, unused dependencies and one-off tools, measured by LOC and image size. **Audit** means a full multi-pass audit that exits with zero open critical/high findings |

## 1. Baseline (v5 @ `v5.0.0-baseline`, measured 2026-09-28)

**Lines of code (tracked `.py`, `.ts`, `.tsx`, `.html`, `.css`):**

| Area | LOC |
|---|---|
| `backend/app` | 7,147 |
| `backend/pipeline` | 7,460 |
| `backend/worker` | 3,948 |
| `backend/cli` + `backend/tools` | 1,178 |
| `backend/tests` | 9,493 |
| `frontend/app` + `components` + `lib` | 9,644 |

**Dependency facts** (from `backend/requirements.txt` and imports outside the tests):
- `ultralytics` is in the **API** requirements (`backend/requirements.txt`) and is imported in one place, the local-YOLO branch in `pipeline/detect.py:84-100`, which never runs in production. It pulls in `torch`, which inflates both the API image and the worker image.
- `onnxruntime` and `scikit-image` are required but imported nowhere. `onnxruntime` becomes used by SPEC-06 in the worker only.
- `weasyprint`, `reportlab` and `jinja2` exist only for Stage 6, which SPEC-08 deletes.
- `alembic` is required but unused. SPEC-01 starts using it.
- Every pin is floor-only (`>=`). There is no lockfile.
- `ops/docker/Dockerfile.api` installs the full `requirements.txt`. **Measure** both image sizes before and after with `docker image inspect --format '{{.Size}}'`.

**Frontend:**
- `next.config.mjs` sets `typescript.ignoreBuildErrors: true` and `eslint.ignoreDuringBuilds: true`.
- There is no `.eslintrc`.

## 2. Deletions and consolidation (lightweight)

| Item | Location | Reason / replacement |
|---|---|---|
| Stage 6 stack | SPEC-08 §2.1 | Replaced by the Research view |
| Local YOLO and torch branches, `YoloMitosisDetector` class | `pipeline/detect.py:31-282` | Replaced by the detector client (SPEC-06 §9) |
| Heuristic "HoVer-Net" verifier | `pipeline/verify.py:22-223` | Heuristic. Kept only in `pipeline/heuristics/` if the attribution study (SPEC-06 §6.2) still needs it, then removed after P2 |
| `_mock_fallback_response`, morphometric fallbacks, synthetic image generators | SPEC-01 §3.9 | Moved to test fakes or deleted |
| One-off case tools | `backend/tools/regrade_case_9d16e702.py`, `backend/tools/generate_report_9d16e702.py` | Case-specific scripts. `tools/diagnostics.py` is kept only if it still works against v6 schemas |
| Old validation CLI | `backend/cli/validate.py` | SPEC-02 harness |
| Duplicated mitotic-score logic | `pipeline/grading.py:210-261`; `worker/grading.py` (mitotic score section); `app/routers/grading.py` recompute paths; `frontend/components/viewer/MitosisViewer.tsx` client-side score | Single `pipeline/scoring.py` (SPEC-06 §5.8). The frontend displays server values only |
| Duplicated config loaders | `worker/triage.py:163-181`, `worker/mitosis.py:35-41`, `pipeline/scoring.py:50-64`, `pipeline/grading.py:30-39`, `pipeline/staging.py:17-27` | `PipelineConfig` (SPEC-01 §3.8) |
| Dead config | `configs/cap_elements.yaml` (`biomarker_defaults`), `configs/mitosis.yaml` (`verifier.weights_path`, `detector.weights_path`, `mock`), `configs/triage.yaml`, `configs/scoring.yaml` (`confidence_weights`) | Removed or migrated (SPEC-04 profiles, SPEC-06 v2 schema) |
| Unsupported documents | `models/detector/EVAL.md`, `ops/audit_evaluator.py`, `ops/all_findings_detailed.json`, README claims | Generated reports only (SPEC-01, SPEC-02). The v5 audit markdown moves to `docs/archive/` with a banner stating that it describes v5 |
| Debug and diagnostic modules | `pipeline/diagnose_case.py`, `pipeline/diagnose_system.py` | Delete, or rewrite as `oncogemma-eval doctor` if still needed |
| Global OpenSlide lock | `app/core/openslide_lock.py` | Thread-local handles (SPEC-04 §3.1) |

**Principle.** Every deletion PR lists the removed symbols and shows `grep` evidence that none are referenced. Tests that exercised deleted behaviour are deleted or rewritten, never skipped.

## 3. Dependencies and build

- **Split requirements by service**, compiled with `uv pip compile --generate-hashes` into committed lockfiles:
  - `requirements/api.in`: FastAPI, SQLAlchemy, Alembic, Pydantic, google-cloud-storage, google-auth, psycopg2, the Research-view stack. **No** WSI or ML libraries.
  - `requirements/worker.in`: + openslide-python and openslide-bin, pyvips, numpy, scipy, shapely, opencv-python-headless, scikit-learn, onnxruntime, pyarrow, pandas, matplotlib (heatmap render), google-cloud-aiplatform, google-genai.
  - `requirements/training.in`: + torch, timm, tiatoolbox (training images only).
  - `requirements/dev.in`: pytest, hypothesis, ruff, mypy, bandit, pip-audit.
- **Docker:** the API image uses `api.lock.txt`; the worker image uses `worker.lock.txt`. Both are multi-stage and non-root, with `PYTHONDONTWRITEBYTECODE=1`.
- **Frontend:**
  - Set `ignoreBuildErrors: false` and `ignoreDuringBuilds: false`.
  - Add `.eslintrc.json` (`next/core-web-vitals`, `react/no-danger`, `react/jsx-no-literals` per SPEC-10).
  - Add `tsconfig` `"strict": true`.
  - Add `npm ci` with the lockfile (`package-lock.json` exists).

**Targets.** These are reported as measurements, not gates:
- the change in LOC per area;
- API image size reduced by at least 50%, which is expected once torch leaves the API image;
- worker image size change;
- cold-start time of the API on Cloud Run.

## 4. Final audit protocol (N16)

### 4.1 When

- **Audit-1:** at the end of P1, before any locked-test run.
- **Audit-2:** at the end of P3.

A locked-test run (SPEC-02 §5.2) requires the latest audit to have zero open critical/high findings.

### 4.2 Automated layer (CI, blocking)

| Tool | Scope | Gate |
|---|---|---|
| `ruff` (incl. `BLE001`, `S110`, `PLW0603`, `F401`, `F841`) | backend | 0 errors |
| `mypy --strict` | `backend/pipeline`, `backend/app/inference`, `eval/` | 0 errors |
| `mypy` (standard) | rest of backend | 0 errors |
| `bandit -ll` | backend | 0 medium+ |
| `pip-audit` on each lockfile | deps | 0 known vulns without an accepted waiver |
| `npm audit --omit=dev` | frontend | 0 high/critical |
| `tsc --noEmit` (strict), `next lint` | frontend | 0 errors |
| `tools/lint_literals.py` | SPEC-01 §3.8 | 0 unallowlisted |
| Route-coverage test | SPEC-03 §4.2 | 0 unguarded |
| `gitleaks` | repo | 0 findings |
| `alembic check` | DB | no drift |
| Test suite + coverage | backend/eval | ≥ 85% line coverage on `pipeline/`, `eval/`, `app/inference/` |

### 4.3 Review layer (multi-pass)

1. **Lens passes.** One pass per lens, each producing candidate findings with a `file:line` and a reproduction:
   - correctness
   - clinical constants and definitions (against SPEC-00 and SPEC-06 to SPEC-07 definitions)
   - provenance and fail-loud behaviour (SPEC-01)
   - security and authz (SPEC-03)
   - data handling and leakage (splits, quarantine)
   - performance and resource use
   - frontend contract drift
2. **Completeness pass** per area: stages 1–5, eval, research, auth.
3. **Verification pass.** Each candidate is re-read against the code by a different reviewer, or a different agent run. Refuted candidates are recorded in an appendix; they are not deleted.
4. **Duplicate merge and root-cause grouping.**

### 4.4 Finding schema and closure rule

```yaml
id: AUD1-0042
severity: critical|high|medium|low
lens: correctness|clinical|provenance|security|data|performance|frontend
location: backend/pipeline/mitosis/nms.py:57
claim: "..."
repro: "pytest backend/tests/... -k ..."   # or a concrete input
status: open|fixed|accepted_risk|refuted
closure:
  kind: test|metric_run|rationale
  ref: "backend/tests/test_nms.py::test_dividing_cell_counts_once"   # or run id, or ADR link
```

- A finding is **fixed** only if `closure.kind` is `test` (a test that fails before and passes after) or `metric_run` (a run showing the expected metric effect). **"File modified since the audit" is not evidence.** That was the flaw of `ops/audit_evaluator.py`.
- `accepted_risk` requires program-owner sign-off and an expiry date.
- **Output:** `docs/audits/audit-<n>.md`, plus `docs/audits/audit-<n>.yaml` (machine-readable), plus a summary table by severity and lens.
- **Exit criterion:** 0 open critical, 0 open high.

## 5. Acceptance criteria

| # | Criterion |
|---|---|
| AC1 | Every §2 deletion is merged with grep evidence. The test suite is green with no skipped tests added |
| AC2 | Lockfiles are committed. CI installs from the locks only. `pip-audit` and `npm audit` are clean or waived |
| AC3 | The frontend builds with type checking and linting enabled |
| AC4 | The LOC and image-size table (before/after) is published in `docs/audits/lightweight-report.md` |
| AC5 | Audit-1 has 0 open critical/high findings, with closures per §4.4, before the P2 locked-test run |
