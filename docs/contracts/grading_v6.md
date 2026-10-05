# Contract: grading (Stage 5) v6 (SPEC-07)

## Types

```ts
interface TubuleSample {
  id: string; center_um: [number, number]; size_um: 512; mpp: 1.0;
  image_url: string; stratum: number; tumor_area_um2: number;
  estimate: { tumor_present: boolean; tubule_percent: number } | null;   // null = estimator failed (needs_human)
  review: { tumor_present?: boolean; tubule_percent?: number; by: string; at: string } | null;
}

interface PleoField {
  id: string; center_um: [number, number]; size_um: 128; mpp: 0.25;
  image_url: string; stratum: number;
  estimate: { pleomorphism_score: 1 | 2 | 3 } | null;
  nuclei: { n: number; area_p50_um2: number; area_cv: number } | null;   // null until nuclear segmentation (WP-8.3, deferred)
  // An independent second score by another model, never shown the estimate (owner decision 2026-10-04).
  // agrees: false flags the field for review; null when either score is missing. The grade uses `estimate`.
  verification?: { producer: string; pleomorphism_score: 1 | 2 | 3 | null; agrees: boolean | null } | null;
  review: { pleomorphism_score?: 1 | 2 | 3; by: string; at: string } | null;
}

interface HistotypeVote {
  sample_id: string;                  // a tubule sample id (t_xx)
  type: string | null;                // null: the call failed
  architecture: string | null; cohesion: string | null; confidence: "low" | "medium" | "high" | null;   // what the model reported seeing; confidence is never used in a computation
  rationale: string | null;
}

type Flag = "needs_human" | "insufficient_nuclei" | "near_grade_boundary" | "hpf_count_lt_10";   // insufficient_nuclei only once WP-8.3 exists

interface GradingStageV6 {
  case_id: string; stage_execution_id: string;
  status: "queued" | "running" | "awaiting_review" | "confirmed" | "failed";
  slide: SlideGeom;
  // estimator: what produced the component, "<arm>:<producer>@<prompt>", e.g. "T1:gemini_referee@tubule@v1"
  tubule: { samples: TubuleSample[]; percent: number | null; score: 1 | 2 | 3 | null; estimator: string; n_used: number };
  pleomorphism: { fields: PleoField[]; score: 1 | 2 | 3 | null; estimator: string; aggregation: "mode" | "p75" | "model" };
  // read-only (Stage 4). flags / hpf_target copy the Stage 4 summary: fewer HPFs than hpf_target is inadequate tissue.
  mitotic: { score: 1 | 2 | 3 | null; count_total: number; n_hpf: number; area_mm2: number; per_mm2: number;
             flags: ("hpf_count_lt_10")[]; hpf_target: number };
  // WP-8.8: the type is voted over patches. type is null when no type is proposed: no patch answered, or the patches
  // disagree (a tie, or the winner's share of the votes below the configured minimum). It is never defaulted.
  histotype: { type: string | null; estimator: string; rationale: string; confirmed: boolean; confirmed_by: string | null;
               agreement: number | null;   // the winner's share of the successful votes; null with no votes
               n_requested: number;        // patches asked; 0 and votes [] for a grading made before WP-8.8
               votes: HistotypeVote[] };
  total: number | null;                 // T + P + M, null unless all three present
  grade: 1 | 2 | 3 | null;
  flags: Flag[];                        // near_grade_boundary when total ∈ {5,6,7,8}
  overrides: { tubule_score?: 1 | 2 | 3; pleo_score?: 1 | 2 | 3; histotype?: string; reasons: Record<string, string> };
  provenance: Provenance;
}
```

## Endpoints

| Method | Path | Body | 2xx | Errors |
|---|---|---|---|---|
| GET | `/api/v1/stages/grading/{case_id}` | — | `200 GradingStageV6` | `404 not_found` (also for a grading written by the v5 worker, or none yet) |
| GET | `/api/v1/stages/grading/{case_id}/{tubule\|pleo}/{sample_id}/image` | — | `200 image/png` (the `image_url` of a sample) | `404 image_not_found` |
| POST | `/api/v1/stages/grading/review-sample` | `{ case_id, kind: "tubule" \| "pleo", sample_id, value: { tumor_present?, tubule_percent? } \| { pleomorphism_score } }` | `200 GradingStageV6` (server re-aggregates) | `404 sample_not_found` · `422 invalid_value` · `409 stage_locked` |
| POST | `/api/v1/stages/grading/override` | `{ case_id, component: "tubule" \| "pleo" \| "histotype", value, reason }` (`reason` ≥ 10 characters) | `200 GradingStageV6` | `422 reason_too_short` · `422 invalid_value` · `409 stage_locked` · `404 not_found` |
| POST | `/api/v1/stages/grading/histotype/confirm` | `{ case_id, type }` | `200 GradingStageV6` | `422 invalid_value` · `409 stage_locked` · `404 not_found` |
| POST | `/api/v1/stages/grading/confirm` | `{ case_id }` | `200 { status: "confirmed", case_status: "done", next_stage: null }` | `409 {error:"histotype_unconfirmed"}` · `409 {error:"missing_component", components:[...]}` · `409 not_awaiting_review` |

## Server rules (WP-8.6)

- An override with `value: null` clears that override (a reason is still required). A histotype override un-confirms the type.
- `review-sample` merges the given fields into the sample's review; the estimate stays as the machine wrote it.
- A pleomorphism mode tie takes the highest tied score (owner decision 2026-10-04). `aggregation` is `"mode"`.

## UI rules tied to the contract

- **Two sample grids.** Tubule samples are shown at 10× (512 µm) and pleomorphism fields at 40× (128 µm). Each shows the estimate, the review value and a "failed" state when `estimate` is null.
- **Mitotic panel.** Read-only, with a link to the Mitoses stage.
- **Boundary warning.** Show a banner when `near_grade_boundary` is present: "Sum is near a grade boundary; check components".
- **Histologic type.** When `histotype.type` is null and `votes` is non-empty, say the patches disagree and list the votes; never pre-select a type. When `agreement` is below 1, show it as a count ("4 of 6 patches"). The model's `confidence` is shown beside a vote, never as a figure for the case. No model name appears.
- **No narrative panel.** There are no CAP/report links, and no "Gate 1/2/3" wording.

## Example (mock fixture `frontend/lib/mock/grading.json`)

```json
{
  "case_id": "c_demo", "stage_execution_id": "se_5", "status": "awaiting_review",
  "slide": {"width_px": 80000, "height_px": 60000, "mpp_x": 0.25, "mpp_y": 0.25},
  "tubule": {"samples": [
      {"id": "t_01", "center_um": [4300, 5300], "size_um": 512, "mpp": 1.0, "image_url": "/mock/t01.png", "stratum": 0,
       "tumor_area_um2": 201000, "estimate": {"tumor_present": true, "tubule_percent": 35}, "review": null},
      {"id": "t_02", "center_um": [9000, 7000], "size_um": 512, "mpp": 1.0, "image_url": "/mock/t02.png", "stratum": 1,
       "tumor_area_um2": 150000, "estimate": null, "review": null}],
    "percent": 35.0, "score": 2, "estimator": "T1:gemini_tubule@v2", "n_used": 1},
  "pleomorphism": {"fields": [
      {"id": "p_01", "center_um": [4310, 5290], "size_um": 128, "mpp": 0.25, "image_url": "/mock/p01.png", "stratum": 0,
       "estimate": {"pleomorphism_score": 2}, "nuclei": {"n": 143, "area_p50_um2": 48.2, "area_cv": 0.41}, "review": null}],
    "score": 2, "estimator": "P2:ordinal_morph@1.0.0", "aggregation": "model"},
  "mitotic": {"score": 2, "count_total": 12, "n_hpf": 10, "area_mm2": 1.963, "per_mm2": 6.11, "flags": [], "hpf_target": 10},
  "histotype": {"type": "IDC-NST", "estimator": "H1:gemini_histotype@v2", "rationale": "Cohesive nests, no single files.",
                "confirmed": false, "confirmed_by": null, "agreement": 1.0, "n_requested": 1,
                "votes": [{"sample_id": "t_01", "type": "IDC-NST", "architecture": "cohesive_nests", "cohesion": "cohesive",
                           "confidence": "high", "rationale": "Cohesive nests, no single files."}]},
  "total": 6, "grade": 2, "flags": ["near_grade_boundary"],
  "overrides": {"reasons": {}},
  "provenance": {"stage": "grading", "model_versions": {"gemini": "gemini-2.5-flash-xxx"}, "config_hash": "3f2a…", "run_mode": "clinical"}
}
```
