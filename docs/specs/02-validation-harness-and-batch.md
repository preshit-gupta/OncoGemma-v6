# SPEC-02 — Validation Harness, Dataset Adapters, Ground-Truth Labels, Splits and Batch Processing

| Field | Value |
|---|---|
| Spec ID | SPEC-02 |
| Category (V4) | Technical |
| Issues covered | A8, V1, V2, N2 |
| Depends on | SPEC-01 (DecisionRecord, RunMode, gateway), SPEC-04 §3.1 (`SlideReader`, `read_region_at_mpp`) |
| Blocks | SPEC-05/06/07 training data, SPEC-08, P2 |

## 1. Problem

`backend/cli/validate.py` cannot evaluate a real archive:

| Line(s) | Defect |
|---|---|
| `257-264` | Hard-codes `mpp_x = mpp_y = 0.25`. Sets `gcs_uri_original` to `cases/{new_case_id}/slides/<name>`, but the local file is never uploaded. Ingest then looks up `cases/{case_id}/{slide_id}.svs` (`worker/ingest.py:332-343`), a path that cannot exist because the case was just created. |
| `280-285`, `298-303`, `355-360`, `380-385`, `405-410`, `441-446` | Every stage exception is caught, the stage is marked `done`, and execution continues. |
| `454` | The case is marked `completed` regardless of the stages' outcomes. |
| `139` | Cases without a prediction are silently excluded from the metrics (violates SPEC-00 rule 2). |
| `211-214`, `517-520` | `hashlib.sha256(f.read())` loads multi-GB slides into memory. |
| `171-173` | The gate (`n ≥ 30`, accuracy ≥ 0.85, κ ≥ 0.70) has no confidence intervals and no F1. |
| `backend/tests/test_batch12_reporting_and_validation.py:268` | The test runs on non-existent slides and asserts only that output files exist. |

There is also no batch path in the application (N2, V2), no dataset adapter, no label extraction and no split management.

## 2. Goals / non-goals

**Goals**
- Evaluate any registered dataset split end to end or per component, headlessly, using **the same stage handlers and queue as the app**.
- Produce SPEC-00 metrics with CIs, and provide a one-shot CLI for a single slide (V1).
- Provide an application batch API with progress, cancel and retry (N2, V2).
- Produce ground-truth labels for TCGA-BRCA from pathology reports, with provenance.

**Non-goals**
- UI for results. That is SPEC-08.

## 3. Dataset adapters

`eval/datasets/` exposes one interface:

```python
class DatasetAdapter(Protocol):
    key: str                                      # registry key, SPEC-00 §3
    def discover(self) -> pd.DataFrame: ...       # remote listing -> raw manifest rows
    def fetch(self, row, dest_uri: str) -> FetchedSlide: ...   # streaming download to GCS, returns uri + sha256 + bytes
    def labels(self) -> pd.DataFrame: ...         # ground-truth rows keyed by (patient_id, slide_id)
    def slide_meta(self, row) -> SlideMeta: ...   # specimen_type, mpp_override, scanner, native magnification
```

### 3.1 `TCGABRCAAdapter` (`tcga_brca_dx`)

- **Discovery:** `POST https://api.gdc.cancer.gov/files`.
  - Filters: `cases.project.project_id = TCGA-BRCA`, `data_type = "Slide Image"`, `experimental_strategy = "Diagnostic Slide"`, `access = open`.
  - Fields: `file_id, file_name, md5sum, file_size, cases.submitter_id`.
  - **Verify** the field and value names against the current GDC data dictionary during implementation.
  - Only `*-DX*` file names are accepted. Frozen `*-TS*` / `*-BS*` slides are excluded.
- **Download:** `GET https://api.gdc.cancer.gov/data/{file_id}`, streamed in 8 MiB chunks into `gs://<datasets>/tcga-brca/dx/{file_id}.svs`.
  - The MD5 is verified against GDC's `md5sum`, and a streaming SHA-256 is computed.
  - Downloads are resumable with HTTP Range.
  - Before downloading, the adapter prints the total `file_size` and requires `--confirm-bytes <N>`. The expected total is on the order of 1 TB.
- **Reports:** a second query with `data_type = "Pathology Report"` (PDF) for the same case barcodes, stored at `gs://<datasets>/tcga-brca/reports/{case}.pdf`.
- **SlideMeta:**
  - `specimen_type = resection`
  - mpp from `openslide.mpp-x/y` (Aperio `MPP`)
  - `native_mag` from `aperio.AppMag`
  - scanner from vendor properties
  - TSS from the barcode
- **Histologic type:** ground truth comes from the expert-committee annotations in Thennavan et al. 2021 (supplementary table). They are imported as a CSV into `eval/datasets/labels/tcga_histotype.csv` with a checksum and citation.

### 3.2 `BCNBAdapter` (`bcnb`)

- **Inputs:** WSI `.jpg` files, the clinical-data spreadsheet (fields include "histological grading") and per-slide tumour-region polygon JSON.
- **Conversion at ingest** (OpenSlide cannot open plain JPEG): `pyvips.Image.new_from_file(jpg).tiffsave(out, tile=True, tile_width=512, tile_height=512, pyramid=True, compression="jpeg", Q=90, bigtiff=True, xres=1000/mpp, yres=1000/mpp, resunit="cm")`.
  - libvips takes `xres`/`yres` in pixels per millimetre, hence `1000/mpp`. `resunit` only sets the unit written into the TIFF tags.
  - The output is a generic tiled TIFF, which OpenSlide reads. MPP is **always** taken from the manifest, never from the TIFF tags.
  - `mpp_override` comes from the adapter config. The manifest records `mpp_source = "dataset_doc"`.
- **Splits:** the official train/val/test (630/210/218) is the default (`datasets.bcnb.split_source: official`). An override to a patient-level re-split is allowed via config.
- **Grade label:** mapped through a configurable table. Values not in the table exclude the case from NS-G, and the exclusion is recorded.
- **Flexible configuration.** The program owner will supply the BCNB details later. Until then every value is a **required config key with no default**, and the adapter raises `DatasetConfigMissing` naming the missing keys:

  ```yaml
  datasets:
    bcnb:
      mpp: null                     # µm/px of the JPG images (required)
      native_mag: null              # e.g. 20 or 40 (informational; used for slices)
      image_glob: "WSIs/*.jpg"
      clinical_file: null           # path to the clinical spreadsheet
      grade_field: null             # column name holding the grade
      grade_map: {}                 # raw value -> 1|2|3, e.g. {"I":1,"II":2,"III":3}
      tumor_polygons: {path: null, format: null}   # annotation format and location
      split_source: official
      license_ref: null
  ```

  Nothing else in the harness depends on BCNB specifics, so these values can arrive at any time before the first BCNB run.

### 3.3 `MIDOGppAdapter` (`midogpp_breast`, `midogpp_other`)

- **Inputs:** ROI TIFFs, plus MS-COCO JSON (or the SlideRunner SQLite) with classes `mitotic figure` and `non-mitotic figure` (imposter). The MIDOG++ repository documents the class names; **verify** them.
- **Tile geometry:** MIDOG++ images are ROIs, not WSIs, so they are evaluated with the **component harness** (§5.4). Stage 4 components run directly on the ROI, and the whole ROI is the evaluation region.
- **mpp:** 0.23 µm/px (Hamamatsu) or 0.25 µm/px (Leica), per case metadata. All inputs are resampled to 0.25 by SPEC-04's `read_region_at_mpp`.
- **Split:** case-level 60/20/20 stratified by scanner, plus LOSO folds (SPEC-00 §4).

### 3.4 `BCSSAdapter`, `TUPAC16Adapter`

- **BCSS:** ROI masks with the TCGA slide barcode and ROI offset. Labels are rasterised onto the 224 µm tile grid (SPEC-05 §4.1). A BCSS patient's split is **inherited** from the TCGA split.
- **TUPAC16:** optional, only if it can be obtained. Loads the slide-level mitotic score and the auxiliary mitosis points.

## 4. Ground-truth extraction from TCGA pathology reports

Pipeline `eval/labels/tcga_reports.py`:

1. **Text:** `pypdf` text layer. If there are fewer than 200 characters per page, run OCR (Cloud Vision `DOCUMENT_TEXT_DETECTION`, or Tesseract 5 offline) and store the text with `text_source ∈ {layer, ocr}`.
   - Candidate shortcut: a public, machine-readable TCGA report corpus (for example "TCGA-Reports", Kefeli et al.). **Verify** its licence and coverage. If it is usable, it replaces OCR, with the corpus version recorded.
2. **Deterministic extraction** (regex grammar). It captures:
   - Nottingham / Elston-Ellis / "modified Bloom-Richardson" / "SBR" grade statements
   - total score
   - components: tubules/glands, nuclear pleomorphism/grade, mitoses/mitotic count

   Each capture keeps the character span.
3. **Structured LLM extraction** (Gemini through the gateway, `task=label_extract`, temperature 0). The output schema is:
   ```
   {grade: 1|2|3|null, total: 3..9|null, tubule: 1|2|3|null, pleo: 1|2|3|null, mitoses: 1|2|3|null,
    evidence: [{field, quote}]}
   ```
   Every non-null field must include a verbatim `quote` that is a substring of the report text. This is checked in code, and a failed check nulls the field.
4. **Reconciliation:** the label is accepted when regex and LLM agree. On disagreement, the case goes to the QA queue. Consistency checks:
   - `total == tubule + pleo + mitoses` when all are present
   - `grade == f(total)` using the 3–5 / 6–7 / 8–9 bands
   - The report is excluded if it describes more than one tumour with different grades. The harness records the reason.
5. **QA:** 100% of disagreements, plus a stratified random 10% of agreements (at least 30), are reviewed in the Research view QA queue (SPEC-08 §4.5) by a researcher.
   - Agreement between the automated extraction and QA is reported as label accuracy.
   - If it falls below 0.95, extraction is reworked before P2.
6. **Output:** `eval/datasets/labels/tcga_grade.parquet`, one row per patient, with:
   - `grade, total, tubule, pleo, mitoses`
   - `label_source ∈ {regex, llm, both, qa}`
   - `label_confidence ∈ {high, medium}` (`high` = both agree and are consistent; `medium` = QA-resolved)
   - `report_sha256`
   - `excluded_reason`

## 5. Harness

### 5.1 Manifest schema (Parquet, `eval/manifests/<name>.parquet`)

| Column | Type | Notes |
|---|---|---|
| `dataset` | str | registry key |
| `patient_id` | str | split unit |
| `slide_id` | str | stable dataset ID (GDC `file_id`, BCNB image ID, MIDOG case ID) |
| `uri` | str | `gs://` URI of the canonical slide (post-conversion for BCNB) |
| `sha256` | str | streaming hash computed by the adapter |
| `specimen_type` | enum | `resection` \| `core_biopsy` (SPEC-04) |
| `mpp_override` | float? | required when the file has no MPP |
| `mpp_source` | enum | `file` \| `dataset_doc` \| `manual` |
| `native_mag` | float? | 20 / 40 |
| `scanner` | str? | |
| `tss` | str? | TCGA only |
| `split` | enum | `train` \| `val` \| `test` |
| `gt_grade`, `gt_total`, `gt_tubule`, `gt_pleo`, `gt_mitoses` | int? | from §4 / clinical data |
| `gt_histotype` | str? | |
| `gt_label_source`, `gt_label_confidence` | str | |
| `regions_uri` | str? | ground-truth geometry (mitosis points, tumour polygons) in GeoJSON, in µm |

### 5.2 Splits (`eval/splits/`)

- `make_splits.py` computes splits deterministically (seed recorded) with iterative stratification over `(gt_grade, tss_group, native_mag)`.
  - `tss_group` merges TSS codes with fewer than 10 patients into `other`.
- The outputs are `eval/splits/<dataset>.parquet` and `SPLITS.lock`, which maps each file to its SHA-256.
- The splits are committed. CI verifies:
  - the lock hashes;
  - that no patient appears in two splits across all datasets sharing patients (TCGA, BCSS, TUPAC16).
- **Test lock:**
  - `--split test` requires `--confirm-test-access "<reason>"`.
  - It writes an `audit_events` row (`event_type='test_split_access'`, actor, reason, config_hash).
  - `validation_runs.is_locked_test = true`.
  - The SPEC-08 dashboard lists every test access.

### 5.3 Runs, items and state machine

```sql
-- alembic 0005_validation
CREATE TABLE validation_runs (
  id UUID PRIMARY KEY, name TEXT NOT NULL, dataset TEXT NOT NULL, split TEXT NOT NULL,
  manifest_uri TEXT NOT NULL, manifest_sha256 CHAR(64) NOT NULL,
  stages TEXT[] NOT NULL,                  -- e.g. {ingest,preprocess,qc,triage,mitosis,grading} or {mitosis_roi}
  mode TEXT NOT NULL CHECK (mode IN ('auto','manual')),
  config_hash CHAR(64) NOT NULL, registry_sha256 CHAR(64) NOT NULL, splits_lock_sha256 CHAR(64) NOT NULL,
  arm TEXT NULL,                           -- ablation arm id (SPEC-06/07)
  is_locked_test BOOLEAN NOT NULL DEFAULT FALSE,
  status TEXT NOT NULL CHECK (status IN ('created','running','completed','cancelled','failed')),
  created_by TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ NULL,
  metrics_uri TEXT NULL
);
CREATE TABLE validation_items (
  run_id UUID NOT NULL REFERENCES validation_runs(id) ON DELETE CASCADE,
  slide_id TEXT NOT NULL, patient_id TEXT NOT NULL, case_id UUID NULL REFERENCES cases(id),
  status TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed','excluded_qc','cancelled')),
  failed_stage TEXT NULL, error_class TEXT NULL, error_detail TEXT NULL,
  prediction JSONB NULL,                   -- normalized predictions (grade, components, counts, histotype, ...)
  started_at TIMESTAMPTZ NULL, finished_at TIMESTAMPTZ NULL, runtime_s REAL NULL, cost_usd NUMERIC(12,4) NULL,
  PRIMARY KEY (run_id, slide_id)
);
ALTER TABLE decision_records ADD CONSTRAINT fk_dr_run FOREIGN KEY (run_id) REFERENCES validation_runs(id);
```

**Item state machine**

```
pending → running → succeeded
                  ↘ failed(failed_stage, error_class)
                  ↘ excluded_qc      (QC hard-fail, reason stored)
```

There are no other transitions. Only `failed` and `cancelled` items are retried. A retry creates new stage-execution attempts on the same case (`attempt + 1`), and the old attempts stay for audit.

**Execution (`auto` mode)**
1. Create the `Case` (with `specimen_type` from the manifest) and the `Slide`. The slide points at the manifest `uri` directly (no copy). `mpp` is set from the manifest override or left for ingest to read.
2. Enqueue `ingest` with `run_mode='eval'` and `run_id`.
3. A run controller (`eval/harness/controller.py`) watches the item's stage executions:
   - On `awaiting_review`, it confirms the stage through **the same service function the API uses** (for example `services.stages.confirm(case_id, stage, actor=f"harness:{run_id}", edits=None)`). HTTP is not called, and no separate code path exists.
   - On `failed`, it marks the item failed.
4. After the final stage, `eval/harness/collect.py` reads the DB/GCS outputs into `validation_items.prediction`.

**Concurrency**
- The controller never runs handlers itself. The worker pool does (SPEC-02 §6.2).
- The controller limits in-flight items to `--concurrency`.

### 5.4 Component harness (ROI datasets)

`--stages mitosis_roi` runs SPEC-06 components on each MIDOG++ ROI:
1. tiling
2. detector
3. classifier / referee arm
4. NMS

It writes candidates and DecisionRecords under a synthetic case. Triage, HPF placement and grading are skipped. Metrics are NS-M on the whole ROI.

`--stages tumor_tiles` runs SPEC-05's tumour head on BCSS ROIs.

### 5.5 CLI (`oncogemma-eval`, entry point in `backend/pyproject.toml`)

```
oncogemma-eval datasets discover  --dataset tcga_brca_dx --out eval/manifests/tcga_raw.parquet
oncogemma-eval datasets fetch     --manifest ... --dest gs://.../datasets/tcga-brca --confirm-bytes N
oncogemma-eval labels extract-tcga --reports gs://.../reports --out eval/datasets/labels/tcga_grade.parquet
oncogemma-eval splits make        --dataset tcga_brca_dx --seed 20260928
oncogemma-eval run      --manifest eval/manifests/tcga.parquet --split val --stages ingest,preprocess,qc,triage,mitosis,grading
                        --mode auto --concurrency 8 --arm A4 --name "tcga-val-A4"
oncogemma-eval resume   --run <run_id>
oncogemma-eval metrics  --run <run_id> [--bootstrap 2000 --seed 7]        # writes metrics.json + report.html
oncogemma-eval compare  --run-a <id> --run-b <id> --metric ns_m_f1         # paired bootstrap ΔF1
oncogemma-eval one-shot --slide gs://.../x.svs --specimen resection [--mpp 0.25] --out result.json   (V1)
```

**`one-shot` (V1).** Runs a single slide in `EVAL` mode with auto-confirmation. It outputs one JSON document containing:
- per-stage outputs (hotspots, candidates with their decision chains, HPFs, patch estimates, grade and components)
- the model registry versions
- `config_hash`
- cost and latency
- any failures

Its exit code is non-zero if any stage failed.

**Streaming hash:** `sha256` over 8 MiB chunks. The adapter-provided hash is reused when present.

## 6. Batch processing in the application (N2, V2)

### 6.1 API

| Method | Path | Role | Body / result |
|---|---|---|---|
| POST | `/api/v1/batches` | researcher, admin | `{name, source: {manifest_uri} \| {gcs_prefix, specimen_type, mpp_override?}, stages, mode, concurrency}` → `{batch_id}` |
| GET | `/api/v1/batches` | researcher, admin, pathologist(read) | list with progress counters |
| GET | `/api/v1/batches/{id}` | same | per-item status, failures grouped by `error_class` |
| POST | `/api/v1/batches/{id}/cancel` | researcher, admin | pending → cancelled; running items finish their current stage |
| POST | `/api/v1/batches/{id}/retry` | researcher, admin | `{statuses: ["failed"]}` |

- A batch **is** a `validation_run` with `dataset='adhoc'` when created from a GCS prefix.
- The UI (SPEC-08) and the CLI share the controller.
- With `gcs_prefix`, every `*.svs|*.ndpi|*.tif|*.tiff|*.mrxs|*.jpg` object under the prefix becomes an item. JPGs are converted per §3.2 and require `mpp_override`.

### 6.2 Worker scaling and rate limits

- **Workers:** the existing poll loop (`backend/worker/main.py:57-141`, `SELECT … FOR UPDATE SKIP LOCKED`) is kept. It runs as a Cloud Run **service with min instances** during batches, or as a **Cloud Run Job** with `--tasks N` for large runs. Both use the same image and entrypoint.
  - The worker count is `ceil(concurrency / per_worker_parallelism)`.
  - Per-worker parallelism defaults to 1 slide, because slides are large and the scratch disk is 32 GiB.
- **Rate limits:** a token bucket per registry model (`limits.qps`, `limits.concurrency`), implemented in the gateway (SPEC-01 §3.4) and backed by Redis (Memorystore) when there are more than 1 worker, or in-process otherwise.
  - A 429 from any provider halves that model's bucket for 60 s (AIMD).
- **Cost ledger:** `SUM(decision_records.cost_usd)` by run and model. The price per call comes from `configs/pricing.yaml`, extended to every registry model. Missing prices are an error in `EVAL`.
- **Scratch hygiene:** each handler streams the slide to a per-execution temp dir, which is deleted in `finally` (the existing pattern). The harness asserts that free disk space is at least 2× the slide size before claiming an item.

## 7. Metrics module (`eval/metrics.py`)

These are pure functions, and SPEC-08 and SPEC-09 import them.

```python
def match_points(gt_um: np.ndarray, pred_um: np.ndarray, radius_um: float = 7.5) -> MatchResult  # Hungarian, SPEC-00 §2.1
def mitosis_f1(cases: Sequence[CaseMitosis], radius_um=7.5, midog_compat=False) -> PRF
def mitosis_pr_curve(cases, scores_key="final_score", n=101) -> PRCurve        # AP
def macro_f1(y_true, y_pred, labels=(1,2,3), none_label="none") -> MacroF1      # SPEC-00 §2.2
def band_metrics(gt_total, pred_total, pred_grade) -> BandMetrics               # F1_high, macroF1_LM, sum_MAE
def qwk(y_true, y_pred, labels=(1,2,3)) -> float
def bootstrap(metric_fn, units, B=2000, seed=...) -> CI                        # case/patient-level
def paired_bootstrap_delta(metric_fn, units_a, units_b, B=2000, seed=...) -> DeltaCI
def mcnemar_exact(correct_a, correct_b) -> float
```

**Known-answer tests** (`backend/tests/eval/test_metrics.py`) cover:
- hand-computed matching cases, including a prediction equidistant between two ground-truth points, and ties at exactly 7.5 µm (`≤` inclusive);
- macro-F1 with a `none` label;
- `F1_high` and `macroF1_LM` on a synthetic 12-case table;
- bootstrap determinism under a fixed seed.

**Report output:** `reports/<run_id>/metrics.json`, which follows a versioned schema with a `metrics_schema_version` field, and `reports/<run_id>/report.html`, a static page rendered from the JSON. Both are uploaded to GCS and linked from the run row.

## 8. Changes to existing code

- Delete `backend/cli/validate.py`, and rewrite `test_batch_validation_cli_harness_run_and_resumability` against the new harness. The old CLI can't be adapted because its design (a separate code path that swallows errors) is the defect.
- Ingest (`worker/ingest.py:302-346`) accepts any `gs://` URI from the slide row, not only `cases/{case}/{slide}.svs`. When the slide row has `mpp_override`, ingest records it as authoritative and records `mpp_source`.
- `stage confirm` logic moves out of routers into `app/services/stages.py`, so that routers and the harness call one function. Today the logic is duplicated across `routers/cases.py:512-576`, `routers/triage.py:600`, `routers/mitosis.py` confirm and `routers/grading.py:1087-1155`.

## 9. Acceptance criteria

| # | Criterion |
|---|---|
| AC1 | **Smoke:** 5 TCGA val slides, all stages in `auto`. Every item ends `succeeded`, `failed` (with a real error class) or `excluded_qc`. Zero swallowed errors (grep the worker logs for caught-and-continued patterns; must be 0). INT-PROV = 1.0 |
| AC2 | **Resume:** kill the controller mid-run and run `resume`. Items finish without duplicate stage executions, and `(run_id, slide_id)` stays unique |
| AC3 | **One-shot:** `one-shot` on one TCGA slide produces JSON valid against `schemas/one_shot.schema.json`. Exit code 0 on success, and 2 when any stage fails |
| AC4 | **Metrics:** known-answer tests pass. `metrics.json` validates against its schema |
| AC5 | **Splits:** the lock check passes. The cross-dataset patient-disjointness test passes |
| AC6 | **Labels:** extraction accuracy against QA ≥ 0.95 on the QA sample (§4.5). Per-field coverage is reported |
| AC7 | **Batch API:** create → progress → cancel → retry-failed, as an end-to-end test against the emulator stack |
| AC8 | **Test lock:** a run on `split=test` without `--confirm-test-access` exits non-zero. With it, an audit event is written |

## 10. Risks

| Risk | Mitigation |
|---|---|
| Storage and egress (about 1 TB of TCGA) | Store in the same region as the workers (us-central1). Use lifecycle rules for raw downloads. Evaluate DICOM copies in the NCI Imaging Data Commons (hosted on GCS) as an egress-free alternative. **Verify** that OpenSlide ≥ 4.0 DICOM support covers them |
| OCR errors in reports | Evidence-quote rule, dual path (regex and LLM), QA sample |
| BCNB grading semantics unclear | Verification gate in §3.2. Exclude and report if ambiguous |
| Long runs hitting VLM quotas | Gateway cache, AIMD rate limiting, cost pre-estimate (`--dry-run` prints the expected calls per model) |
