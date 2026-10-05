# WP-7.9 — Morphology descriptions of mitotic figures (descriptive, never a decision)

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | M | SPEC-06 §5.6 (new task, D22); SPEC-01 §3.3–3.6 | WP-6.5 (fixed HPF circles, `hpf_seq`) | Claude (`backend/**`, `configs/**`) |

## Goal

Without the referee, Stage 4 is a black box to the pathologist: a figure is labelled, with no account of what it looks like. v5's referee returned a short morphological `rationale` with its verdict. The referee lowered F1 (0.862 → 0.676 on MIDOG++ 094, D17), so it stays off.

This card adds a **description only**. For each figure the count rests on, Gemini describes the visible morphology for the pathologist to interpret. The description never changes `final_decision`, `counted`, the HPFs or the score. This fixes owner review issue S4-2 (plan §2.4).

**Owner decision (2026-10-05, D22):** the describer is **Gemini** (same release as the referee). It is descriptive, not a judgement.

## Read first (only these)

- `docs/IMPLEMENTATION_PLAN.md` §2.4; `docs/specs/06-stage4-mitosis.md` §3 (the operational definition, for the vocabulary), §5.6
- `backend/worker/mitosis.py`: the referee block (the `gateway.invoke_or_fallback` pattern, thread pool, review crops)
- `backend/app/core/tasks.py`, `backend/app/inference/schemas.py` (`MitosisVerdict`, as an example of a strict schema), `configs/models.yaml` (`gemini_referee`), `configs/fallbacks.yaml`
- `docs/contracts/mitosis_v6.md` (`Candidate`)

## Files you may touch

- `backend/app/core/tasks.py` (`Task.MITOSIS_DESCRIBE`)
- `backend/app/inference/schemas.py` (`MitosisDescription`). This file belongs to lane D, but no lane-D card is open.
- `configs/models.yaml` (`gemini_describer`, the same model variable as `gemini_referee`), `configs/mitosis.yaml` (`describe:` block), `configs/fallbacks.yaml`, `backend/app/core/pipeline_config.py`
- **Create** `configs/prompts/mitosis_describe@v1.md`
- `backend/worker/mitosis.py`, `backend/app/routers/mitosis.py`, `backend/app/models/detection.py`
- **Create** `backend/alembic/versions/0018_detection_descriptions.py`
- `docs/contracts/mitosis_v6.md`, `frontend/lib/mock/mitosis.json` (two example descriptions, so WP-7.10 can build in mock mode)
- Tests: **create** `backend/tests/test_mitosis_describe.py`; extend `test_mitosis_worker.py`, `backend/tests/inference/` (schema)

## Tasks

1. **Schema** `MitosisDescription` (strict). It has enumerated morphology fields. Every field allows `not_assessable`.
   - `chromatin`: e.g. condensed clumps, band or plate, two separated masses, fine granular, smooth dense, beaded fragments.
   - `nuclear_membrane`: not visible, partly visible, intact.
   - `outline`: hairy projections, smooth.
   - `cytoplasm`: clear halo, eosinophilic, none visible.
   - `relative_size`: larger, similar, smaller than neighbours.
   - `setting`: among tumour cells, stroma, inflammatory infiltrate, necrosis, lumen.
   - `summary`: at most 60 words.

   The schema has **no** verdict, label, phase, mimic, confidence, count or decision field. A validator rejects a summary that contains any phrase in `mitosis.yaml describe.forbidden_phrases`. Seed the list with: "is a mitotic figure", "is not a mitotic figure", "not mitotic", "should be counted", "should not be counted", "confidence", "likely mitosis", "unlikely". The rejection is a `SchemaInvalidError`. Refine the vocabulary with SPEC-06 §3; keep each field to at most 7 values.
2. **Prompt** `mitosis_describe@v1.md`.
   - **Inputs:** the crop (64 µm @ 0.25 µm/px) and the context (256 µm @ 1.0 µm/px). These are the raw-colour review images the pathologist sees (`mitosis.yaml review_crops`), so read them once and reuse them. The prompt states that the cell of interest is at the **exact centre** of both images. No marker is promised.
   - **Instruction:** describe what is visible. Never say whether the cell is or is not a mitotic figure, how sure the model is, or whether to count it.
3. **Which candidates.**
   - Described (`description_status: "ok"`, or `"unavailable"` on failure): candidates with `final_decision ∈ {mitosis, equivocal}` **inside an HPF circle** (`hpf_seq` not null).
   - Every other candidate: `description_status: "not_requested"`. This includes `not_mitosis` candidates, those outside the circles, and pathologist-added ones.
   - A pathologist's review does not re-request a description.
4. **Worker** (`worker/mitosis.py`), after the HPFs and the count:
   - One `gateway.invoke_or_fallback(Task.MITOSIS_DESCRIBE, describe.producer, …, EntityRef(CANDIDATE, id), MitosisDescription)` per candidate, on the existing `MODEL_CALL_THREADS` pool. Append the record id to `record_ids`.
   - `describe.enabled: true` in clinical runs. `describe.run_in_eval: false`: eval runs skip it explicitly and record `not_requested`. Metrics cannot depend on it.
   - **Failure (clinical):** a `fallbacks.yaml` entry for `mitosis_describe` (`ModelUnavailableError`, `ModelTimeoutError`, `SchemaInvalidError` → `null`). The candidate gets `description: null`, `description_status: "unavailable"` and its fallback DecisionRecord. The stage completes. Any other error fails the stage, as today.
5. **Persistence and API.**
   - Migration `0018`: `detections.description` (JSON, null) and `detections.description_status` (TEXT, not null, default `'not_requested'`).
   - `Candidate` serves both. Re-running Stage 4 regenerates them. Pathologist-decided rows keep theirs.
6. **Invariant.** Run the worker with descriptions on, off and failing (a fake gateway). `final_decision`, `counted`, `hpfs`, `summary` and the `mitosis_count` record output must be identical in all three.

## Interfaces / contract

```ts
interface MitosisDescription {
  chromatin: "condensed_clumps" | "band_or_plate" | "two_separated_masses" | "fine_granular" | "smooth_dense" | "beaded_fragments" | "not_assessable";
  nuclear_membrane: "not_visible" | "partly_visible" | "intact" | "not_assessable";
  outline: "hairy_projections" | "smooth" | "not_assessable";
  cytoplasm: "clear_halo" | "eosinophilic" | "none_visible" | "not_assessable";
  relative_size: "larger" | "similar" | "smaller" | "not_assessable";
  setting: "tumour_cells" | "stroma" | "inflammatory" | "necrosis" | "lumen" | "not_assessable";
  summary: string;              // <= 60 words, descriptive only
}
Candidate += {
  description: MitosisDescription | null;
  description_status: "ok" | "unavailable" | "not_requested";
}
```

UI rule (add it to the contract): the panel is titled as a morphology description and states that the count does not use it. No model name appears (AGENTS rule 6).

## Acceptance (run these)

```powershell
python -m pytest backend/tests/test_mitosis_describe.py backend/tests/test_mitosis_worker.py backend/tests/inference -q -p no:cacheprovider
python -m pytest backend/tests -q -p no:cacheprovider
```

Tests that must exist:
- the invariant of task 6;
- only `mitosis` and `equivocal` candidates inside circles are described;
- a forbidden phrase in `summary` gives `unavailable`, and the stage still completes;
- the schema has no verdict-like field (a static check of the field names);
- an eval run makes no describe call.

## Out of scope — do not do

- Any decision from the description: no referee, no re-labelling, no `equivocal` from it. The referee v2 stays deferred (D19).
- On-demand descriptions for pathologist-added figures. A follow-up, if the owner wants it.
- Frontend (WP-7.10). Prompt tuning against labelled data.

## Done checklist

- [ ] `MitosisDescription` schema, prompt `@v1`, `Task.MITOSIS_DESCRIBE`, `gemini_describer`
- [ ] Described in clinical Stage 4; fallback entry; eval skips
- [ ] Migration `0018`; contract and mock updated
- [ ] Invariant test proves the count is untouched; full suite passes
- [ ] `docs/STATUS.md`: re-run Stage 4 for open cases to get descriptions
