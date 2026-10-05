import { apiFetch, idempotencyHeaders } from "@/lib/api/auth";
import mockGradingData from "@/lib/mock/grading.json";

export interface TubuleSample {
  id: string;
  center_um: [number, number];
  size_um: 512;
  mpp: 1.0;
  image_url: string;
  stratum: number;
  tumor_area_um2: number;
  estimate: { tumor_present: boolean; tubule_percent: number } | null;
  review: { tumor_present?: boolean; tubule_percent?: number; by: string; at: string } | null;
}

export interface PleoField {
  id: string;
  center_um: [number, number];
  size_um: 128;
  mpp: 0.25;
  image_url: string;
  stratum: number;
  estimate: { pleomorphism_score: 1 | 2 | 3 } | null;
  nuclei: { n: number; area_p50_um2: number; area_cv: number } | null;
  verification?: { producer: string; pleomorphism_score: 1 | 2 | 3 | null; agrees: boolean | null } | null;
  review: { pleomorphism_score?: 1 | 2 | 3; by: string; at: string } | null;
}

export type Flag = "needs_human" | "insufficient_nuclei" | "near_grade_boundary" | "hpf_count_lt_10";

export interface SlideGeom {
  width_px: number;
  height_px: number;
  mpp_x: number;
  mpp_y: number;
}

export interface ProvenanceInfo {
  stage: string;
  model_versions: Record<string, string>;
  config_hash: string;
  run_mode: string;
}

export interface GradingStageV6 {
  case_id: string;
  stage_execution_id: string;
  status: "queued" | "running" | "awaiting_review" | "confirmed" | "failed";
  slide: SlideGeom;
  tubule: {
    samples: TubuleSample[];
    percent: number | null;
    score: 1 | 2 | 3 | null;
    estimator: string;
    n_used: number;
  };
  pleomorphism: {
    fields: PleoField[];
    score: 1 | 2 | 3 | null;
    estimator: string;
    aggregation: "mode" | "p75" | "model";
  };
  mitotic: {
    score: 1 | 2 | 3 | null;
    count_total: number;
    n_hpf: number;
    area_mm2: number;
    per_mm2: number;
    // copied from the Stage 4 summary: fewer HPFs than hpf_target is inadequate tissue
    flags: "hpf_count_lt_10"[];
    hpf_target: number;
  };
  histotype: {
    type: string | null;
    estimator: string;
    rationale: string;
    confirmed: boolean;
    confirmed_by: string | null;
  };
  total: number | null;
  grade: 1 | 2 | 3 | null;
  flags: Flag[];
  overrides: {
    tubule_score?: 1 | 2 | 3;
    pleo_score?: 1 | 2 | 3;
    histotype?: string;
    reasons: Record<string, string>;
  };
  provenance: ProvenanceInfo;
}

let mockState: GradingStageV6 = JSON.parse(JSON.stringify(mockGradingData));

export function resetMockGradingState(): void {
  mockState = JSON.parse(JSON.stringify(mockGradingData));
}

function recomputeMockGrading(): void {
  // 1. Tubule recomputation
  const activeTubuleSamples = mockState.tubule.samples.filter((s) => {
    const tumorPresent = s.review?.tumor_present ?? s.estimate?.tumor_present ?? false;
    return tumorPresent;
  });

  mockState.tubule.n_used = activeTubuleSamples.length;

  if (activeTubuleSamples.length > 0) {
    let sumWeight = 0;
    let sumWeightedPct = 0;
    for (const sample of activeTubuleSamples) {
      const area = sample.tumor_area_um2 > 0 ? sample.tumor_area_um2 : 1;
      const pct = sample.review?.tubule_percent ?? sample.estimate?.tubule_percent ?? 0;
      sumWeight += area;
      sumWeightedPct += area * pct;
    }
    const finalPct = sumWeight > 0 ? sumWeightedPct / sumWeight : 0;
    mockState.tubule.percent = Number(finalPct.toFixed(1));

    // Cutpoints: >75% -> 1, 10-75% -> 2, <10% -> 3
    if (finalPct > 75) {
      mockState.tubule.score = 1;
    } else if (finalPct >= 10) {
      mockState.tubule.score = 2;
    } else {
      mockState.tubule.score = 3;
    }
  } else {
    mockState.tubule.percent = null;
    mockState.tubule.score = null;
  }

  // 2. Pleomorphism recomputation
  const pleoScores: (1 | 2 | 3)[] = [];
  for (const field of mockState.pleomorphism.fields) {
    const sc = field.review?.pleomorphism_score ?? field.estimate?.pleomorphism_score;
    if (sc) pleoScores.push(sc);
  }

  if (pleoScores.length > 0) {
    if (mockState.pleomorphism.aggregation === "p75") {
      pleoScores.sort((a, b) => a - b);
      const idx = Math.min(pleoScores.length - 1, Math.floor(pleoScores.length * 0.75));
      mockState.pleomorphism.score = pleoScores[idx];
    } else if (mockState.pleomorphism.aggregation === "mode") {
      const counts: Record<number, number> = { 1: 0, 2: 0, 3: 0 };
      for (const s of pleoScores) counts[s] = (counts[s] || 0) + 1;
      let maxCount = -1;
      let modeScore: 1 | 2 | 3 = 2;
      for (const s of [1, 2, 3] as const) {
        if (counts[s] > maxCount) {
          maxCount = counts[s];
          modeScore = s;
        }
      }
      mockState.pleomorphism.score = modeScore;
    } else {
      // "model" aggregation: keep existing score unless review provided
      const anyReviewed = mockState.pleomorphism.fields.some((f) => f.review?.pleomorphism_score);
      if (anyReviewed) {
        const counts: Record<number, number> = { 1: 0, 2: 0, 3: 0 };
        for (const s of pleoScores) counts[s] = (counts[s] || 0) + 1;
        let maxCount = -1;
        let modeScore: 1 | 2 | 3 = 2;
        for (const s of [1, 2, 3] as const) {
          if (counts[s] > maxCount) {
            maxCount = counts[s];
            modeScore = s;
          }
        }
        mockState.pleomorphism.score = modeScore;
      }
    }
  } else {
    mockState.pleomorphism.score = null;
  }

  // 3. Nottingham Total and Grade
  const effT = mockState.overrides.tubule_score ?? mockState.tubule.score;
  const effP = mockState.overrides.pleo_score ?? mockState.pleomorphism.score;
  const effM = mockState.mitotic.score;

  if (effT !== null && effP !== null && effM !== null) {
    const total = effT + effP + effM;
    mockState.total = total;
    if (total <= 5) {
      mockState.grade = 1;
    } else if (total <= 7) {
      mockState.grade = 2;
    } else {
      mockState.grade = 3;
    }

    if ([5, 6, 7, 8].includes(total)) {
      if (!mockState.flags.includes("near_grade_boundary")) {
        mockState.flags.push("near_grade_boundary");
      }
    } else {
      mockState.flags = mockState.flags.filter((f) => f !== "near_grade_boundary");
    }
  } else {
    mockState.total = null;
    mockState.grade = null;
    mockState.flags = mockState.flags.filter((f) => f !== "near_grade_boundary");
  }

  // Check needs_human flag
  const hasUnreviewedFailedTubule = mockState.tubule.samples.some(
    (s) => s.estimate === null && s.review === null
  );
  const hasUnreviewedFailedPleo = mockState.pleomorphism.fields.some(
    (f) => f.estimate === null && f.review === null
  );
  if (hasUnreviewedFailedTubule || hasUnreviewedFailedPleo) {
    if (!mockState.flags.includes("needs_human")) {
      mockState.flags.push("needs_human");
    }
  } else {
    mockState.flags = mockState.flags.filter((f) => f !== "needs_human");
  }
}

export async function getGrading(caseId: string): Promise<GradingStageV6> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    return JSON.parse(JSON.stringify(mockState));
  }

  const res = await apiFetch(`/api/v1/stages/grading/${caseId}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || err.error || `Failed to fetch grading stage (${res.status})`);
  }
  return res.json();
}

export async function reviewSample(payload: {
  case_id: string;
  kind: "tubule" | "pleo";
  sample_id: string;
  value:
    | { tumor_present?: boolean; tubule_percent?: number }
    | { pleomorphism_score?: 1 | 2 | 3 };
}): Promise<GradingStageV6> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    if (payload.kind === "tubule") {
      const sample = mockState.tubule.samples.find((s) => s.id === payload.sample_id);
      if (!sample) {
        const err: any = new Error("sample_not_found");
        err.status = 404;
        throw err;
      }
      const val = payload.value as { tumor_present?: boolean; tubule_percent?: number };
      sample.review = {
        tumor_present: val.tumor_present ?? sample.estimate?.tumor_present ?? false,
        tubule_percent: val.tubule_percent ?? sample.estimate?.tubule_percent ?? 0,
        by: "pathologist",
        at: new Date().toISOString(),
      };
    } else if (payload.kind === "pleo") {
      const field = mockState.pleomorphism.fields.find((f) => f.id === payload.sample_id);
      if (!field) {
        const err: any = new Error("sample_not_found");
        err.status = 404;
        throw err;
      }
      const val = payload.value as { pleomorphism_score?: 1 | 2 | 3 };
      field.review = {
        pleomorphism_score: val.pleomorphism_score ?? 2,
        by: "pathologist",
        at: new Date().toISOString(),
      };
    }
    recomputeMockGrading();
    return JSON.parse(JSON.stringify(mockState));
  }

  const res = await apiFetch(`/api/v1/stages/grading/review-sample`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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

export async function overrideComponent(payload: {
  case_id: string;
  component: "tubule" | "pleo" | "histotype";
  value: any;
  reason: string;
}): Promise<GradingStageV6> {
  if (payload.reason.trim().length < 10) {
    const err: any = new Error("reason_too_short");
    err.status = 422;
    err.data = { error: "reason_too_short" };
    throw err;
  }

  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    if (payload.component === "tubule") {
      if (payload.value === null || payload.value === undefined) {
        delete mockState.overrides.tubule_score;
        delete mockState.overrides.reasons["tubule"];
      } else {
        mockState.overrides.tubule_score = payload.value as (1 | 2 | 3);
        mockState.overrides.reasons["tubule"] = payload.reason;
      }
    } else if (payload.component === "pleo") {
      if (payload.value === null || payload.value === undefined) {
        delete mockState.overrides.pleo_score;
        delete mockState.overrides.reasons["pleo"];
      } else {
        mockState.overrides.pleo_score = payload.value as (1 | 2 | 3);
        mockState.overrides.reasons["pleo"] = payload.reason;
      }
    } else if (payload.component === "histotype") {
      if (payload.value === null || payload.value === undefined) {
        delete mockState.overrides.histotype;
        delete mockState.overrides.reasons["histotype"];
      } else {
        mockState.overrides.histotype = payload.value;
        mockState.overrides.reasons["histotype"] = payload.reason;
        mockState.histotype.type = payload.value;
      }
    }
    recomputeMockGrading();
    return JSON.parse(JSON.stringify(mockState));
  }

  const res = await apiFetch(`/api/v1/stages/grading/override`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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

export async function confirmHistotype(payload: {
  case_id: string;
  type: string;
}): Promise<GradingStageV6> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    mockState.histotype.type = payload.type;
    mockState.histotype.confirmed = true;
    mockState.histotype.confirmed_by = "pathologist";
    return JSON.parse(JSON.stringify(mockState));
  }

  const res = await apiFetch(`/api/v1/stages/grading/histotype/confirm`, {
    method: "POST",
    headers: { ...idempotencyHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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

export async function confirmGrading(payload: {
  case_id: string;
}): Promise<{ status: "confirmed"; case_status: "done"; next_stage: null }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    if (!mockState.histotype.confirmed) {
      const err: any = new Error("histotype_unconfirmed");
      err.status = 409;
      err.data = { error: "histotype_unconfirmed" };
      throw err;
    }

    const effT = mockState.overrides.tubule_score ?? mockState.tubule.score;
    const effP = mockState.overrides.pleo_score ?? mockState.pleomorphism.score;
    const effM = mockState.mitotic.score;
    const missing: string[] = [];
    if (effT === null || effT === undefined) missing.push("tubule");
    if (effP === null || effP === undefined) missing.push("pleomorphism");
    if (effM === null || effM === undefined) missing.push("mitotic");

    if (missing.length > 0) {
      const err: any = new Error("missing_component");
      err.status = 409;
      err.data = { error: "missing_component", components: missing };
      throw err;
    }

    if (mockState.status !== "awaiting_review" && mockState.status !== "confirmed") {
      const err: any = new Error("not_awaiting_review");
      err.status = 409;
      err.data = { error: "not_awaiting_review" };
      throw err;
    }

    mockState.status = "confirmed";
    return { status: "confirmed", case_status: "done", next_stage: null };
  }

  const res = await apiFetch(`/api/v1/stages/grading/confirm`, {
    method: "POST",
    headers: { ...idempotencyHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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
