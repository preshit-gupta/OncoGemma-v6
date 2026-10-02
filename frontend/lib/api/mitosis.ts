import { apiFetch, idempotencyHeaders } from "@/lib/api/auth";
import mockMitosisData from "@/lib/mock/mitosis.json";

export interface VlmVerdict {
  verdict: "MITOTIC_FIGURE" | "NOT_MITOTIC_FIGURE" | "EQUIVOCAL";
  criteria: {
    membrane_absent: boolean;
    condensed_chromosome_projections: boolean;
    phase: "prometaphase" | "metaphase" | "anaphase" | "telophase" | "atypical" | "none";
    neoplastic_cell: boolean;
  };
  mimic:
    | "none"
    | "apoptotic_body"
    | "pyknotic_nucleus"
    | "hyperchromatic_interphase"
    | "lymphocyte"
    | "prophase"
    | "crush"
    | "other";
  rationale: string;
  rule_override: boolean;
}

export interface Candidate {
  id: string;
  hotspot_id: string | null;
  centroid_um: [number, number];
  p_a: number | null;
  p_b: number | null;
  vlm: VlmVerdict | null;
  in_tumor: boolean;
  final_decision: "mitosis" | "not_mitosis" | "equivocal";
  decision_path: "A" | "AB" | "ABC" | "human";
  review_label: "mitosis" | "not_mitosis" | null;
  counted: boolean;
  crop_url: string;
  context_url: string;
}

export interface Hpf {
  seq: number;
  center_um: [number, number];
  radius_um: number;
  count: number;
  tissue_coverage: number;
  tumor_fraction: number;
}

export interface MitosisSummary {
  count_total: number;
  n_hpf: number;
  area_mm2: number;
  per_mm2: number;
  mitotic_score: 1 | 2 | 3;
  n_equivocal: number;
  flags: ("hpf_count_lt_10")[];
}

export interface SlideGeom {
  width_px: number;
  height_px: number;
  mpp_x: number;
  mpp_y: number;
}

export interface Provenance {
  stage: string;
  model_versions: Record<string, string>;
  config_hash: string;
  run_mode: string;
}

export interface MitosisStageV6 {
  case_id: string;
  stage_execution_id: string;
  status: "queued" | "running" | "awaiting_review" | "confirmed" | "failed";
  slide: SlideGeom;
  candidates: Candidate[];
  hpfs: Hpf[];
  summary: MitosisSummary;
  provenance: Provenance;
}

// In-memory mock state
let mockState: MitosisStageV6 = JSON.parse(JSON.stringify(mockMitosisData));

function isInsideAnyHpf(candidate: Candidate, hpfs: Hpf[]): boolean {
  for (const hpf of hpfs) {
    const dx = candidate.centroid_um[0] - hpf.center_um[0];
    const dy = candidate.centroid_um[1] - hpf.center_um[1];
    const dist = Math.sqrt(dx * dx + dy * dy);
    if (dist <= hpf.radius_um) {
      return true;
    }
  }
  return false;
}

function recomputeMockState() {
  // Recompute candidate.counted per contract:
  // counted using review_label ?? (final_decision == "mitosis" && in_tumor)
  for (const c of mockState.candidates) {
    if (c.review_label !== null) {
      c.counted = c.review_label === "mitosis";
    } else {
      c.counted = c.final_decision === "mitosis" && c.in_tumor;
    }
  }

  // Count candidates inside HPFs
  let countInHpfs = 0;
  let equivocalInHpfs = 0;

  for (const c of mockState.candidates) {
    const inHpf = isInsideAnyHpf(c, mockState.hpfs);
    if (inHpf) {
      if (c.counted) {
        countInHpfs += 1;
      }
      if (c.final_decision === "equivocal" && c.review_label === null) {
        equivocalInHpfs += 1;
      }
    }
  }

  const areaMm2 = mockState.summary.area_mm2 || 0.216;
  const perMm2 = Number((countInHpfs / areaMm2).toFixed(2));
  let score: 1 | 2 | 3 = 1;
  if (perMm2 >= 7.30) {
    score = 3;
  } else if (perMm2 >= 3.65) {
    score = 2;
  }

  mockState.summary.count_total = countInHpfs;
  mockState.summary.per_mm2 = perMm2;
  mockState.summary.mitotic_score = score;
  mockState.summary.n_equivocal = equivocalInHpfs;
}

export async function getMitosis(caseId: string): Promise<MitosisStageV6> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    return JSON.parse(JSON.stringify(mockState));
  }

  const res = await apiFetch(`/api/v1/stages/mitosis/${caseId}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to fetch mitosis data (${res.status})`);
  }
  return res.json();
}

export async function reviewCandidate(
  caseId: string,
  candidateId: string,
  reviewLabel: "mitosis" | "not_mitosis" | null
): Promise<MitosisStageV6> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const cand = mockState.candidates.find((c) => c.id === candidateId);
    if (!cand) {
      const err: any = new Error("candidate_not_found");
      err.status = 404;
      throw err;
    }
    cand.review_label = reviewLabel;
    recomputeMockState();
    return JSON.parse(JSON.stringify(mockState));
  }

  const res = await apiFetch(`/api/v1/stages/mitosis/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      case_id: caseId,
      candidate_id: candidateId,
      review_label: reviewLabel,
    }),
  });

  if (!res.ok) {
    const errData = await res.json().catch(() => ({}));
    const err: any = new Error(errData.detail || errData.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = errData;
    throw err;
  }

  return res.json();
}

export async function addCandidate(
  caseId: string,
  centroidUm: [number, number]
): Promise<MitosisStageV6> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const newId = `m_u_${Date.now()}`;
    const newCand: Candidate = {
      id: newId,
      hotspot_id: null,
      centroid_um: centroidUm,
      p_a: null,
      p_b: null,
      vlm: null,
      in_tumor: true,
      final_decision: "mitosis",
      decision_path: "human",
      review_label: "mitosis",
      counted: true,
      crop_url: "/mock/crop_m1.png",
      context_url: "/mock/ctx_m1.png",
    };
    mockState.candidates.push(newCand);
    recomputeMockState();
    return JSON.parse(JSON.stringify(mockState));
  }

  const res = await apiFetch(`/api/v1/stages/mitosis/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      case_id: caseId,
      centroid_um: centroidUm,
    }),
  });

  if (!res.ok) {
    const errData = await res.json().catch(() => ({}));
    const err: any = new Error(errData.detail || errData.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = errData;
    throw err;
  }

  return res.json();
}

export async function replaceHpfs(caseId: string): Promise<MitosisStageV6> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    return JSON.parse(JSON.stringify(mockState));
  }

  const res = await apiFetch(`/api/v1/stages/mitosis/replace-hpfs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ case_id: caseId }),
  });

  if (!res.ok) {
    const errData = await res.json().catch(() => ({}));
    const err: any = new Error(errData.detail || errData.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = errData;
    throw err;
  }

  return res.json();
}

export async function confirmMitosis(
  caseId: string
): Promise<{ status: string; next_stage: string }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const unreviewedEquivocals: string[] = [];
    for (const c of mockState.candidates) {
      if (
        c.final_decision === "equivocal" &&
        c.review_label === null &&
        isInsideAnyHpf(c, mockState.hpfs)
      ) {
        unreviewedEquivocals.push(c.id);
      }
    }

    if (unreviewedEquivocals.length > 0) {
      const err: any = new Error("equivocal_unreviewed");
      err.status = 409;
      err.data = { error: "equivocal_unreviewed", ids: unreviewedEquivocals };
      throw err;
    }

    mockState.status = "confirmed";
    return { status: "confirmed", next_stage: "grading" };
  }

  const res = await apiFetch(`/api/v1/stages/mitosis/confirm`, {
    method: "POST",
    headers: { ...idempotencyHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ case_id: caseId }),
  });

  if (!res.ok) {
    const errData = await res.json().catch(() => ({}));
    const err: any = new Error(errData.detail || errData.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = errData;
    throw err;
  }

  return res.json();
}
