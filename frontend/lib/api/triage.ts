import { apiFetch, idempotencyHeaders } from "@/lib/api/auth";
import mockTriageData from "@/lib/mock/triage.json";

export interface SlideGeom {
  width_px: number;
  height_px: number;
  mpp_x: number;
  mpp_y: number;
}

export interface Heatmap {
  png_url: string; // 1 px per tile; alpha 0 outside tissue
  tile_um: number; // 224
  origin_um: [number, number]; // top-left of tile (0,0)
  nx: number;
  ny: number; // image size in tiles (= png width/height in px)
  value: "p_tumor_cal";
  head_version: string;
}

export type ScoreKind = "mean_p_tumor" | "prescan_expected_count" | "prescan_then_tumor";

export interface Hotspot {
  id: string; // "hs_01".. for model, "hs_u_<n>" for pathologist-added
  polygon_um: [number, number][]; // closed ring (first == last)
  window_um: number | null; // side of model square windows; null for free polygons
  rank: number | null; // 1 = best (model hotspots only)
  rank_score: number | null;
  score_kind: ScoreKind | null;
  tumor_fraction: number | null; // 0..1
  prescan_expected: number | null; // expected mitoses in window (when score_kind uses prescan)
  source: "model" | "pathologist_added" | "pathologist_modified";
  excluded: boolean;
  exclude_reason: string | null;
}

export interface Provenance {
  stage: string;
  model_versions: Record<string, string>;
  config_hash: string;
  run_mode: string;
}

export interface TriageStageV6 {
  case_id: string;
  stage_execution_id: string;
  status: "queued" | "running" | "awaiting_review" | "confirmed" | "failed";
  slide: SlideGeom;
  heatmap: Heatmap | null;
  tumor_threshold: number;
  hotspots: Hotspot[];
  machine_hotspots: Hotspot[];
  flags: ("hotspots_limited_by_tissue" | "no_invasive_tumor_detected")[];
  provenance: Provenance;
}

export type EditOp =
  | { op: "add"; polygon_um: [number, number][] }
  | { op: "modify"; id: string; polygon_um: [number, number][] }
  | { op: "exclude"; id: string; reason: string }
  | { op: "delete"; id: string };

export interface OverlapError {
  error: "hotspot_overlap";
  ids: [string, string][];
}

export interface InvalidPolygonError {
  error: "invalid_polygon";
  id: string;
  reason: "not_closed" | "too_many_vertices" | "out_of_bounds" | "area_out_of_range" | "self_intersecting";
}

// In-memory mock state
let mockState: TriageStageV6 = JSON.parse(JSON.stringify(mockTriageData));

function getAABB(polygon_um: [number, number][]) {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const [x, y] of polygon_um) {
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  return { minX, maxX, minY, maxY };
}

function checkAABBOverlap(
  boxA: { minX: number; maxX: number; minY: number; maxY: number },
  boxB: { minX: number; maxX: number; minY: number; maxY: number },
  minGapUm: number = 100
): boolean {
  // With minGapUm buffer, overlap area > 0 if distance in both dimensions < minGapUm
  const overlapX = Math.min(boxA.maxX, boxB.maxX) - Math.max(boxA.minX, boxB.minX) + minGapUm;
  const overlapY = Math.min(boxA.maxY, boxB.maxY) - Math.max(boxA.minY, boxB.minY) + minGapUm;
  return overlapX > 0 && overlapY > 0;
}

export async function getTriage(caseId: string): Promise<TriageStageV6> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    return JSON.parse(JSON.stringify(mockState));
  }

  const res = await apiFetch(`/api/v1/stages/triage/${caseId}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to fetch triage data (${res.status})`);
  }
  return res.json();
}

export async function postTriageEdits(
  caseId: string,
  edits: EditOp[]
): Promise<TriageStageV6> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    let nextHotspots: Hotspot[] = JSON.parse(JSON.stringify(mockState.hotspots));
    let nextUserIdx = nextHotspots.filter((h) => h.source.startsWith("pathologist_")).length + 1;

    for (const edit of edits) {
      if (edit.op === "add") {
        const newId = `hs_u_${nextUserIdx++}`;
        const newHs: Hotspot = {
          id: newId,
          polygon_um: edit.polygon_um,
          window_um: null,
          rank: null,
          rank_score: null,
          score_kind: null,
          tumor_fraction: null,
          prescan_expected: null,
          source: "pathologist_added",
          excluded: false,
          exclude_reason: null,
        };
        nextHotspots.push(newHs);
      } else if (edit.op === "modify") {
        const target = nextHotspots.find((h) => h.id === edit.id);
        if (target) {
          target.polygon_um = edit.polygon_um;
          target.source = "pathologist_modified";
        }
      } else if (edit.op === "exclude") {
        const target = nextHotspots.find((h) => h.id === edit.id);
        if (target) {
          target.excluded = true;
          target.exclude_reason = edit.reason;
        }
      } else if (edit.op === "delete") {
        nextHotspots = nextHotspots.filter((h) => h.id !== edit.id);
      }
    }

    // Validate overlaps among active hotspots
    const activeHotspots = nextHotspots.filter((h) => !h.excluded);
    const conflictingPairs: [string, string][] = [];

    for (let i = 0; i < activeHotspots.length; i++) {
      for (let j = i + 1; j < activeHotspots.length; j++) {
        const hsA = activeHotspots[i];
        const hsB = activeHotspots[j];
        const boxA = getAABB(hsA.polygon_um);
        const boxB = getAABB(hsB.polygon_um);
        if (checkAABBOverlap(boxA, boxB, 100)) {
          conflictingPairs.push([hsA.id, hsB.id]);
        }
      }
    }

    if (conflictingPairs.length > 0) {
      const err: any = new Error("hotspot_overlap");
      err.status = 422;
      err.data = { error: "hotspot_overlap", ids: conflictingPairs };
      throw err;
    }

    mockState.hotspots = nextHotspots;
    return JSON.parse(JSON.stringify(mockState));
  }

  const res = await apiFetch(`/api/v1/stages/triage/edits`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ case_id: caseId, edits }),
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

export async function confirmTriage(
  caseId: string,
  noInvasiveTumor: boolean = false
): Promise<{ status: string; next_stage: string | null }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    mockState.status = "confirmed";
    return {
      status: "confirmed",
      next_stage: noInvasiveTumor ? null : "mitosis",
    };
  }

  const res = await apiFetch(`/api/v1/stages/triage/confirm`, {
    method: "POST",
    headers: { ...idempotencyHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({
      case_id: caseId,
      no_invasive_tumor: noInvasiveTumor,
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
