# Contract: mitosis (Stage 4) v6 (SPEC-06)

## Types

```ts
interface VlmVerdict {
  verdict: "MITOTIC_FIGURE" | "NOT_MITOTIC_FIGURE" | "EQUIVOCAL";
  criteria: { membrane_absent: boolean; condensed_chromosome_projections: boolean;
              phase: "prometaphase" | "metaphase" | "anaphase" | "telophase" | "atypical" | "none";
              neoplastic_cell: boolean };
  mimic: "none" | "apoptotic_body" | "pyknotic_nucleus" | "hyperchromatic_interphase" | "lymphocyte" | "prophase" | "crush" | "other";
  rationale: string;
  rule_override: boolean;       // true = deterministic post-rule downgraded the VLM verdict (SPEC-06 §5.4)
}

interface Candidate {
  id: string;
  hotspot_id: string | null;
  centroid_um: [number, number];
  p_a: number | null;           // detector probability (null for pathologist-added); calibration is deferred (D19)
  p_b: number | null;           // calibrated classifier probability
  vlm: VlmVerdict | null;
  in_tumor: boolean;            // tumour-cell gate (SPEC-06 §5.5); only an eval ablation run (mitosis.tumor_gate.enabled: false) carries null
  final_decision: "mitosis" | "not_mitosis" | "equivocal";
  decision_path: "A" | "AB" | "ABC" | "human";
  review_label: "mitosis" | "not_mitosis" | null;
  counted: boolean;             // server-computed; UI never derives it
  hpf_seq: number | null;       // the HPF circle that contains it (server-computed); null outside every circle: shown, never counted
  crop_url: string;             // 64 µm @ 0.25 µm/px
  context_url: string;          // 256 µm @ 1.0 µm/px
  description: MitosisDescription | null;   // morphology only (WP-7.9, D22); null unless description_status is "ok"
  description_status: "ok" | "unavailable" | "not_requested";
}

// What the figure looks like, for the pathologist to interpret. It has no verdict, label, phase, mimic, confidence,
// count or decision field, and it never changes final_decision, counted, the HPFs or the score. Present for candidates
// with final_decision "mitosis" or "equivocal" inside an HPF circle ("unavailable" when the description failed);
// "not_requested" for every other candidate, pathologist-added ones included, and for every eval run.
interface MitosisDescription {
  chromatin: "condensed_clumps" | "band_or_plate" | "two_separated_masses" | "fine_granular" | "smooth_dense" | "beaded_fragments" | "not_assessable";
  nuclear_membrane: "not_visible" | "partly_visible" | "intact" | "not_assessable";
  outline: "hairy_projections" | "smooth" | "not_assessable";
  cytoplasm: "clear_halo" | "eosinophilic" | "none_visible" | "not_assessable";
  relative_size: "larger" | "similar" | "smaller" | "not_assessable";
  setting: "tumour_cells" | "stroma" | "inflammatory" | "necrosis" | "lumen" | "not_assessable";
  summary: string;              // <= 60 words, descriptive only
}

// An HPF is the circle of a confirmed Stage 3 site: same centre, radius hpf_diameter_um / 2 (250). Nothing is searched for
// inside the frame and nothing moves to follow the figures. seq follows the model sites by rank, then the pinned sites in id order.
interface Hpf {
  seq: number; center_um: [number, number]; radius_um: number;
  count: number; tissue_coverage: number; tumor_fraction: number;   // coverage and tumour share of the circle, for audit
  hotspot_id: string;                   // the site it came from
  frame_um: [number, number][];         // that site's padded frame (closed ring): the review image covers it (600 µm)
}

interface MitosisSummary {
  count_total: number; n_hpf: number; area_mm2: number; per_mm2: number;
  mitotic_score: 1 | 2 | 3 | null; n_equivocal: number;  // null when n_hpf = 0
  hpf_target: number;                    // HPFs wanted (10); the UI never hardcodes it
  flags: ("hpf_count_lt_10")[];          // fewer HPFs than hpf_target: inadequate tissue, acknowledged in Stage 3
}

interface MitosisStageV6 {
  case_id: string; stage_execution_id: string;
  status: "queued" | "running" | "awaiting_review" | "confirmed" | "failed";
  slide: SlideGeom;
  candidates: Candidate[];
  hpfs: Hpf[];
  summary: MitosisSummary;
  provenance: Provenance;
}
```

## Endpoints

| Method | Path | Body | 2xx | Errors |
|---|---|---|---|---|
| GET | `/api/v1/stages/mitosis/{case_id}` | — | `200 MitosisStageV6` | `404 not_found` |
| POST | `/api/v1/stages/mitosis/review` | `{ case_id, candidate_id, review_label: "mitosis" \| "not_mitosis" \| null }` | `200 MitosisStageV6` (counts and score recomputed server-side) | `404 candidate_not_found` · `409 stage_locked` |
| POST | `/api/v1/stages/mitosis/add` | `{ case_id, centroid_um: [x, y] }` | `200 MitosisStageV6` (new candidate with `decision_path:"human"`, `review_label:"mitosis"`) | `422 out_of_bounds` · `409 stage_locked` |
| POST | `/api/v1/stages/mitosis/confirm` | `{ case_id }` | `200 { status: "confirmed", next_stage: "grading" }` | `409 {error:"equivocal_unreviewed", ids:[...]}` (every `equivocal` candidate inside an HPF needs a `review_label`) · `409 not_awaiting_review` |

## UI rules tied to the contract

- **Queue order.** Show `equivocal` candidates first, then `mitosis`, then `not_mitosis`. Within each group, sort by `p_b ?? p_a`, descending.
- **Counted badge.** A candidate is "counted" only if `counted === true`. The score shown is always `summary.mitotic_score`.
- **Decision chain panel** for the selected candidate. It lists:
  - `p_a` (Detector)
  - `p_b` (Classifier)
  - the VLM verdict, the criteria checklist, `mimic` and a "Rule override" badge
  - `review_label`
- **Morphology description panel** (WP-7.10). When `description_status` is `"ok"`, show the six fields and the `summary` in a panel titled as a morphology description, with the text "The count does not use this description." `"unavailable"` shows that no description is available; `"not_requested"` shows no panel. Never show a model name (AGENTS rule 6) and never present the description as a verdict, a confidence or a recommendation.

## Example (mock fixture `frontend/lib/mock/mitosis.json`)

```json
{
  "case_id": "c_demo", "stage_execution_id": "se_4", "status": "awaiting_review",
  "slide": {"width_px": 80000, "height_px": 60000, "mpp_x": 0.25, "mpp_y": 0.25},
  "candidates": [
    {"id": "m_0001", "hotspot_id": "hs_01", "centroid_um": [4210.5, 5120.0], "p_a": 0.91, "p_b": 0.88, "vlm": null,
     "in_tumor": true, "final_decision": "mitosis", "decision_path": "AB", "review_label": null, "counted": true, "hpf_seq": 1,
     "description_status": "ok", "description": {"chromatin": "band_or_plate", "nuclear_membrane": "not_visible", "outline": "hairy_projections",
       "cytoplasm": "clear_halo", "relative_size": "larger", "setting": "tumour_cells", "summary": "Dense dark chromatin arranged as a flat band across the cell centre with fine projections at its edges."},
     "crop_url": "/mock/crop_m1.png", "context_url": "/mock/ctx_m1.png"},
    {"id": "m_0002", "hotspot_id": "hs_01", "centroid_um": [4400.0, 5300.2], "p_a": 0.52, "p_b": 0.49,
     "vlm": {"verdict": "EQUIVOCAL", "criteria": {"membrane_absent": true, "condensed_chromosome_projections": false,
             "phase": "none", "neoplastic_cell": true}, "mimic": "pyknotic_nucleus", "rationale": "Dense round body, no projections.",
             "rule_override": false},
     "in_tumor": true, "final_decision": "equivocal", "decision_path": "ABC", "review_label": null, "counted": false, "hpf_seq": 1,
     "description_status": "ok", "description": {"chromatin": "smooth_dense", "nuclear_membrane": "not_assessable", "outline": "smooth",
       "cytoplasm": "eosinophilic", "relative_size": "smaller", "setting": "tumour_cells", "summary": "A small, round, uniformly dark body with a smooth edge, surrounded by pink cytoplasm."},
     "crop_url": "/mock/crop_m2.png", "context_url": "/mock/ctx_m2.png"},
    {"id": "m_0003", "hotspot_id": "hs_02", "centroid_um": [5010.0, 5200.0], "p_a": 0.40, "p_b": 0.08, "vlm": null,
     "in_tumor": true, "final_decision": "not_mitosis", "decision_path": "AB", "review_label": null, "counted": false, "hpf_seq": null,
     "description": null, "description_status": "not_requested",
     "crop_url": "/mock/crop_m3.png", "context_url": "/mock/ctx_m3.png"}
  ],
  "hpfs": [{"seq": 1, "center_um": [4300, 5300], "radius_um": 250, "count": 1, "tissue_coverage": 0.96, "tumor_fraction": 0.9,
            "hotspot_id": "hs_01", "frame_um": [[4000,5000],[4600,5000],[4600,5600],[4000,5600],[4000,5000]]}],
  "summary": {"count_total": 1, "n_hpf": 1, "area_mm2": 0.196, "per_mm2": 5.09, "mitotic_score": 2,
              "n_equivocal": 1, "hpf_target": 10, "flags": ["hpf_count_lt_10"]},
  "provenance": {"stage": "mitosis", "model_versions": {"kongnet_det_midog_1": "sha256:…", "mitosis_classifier": "1.0.0"},
                 "config_hash": "3f2a…", "run_mode": "clinical"}
}
```

The mock `review` handler must recompute `counted` using `review_label ?? (final_decision == "mitosis" && (in_tumor ?? true))`. It must then recompute `count_total` over candidates inside HPFs, `per_mm2 = count_total / area_mm2`, and `mitotic_score` with thresholds 3.65 and 7.30 per mm². The UI never performs that calculation; the mock only simulates the server.
