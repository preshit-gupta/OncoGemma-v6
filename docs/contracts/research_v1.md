# Contract: research v1 (SPEC-08 §7, SPEC-02 §6–7)

All paths are under `/api/v1/research` except batches (`/api/v1/batches`). Lists are cursor-paginated as `{ items: T[], next_cursor: string | null }`.

## Core types

```ts
interface Metric { value: number | null; ci_low: number | null; ci_high: number | null; n: number;
                   status: "final" | "provisional" | "invalid" }     // SPEC-00 §2.4 (CI half-width rule), §2.5 (gate)

interface Gate { valid: boolean; int_prov: number; int_fall: number; disjoint: boolean }

interface RunSummary {
  id: string; name: string; dataset: string; split: "train" | "val" | "test"; arm: string | null;
  status: "created" | "running" | "completed" | "cancelled" | "failed";
  is_locked_test: boolean; created_by: string; created_at: string; finished_at: string | null;
  n_items: number; n_failed: number;
  headline: { ns_m?: Metric; ns_g?: Metric };
  gate: Gate;
}

interface RunDetail extends RunSummary {
  config_hash: string; registry_sha256: string; splits_lock_sha256: string; manifest_sha256: string;
  stages: string[]; license_scopes: ("commercial_ok" | "research" | "pending")[];
}

interface Confusion { labels: (1 | 2 | 3 | "none")[]; matrix: number[][] }       // rows = true, cols = predicted
interface SliceRow { slice: string; key: string; metric: string; value: number | null; ci_low: number | null; ci_high: number | null; n: number }
interface Reliability { bins: { p_mean: number; frac_pos: number; n: number }[]; ece: number }

interface MetricsV1 {                       // = reports/<run_id>/metrics.json
  metrics_schema_version: 1;
  run_id: string; generated_at: string;
  bootstrap: { B: number; seed: number };
  coverage: number;
  headline: { ns_m?: Metric; ns_g?: Metric };
  stages: {
    s3?: { f1: Metric; p_at_k: Metric; coverage: number };
    s4?: { f1: Metric; precision: Metric; recall: Metric; ap: number | null;
           count_mae_per_2mm2: Metric; count_bias_per_2mm2: Metric;
           per_scanner: Record<string, Metric>; per_mag: Record<string, Metric> };
    s5?: { f1_t: Metric; f1_p: Metric; f1_m: Metric; f1_high: Metric; macro_f1_lm: Metric;
           sum_mae: Metric; qwk: Metric; histotype_f1: Metric; ilc_f1: Metric };
  };
  confusion: { grade?: Confusion; tubule?: Confusion; pleo?: Confusion; mitotic?: Confusion };
  slices: SliceRow[];
  calibration: { p_a?: Reliability; p_b?: Reliability; p_tumor?: Reliability };
  curves: { mitosis_pr?: { thresholds: number[]; precision: number[]; recall: number[]; f1: number[] } };
  cost: { usd_total: number; usd_per_slide: number;
          by_model: Record<string, { calls: number; usd: number; p50_ms: number; p95_ms: number }> };
  // Optional additions (WP-5.5c). Schema: backend/eval/schemas/metrics.schema.json.
  run?: { id: string; name: string; dataset: string; split: string; arm: string | null; stages: string[];
          mode: "auto" | "manual"; status: string; is_locked_test: boolean; config_hash: string;
          registry_sha256: string; manifest_uri: string; manifest_sha256: string; splits_lock_sha256: string | null };
  counts?: { items: Record<string, number>;          // by item status
             no_invasive_tumor: number;              // succeeded slides with no invasive tumour; graded "none" in every denominator
             failures: { status: "failed" | "excluded_qc"; stage: string | null; error_class: string | null; n: number }[];
             int_fall: number };                     // fallback decisions in the run (SPEC-00 §2.3, must be 0)
  unavailable?: Record<string, string>;               // metric or member -> why the run cannot support it
}

interface ItemRow {
  slide_id: string; patient_id: string;
  status: "pending" | "running" | "succeeded" | "failed" | "excluded_qc" | "cancelled";
  failed_stage: string | null; error_class: string | null;
  gt:   { grade: number | null; total: number | null; tubule: number | null; pleo: number | null; mitoses: number | null; histotype: string | null };
  pred: { grade: number | null; total: number | null; tubule: number | null; pleo: number | null; mitoses: number | null; histotype: string | null };
  sum_error: number | null; runtime_s: number | null; cost_usd: number | null;
}

interface DecisionNode {
  id: string; task: string; entity_type: string; entity_id: string;
  producer_kind: "model" | "heuristic" | "human" | "fallback" | "shadow";
  producer_id: string; producer_version: string;
  status: "ok" | "schema_invalid" | "timeout" | "unavailable" | "error" | "skipped";
  input_spec: Record<string, unknown>; output: unknown; latency_ms: number;
  children: DecisionNode[];
}

interface MitosisErrorCard {
  slide_id: string; kind: "fp" | "fn"; centroid_um: [number, number]; crop_url: string;
  gt_points_um: [number, number][]; pred_points_um: [number, number][];     // within the crop, for markers
  p_a: number | null; p_b: number | null; vlm_verdict: string | null; rule_override: boolean | null;
  decision_record_id: string | null;
}

interface CompareRow { metric: string; a: Metric; b: Metric; delta: number; delta_low: number; delta_high: number; mcnemar_p?: number }
interface CompareV1 {
  a: RunSummary; b: RunSummary; manifest_sha256: string;
  metrics: CompareRow[];
  slices: { slice: string; key: string; metric: string; delta: number; delta_low: number; delta_high: number }[];
  flips: { slide_id: string; component: "grade" | "tubule" | "pleo" | "mitoses"; a_correct: boolean; b_correct: boolean }[];
}

interface Issue {
  id: string; title: string;
  category: "biological" | "model" | "staging" | "technical";
  severity: "critical" | "high" | "medium" | "low";
  status: "open" | "triaged" | "in_progress" | "resolved" | "wont_fix";
  metric_impact: { metric: string; slice?: string; est_delta?: number } | null;
  evidence: { run_id: string; slide_id?: string; entity_type?: string; entity_id?: string; decision_record_id?: string }[];
  spec_ref: string | null; owner: string | null; created_by: string; created_at: string;
  resolved_in: string | null; resolution_note: string | null;
}

interface AnnotationTask {
  id: string; dataset: string; slide_id: string; kind: "mitosis_points" | "component_scores" | "grade" | "tumor_region";
  regions_um: [number, number][][]; blind: boolean; definition_md: string; protocol_version: string;
  my_annotation: { id: string; status: "draft" | "submitted" | "adjudicated"; payload: unknown } | null;
}

interface QAItem {
  patient_id: string; report_text_url: string;
  regex: Record<string, number | null>;
  llm: { values: Record<string, number | null>; evidence: { field: string; quote: string }[] };
  status: "pending" | "accepted" | "edited" | "excluded";
}
```

## Endpoints

| Method | Path | Body / query | 2xx | Errors |
|---|---|---|---|---|
| GET | `/runs` | `?dataset&split&arm&status&cursor` | `Page<RunSummary>` | |
| GET | `/runs/{id}` | | `RunDetail` | `404` |
| GET | `/runs/{id}/metrics` | | `MetricsV1` | `404 metrics_not_ready` |
| GET | `/runs/{id}/items` | `?status&cursor` | `Page<ItemRow>` | |
| GET | `/runs/{id}/items/{slide_id}` | | `{ item: ItemRow, case_id: string, decisions: DecisionNode[] }` | `404` |
| GET | `/runs/{id}/errors/mitosis` | `?kind=fp\|fn&cursor` | `Page<MitosisErrorCard>` | |
| GET | `/runs/{id}/curves/mitosis` | `?tau_a&tau_b` (optional what-if) | `{ pr: {...}, what_if?: { f1, precision, recall } }` | `400 not_val_split` (what-if only on val) |
| GET | `/compare` | `?a&b` | `CompareV1` | `400 manifest_mismatch` |
| GET | `/issues` | `?category&severity&status` | `Issue[]` | |
| POST | `/issues` | `{ title, category, severity, metric_impact?, evidence, spec_ref? }` | `201 Issue` | `422` |
| PATCH | `/issues/{id}` | `{ status?, owner?, resolved_in?, resolution_note? }` | `Issue` | `422 resolution_requires_run` |
| GET | `/annotation-tasks` | `?cursor` | `Page<AnnotationTask>` | |
| POST | `/annotations` | `{ task_id, payload, status: "draft" \| "submitted" }` | `201 { id, status }` | `422 invalid_payload` |
| GET | `/labels-qa` | `?status` | `QAItem[]` | |
| POST | `/labels-qa/{patient_id}` | `{ action: "accept" \| "edit" \| "exclude", values?, reason? }` | `QAItem` | `422 reason_required` |
| POST | `/api/v1/batches` | `{ name, source: {manifest_uri, split, confirm_test_access?} \| {gcs_prefix, specimen_type, mpp_override?}, stages, mode, concurrency }` (`split: "test"` needs `eval:test_split` and `confirm_test_access`; JPEG prefixes are refused until conversion exists) | `201 { batch_id }` | `403` · `422` |
| GET | `/api/v1/batches` | | `BatchSummary[]` (newest first) | |
| GET | `/api/v1/batches/{id}` | | `BatchSummary & { items: { slide_id, patient_id, status, case_id, failed_stage, error_class, error_detail, runtime_s, cost_usd }[] }` | `404` |
| GET | `/api/v1/batches/{id}/events` | Server-sent events (SSE) | `data: {"status": ..., "counts":{...by status}, "failures_by_error_class":{...}}` every ≤ 2 s; the stream ends when the batch does | |
| POST | `/api/v1/batches/{id}/cancel` · `/retry` | `{ statuses: ["failed"] }` (retry) | `202` | |

`BatchSummary` (WP-5.5d): `{ batch_id, name, dataset, split, stages, mode, concurrency, status, is_locked_test, created_by, created_at, finished_at, n_items, counts: Record<status, number>, failures_by_error_class: Record<string, number> }`. A batch is a validation run; ad-hoc batches have `dataset` and `split` `"adhoc"`.

Mitosis annotation payload: `{ "points": [{ "x_um": n, "y_um": n, "class": "MF" | "imposter" }] }`. One point per dividing cell (SPEC-06 §3).

## Example fixture (`frontend/lib/mock/research.json`)

The fixture must contain:
- two `RunSummary` entries: one valid run and one with `gate.valid = false`;
- one full `MetricsV1` document for the valid run;
- 20 `ItemRow`s;
- 6 `MitosisErrorCard`s;
- one `CompareV1`;
- 3 `Issue`s;
- 1 `AnnotationTask` of kind `mitosis_points`;
- 2 `QAItem`s.

Values are illustrative. Headline example:

```json
{"ns_m": {"value": 0.71, "ci_low": 0.66, "ci_high": 0.76, "n": 30, "status": "final"},
 "ns_g": {"value": 0.58, "ci_low": 0.49, "ci_high": 0.67, "n": 104, "status": "provisional"}}
```
