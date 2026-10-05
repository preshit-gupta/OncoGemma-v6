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

type ScoreKind = "mean_p_tumor" | "periphery_then_tumor" | "prescan_expected_count" | "prescan_then_tumor";

// An HPF site: a circle 0.5 mm across (0.196 mm²) in a padded 0.6 mm frame (owner decision D22).
// Circles never overlap; frames may. A figure in a frame's padding is shown, and it is counted only
// in the circle that contains it.
interface Hotspot {
  id: string;                               // "hs_01".. for model (by rank), "hs_u_<n>" for pathologist-added
  center_um: [number, number];              // centre of the circle
  hpf_diameter_um: number;                  // 500
  polygon_um: [number, number][];           // the padded frame: a square of side window_um centred on center_um, closed ring (first == last)
  window_um: number;                        // frame side (600)
  rank: number | null;                      // 1 = best (model sites only)
  rank_score: number | null;
  score_kind: ScoreKind | null;
  tissue_fraction: number | null;           // 0..1 over the circle; null when pinned
  tumor_fraction: number | null;            // 0..1 over the circle; null when pinned
  prescan_expected: number | null;          // expected mitoses in the circle (when score_kind uses prescan)
  at_periphery: boolean | null;             // arm H1P: centre within the periphery band (1 mm) of the invasive front; null when pinned
  front_distance_um: number | null;         // distance of the centre to the invasive front; null when pinned or the slide has no front
  source: "model" | "pathologist_added" | "pathologist_modified";   // modified = moved
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
  hpf_target: number;                       // sites wanted (10); fewer active sites is inadequate tissue
  n_sites_available: number;                // valid candidate sites on the lattice before the disjoint selection
  n_sites_at_periphery: number;             // selected sites with at_periphery true (WP-6.6)
  provenance: Provenance;
}

type EditOp =
  | { op: "add"; center_um: [number, number] }       // pins a site; the server assigns "hs_u_<n>" and builds the frame
  | { op: "move"; id: string; center_um: [number, number] }
  | { op: "exclude"; id: string; reason: string }
  | { op: "restore"; id: string }                    // clears `excluded`
  | { op: "delete"; id: string };
```

## Endpoints

| Method | Path | Body | 2xx | Errors |
|---|---|---|---|---|
| GET | `/api/v1/stages/triage/{case_id}` | — | `200 TriageStageV6` | `404 not_found` |
| POST | `/api/v1/stages/triage/edits` | `{ case_id, edits: EditOp[] }` | `200 TriageStageV6` | `422 {error:"hotspot_overlap", ids:[[a,b],...]}` (active circles overlap) · `422 {error:"invalid_site", id, reason}` (reason ∈ `out_of_bounds`, `unknown_site`) · `422 {error:"too_many_sites", hpf_target}` · `409 {error:"triage_rerun_required"}` |
| POST | `/api/v1/stages/triage/confirm` | `{ case_id, no_invasive_tumor: boolean, accept_fewer_hpfs?: boolean }` | `200 { status: "confirmed", next_stage: "mitosis" \| null, accept_fewer_hpfs: boolean }` | `409 {error:"hotspot_overlap", ids}` · `409 {error:"hpf_sites_lt_10", n_active, hpf_target}` (fewer active sites than the target and no acceptance) · `409 {error:"triage_rerun_required"}` · `409 {error:"not_awaiting_review"}` |

## HPF site rules

- **Edits accumulate.** Each `POST /edits` appends its ops to the stored list; the client may send one op per request. Pinned ids `hs_u_<n>` are numbered over the whole edit history and are stable across requests (ids of deleted pins are not reused).
- **The server builds each frame from `center_um`.** Clients never send a polygon.
- **A pinned site is used as it is.** It has no tissue or tumour minimum (that is the pathologist's call), so its `tissue_fraction`, `tumor_fraction` and `rank` are null. A *moved* model site keeps its `rank` and loses `rank_score` and the fractions, which belonged to its old position.
- **Validity.** The circle must lie inside the slide (`invalid_site` / `out_of_bounds`). Active circles must not overlap (`hotspot_overlap`; the distance between circle edges is at least the profile's `gap_um`, 0). Frames may overlap. At most `hpf_target` sites may be active (`too_many_sites`).
- **Inadequate tissue.** With fewer than `hpf_target` active sites the case is *inadequate*: the UI says so in Stages 3–5, and Confirm needs `accept_fewer_hpfs: true`. The acceptance is stored in the `stage_confirmed` audit event and in the input of the Stage 4 execution (and echoed in its output). The mitotic score is still computed per mm² over the circles examined.
- **Old data.** A triage output or edit list from before HPF sites (no `center_um`, or `modify` / `polygon_um` ops) answers `409 triage_rerun_required` on GET, edits, confirm and thumbnails; the owner re-runs Stage 3 for those cases.

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
    {"id": "hs_01", "center_um": [4300,5300], "hpf_diameter_um": 500,
     "polygon_um": [[4000,5000],[4600,5000],[4600,5600],[4000,5600],[4000,5000]], "window_um": 600,
     "rank": 1, "rank_score": 6.4, "score_kind": "prescan_then_tumor", "tissue_fraction": 0.97, "tumor_fraction": 0.91, "prescan_expected": 6.4,
     "source": "model", "excluded": false, "exclude_reason": null},
    {"id": "hs_02", "center_um": [4850,5300], "hpf_diameter_um": 500,
     "polygon_um": [[4550,5000],[5150,5000],[5150,5600],[4550,5600],[4550,5000]], "window_um": 600,
     "rank": 2, "rank_score": 3.1, "score_kind": "prescan_then_tumor", "tissue_fraction": 0.93, "tumor_fraction": 0.77, "prescan_expected": 3.1,
     "source": "model", "excluded": false, "exclude_reason": null}
  ],
  "machine_hotspots": [],
  "flags": ["hotspots_limited_by_tissue"],
  "hpf_target": 10, "n_sites_available": 46,
  "provenance": {"stage": "triage", "model_versions": {"path_foundation": "pf@2026-08", "tumor_head": "1.0.0"},
                 "config_hash": "3f2a…", "run_mode": "clinical"}
}
```

In the fixture, `machine_hotspots` should equal `hotspots`. The mock `edits` handler must accumulate ops, return `hotspot_overlap` when a pinned or moved circle overlaps another active circle (frames may overlap), and return `too_many_sites` for an eleventh active site, so that the UI error paths can be tested. The mock `confirm` handler must return `hpf_sites_lt_10` when fewer than `hpf_target` sites are active and `accept_fewer_hpfs` is not true. (The fixtures change in WP-6.7 and WP-7.10.)
