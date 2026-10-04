import { apiFetch } from "@/lib/api/auth";
import mockData from "@/lib/mock/research.json";

export interface Metric {
  value: number | null;
  ci_low: number | null;
  ci_high: number | null;
  n: number;
  status: "final" | "provisional" | "invalid";
}

export interface Gate {
  valid: boolean;
  int_prov: number;
  int_fall: number;
  disjoint: boolean;
}

export interface RunSummary {
  id: string;
  name: string;
  dataset: string;
  split: "train" | "val" | "test";
  arm: string | null;
  status: "created" | "running" | "completed" | "cancelled" | "failed";
  is_locked_test: boolean;
  created_by: string;
  created_at: string;
  finished_at: string | null;
  n_items: number;
  n_failed: number;
  headline: { ns_m?: Metric; ns_g?: Metric };
  gate: Gate;
}

export interface RunDetail extends RunSummary {
  config_hash: string;
  registry_sha256: string;
  splits_lock_sha256: string;
  manifest_sha256: string;
  stages: string[];
  license_scopes: ("commercial_ok" | "research" | "pending")[];
}

export interface Confusion {
  labels: (1 | 2 | 3 | "none")[];
  matrix: number[][];
}

export interface SliceRow {
  slice: string;
  key: string;
  metric: string;
  value: number | null;
  ci_low: number | null;
  ci_high: number | null;
  n: number;
}

export interface Reliability {
  bins: { p_mean: number; frac_pos: number; n: number }[];
  ece: number;
}

export interface MetricsV1 {
  metrics_schema_version: 1;
  run_id: string;
  generated_at: string;
  bootstrap: { B: number; seed: number };
  coverage: number;
  headline: { ns_m?: Metric; ns_g?: Metric };
  stages: {
    s3?: { f1: Metric; p_at_k: Metric; coverage: number };
    s4?: {
      f1: Metric;
      precision: Metric;
      recall: Metric;
      ap: number | null;
      count_mae_per_2mm2: Metric;
      count_bias_per_2mm2: Metric;
      per_scanner: Record<string, Metric>;
      per_mag: Record<string, Metric>;
    };
    s5?: {
      f1_t: Metric;
      f1_p: Metric;
      f1_m: Metric;
      f1_high: Metric;
      macro_f1_lm: Metric;
      sum_mae: Metric;
      qwk: Metric;
      histotype_f1: Metric;
      ilc_f1: Metric;
    };
  };
  confusion: {
    grade?: Confusion;
    tubule?: Confusion;
    pleo?: Confusion;
    mitotic?: Confusion;
  };
  slices: SliceRow[];
  calibration: {
    p_a?: Reliability;
    p_b?: Reliability;
    p_tumor?: Reliability;
  };
  curves: {
    mitosis_pr?: {
      thresholds: number[];
      precision: number[];
      recall: number[];
      f1: number[];
    };
  };
  cost: {
    usd_total: number;
    usd_per_slide: number;
    by_model: Record<
      string,
      { calls: number; usd: number; p50_ms: number; p95_ms: number }
    >;
  };
}

export interface ItemRow {
  slide_id: string;
  patient_id: string;
  status:
    | "pending"
    | "running"
    | "succeeded"
    | "failed"
    | "excluded_qc"
    | "cancelled";
  failed_stage: string | null;
  error_class: string | null;
  gt: {
    grade: number | null;
    total: number | null;
    tubule: number | null;
    pleo: number | null;
    mitoses: number | null;
    histotype: string | null;
  };
  pred: {
    grade: number | null;
    total: number | null;
    tubule: number | null;
    pleo: number | null;
    mitoses: number | null;
    histotype: string | null;
  };
  sum_error: number | null;
  runtime_s: number | null;
  cost_usd: number | null;
}

export interface DecisionNode {
  id: string;
  task: string;
  entity_type: string;
  entity_id: string;
  producer_kind: "model" | "heuristic" | "human" | "fallback" | "shadow";
  producer_id: string;
  producer_version: string;
  status:
    | "ok"
    | "schema_invalid"
    | "timeout"
    | "unavailable"
    | "error"
    | "skipped";
  input_spec: Record<string, unknown>;
  output: unknown;
  latency_ms: number;
  children: DecisionNode[];
}

export interface MitosisErrorCard {
  slide_id: string;
  kind: "fp" | "fn";
  centroid_um: [number, number];
  crop_url: string;
  gt_points_um: [number, number][];
  pred_points_um: [number, number][];
  p_a: number | null;
  p_b: number | null;
  vlm_verdict: string | null;
  rule_override: boolean | null;
  decision_record_id: string | null;
}

export interface CompareRow {
  metric: string;
  a: Metric;
  b: Metric;
  delta: number;
  delta_low: number;
  delta_high: number;
  mcnemar_p?: number;
}

export interface CompareV1 {
  a: RunSummary;
  b: RunSummary;
  manifest_sha256: string;
  metrics: CompareRow[];
  slices: {
    slice: string;
    key: string;
    metric: string;
    delta: number;
    delta_low: number;
    delta_high: number;
  }[];
  flips: {
    slide_id: string;
    component: "grade" | "tubule" | "pleo" | "mitoses";
    a_correct: boolean;
    b_correct: boolean;
  }[];
}

export interface Issue {
  id: string;
  title: string;
  category: "biological" | "model" | "staging" | "technical";
  severity: "critical" | "high" | "medium" | "low";
  status: "open" | "triaged" | "in_progress" | "resolved" | "wont_fix";
  metric_impact: { metric: string; slice?: string; est_delta?: number } | null;
  evidence: {
    run_id: string;
    slide_id?: string;
    entity_type?: string;
    entity_id?: string;
    decision_record_id?: string;
  }[];
  spec_ref: string | null;
  owner: string | null;
  created_by: string;
  created_at: string;
  resolved_in: string | null;
  resolution_note: string | null;
}

export interface AnnotationTask {
  id: string;
  dataset: string;
  slide_id: string;
  kind: "mitosis_points" | "component_scores" | "grade" | "tumor_region";
  regions_um: [number, number][][];
  blind: boolean;
  definition_md: string;
  protocol_version: string;
  my_annotation: {
    id: string;
    status: "draft" | "submitted" | "adjudicated";
    payload: unknown;
  } | null;
}

export interface QAItem {
  patient_id: string;
  report_text_url: string;
  regex: Record<string, number | null>;
  llm: {
    values: Record<string, number | null>;
    evidence: { field: string; quote: string }[];
  };
  status: "pending" | "accepted" | "edited" | "excluded";
}

let mockResearchState = JSON.parse(JSON.stringify(mockData));

export async function getRuns(params?: {
  dataset?: string;
  split?: string;
  arm?: string;
  status?: string;
  cursor?: string;
}): Promise<{ items: RunSummary[]; next_cursor: string | null }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    let list: RunSummary[] = mockResearchState.runs;
    if (params?.dataset) {
      list = list.filter((r) => r.dataset === params.dataset);
    }
    if (params?.split) {
      list = list.filter((r) => r.split === params.split);
    }
    if (params?.status) {
      list = list.filter((r) => r.status === params.status);
    }
    return { items: list, next_cursor: null };
  }

  const query = new URLSearchParams(params as any).toString();
  const res = await apiFetch(`/api/v1/research/runs?${query}`);
  if (!res.ok) throw new Error(`Failed to fetch runs: ${res.status}`);
  return res.json();
}

export async function getRun(id: string): Promise<RunDetail> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const run = mockResearchState.runs.find((r: any) => r.id === id);
    if (!run) {
      const err: any = new Error("Run not found");
      err.status = 404;
      throw err;
    }
    return JSON.parse(JSON.stringify(run));
  }

  const res = await apiFetch(`/api/v1/research/runs/${id}`);
  if (!res.ok) throw new Error(`Failed to fetch run ${id}: ${res.status}`);
  return res.json();
}

export async function getRunMetrics(id: string): Promise<MetricsV1> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    if (mockResearchState.metrics.run_id === id || id === "run_01") {
      return JSON.parse(JSON.stringify(mockResearchState.metrics));
    }
    const emptyMetrics: MetricsV1 = {
      ...mockResearchState.metrics,
      run_id: id,
    };
    return emptyMetrics;
  }

  const res = await apiFetch(`/api/v1/research/runs/${id}/metrics`);
  if (!res.ok) throw new Error(`Failed to fetch metrics for ${id}: ${res.status}`);
  return res.json();
}

export async function getRunItems(
  id: string,
  params?: { status?: string; cursor?: string }
): Promise<{ items: ItemRow[]; next_cursor: string | null }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    let items: ItemRow[] = mockResearchState.items;
    if (params?.status) {
      items = items.filter((i) => i.status === params.status);
    }
    return { items, next_cursor: null };
  }

  const query = new URLSearchParams(params as any).toString();
  const res = await apiFetch(`/api/v1/research/runs/${id}/items?${query}`);
  if (!res.ok) throw new Error(`Failed to fetch items: ${res.status}`);
  return res.json();
}

export async function getRunItemDetail(
  id: string,
  slideId: string
): Promise<{ item: ItemRow; case_id: string; decisions: DecisionNode[] }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const detail = mockResearchState.item_details[slideId];
    if (detail) return JSON.parse(JSON.stringify(detail));
    const fallbackItem =
      mockResearchState.items.find((i: ItemRow) => i.slide_id === slideId) ||
      mockResearchState.items[0];
    return {
      item: fallbackItem,
      case_id: `c_${slideId}`,
      decisions: mockResearchState.item_details["TCGA-A2-1001"]?.decisions || [],
    };
  }

  const res = await apiFetch(`/api/v1/research/runs/${id}/items/${slideId}`);
  if (!res.ok) throw new Error(`Failed to fetch item detail: ${res.status}`);
  return res.json();
}

export async function getRunMitosisErrors(
  id: string,
  params?: { kind?: "fp" | "fn"; cursor?: string }
): Promise<{ items: MitosisErrorCard[]; next_cursor: string | null }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    let items: MitosisErrorCard[] = mockResearchState.mitosis_errors;
    if (params?.kind) {
      items = items.filter((e) => e.kind === params.kind);
    }
    return { items, next_cursor: null };
  }

  const query = new URLSearchParams(params as any).toString();
  const res = await apiFetch(`/api/v1/research/runs/${id}/errors/mitosis?${query}`);
  if (!res.ok) throw new Error(`Failed to fetch mitosis errors: ${res.status}`);
  return res.json();
}

export async function getRunMitosisCurves(
  id: string,
  params?: { tau_a?: number; tau_b?: number }
): Promise<{ pr: any; what_if?: { f1: number; precision: number; recall: number } }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const pr = mockResearchState.metrics.curves.mitosis_pr;
    let what_if = undefined;
    if (params?.tau_a !== undefined || params?.tau_b !== undefined) {
      const ta = params.tau_a ?? 0.5;
      const tb = params.tau_b ?? 0.5;
      const prec = Number((0.74 + (ta - 0.5) * 0.15).toFixed(2));
      const rec = Number((0.68 - (tb - 0.5) * 0.12).toFixed(2));
      const f1 = Number(((2 * prec * rec) / (prec + rec)).toFixed(2));
      what_if = { f1, precision: prec, recall: rec };
    }
    return { pr, what_if };
  }

  const query = new URLSearchParams(params as any).toString();
  const res = await apiFetch(`/api/v1/research/runs/${id}/curves/mitosis?${query}`);
  if (!res.ok) throw new Error(`Failed to fetch curves: ${res.status}`);
  return res.json();
}

export async function getCompare(a: string, b: string): Promise<CompareV1> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    return JSON.parse(JSON.stringify(mockResearchState.compare));
  }

  const res = await apiFetch(`/api/v1/research/compare?a=${a}&b=${b}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function getIssues(params?: {
  category?: string;
  severity?: string;
  status?: string;
}): Promise<Issue[]> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    let items: Issue[] = mockResearchState.issues;
    if (params?.category) items = items.filter((i) => i.category === params.category);
    if (params?.severity) items = items.filter((i) => i.severity === params.severity);
    if (params?.status) items = items.filter((i) => i.status === params.status);
    return JSON.parse(JSON.stringify(items));
  }

  const query = new URLSearchParams(params as any).toString();
  const res = await apiFetch(`/api/v1/research/issues?${query}`);
  if (!res.ok) throw new Error(`Failed to fetch issues: ${res.status}`);
  return res.json();
}

export async function createIssue(payload: {
  title: string;
  category: "biological" | "model" | "staging" | "technical";
  severity: "critical" | "high" | "medium" | "low";
  metric_impact?: any;
  evidence: any[];
  spec_ref?: string;
}): Promise<Issue> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const newIssue: Issue = {
      id: `iss_${Date.now()}`,
      title: payload.title,
      category: payload.category,
      severity: payload.severity,
      status: "open",
      metric_impact: payload.metric_impact || null,
      evidence: payload.evidence,
      spec_ref: payload.spec_ref || null,
      owner: null,
      created_by: "current_user",
      created_at: new Date().toISOString(),
      resolved_in: null,
      resolution_note: null,
    };
    mockResearchState.issues.push(newIssue);
    return newIssue;
  }

  const res = await apiFetch(`/api/v1/research/issues`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to create issue: ${res.status}`);
  return res.json();
}

export async function updateIssue(
  id: string,
  payload: {
    status?: "open" | "triaged" | "in_progress" | "resolved" | "wont_fix";
    owner?: string | null;
    resolved_in?: string | null;
    resolution_note?: string | null;
  }
): Promise<Issue> {
  if (payload.status === "resolved" && !payload.resolved_in) {
    const err: any = new Error("resolution_requires_run");
    err.status = 422;
    err.data = { error: "resolution_requires_run" };
    throw err;
  }

  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const issue = mockResearchState.issues.find((i: Issue) => i.id === id);
    if (!issue) throw new Error("Issue not found");
    if (payload.status) issue.status = payload.status;
    if (payload.owner !== undefined) issue.owner = payload.owner;
    if (payload.resolved_in !== undefined) issue.resolved_in = payload.resolved_in;
    if (payload.resolution_note !== undefined) issue.resolution_note = payload.resolution_note;
    return JSON.parse(JSON.stringify(issue));
  }

  const res = await apiFetch(`/api/v1/research/issues/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to update issue: ${res.status}`);
  return res.json();
}

export async function getAnnotationTasks(params?: {
  cursor?: string;
}): Promise<{ items: AnnotationTask[]; next_cursor: string | null }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    return { items: mockResearchState.annotation_tasks, next_cursor: null };
  }

  const query = new URLSearchParams(params as any).toString();
  const res = await apiFetch(`/api/v1/research/annotation-tasks?${query}`);
  if (!res.ok) throw new Error(`Failed to fetch tasks: ${res.status}`);
  return res.json();
}

export async function submitAnnotation(payload: {
  task_id: string;
  payload: any;
  status: "draft" | "submitted";
}): Promise<{ id: string; status: string }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const task = mockResearchState.annotation_tasks.find((t: any) => t.id === payload.task_id);
    if (task) {
      task.my_annotation = {
        id: `ann_${Date.now()}`,
        status: payload.status,
        payload: payload.payload,
      };
    }
    return { id: `ann_${Date.now()}`, status: payload.status };
  }

  const res = await apiFetch(`/api/v1/research/annotations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to submit annotation: ${res.status}`);
  return res.json();
}

export async function getQAItems(params?: {
  status?: string;
}): Promise<QAItem[]> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    let items: QAItem[] = mockResearchState.qa_items;
    if (params?.status) items = items.filter((q) => q.status === params.status);
    return JSON.parse(JSON.stringify(items));
  }

  const query = new URLSearchParams(params as any).toString();
  const res = await apiFetch(`/api/v1/research/labels-qa?${query}`);
  if (!res.ok) throw new Error(`Failed to fetch QA items: ${res.status}`);
  return res.json();
}

export async function reviewQAItem(
  patientId: string,
  payload: {
    action: "accept" | "edit" | "exclude";
    values?: any;
    reason?: string;
  }
): Promise<QAItem> {
  if ((payload.action === "edit" || payload.action === "exclude") && !payload.reason) {
    const err: any = new Error("reason_required");
    err.status = 422;
    throw err;
  }

  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const item = mockResearchState.qa_items.find((q: any) => q.patient_id === patientId);
    if (!item) throw new Error("QA item not found");
    if (payload.action === "accept") item.status = "accepted";
    if (payload.action === "edit") item.status = "edited";
    if (payload.action === "exclude") item.status = "excluded";
    if (payload.values) item.llm.values = payload.values;
    return JSON.parse(JSON.stringify(item));
  }

  const res = await apiFetch(`/api/v1/research/labels-qa/${patientId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to update QA item: ${res.status}`);
  return res.json();
}

// Pipeline stages in canonical order (backend/app/core/run_context.py). A batch runs a prefix
// of these starting at ingest and ending at a stage that stops for review (eval/harness/runs.py).
export const PIPELINE_STAGES = ["ingest", "preprocess", "qc", "triage", "mitosis", "grading"] as const;
export type PipelineStage = (typeof PIPELINE_STAGES)[number];
export const RUN_END_STAGES = ["triage", "mitosis", "grading"] as const;
export type RunEndStage = (typeof RUN_END_STAGES)[number];

export function stagesThrough(end: RunEndStage): PipelineStage[] {
  return PIPELINE_STAGES.slice(0, PIPELINE_STAGES.indexOf(end) + 1);
}

export type BatchSource =
  | { manifest_uri: string; split: "train" | "val" | "test"; confirm_test_access?: string }
  | { gcs_prefix: string; specimen_type: "resection" | "core_biopsy"; mpp_override?: number };

export interface CreateBatchPayload {
  name: string;
  source: BatchSource;
  stages: PipelineStage[];
  mode: "auto" | "manual";
  concurrency: number;
}

export type BatchStatus = "created" | "running" | "completed" | "cancelled" | "failed";
export const BATCH_FINAL_STATUSES: BatchStatus[] = ["completed", "cancelled", "failed"];

// Payload of GET /api/v1/batches/{id}/events (research_v1.md).
export interface BatchEvent {
  status: BatchStatus;
  counts: Partial<Record<ItemRow["status"], number>>;
  failures_by_error_class: Record<string, number>;
}

// Mock batches: scripted progress, one event per second (WP-9.2 card).
const MOCK_BATCH_SIZE = 10;
const MOCK_ITEMS_PER_TICK = 2;
const mockBatches: Record<
  string,
  { status: BatchStatus; counts: Record<string, number>; failures: Record<string, number>; failedOnce: boolean }
> = {};

function mockBatchEvent(id: string): BatchEvent {
  const b = mockBatches[id];
  return {
    status: b.status,
    counts: { ...b.counts },
    failures_by_error_class: { ...b.failures },
  };
}

function mockBatchTick(id: string) {
  const b = mockBatches[id];
  if (b.status !== "running") return;
  // Finish the running items (the first finished item of a batch fails), then start the next ones.
  let finished = b.counts.running ?? 0;
  if (finished > 0 && !b.failedOnce) {
    b.failedOnce = true;
    b.counts.failed = (b.counts.failed ?? 0) + 1;
    b.failures.mock_error = (b.failures.mock_error ?? 0) + 1;
    finished -= 1;
  }
  b.counts.succeeded = (b.counts.succeeded ?? 0) + finished;
  const started = Math.min(MOCK_ITEMS_PER_TICK, b.counts.pending ?? 0);
  b.counts.pending = (b.counts.pending ?? 0) - started;
  b.counts.running = started;
  if (started === 0) b.status = "completed";
}

async function batchErrorMessage(res: Response, what: string): Promise<string> {
  const body = await res.json().catch(() => ({}));
  const detail = typeof body.detail === "string" ? body.detail : body.detail ? JSON.stringify(body.detail) : null;
  return `${what}: ${res.status}${detail ? ` ${detail}` : ""}`;
}

export async function createBatch(payload: CreateBatchPayload): Promise<{ batch_id: string }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const batch_id = `batch_${Date.now()}`;
    mockBatches[batch_id] = {
      status: "running",
      counts: { pending: MOCK_BATCH_SIZE },
      failures: {},
      failedOnce: false,
    };
    return { batch_id };
  }

  const res = await apiFetch(`/api/v1/batches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await batchErrorMessage(res, "Failed to create batch"));
  return res.json();
}

/**
 * Subscribe to batch progress (SSE, GET /api/v1/batches/{id}/events). `onEvent` receives every
 * progress message; the subscription closes itself when the batch reaches a final status.
 * Returns an unsubscribe function.
 */
export function subscribeBatchEvents(
  id: string,
  onEvent: (event: BatchEvent) => void,
  onError: (message: string) => void
): () => void {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    if (!mockBatches[id]) {
      onError(`Unknown batch ${id}`);
      return () => {};
    }
    onEvent(mockBatchEvent(id));
    const interval = setInterval(() => {
      mockBatchTick(id);
      const event = mockBatchEvent(id);
      onEvent(event);
      if (BATCH_FINAL_STATUSES.includes(event.status)) clearInterval(interval);
    }, 1000);
    return () => clearInterval(interval);
  }

  const source = new EventSource(`/api/v1/batches/${id}/events`, { withCredentials: true });
  let final = false;
  source.onmessage = (msg) => {
    let event: BatchEvent;
    try {
      event = JSON.parse(msg.data);
    } catch {
      source.close();
      onError(`Malformed batch event: ${String(msg.data).slice(0, 200)}`);
      return;
    }
    onEvent(event);
    if (BATCH_FINAL_STATUSES.includes(event.status)) {
      final = true;
      source.close();
    }
  };
  source.onerror = () => {
    // The server ends the stream after the final event; anything else is a real failure.
    // While readyState is CONNECTING the browser is retrying on its own.
    if (final) return;
    if (source.readyState === EventSource.CLOSED) {
      onError(`Batch progress stream for ${id} closed`);
    }
  };
  return () => source.close();
}

export async function cancelBatch(id: string): Promise<void> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const b = mockBatches[id];
    if (b && !BATCH_FINAL_STATUSES.includes(b.status)) {
      b.status = "cancelled";
      b.counts.cancelled = (b.counts.pending ?? 0) + (b.counts.running ?? 0);
      b.counts.pending = 0;
      b.counts.running = 0;
    }
    return;
  }
  const res = await apiFetch(`/api/v1/batches/${id}/cancel`, { method: "POST" });
  if (!res.ok) throw new Error(`Failed to cancel batch: ${res.status}`);
}

export async function retryBatch(id: string, payload?: { statuses: string[] }): Promise<void> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const b = mockBatches[id];
    if (b) {
      b.counts.pending = (b.counts.pending ?? 0) + (b.counts.failed ?? 0);
      b.counts.failed = 0;
      b.failures = {};
      b.status = "running";
    }
    return;
  }
  const res = await apiFetch(`/api/v1/batches/${id}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || { statuses: ["failed"] }),
  });
  if (!res.ok) throw new Error(`Failed to retry batch: ${res.status}`);
}
