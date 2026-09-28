# SPEC-01 — Measurement Integrity: Provenance, Fail-Loud Execution, Strict Schemas, Hardcoded-Value Elimination, Model Registry

| Field | Value |
|---|---|
| Spec ID | SPEC-01 |
| Category (V4) | Technical |
| Issues covered | A2, A4, A7, N14 (and the prerequisites for every metric in SPEC-00 §2) |
| Depends on | — (first in build order) |
| Blocks | 02, 04, 05, 06, 07, 08, 09 |

## 1. Problem

In v5, an F1 number cannot be attributed to any model. Three defect classes cause this.

### 1.1 Silent fallbacks that fabricate or substitute outputs

| Location (v6 repo) | Behaviour |
|---|---|
| `backend/pipeline/detect.py:264-282` | When KongNet returns `[]` for a tile, the optical-density (OD) heuristic's candidates are returned instead. `model_version` still reads `vertex_ai_midog@…` (`detect.py:256-262`). |
| `backend/worker/mitosis.py:398` | The referee `label_source` is `gemini_referee_*` whenever `USE_GEMINI_FLASH_REFEREE` is true, regardless of which component actually answered. |
| `backend/pipeline/medgemma.py:1069-1071` | Any Gemini exception is caught, and a morphometric heuristic verdict is returned in its place. |
| `backend/pipeline/medgemma.py:361-373` | If a vision call fails, the same prompt is retried **without images**. |
| `backend/pipeline/medgemma.py:1204`, `1233-1316` | Tumour verification falls back to colour thresholds that emit clinical text ("…diagnostic of invasive carcinoma"). |
| `backend/pipeline/medgemma.py:764-778`, `846-855` | Doer-only results return `confidence="high"` and `verifier_verdict="DOER_CONFIRMED"`. |
| `backend/worker/grading.py:495`, `507` | The fallback calls `base64.b64encode`, but `base64` is **not imported**. The resulting `NameError` is caught, and the patch returns `TubuleResponse(tubule_percent=20, …)` / `PleoResponse(pleomorphism_score=2, …)` (`:498`, `:510`). |
| `backend/worker/grading.py:519-528` | A histologic-type error becomes `IDC-NST`. |
| `backend/worker/triage.py:600`, `706-710` | Synthetic pink images are sent to the referee and uploaded as evidence. |
| `backend/app/routers/triage.py:259` | `generate_synthetic_microscopic_patch` exists as a runtime evidence path. |
| `backend/worker/triage.py:66-68`, `155-159`; `pipeline/probe.py:56-62` | Random embeddings or a mock linear projection are used when the endpoint or model is missing. `worker/triage.py:516-517` trains the **synthetic** probe at runtime if the file is absent. |

### 1.2 Output schemas that coerce garbage into clinical values

`backend/pipeline/medgemma.py:23-242`:

- `TumorVerificationResponse` defaults to `tumor_present=True`, `lesion_type="invasive_carcinoma"` and `confidence="high"`. A reply of `{}` therefore validates as a high-confidence tumour.
- `MitosisConfirmationResponse.sanitize_verdict` tests `"CONFIRM" in vu` **before** any negation. `"NOT CONFIRMED"`, `"UNCONFIRMED"` and `"CANNOT CONFIRM"` all become `CONFIRMED`, a direct mechanism for the mitosis over-count (N10).
- `TubuleResponse.sanitize_tubule_percent` maps an unparsable value to `0`, which gives tubule score 3 and biases grades upward (N11).
- `PleoResponse.sanitize_pleo_score` maps an unparsable value to `2`.
- `HistologicTypeResponse.sanitize_type` maps any non-string to `IDC-NST`.
- Every `sanitize_confidence` maps any unrecognised string to `"medium"`. `sanitize_bool` uses `bool(v)`.

### 1.3 Hardcoded values and unsupported claims

- Literal fallbacks in numeric paths include:
  - `width_px … or 20000` (`worker/mitosis.py:84`, `worker/triage.py:270`)
  - `mpp_x or 0.25` (`worker/preprocess.py:53-54`)
  - `prob … or 0.85` (`worker/grading.py` in `select_max_density_hotspot_patches`)
  - `avg_path_prob … else 0.60` (`worker/triage.py:521`)
  - `mpp_x: float = 0.25` parameter defaults (`pipeline/verify.py:230`, `pipeline/stain.py:246-247`, `pipeline/tiles.py:73-74`)
  - `Grading.histologic_type` column default `"IDC-NST"` (`app/models/grading.py:26`)
  - The dead `biomarker_defaults` block (`configs/cap_elements.yaml`)
- `StageExecution.config_hash` exists (`app/models/stage_execution.py:27`) but no handler writes it.
- `models/detector/EVAL.md` reports P/R/F1 for YOLOv8x and HoVer-Net, neither of which is deployed, and no evaluation code produced those numbers. The README's "462 / 462 findings resolved" claim is backed by `ops/audit_evaluator.py`, which only checks whether each finding's file was modified.

## 2. Goals / non-goals

**Goals**
- G1. Every decision (model, heuristic, human) is recorded with the identity of the component that **actually** produced it.
- G2. In `eval` run mode, a fallback is impossible: failure is explicit and counted.
- G3. Model outputs are validated strictly. An invalid output is a recorded failure, never a default.
- G4. No clinical or numeric literal lives in pipeline code. All of them come from typed, hashed configuration.
- G5. Model identities and input contracts come from one registry.
- G6. Schema changes use migrations.

**Non-goals**
- Changing any model or threshold. That is the job of SPEC-04 to SPEC-07.

## 3. Design

### 3.1 Schema migrations (prerequisite)

v5 builds its schema with `Base.metadata.create_all` at startup. There is no `alembic/` directory, although `alembic` is already in `backend/requirements.txt`.

- Add `backend/alembic/` with revision `0001_v5_baseline`. It is autogenerated from the current models and verified against a fresh Postgres.
- Every table added in v6 gets its own revision.
- Startup runs `alembic upgrade head` in the API entrypoint for `ENV != test`. `create_all` survives only in the test fixtures.
- CI runs `alembic upgrade head` on an empty Postgres 15 service container, then `alembic check`, which detects drift between the models and the migrations.

### 3.2 Run mode

```python
# backend/app/core/run_context.py
class RunMode(str, Enum):
    CLINICAL = "clinical"   # interactive app use
    EVAL = "eval"           # harness / batch validation runs
    SHADOW = "shadow"       # SPEC-09 candidate models scored in parallel, never user-visible

@dataclass(frozen=True)
class DecisionContext:
    case_id: UUID
    stage_execution_id: UUID
    stage: Literal["ingest", "preprocess", "qc", "triage", "mitosis", "grading"]
    run_mode: RunMode
    run_id: UUID | None           # validation_runs.id when run_mode == EVAL
    config_hash: str              # §3.6
```

- `run_mode` is stored on `stage_executions` as a new column `run_mode TEXT NOT NULL DEFAULT 'clinical'`.
- The harness and batch runner (SPEC-02) create stage executions with `run_mode='eval'`.
- Handlers build a `DecisionContext` from the stage execution. They never read a global flag.

### 3.3 DecisionRecord

```sql
-- alembic revision 0002_decision_records
CREATE TABLE decision_records (
  id                  UUID PRIMARY KEY,
  case_id             UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  stage_execution_id  UUID NOT NULL REFERENCES stage_executions(id) ON DELETE CASCADE,
  run_id              UUID NULL,                       -- FK added in SPEC-02 revision
  stage               TEXT NOT NULL,
  task                TEXT NOT NULL,                   -- see task catalogue below
  entity_type         TEXT NOT NULL,                   -- tile_batch | tile | hotspot | candidate | patch | field | slide
  entity_id           TEXT NOT NULL,                   -- e.g. 'm_0042', 'p_007', 'tb_0003'
  entity_ids_uri      TEXT NULL,                       -- gs:// parquet listing entity ids for *_batch records
  producer_kind       TEXT NOT NULL CHECK (producer_kind IN ('model','heuristic','human','fallback','shadow')),
  producer_id         TEXT NOT NULL,                   -- registry key (§3.7) or 'user:<uid>'
  producer_version    TEXT NOT NULL,                   -- registry version / weights sha256 / prompt sha
  endpoint            TEXT NULL,
  prompt_id           TEXT NULL,
  prompt_sha256       CHAR(64) NULL,
  input_sha256        CHAR(64) NOT NULL,               -- sha256 over canonical input bytes (+ params)
  input_spec          JSONB NOT NULL,                  -- {"mpp":0.25,"size_px":[512,512],"color":"raw","stain_profile_id":null}
  params              JSONB NOT NULL DEFAULT '{}',     -- thresholds, temperature, etc.
  output              JSONB NULL,                      -- schema-validated output only
  raw_output_uri      TEXT NULL,                       -- gs:// for verbatim responses (always stored for VLMs)
  status              TEXT NOT NULL CHECK (status IN ('ok','schema_invalid','timeout','unavailable','error','skipped')),
  error_class         TEXT NULL,
  error_detail        TEXT NULL,
  latency_ms          INTEGER NOT NULL,
  cost_usd            NUMERIC(12,6) NULL,
  cache_hit           BOOLEAN NOT NULL DEFAULT FALSE,
  run_mode            TEXT NOT NULL,
  config_hash         CHAR(64) NOT NULL,
  supersedes_id       UUID NULL REFERENCES decision_records(id),   -- human override → model record
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_dr_case_stage   ON decision_records (case_id, stage);
CREATE INDEX ix_dr_run          ON decision_records (run_id);
CREATE INDEX ix_dr_producer     ON decision_records (task, producer_id, producer_version);
CREATE INDEX ix_dr_entity       ON decision_records (case_id, entity_type, entity_id);
```

**Granularity rule.** Bulk inference writes **one record per request batch** (`entity_type='tile_batch'`) and puts the entity IDs in a Parquet sidecar (`entity_ids_uri`). This covers Path Foundation embeddings, tumour-head scoring and the detector tile sweep. Every *decision that can change a count or a score* gets **one record per entity**:
- a mitosis candidate classification or referee verdict
- per-patch tubule and pleomorphism estimates
- hotspot selection
- histologic type
- the final slide-level scores

Expected volume is about 10³ per-entity rows per slide, which is acceptable.

**Task catalogue** (closed enum in `app/core/tasks.py`):
- `pf_embed`, `tumor_head`, `hotspot_select`
- `mitosis_detect`, `mitosis_classify`, `mitosis_referee`, `mitosis_count`
- `tubule_patch`, `tubule_slide`, `pleo_field`, `pleo_slide`
- `histotype`, `grade_aggregate`
- `human_edit`

**Human edits.** Every pathologist or researcher edit writes `producer_kind='human'` with `supersedes_id` pointing at the model record it overrides. This chain is the raw material for SPEC-09.

**Consumers:**
- SPEC-08 error browser (the decision chain per candidate or patch)
- INT-PROV and INT-FALL (SPEC-00 §2.3)
- the cost and latency report (SPEC-02 §6)

### 3.4 ModelGateway (single egress for all inference)

```python
# backend/app/inference/gateway.py
class ModelGateway:
    def invoke(
        self,
        task: Task,
        producer_id: str,                    # registry key, e.g. "kongnet_det_midog_1"
        inputs: ModelInputs,                 # typed: images (bytes + InputSpec), numeric features, prompt vars
        ctx: DecisionContext,
        entity: EntityRef,
        output_model: type[BaseModel],       # strict schema (§3.5)
        params: Mapping[str, Any] = {},
    ) -> GatewayResult: ...

@dataclass
class GatewayResult:
    output: BaseModel
    record_id: UUID
    cache_hit: bool
```

- **Adapters** are registered per `provider` in the registry:
  - `vertex_endpoint_predict`
  - `vertex_endpoint_raw_predict`
  - `vertex_genai` (Gemini via ADC)
  - `local_sklearn`
  - `local_torch`
  - `local_onnx`

  Each adapter implements `call(inputs, params) -> RawResponse` and nothing more.
- **Input contract enforcement.** Before any call, the gateway asserts that `inputs.image.spec` matches the registry's `input` block (mpp ± `mpp_tolerance`, `size_px`, `color`, `format`). A mismatch raises `InputContractError`. This is the structural fix for A5: a 20× tile can no longer reach KongNet unresampled.
- **Vision tasks require images.** The gateway raises `InputContractError` if a registry model with `requires_image: true` receives none. That rule removes `medgemma.py:361-373`.
- **Retries.** Only transport errors are retried (HTTP 429/500/502/503/504, `DEADLINE_EXCEEDED`, `UNAVAILABLE`):
  - exponential backoff with full jitter, base 1 s, cap 30 s
  - `max_attempts` taken from the registry (default 4)
  - a per-call deadline from the registry

  Schema-invalid outputs are **not** retried by default. The exception is VLM tasks, which may declare `schema_retries: 1` with the identical prompt. Every attempt is recorded.
- **Cache.** The key is `sha256(producer_id, producer_version, prompt_sha256, input_sha256, canonical(params))`, and the value is the validated output stored at `gs://<artifacts>/cache/<task>/<key>.json`.
  - The cache is enabled for `EVAL`/`SHADOW`, and for `CLINICAL` only for deterministic models (temperature 0 or non-generative).
  - A cache hit still writes a DecisionRecord with `cache_hit=true` and `latency_ms` = lookup time.
  - The cache is what makes ablations cheap (SPEC-06 §6).
- **Exceptions.** The gateway raises `ModelUnavailableError(task, producer_id, cause)`, `SchemaInvalidError(…)`, `InputContractError(…)` or `ModelTimeoutError(…)`. It never returns a fallback.

### 3.5 Strict output schemas

Every model output model:

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
```

**Rules**
1. **No defaults on decision fields.** Every field that carries a clinical decision is required: `tumor_present`, `lesion_type`, `verdict`, `tubule_percent`, `pleomorphism_score`, `type`, `confidence`. Rationale fields may default to `""`.
2. **No coercing validators.** Every `mode="before"` sanitizer in `medgemma.py:32-242` is deleted. Allowed normalisation is limited to:
   - trimming whitespace;
   - exact case-insensitive enum matching, e.g. `"confirmed"` → `CONFIRMED`.

   Anything else raises `ValidationError`, which the gateway turns into `status='schema_invalid'`.
3. **Gemini calls pass `response_schema`** (a JSON Schema derived from the Pydantic model) together with `response_mime_type="application/json"` in `GenerateContentConfig`.
4. **MedGemma calls** use guided JSON decoding if the serving container supports it (vLLM `guided_json`; **verify** the container). Otherwise they rely on post-hoc strict validation only.
5. **`confidence` is removed from grading math.** VLM self-reported confidence is uncalibrated, so the v5 `confidence_weights` (`configs/scoring.yaml`) no longer weight anything. Calibrated probabilities come from trained heads (SPEC-05/06/07).

**Rewritten models** (field lists; enums are closed):

| Model | Fields |
|---|---|
| `TumorVerdict` | `tumor_present: bool`, `lesion_type: Literal[...]` (no `unassessed`), `rationale: str` |
| `MitosisVerdict` | `verdict: Literal["MITOTIC_FIGURE","NOT_MITOTIC_FIGURE","EQUIVOCAL"]`, `mimic: Literal["none","apoptotic_body","pyknotic_nucleus","hyperchromatic_interphase","lymphocyte","prophase","crush","other"]`, `criteria: MitosisCriteria{membrane_absent: bool, condensed_chromosome_projections: bool, phase: Literal[...], neoplastic_cell: bool}` (normative definition in SPEC-06 §5.4), `rationale: str` |
| `TubuleEstimate` | `tumor_present: bool`, `tubule_percent: conint(ge=0, le=100)`, `rationale: str` |
| `PleoEstimate` | `pleomorphism_score: Literal[1,2,3]`, `rationale: str` |
| `HistotypeVerdict` | `type: Literal["IDC-NST","ILC","mixed_ductal_lobular","mucinous","tubular","papillary","micropapillary","metaplastic","other"]`, `rationale: str` |

### 3.6 Fallback policy

`configs/fallbacks.yaml` is empty by default. An entry has this shape:

```yaml
# Allowed ONLY in run_mode=clinical. Every fallback sets needs_human=true on the entity and is visible in the UI.
fallbacks:
  - task: mitosis_referee
    on: [ModelUnavailableError, ModelTimeoutError]
    to: null            # null = "no automated verdict"; entity stays 'unreviewed'
```

**Resolution**

```python
def resolve(task, error, ctx) -> Fallback | None:
    if ctx.run_mode in (RunMode.EVAL, RunMode.SHADOW):
        raise error                            # fail loud
    entry = policy.lookup(task, type(error))
    if entry is None:
        raise error
    return entry                               # recorded as producer_kind='fallback'
```

**Stage failure semantics**
- An unhandled `ModelUnavailableError`, `SchemaInvalidError` or `InputContractError` sets `stage_executions.status='failed'`.
- It also writes `error = {"class": ..., "task": ..., "producer_id": ..., "entity": ...}` as JSON.
- The harness classifies these as `failed(stage, error_class)` (SPEC-02 §5.3).
- The v5 practice of catching exceptions and continuing is prohibited in handlers. It is enforced by a `ruff` rule set: `BLE001` (blind except) with an explicit allowlist, and `S110` (try-except-pass).

### 3.7 Model registry

`configs/models.yaml` is loaded into `ModelRegistry`, a Pydantic model with `extra="forbid"`:

```yaml
schema_version: 1
models:
  path_foundation:
    kind: embedding
    provider: vertex_endpoint_raw_predict
    endpoint_id: ${VERTEX_PATH_FOUNDATION_ENDPOINT_ID}
    region: us-central1
    version: "<deployed model resource id>@<deploy date>"
    requires_image: true
    input:  {mpp: 1.0, mpp_tolerance: 0.02, size_px: [224, 224], color: raw, format: png}
    output_schema: EmbeddingBatch       # (N, 384) float32
    limits: {max_batch: 6, max_request_bytes: 1250000, qps: 8, deadline_s: 60, max_attempts: 4}
    license_ref: docs/licenses/path_foundation.md
  tumor_head:
    kind: classifier
    provider: local_sklearn
    artifact_uri: gs://<models>/tumor_head/<version>/model.joblib
    artifact_sha256: <sha>
    version: "<semver>"
    trained_on: {snapshot_id: <SPEC-09 snapshot>, splits_lock_sha256: <sha>}
    input:  {features: path_foundation}
  kongnet_det_midog_1:
    kind: detector
    provider: vertex_endpoint_predict
    endpoint_id: ${VERTEX_MITOSIS_ENDPOINT_ID}
    version: "<weights sha256 reported by /metadata>"
    requires_image: true
    input:  {mpp: 0.25, mpp_tolerance: 0.01, size_px: [512, 512], color: raw, format: png}
    output_schema: DetectionList        # [{cx, cy, prob}] in input-pixel coordinates
  mitosis_classifier: { ... SPEC-06 ... }
  gemini_referee:
    kind: vlm
    provider: vertex_genai
    model: gemini-2.5-flash             # pinned version string; no floating alias in eval
    requires_image: true
    params: {temperature: 0.0}
  medgemma: { ... SPEC-07 ... }
heuristics:
  od_hyperchromatic_sweep: {version: "v5-2b87ab7", module: pipeline.heuristics.od_sweep}   # ablation only
```

- `StageExecution.model_versions` is populated from the registry entries the handler resolved. No literal version strings appear in code; today they sit at `detect.py:63,75,88,99` and `verify.py:33,39`.
- The registry's canonical hash is part of `config_hash` (§3.8).
- **Gemini version pinning.** In `EVAL`, the registry must name a fully versioned model ID rather than an alias. The model fallback chain in `medgemma.py:979-982` (`gemini-2.5-flash` → `2.0` → `1.5`) is removed. Switching models silently invalidates any metric.

### 3.8 Configuration as typed, hashed data (N14)

- Every `configs/*.yaml` file, every `configs/prompts/*`, `models.yaml`, `fallbacks.yaml` and `specimen_profiles.yaml` (SPEC-04) is loaded at startup into `PipelineConfig`. `PipelineConfig` is a tree of Pydantic models with `extra="forbid"` and value constraints, for example `conint(ge=1)` or `confloat(gt=0, le=2)` on mpp.
- Load failure aborts startup.
- `config_hash = sha256(canonical_json(PipelineConfig.model_dump(mode="json")))`. It is written to `stage_executions.config_hash` for every execution and to every DecisionRecord.
- Handlers receive `PipelineConfig` by dependency injection. `yaml.safe_load` calls inside handlers are removed (`worker/mitosis.py:35-41`, `worker/triage.py:163-181`, `pipeline/scoring.py:50-64`, `pipeline/grading.py:30-39`).

**Literal scanner** — `tools/lint_literals.py`, run in CI:

| Rule | AST pattern | Example it catches |
|---|---|---|
| L1 | `BoolOp(Or)` whose last operand is a numeric/str `Constant` | `slide.width_px or 20000` |
| L2 | `Call(attr='get', args=[key, Constant])` in `pipeline/`, `worker/`, `app/routers/` | `cfg.get("det_threshold", 0.35)` |
| L3 | Numeric parameter defaults for parameters named `*mpp*`, `*threshold*`, `*radius*`, `*conf*`, `*um*` | `mpp_x: float = 0.25` |
| L4 | Numeric `Constant` in `Compare` nodes inside `pipeline/` | `if ver_c < 0.35` |

- Allowed literals: `0`, `1`, `-1`, `2`, `255`, `1e-6`, `1e-8`, `0.5` (as a midpoint), plus `tools/literals_allowlist.yaml` (path, line hash, justification).
- CI fails on any hit that is not allowlisted.
- The initial run produces `tools/literals_baseline.json`, which is burned down to zero by the end of P1.

### 3.9 Deletions (this spec)

| Item | Location | Replacement |
|---|---|---|
| Synthetic evidence image generator | `app/routers/triage.py:259` (+ callers) | 404 / explicit error |
| Synthetic crops | `worker/triage.py:599-600`, `703-711` | `SlideReadError` → stage fails |
| Mock/random embeddings | `worker/triage.py:66-68`, `155-159`, `184-191`, `439-442`, `506-509` | Test fake `FakeEmbeddingAdapter` injected in tests only |
| Runtime synthetic probe training | `worker/triage.py:514-517`, `pipeline/probe.py:63-92` | Missing artifact → `ModelUnavailableError`; `train_default_probe` moves to `tests/fakes/` |
| Probe mock projection | `pipeline/probe.py:56-62` | Removed |
| `_mock_fallback_response` | `pipeline/medgemma.py:478-695` | `tests/fakes/fake_vlm.py` |
| Morphometric referee/tumour fallbacks | `pipeline/medgemma.py:1117-1177`, `1233-1316` | Removed (heuristics live only under `heuristics:` in the registry, for ablation) |
| Heuristic substitution in detector | `pipeline/detect.py:264-282` | SPEC-06 §5.1 |
| Grading fallbacks | `worker/grading.py:490-511`, `519-528` | Removed; errors propagate |
| IDC-NST column default | `app/models/grading.py:26` | `nullable=True`, no default (migration) |
| `biomarker_defaults` | `configs/cap_elements.yaml` | Deleted with Stage 6 (SPEC-08) |
| `models/detector/EVAL.md` | — | Generated reports at `reports/<run_id>/` (SPEC-02 §7) |
| README accuracy/audit claims | `README.md` §§ badges, "462 / 462" | Links to latest generated report; badge from CI |

## 4. Metrics and acceptance criteria

| # | Criterion | How verified |
|---|---|---|
| AC1 | INT-PROV = 1.0 on the harness smoke run (5 TCGA val slides, SPEC-02 §9) | `eval/checks/provenance.py` counts per-entity decisions in outputs and joins them to `decision_records` |
| AC2 | INT-FALL = 0 in `EVAL` | Query: `producer_kind IN ('fallback','heuristic') AND run_mode='eval'` must return 0 unless the heuristic is the configured arm (SPEC-06) |
| AC3 | Each former fallback path raises in `EVAL` | One parametrised test per row of §1.1, using `FakeAdapter(raise_=ServiceUnavailable)` |
| AC4 | Schema strictness | Property tests (Hypothesis) feed random JSON, missing fields, negations (`"NOT CONFIRMED"`) and out-of-range numbers. Each must yield `schema_invalid`, never a default |
| AC5 | Input contract | A 0.5 µm/px tile passed to `kongnet_det_midog_1` raises `InputContractError` |
| AC6 | No literals | `tools/lint_literals.py` is green against an empty baseline |
| AC7 | Reproducibility | Two EVAL runs of the same slide with cache off and temperature 0 give identical `config_hash`. Detector and classifier outputs are identical. VLM verdict agreement is reported rather than asserted |
| AC8 | Migrations | `alembic upgrade head` then `alembic check` pass in CI on an empty Postgres |

## 5. Test plan

- **Unit:**
  - gateway retry, deadline, cache-key and contract checks
  - strict schema property tests
  - `FallbackPolicy.resolve` matrix (run_mode × error × allowlist)
- **Integration** (a Postgres service container plus a GCS emulator, `fake-gcs-server`, already in `ops/docker-compose.yml`): run triage, mitosis and grading handlers with fake adapters, then inject a 503 on each adapter in turn and assert the stage status is `failed` with the correct `error.class`.
- **Static:**
  - `ruff` with `BLE001`, `S110` and `PLW0603`
  - `tools/lint_literals.py`
  - `grep -R "_mock_fallback_response\|generate_synthetic\|train_default_probe" backend/{app,pipeline,worker}` must be empty

## 6. Migration and rollout

1. Alembic baseline (0001), then `decision_records` (0002), then the `run_mode` column (0003), then `Grading.histologic_type` nullable (0004).
2. Introduce the gateway with adapters wrapping the existing clients. Move call sites stage by stage (triage → mitosis → grading). Each move deletes the stage's local fallbacks in the same PR.
3. Tests that relied on mock branches (for example `test_grading.py::test_medgemma_endpoint_failure_raises_when_mock_disabled`) are rewritten against fakes.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Fail-loud increases the stage failure rate in clinical use | That is intended: failures surface as `needs_human` with a UI banner (SPEC-10 copy) instead of fabricated values. Track the failure rate per task in the Research view |
| Decision-record volume | Batch granularity rule (§3.3) and a monthly partition by `created_at` if row count exceeds 50M |
| Gemini pinned version deprecation | The registry change is a config change: new `config_hash`, re-validated by SPEC-09's gate |
