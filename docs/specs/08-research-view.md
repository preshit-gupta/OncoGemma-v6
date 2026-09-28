# SPEC-08 — Research View (replaces Stage 6 CAP Report): Validation Analytics, Error Analysis, Issue Register, Annotation

| Field | Value |
|---|---|
| Spec ID | SPEC-08 |
| Category (V4) | Technical |
| Issues covered | N3, V3, V4 (workflow), plus the SPEC-06 §8 annotation mode |
| Depends on | SPEC-01 (DecisionRecords), SPEC-02 (runs, metrics module, labels), SPEC-03 (RBAC) |

## 1. Scope

1. **Remove Stage 6** (CAP synoptic report, AJCC staging, narrative LLMs, PDF, PIN sign-off) from the v6 product. The code stays recoverable from tag `v5.0.0-baseline`.
2. **Add the Research view**, which is the "validation data analyser" (V3) and the "Research view for data analysis and validation, with all relevant metrics" (N3):
   - run dashboards
   - comparisons
   - error browser
   - issue register (V4)
   - ground-truth annotation and label QA

## 2. Stage 6 removal

### 2.1 Delete

| Area | Items |
|---|---|
| Backend | `app/routers/report.py`, `worker/report.py`, `pipeline/report_pdf.py`, `pipeline/templates/report.{html,css}`, `pipeline/staging.py`, `app/models/report.py` (Alembic migration drops `reports`) |
| Narrative LLM | `generate_findings_narrative`, `generate_cap_report_narrative` (`pipeline/medgemma.py:1319+`); the grading worker's narrative call (`worker/grading.py:469, 477, 611`) |
| Config | `configs/cap_elements.yaml`, `configs/staging.yaml`, `configs/prompts/{cap_report,findings_narrative}@v1.md` |
| Frontend | `components/viewer/ReportWorkspace.tsx`, report calls in `lib/api.ts`, the report step in `StageRail.tsx` and `app/cases/[id]/page.tsx` |
| Tests | `test_cap_reporting.py`, `test_batch7_report_signing.py`, `test_batch18_reporting_pdf_audit.py`, the report parts of `test_batch12_reporting_and_validation.py` |
| Tools | `tools/generate_report_9d16e702.py` |

### 2.2 Rewire

- `KNOWN_STAGES` (`app/routers/cases.py:271`): `ingest, preprocess, qc, triage, mitosis, grading`.
- `next_stage_map` (`cases.py:512-517`): `grading` → `None`.
- Grading confirm (`app/routers/grading.py:1087-1155`) sets `case.status = 'done'` instead of creating a `report` stage.
- `worker/main.py:20, 29`, `app/core/rehydrate.py`, `app/core/cloud_tasks.py`, `worker/cloud_job_entry.py`: remove `report`.

### 2.3 Clinical output after grading

- **Case summary panel.** A read-only panel on the case page shows:
  - grade and components with their estimator and provenance
  - histologic type
  - mitotic count, n_HPF and area
  - flags: `needs_human`, `hpf_count_lt_10`, `insufficient_nuclei`
- **Structured export.** `GET /api/v1/cases/{id}/summary.json`, conforming to `schemas/case_summary.schema.json`.
- **No free text.** There is no narrative and no PDF.

## 3. Information architecture

| Route | Permission | Purpose |
|---|---|---|
| `/research` | `research:read` | Runs and batches list, filters (dataset, split, arm, status), "New batch" (`batch:create`) |
| `/research/runs/[id]` | `research:read` | Run dashboard (§4.1) |
| `/research/runs/[id]/items/[slideId]` | `research:read` | Case drill-down (§4.2) |
| `/research/compare?a=&b=` | `research:read` | Paired comparison (§4.3) |
| `/research/issues` | `research:read` / `issue:write` | Issue register (§5) |
| `/research/annotate` | `research:annotate` | Annotation tasks (§6) |
| `/research/labels-qa` | `labels:qa` | TCGA report label QA queue (§4.5) |

## 4. Views

### 4.1 Run dashboard

**Header**
- run name, dataset, split, arm, status
- **Locked-test badge** when `is_locked_test`
- `config_hash`, `registry_sha256`, `splits_lock_sha256` (copyable)
- created by / at
- **measurement-validity gate** (INT-PROV, INT-FALL, split disjointness): pass or fail, per SPEC-00 §2.5

**Headline tiles**
- NS-M F1 and NS-G macro-F1, each with its 95% CI, n, coverage, and a `final` / `provisional` badge (the SPEC-00 §2.4 CI-width rule)
- Secondary numbers: P, R, AP, QWK, `F1_high`, `macroF1_LM`, `sum_MAE`

**Tabs**
- **Stages.** One table per stage:
  - S3: S3-F1, P@K, COV
  - S4: NS-M, P, R, AP, count MAE and bias, per-scanner, per-magnification
  - S5: `F1_T`, `F1_P`, `F1_M`, S5-HT, ILC F1
- **Confusion.** 3×3 matrices for grade, T, P and M, plus a `none` column. Clicking a cell filters the Items tab.
- **Mitosis curves.**
  - PR curve with the operating point marked
  - F1 vs τ curves for `τ_A`, `τ_B`, computed server-side from cached candidate probabilities (`stage_a.parquet`, detections)
  - Changing τ in the UI shows *what-if* metrics, labelled "val what-if", never saved as a result
- **Calibration.** Reliability diagrams with ECE for `p_A`, `p_B` and `p_tumor_cal`.
- **Slices.** A table with rows = pre-registered slices and columns = metric, CI and n. Slices:
  - dataset
  - specimen type
  - scanner
  - TSS group
  - native magnification (20× / 40×)
  - true grade band (3–5, 6–7, 8–9)
  - label confidence
- **Items.** A table of slides with:
  - status and failed stage / `error_class`
  - ground truth vs prediction for grade, T, P, M and type
  - sum error, runtime, cost
  - filters and CSV export
- **Errors.**
  - Mitosis FP and FN galleries at 64 µm @ 0.25 µm/px, with ground truth (○) and prediction (×) markers, `p_A`/`p_B`/VLM verdict, and the post-rule flag.
  - Grading misgrades sorted by |sum error|, showing per-component estimator evidence samples.
  - Every card has **"Log issue"** (§5).
- **Cost & latency.** Per model and per stage, from `decision_records` (p50/p95 latency, calls per slide, USD per slide).

### 4.2 Case drill-down

- Ground truth vs prediction for every component, plus the DecisionRecord chain for the slide: tree view `slide → hotspots → candidates / samples → decisions`, each with producer, version, input spec and output.
- Links open the existing stage viewers read-only for the eval case (Triage, Mitosis and Grading workspaces, with edit controls disabled for eval cases).

### 4.3 Comparison

Pick runs A and B on the same dataset and split. `manifest_sha256` must match, otherwise the page returns 400. The view shows:
- Paired ΔF1 with CI for each headline and stage metric, and McNemar p for grade correctness.
- The per-slice Δ table.
- **Flips:** slides whose grade or component went correct→wrong or wrong→correct, with links.
- Export as `compare.json`, the same payload as `oncogemma-eval compare`.

### 4.4 Batches

- New batch form (SPEC-02 §6.1): manifest URI or GCS prefix, specimen type, mpp override, stages, mode, concurrency.
- The form shows the `--dry-run` estimate (calls per model, USD) before submission.
- Progress uses server-sent events (`GET /api/v1/batches/{id}/events`), with counts by status and a failure histogram by `error_class`.

### 4.5 Label QA queue (SPEC-02 §4.5)

- Shows the report text (text layer or OCR), with regex and LLM evidence quotes highlighted, side by side with the extracted fields.
- Actions: accept, edit (with required reason) or exclude (reason).
- Every action writes `gt_annotations(task='label_qa')`. Accuracy against automated extraction is reported live.

## 5. Issue register (V4 workflow)

```sql
CREATE TABLE issues (
  id UUID PRIMARY KEY, title TEXT NOT NULL,
  category TEXT NOT NULL CHECK (category IN ('biological','model','staging','technical')),
  severity TEXT NOT NULL CHECK (severity IN ('critical','high','medium','low')),
  status TEXT NOT NULL CHECK (status IN ('open','triaged','in_progress','resolved','wont_fix')) DEFAULT 'open',
  metric_impact JSONB NULL,          -- {"metric":"ns_m_f1","slice":"scanner=Aperio","est_delta":-0.04}
  evidence JSONB NOT NULL DEFAULT '[]',   -- [{"run_id":...,"slide_id":...,"entity_type":...,"entity_id":...,"decision_record_id":...}]
  spec_ref TEXT NULL,                -- e.g. "SPEC-06 §5.4"
  owner UUID NULL REFERENCES users(id),
  created_by UUID NOT NULL REFERENCES users(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_in TEXT NULL,             -- commit sha or run id demonstrating the fix
  resolution_note TEXT NULL
);
```

- **Categories** are those defined in the Vision Doc (SPEC-00 §7). The create dialog shows each category's default remedy class as guidance.
- **Resolution rule:** `status='resolved'` requires `resolved_in` to reference a validation run whose paired comparison against the run in `evidence` shows a non-negative Δ on `metric_impact.metric`. A resolution is never accepted just because a file changed. That is the lesson of the v5 audit evaluator.
- **Exports:** CSV/JSON, and a per-category summary on `/research/issues`.

## 6. Ground-truth annotation mode

```sql
CREATE TABLE gt_annotations (
  id UUID PRIMARY KEY, dataset TEXT NOT NULL, slide_id TEXT NOT NULL,
  task TEXT NOT NULL CHECK (task IN ('mitosis_points','component_scores','grade','tumor_region','label_qa')),
  region_geojson JSONB NULL,         -- µm coordinates
  payload JSONB NOT NULL,            -- e.g. {"points":[{"x":..,"y":..,"class":"MF|imposter"}]} or {"tubule":2,"pleo":3}
  annotator_id UUID NOT NULL REFERENCES users(id),
  protocol_version TEXT NOT NULL,    -- sha of the definition file used (SPEC-06 §3)
  blind BOOLEAN NOT NULL DEFAULT TRUE,
  status TEXT NOT NULL CHECK (status IN ('draft','submitted','adjudicated')),
  adjudicates UUID[] NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- **Tasks** are created from a manifest and a protocol, for example the SPEC-06 §8 TCGA mitosis protocol.
- **Blinding:** with `blind=true`, the viewer hides every model output (heatmap, hotspots, candidates, scores) for that slide.
- **Mitosis annotation UI:**
  - Reuses the OpenSeadragon viewer at 40×, restricted to the task's HPF regions.
  - Keyboard: <kbd>M</kbd> places an MF point, <kbd>X</kbd> places an imposter, <kbd>Del</kbd> removes, <kbd>Space</kbd> toggles 10×/40× (the existing v5 hotkeys).
  - The definition file is shown in a side panel.
- **Agreement:** inter-annotator F1 (SPEC-00 §2.1 matching), κ for scores, and the adjudication queue for disagreements.
- **Output:** `oncogemma-eval datasets export-gt --task mitosis_points` writes a GeoJSON ground-truth file used as `regions_uri` in manifests (SPEC-02 §5.1).
- **Separation from clinical data:** annotations never modify detections or grading rows. They feed SPEC-09 only when an annotation is on a non-test patient.

## 7. API (all under `/api/v1/research`, guarded per SPEC-03)

| Method | Path | Returns |
|---|---|---|
| GET | `/runs?dataset&split&arm&status` | Run list with headline metrics |
| GET | `/runs/{id}` | Run header plus gate status |
| GET | `/runs/{id}/metrics` | `metrics.json` (schema-versioned) |
| GET | `/runs/{id}/items?status&filter…&cursor` | Paginated items |
| GET | `/runs/{id}/items/{slide_id}` | Drill-down, including the decision tree |
| GET | `/runs/{id}/errors/mitosis?kind=fp\|fn&cursor` | Gallery cards with signed crop URLs |
| GET | `/runs/{id}/curves/mitosis` | PR and F1-vs-τ points (server-computed from cached probabilities) |
| GET | `/compare?a=&b=` | Paired comparison payload |
| POST | `/issues` · PATCH `/issues/{id}` · GET `/issues` | Issue register |
| GET/POST/PATCH | `/annotations…`, `/annotation-tasks…` | Annotation |
| GET/POST | `/labels-qa…` | QA queue |

- Metrics are computed **once** per run by `eval/metrics.py`, with `oncogemma-eval metrics` triggered automatically when a run completes.
- They are stored in `metrics.json` and in `run_metrics(run_id, metric_id, slice_key, value, ci_low, ci_high, n, status)` for fast listing.
- The API never recomputes a metric differently from the CLI.

## 8. Frontend implementation notes

- **Framework:** Next.js 14 app router (existing).
- **Charts:** `recharts`, with one shared colour palette that is colour-blind safe. CIs are always drawn as error bars, and every chart shows `n`.
- **Visual system:** tables and cards reuse the existing Tailwind design tokens. Labels follow SPEC-10.
- **Images:** signed GCS URLs with 15-min expiry, lazily loaded, and virtualised grids for galleries (`react-window`).
- **Numbers:** always shown with CI and n. Never a bare percentage.

## 9. Acceptance criteria

| # | Criterion |
|---|---|
| AC1 | Stage 6 is gone: `grep -R "report" backend/app/routers backend/worker` finds only unrelated words. The stage list is 6 stages. Grading confirm sets `case.status='done'`. The remaining test suite is green |
| AC2 | Parity: for a fixture run, every number on the dashboard equals the value in the CLI `metrics.json`, verified by a snapshot test that reads the rendered DOM |
| AC3 | Compare parity: `/compare` output equals `oncogemma-eval compare` output for the same runs |
| AC4 | Gate display: a run with INT-PROV < 1.0 shows the gate as failed, and its headline tiles are labelled "invalid" |
| AC5 | Issue resolution rule: PATCH to `resolved` without a qualifying `resolved_in` run returns 422 |
| AC6 | Annotation blinding: for a `blind` task, the viewer's network log contains no requests for model outputs of that slide (E2E test) |
| AC7 | RBAC: a viewer cannot reach `/research*` (403 server-side). A pathologist can read runs but cannot create batches |
| AC8 | Performance: dashboard first paint < 2 s for a 1,000-item run (metrics precomputed). The errors gallery paginates 100 cards per page |
