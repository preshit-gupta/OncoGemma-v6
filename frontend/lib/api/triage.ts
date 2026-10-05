import { apiFetch, idempotencyHeaders } from "@/lib/api/auth";
import mockTriageData from "@/lib/mock/triage.json";
import mockTriageInadequateData from "@/lib/mock/triage_inadequate.json";

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
  id: string; // "hs_01".. for model (by rank), "hs_u_<n>" for pathologist-added
  center_um: [number, number]; // centre of the HPF circle
  hpf_diameter_um: number; // circle diameter
  polygon_um: [number, number][]; // the padded frame, closed ring (first == last)
  window_um: number; // frame side
  rank: number | null; // 1 = best (model sites only)
  rank_score: number | null;
  score_kind: ScoreKind | null;
  tissue_fraction: number | null; // 0..1 over the circle; null when pinned
  tumor_fraction: number | null; // 0..1 over the circle; null when pinned
  prescan_expected: number | null; // expected mitoses in the circle (when score_kind uses prescan)
  at_periphery: boolean; // the site lies at the tumour edge
  front_distance_um: number | null; // distance from the tumour front
  source: "model" | "pathologist_added" | "pathologist_modified"; // modified = moved
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
  hpf_target: number; // sites wanted; fewer active sites is inadequate tissue
  n_sites_available: number; // valid candidate sites before the disjoint selection
  provenance: Provenance;
}

export type EditOp =
  | { op: "add"; center_um: [number, number] }
  | { op: "move"; id: string; center_um: [number, number] }
  | { op: "exclude"; id: string; reason: string }
  | { op: "restore"; id: string }
  | { op: "delete"; id: string };

export interface OverlapError {
  error: "hotspot_overlap";
  ids: [string, string][];
}

export interface InvalidSiteError {
  error: "invalid_site";
  id: string;
  reason: "out_of_bounds" | "unknown_site";
}

export interface TooManySitesError {
  error: "too_many_sites";
  hpf_target: number;
}

export interface HpfSitesLtTargetError {
  error: "hpf_sites_lt_10";
  n_active: number;
  hpf_target: number;
}

// In-memory mock state
let mockState: TriageStageV6 | null = null;

function getMockState(): TriageStageV6 {
  if (mockState === null) mockState = loadMockState();
  return mockState;
}

function mockError(status: number, data: Record<string, unknown>) {
  const err: any = new Error(String(data.error));
  err.status = status;
  err.data = data;
  return err;
}

// The frame is a square of side window_um centred on the circle; the server builds it, the mock mimics it.
function mockFrame(center: [number, number], windowUm: number): [number, number][] {
  const h = windowUm / 2;
  const [x, y] = center;
  return [[x - h, y - h], [x + h, y - h], [x + h, y + h], [x - h, y + h], [x - h, y - h]];
}

// Query flag ?mock=inadequate switches the fixture to the inadequate-tissue state.
function loadMockState(): TriageStageV6 {
  const wantsInadequate =
    typeof window !== "undefined" && new URLSearchParams(window.location.search).get("mock") === "inadequate";
  return JSON.parse(JSON.stringify(wantsInadequate ? mockTriageInadequateData : mockTriageData));
}

export async function getTriage(caseId: string): Promise<TriageStageV6> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    return JSON.parse(JSON.stringify(getMockState()));
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
    const state = getMockState();
    const template = state.hotspots[0];
    let nextHotspots: Hotspot[] = JSON.parse(JSON.stringify(state.hotspots));
    let nextUserIdx = nextHotspots.filter((h) => h.id.startsWith("hs_u_")).length + 1;

    for (const edit of edits) {
      if (edit.op === "add") {
        nextHotspots.push({
          id: `hs_u_${nextUserIdx++}`,
          center_um: edit.center_um,
          hpf_diameter_um: template.hpf_diameter_um,
          polygon_um: mockFrame(edit.center_um, template.window_um),
          window_um: template.window_um,
          rank: null,
          rank_score: null,
          score_kind: null,
          tissue_fraction: null,
          tumor_fraction: null,
          prescan_expected: null,
          at_periphery: false,
          front_distance_um: null,
          source: "pathologist_added",
          excluded: false,
          exclude_reason: null,
        });
        continue;
      }
      const target = nextHotspots.find((h) => h.id === edit.id);
      if (!target) throw mockError(422, { error: "invalid_site", id: edit.id, reason: "unknown_site" });
      if (edit.op === "move") {
        target.center_um = edit.center_um;
        target.polygon_um = mockFrame(edit.center_um, target.window_um);
        target.source = "pathologist_modified";
      } else if (edit.op === "exclude") {
        target.excluded = true;
        target.exclude_reason = edit.reason;
      } else if (edit.op === "restore") {
        target.excluded = false;
        target.exclude_reason = null;
      } else if (edit.op === "delete") {
        nextHotspots = nextHotspots.filter((h) => h.id !== edit.id);
      }
    }

    const active = nextHotspots.filter((h) => !h.excluded);
    const slideW = state.slide.width_px * state.slide.mpp_x;
    const slideH = state.slide.height_px * state.slide.mpp_y;
    for (const h of active) {
      const r = h.hpf_diameter_um / 2;
      const [x, y] = h.center_um;
      if (x - r < 0 || y - r < 0 || x + r > slideW || y + r > slideH) {
        throw mockError(422, { error: "invalid_site", id: h.id, reason: "out_of_bounds" });
      }
    }

    // Circles must not overlap: centre distance below one diameter. Frames may overlap.
    const conflictingPairs: [string, string][] = [];
    for (let i = 0; i < active.length; i++) {
      for (let j = i + 1; j < active.length; j++) {
        const a = active[i];
        const b = active[j];
        const dist = Math.hypot(a.center_um[0] - b.center_um[0], a.center_um[1] - b.center_um[1]);
        if (dist < (a.hpf_diameter_um + b.hpf_diameter_um) / 2) conflictingPairs.push([a.id, b.id]);
      }
    }
    if (conflictingPairs.length > 0) {
      throw mockError(422, { error: "hotspot_overlap", ids: conflictingPairs });
    }
    if (active.length > state.hpf_target) {
      throw mockError(422, { error: "too_many_sites", hpf_target: state.hpf_target });
    }

    state.hotspots = nextHotspots;
    return JSON.parse(JSON.stringify(state));
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
  noInvasiveTumor: boolean = false,
  acceptFewerHpfs: boolean = false
): Promise<{ status: string; next_stage: string | null; accept_fewer_hpfs: boolean }> {
  if (process.env.NEXT_PUBLIC_API_MOCK === "1") {
    const state = getMockState();
    const nActive = state.hotspots.filter((h) => !h.excluded).length;
    if (!noInvasiveTumor && nActive < state.hpf_target && !acceptFewerHpfs) {
      throw mockError(409, { error: "hpf_sites_lt_10", n_active: nActive, hpf_target: state.hpf_target });
    }
    state.status = "confirmed";
    return {
      status: "confirmed",
      next_stage: noInvasiveTumor ? null : "mitosis",
      accept_fewer_hpfs: acceptFewerHpfs,
    };
  }

  const res = await apiFetch(`/api/v1/stages/triage/confirm`, {
    method: "POST",
    headers: { ...idempotencyHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({
      case_id: caseId,
      no_invasive_tumor: noInvasiveTumor,
      accept_fewer_hpfs: acceptFewerHpfs,
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
