import { apiFetch, getCookie, idempotencyHeaders } from "./api/auth";

export const API_BASE = "";

export type SpecimenType = "resection" | "core_biopsy";

export interface Case {
  id: string;
  created_by: string;
  status: string;
  created_at: string;
  specimen_type: SpecimenType | "unknown";
}

export interface CaseDetail extends Case {
  slides: Array<{
    id: string;
    gcs_uri_original: string;
    gcs_uri_pyramid?: string;
    format?: string;
    scanner?: string;
    status?: string;
    mpp_x?: number;
    mpp_y?: number;
    base_mag?: number;
    width_px?: number;
    height_px?: number;
    checksum_sha256?: string;
    label_stripped_at?: string;
  }>;
  stages: Array<{
    id: string;
    stage: string;
    attempt: number;
    status: string;
    output_ref?: string;
    error?: string;
    started_at?: string;
    completed_at?: string;
  }>;
  tile_url_template?: string | null;
}

export function formatApiError(errData: any, fallbackMessage: string): string {
  if (!errData) return fallbackMessage;
  if (typeof errData === "string") return errData;
  const detail = errData.detail !== undefined ? errData.detail : errData;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail.map((d: any) => {
      if (typeof d === "string") return d;
      if (d && typeof d === "object") {
        const field = Array.isArray(d.loc) ? d.loc.slice(1).join(".") : d.loc;
        const msg = d.msg || JSON.stringify(d);
        return field ? `${field}: ${msg}` : msg;
      }
      return String(d);
    });
    return msgs.join("; ") || fallbackMessage;
  }
  if (typeof detail === "object" && detail !== null) {
    if (detail.message) {
      if (Array.isArray(detail.missing_items) && detail.missing_items.length > 0) {
        return `${detail.message}: ${detail.missing_items.join(", ")}`;
      }
      return detail.message;
    }
    if (detail.missing_items && Array.isArray(detail.missing_items)) {
      return `Missing items: ${detail.missing_items.join(", ")}`;
    }
    if (detail.error && typeof detail.error === "string") {
      return detail.error;
    }
    try {
      return JSON.stringify(detail);
    } catch (_) {
      return fallbackMessage;
    }
  }
  return String(detail) || fallbackMessage;
}

export async function fetchCases(): Promise<Case[]> {
  const res = await apiFetch(`${API_BASE}/api/v1/cases`);
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, "Failed to fetch cases"));
  }
  return res.json();
}

export async function createCase(specimenType: SpecimenType): Promise<Case> {
  const res = await apiFetch(`${API_BASE}/api/v1/cases`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ specimen_type: specimenType }),
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, "Failed to create case"));
  }
  return res.json();
}

export async function updateCaseSpecimenType(caseId: string, specimenType: SpecimenType) {
  const res = await apiFetch(`${API_BASE}/api/v1/cases/${caseId}/specimen-type`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ specimen_type: specimenType }),
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, `Failed to set specimen type (${res.status})`));
  }
  return res.json();
}

export async function uploadSlideDirectToGCS(
  caseId: string,
  file: File,
  onProgress?: (percent: number) => void
): Promise<any> {
  const sessionKey = `og_upload_session_${caseId}`;
  let upload_url: string = "";
  let gcs_uri: string = "";
  // Signed headers: the PUT must send exactly these or GCS rejects the signature.
  let upload_headers: Record<string, string> = {};

  // Check localStorage for active resumable upload session (Issue #413)
  try {
    const saved = localStorage.getItem(sessionKey);
    if (saved) {
      const parsed = JSON.parse(saved);
      if (parsed.fileName === file.name && parsed.fileSize === file.size && parsed.upload_url && parsed.upload_headers) {
        upload_url = parsed.upload_url;
        gcs_uri = parsed.gcs_uri;
        upload_headers = parsed.upload_headers;
      }
    }
  } catch (_) {}

  // 1. Request Signed Upload URL from FastAPI control plane if not cached
  if (!upload_url) {
    const urlRes = await apiFetch(`${API_BASE}/api/v1/cases/${caseId}/slide/upload-url`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        filename: file.name,
        size_bytes: file.size,
        content_type: file.type || "application/octet-stream",
      }),
    });

    if (!urlRes.ok) {
      const body = await urlRes.json().catch(() => null);
      throw new Error(`Failed to acquire direct upload URL (HTTP ${urlRes.status}): ${formatApiError(body, urlRes.statusText)}`);
    }

    const data = await urlRes.json();
    upload_url = data.upload_url;
    gcs_uri = data.gcs_uri;
    upload_headers = data.upload_headers;

    try {
      localStorage.setItem(sessionKey, JSON.stringify({
        upload_url,
        gcs_uri,
        upload_headers,
        fileName: file.name,
        fileSize: file.size,
        startedAt: Date.now(),
      }));
    } catch (_) {}
  }

  // 2. Upload file directly from browser to GCS bucket via Signed URL
  await new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();

    if (xhr.upload && onProgress) {
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
          const percent = Math.round((e.loaded / e.total) * 100);
          onProgress(percent);
        }
      });
    }

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
      } else {
        reject(new Error(`Direct GCS upload failed with status ${xhr.status}`));
      }
    });

    xhr.addEventListener("error", () => reject(new Error("Network connection error during direct GCS upload")));
    xhr.addEventListener("abort", () => reject(new Error("Direct GCS upload aborted")));

    xhr.open("PUT", upload_url);
    for (const [name, value] of Object.entries(upload_headers)) {
      xhr.setRequestHeader(name, value);
    }
    xhr.send(file);
  });

  // 3. Finalize upload with API to record slide metadata and trigger cloud pipeline stage
  const finalizeRes = await apiFetch(`${API_BASE}/api/v1/cases/${caseId}/slide/finalize`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ gcs_uri }),
  });

  if (!finalizeRes.ok) {
    const b = await finalizeRes.json().catch(() => null);
    throw new Error(`Failed to finalize slide registration in cloud: ${formatApiError(b, finalizeRes.statusText)}`);
  }

  try {
    localStorage.removeItem(sessionKey);
  } catch (_) {}

  return finalizeRes.json();
}

export async function uploadSlideFile(
  caseId: string,
  file: File,
  onProgress?: (percent: number) => void
): Promise<any> {
  // First attempt zero-server-transit direct GCS upload
  try {
    return await uploadSlideDirectToGCS(caseId, file, onProgress);
  } catch (directErr) {
    if (file.size > 25 * 1024 * 1024) {
      // Cloud Run HTTP body limit is 32MB; large WSI files cannot be proxied through the API
      throw directErr;
    }
    console.warn("Direct GCS upload attempt failed, falling back to API proxy upload:", directErr);
  }

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const formData = new FormData();
    formData.append("file", file);

    if (xhr.upload && onProgress) {
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
          const percent = Math.round((e.loaded / e.total) * 100);
          onProgress(percent);
        }
      });
    }

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch (_) {
          resolve({});
        }
      } else {
        let errorMsg = `HTTP Upload Error (${xhr.status})`;
        try {
          const body = JSON.parse(xhr.responseText);
          errorMsg = formatApiError(body, errorMsg);
        } catch (_) {}
        reject(new Error(errorMsg));
      }
    });

    xhr.addEventListener("error", () => reject(new Error("Network connection error during file upload")));
    xhr.addEventListener("abort", () => reject(new Error("Slide upload aborted")));

    xhr.open("POST", `${API_BASE}/api/v1/cases/${caseId}/slide/upload`);
    xhr.withCredentials = true;
    const csrf = getCookie("og_csrf");
    if (csrf) {
      xhr.setRequestHeader("X-CSRF-Token", csrf);
    }
    xhr.send(formData);
  });
}

export async function retryStage(caseId: string, stageName: string) {
  const res = await apiFetch(`${API_BASE}/api/v1/cases/${caseId}/stages/${stageName}/retry`, {
    method: "POST",
    headers: { 
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, `Failed to retry stage execution (${res.status})`));
  }
  return res.json();
}

export async function approveStage(caseId: string, stageName: string, payload?: { override_justification?: string }) {
  const res = await apiFetch(`${API_BASE}/api/v1/cases/${caseId}/stages/${stageName}/approve`, {
    method: "POST",
    headers: {
      ...idempotencyHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, `Failed to approve stage (${res.status})`));
  }
  return res.json();
}

export async function confirmTriageStage(caseId: string, noInvasiveTumor: boolean = false) {
  const res = await apiFetch(`${API_BASE}/api/v1/stages/triage/confirm`, {
    method: "POST",
    headers: {
      ...idempotencyHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      case_id: caseId,
      no_invasive_tumor: noInvasiveTumor,
      reviewed_by: "pathologist_01",
    }),
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, `Failed to confirm triage stage (${res.status})`));
  }
  return res.json();
}

export async function deleteCase(caseId: string) {
  const res = await apiFetch(`${API_BASE}/api/v1/cases/${caseId}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, `Failed to delete case (${res.status})`));
  }
}

export async function clearAllCases() {
  const res = await apiFetch(`${API_BASE}/api/v1/cases`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, `Failed to clear cases (${res.status})`));
  }
  return res.json();
}

export async function fetchCaseDetail(caseId: string): Promise<CaseDetail> {
  const res = await apiFetch(`${API_BASE}/api/v1/cases/${caseId}`);
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, "Failed to fetch case detail"));
  }
  return res.json();
}

// Stage 4: Mitosis Detection & Virtual HPFs Interfaces
export interface MitosisCandidate {
  id: string;
  hotspot_id?: string | null;
  centroid_um: [number, number];
  det_conf?: number | null;
  ver_conf?: number | null;
  label: "mitosis" | "not_mitosis" | "unreviewed";
  label_source: string;
  medgemma_verdict?: string | null;
  medgemma_rationale?: string | null;
  medgemma_confidence?: "low" | "medium" | "high" | null;
  crop_uri?: string | null;
  crop_orig_uri?: string | null;
}

export interface VirtualHpfSite {
  seq: number;
  center_um: [number, number];
  radius_um: number;
  count: number;
  source?: string;
}

export interface MitoticScoreSummary {
  count_total: number;
  n_hpf: number;
  area_mm2: number;
  per_mm2: number;
  classic_per_10hpf: number;
  mitotic_score: number; // 1, 2, or 3
}

export interface MitosisStageData {
  case_id: string;
  stage_execution_id: string;
  status: string;
  candidates: MitosisCandidate[];
  hpfs: VirtualHpfSite[];
  summary: MitoticScoreSummary;
  slide?: { width_px: number; height_px: number; mpp_x: number; mpp_y: number };
  model_versions: Record<string, string>;
  reviewed_at?: string | null;
  reviewed_by?: string | null;
}

export async function fetchMitosisStageData(caseId: string): Promise<MitosisStageData> {
  const res = await apiFetch(`${API_BASE}/api/v1/stages/mitosis/${caseId}`);
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, `Failed to fetch mitosis stage data (Status: ${res.status})`));
  }
  return res.json();
}

export async function recomputeMitosis(payload: {
  case_id: string;
  candidate_labels?: Record<string, string>;
  hpfs?: Array<{ seq: number; center_um: [number, number]; radius_um?: number; source?: string }>;
  audit_toggle?: { id: string; from: string; to: string };
}): Promise<{ case_id: string; hpfs: VirtualHpfSite[]; summary: MitoticScoreSummary }> {
  const res = await apiFetch(`${API_BASE}/api/v1/stages/mitosis/recompute`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, "Failed to recompute mitosis score"));
  }
  return res.json();
}

export async function addPathologistMitosis(
  caseId: string,
  centroidUm: [number, number],
  label: string = "mitosis",
  reviewedBy: string = "pathologist_01"
): Promise<{ status: string; candidate: MitosisCandidate }> {
  const res = await apiFetch(`${API_BASE}/api/v1/stages/mitosis/add_candidate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      case_id: caseId,
      centroid_um: centroidUm,
      label,
      reviewed_by: reviewedBy,
    }),
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, "Failed to add candidate mitosis"));
  }
  return res.json();
}

export async function bulkRejectUnreviewedMitosis(
  caseId: string,
  reviewedBy: string = "pathologist_01"
): Promise<MitosisStageData> {
  const res = await apiFetch(`${API_BASE}/api/v1/stages/mitosis/bulk_action`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      case_id: caseId,
      action: "reject_remaining_unreviewed",
      reviewed_by: reviewedBy,
    }),
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, "Failed to bulk reject unreviewed candidates"));
  }
  return res.json();
}

export async function replaceMitosisHpfs(caseId: string): Promise<MitosisStageData> {
  const res = await apiFetch(`${API_BASE}/api/v1/stages/mitosis/re_place_hpfs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      case_id: caseId,
      action: "re_place_hpfs",
    }),
  });
  if (!res.ok) {
    const errData = await res.json().catch(() => null);
    throw new Error(formatApiError(errData, "Failed to re-place HPF sites"));
  }
  return res.json();
}

export async function confirmMitosisStage(
  caseId: string,
  reviewedBy: string = "pathologist_01"
): Promise<any> {
  const res = await apiFetch(`${API_BASE}/api/v1/stages/mitosis/confirm`, {
    method: "POST",
    headers: {
      ...idempotencyHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      case_id: caseId,
      reviewed_by: reviewedBy,
    }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(formatApiError(data, "Failed to confirm mitosis stage"));
  }
  return res.json();
}

// Stage 5 grading types and API functions migrated to @/lib/api/grading.ts (grading_v6 contract)



export async function updateSlideMpp(
  caseId: string,
  slideId: string,
  mppX: number,
  mppY?: number
): Promise<any> {
  const res = await apiFetch(`${API_BASE}/api/v1/cases/${caseId}/slides/${slideId}/mpp`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ mpp_x: mppX, mpp_y: mppY }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Failed to update MPP" }));
    throw new Error(formatApiError(err, "Failed to update MPP"));
  }
  return res.json();
}
