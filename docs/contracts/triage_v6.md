# Contract: triage (Stage 3) v6 (SPEC-05)

## Types

```ts
interface SlideGeom { width_px: number; height_px: number; mpp_x: number; mpp_y: number }

interface Heatmap {
  png_url: string;                // 1 px per tile; alpha 0 outside tissue
  tile_um: number;                // 224
  origin_um: [number, number];    // top-left of tile (0,0)
  nx: number; ny: number;         // image size in tiles (= png width/height in px)
  value: "p_tumor_cal";
  head_version: string;
}

type ScoreKind = "mean_p_tumor" | "prescan_expected_count" | "prescan_then_tumor";

interface Hotspot {
  id: string;                               // "hs_01".. for model, "hs_u_<n>" for pathologist-added
  polygon_um: [number, number][];           // closed ring (first == last)
  window_um: number | null;                 // side of model square windows; null for free polygons
  rank: number | null;                      // 1 = best (model hotspots only)
  rank_score: number | null;
  score_kind: ScoreKind | null;
  tumor_fraction: number | null;            // 0..1
  prescan_expected: number | null;          // expected mitoses in window (when score_kind uses prescan)
  source: "model" | "pathologist_added" | "pathologist_modified";
  excluded: boolean;
  exclude_reason: string | null;
}

interface TriageStageV6 {
  case_id: string;
  stage_execution_id: string;
  status: "queued" | "running" | "awaiting_review" | "confirmed" | "failed";
  slide: SlideGeom;
  heatmap: Heatmap | null;
  tumor_threshold: number;                  // calibrated τ_tumor used for the mask
  hotspots: Hotspot[];                      // effective (after edits)
  machine_hotspots: Hotspot[];              // as produced by the model
  flags: ("hotspots_limited_by_tissue" | "no_invasive_tumor_detected")[];
  provenance: Provenance;
}

type EditOp =
  | { op: "add"; polygon_um: [number, number][] }
  | { op: "modify"; id: string; polygon_um: [number, number][] }
  | { op: "exclude"; id: string; reason: string }
  | { op: "delete"; id: string };
```

## Endpoints

| Method | Path | Body | 2xx | Errors |
|---|---|---|---|---|
| GET | `/api/v1/stages/triage/{case_id}` | — | `200 TriageStageV6` | `404 not_found` |
| POST | `/api/v1/stages/triage/edits` | `{ case_id, edits: EditOp[] }` | `200 TriageStageV6` | `422 {error:"hotspot_overlap", ids:[[a,b],...]}` · `422 {error:"invalid_polygon", id, reason}` (reason ∈ `not_closed`, `too_many_vertices`, `out_of_bounds`, `area_out_of_range`, `self_intersecting`) |
| POST | `/api/v1/stages/triage/confirm` | `{ case_id, no_invasive_tumor: boolean }` | `200 { status: "confirmed", next_stage: "mitosis" \| null }` | `409 {error:"hotspot_overlap", ids}` · `409 {error:"not_awaiting_review"}` |

## Overlay geometry (frontend)

- The heatmap image's top-left sits at `(origin_um[0] / mpp_x, origin_um[1] / mpp_y)` in level-0 pixels.
- Its size is `(nx · tile_um / mpp_x, ny · tile_um / mpp_y)` px.
- Render it with nearest-neighbour scaling (`image-rendering: pixelated`), so that tiles stay crisp.

## Example (mock fixture `frontend/lib/mock/triage.json`)

```json
{
  "case_id": "c_demo", "stage_execution_id": "se_3", "status": "awaiting_review",
  "slide": {"width_px": 80000, "height_px": 60000, "mpp_x": 0.25, "mpp_y": 0.25},
  "heatmap": {"png_url": "/mock/heatmap_demo.png", "tile_um": 224, "origin_um": [0, 0], "nx": 89, "ny": 67,
              "value": "p_tumor_cal", "head_version": "tumor_head@1.0.0"},
  "tumor_threshold": 0.42,
  "hotspots": [
    {"id": "hs_01", "polygon_um": [[4000,5000],[4600,5000],[4600,5600],[4000,5600],[4000,5000]], "window_um": 600,
     "rank": 1, "rank_score": 6.4, "score_kind": "prescan_then_tumor", "tumor_fraction": 0.91, "prescan_expected": 6.4,
     "source": "model", "excluded": false, "exclude_reason": null},
    {"id": "hs_02", "polygon_um": [[4800,5000],[5400,5000],[5400,5600],[4800,5600],[4800,5000]], "window_um": 600,
     "rank": 2, "rank_score": 3.1, "score_kind": "prescan_then_tumor", "tumor_fraction": 0.77, "prescan_expected": 3.1,
     "source": "model", "excluded": false, "exclude_reason": null}
  ],
  "machine_hotspots": [],
  "flags": ["hotspots_limited_by_tissue"],
  "provenance": {"stage": "triage", "model_versions": {"path_foundation": "pf@2026-08", "tumor_head": "1.0.0"},
                 "config_hash": "3f2a…", "run_mode": "clinical"}
}
```

In the fixture, `machine_hotspots` should equal `hotspots`. The mock `edits` handler must return `hotspot_overlap` when a submitted polygon intersects another active hotspot, so that the UI error path can be tested.
