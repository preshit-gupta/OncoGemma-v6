# SPEC-09 — Self-Improvement Loop: Corrections → Training Examples → Gated Retraining → Shadow → Promotion

| Field | Value |
|---|---|
| Spec ID | SPEC-09 |
| Category (V4) | Model |
| Issues covered | N12 |
| Depends on | SPEC-01 (DecisionRecord with `supersedes_id`, registry), SPEC-02 (splits, metrics), SPEC-05/06/07 (trainable heads), SPEC-08 (annotations, issues) |
| Phase | Built in P1 (capture only). Retraining and promotion become active in P3 |

## 1. Problem

In v5, every model is a one-shot classifier. Pathologist corrections are stored but never learned from:
- `stage_executions.review_edits`, the RFC-6902 diffs
- `detections.label_source='pathologist'`
- grading `overrides`

The next case therefore repeats the same error. The Build Notes ask for corrections to "train the specific model so that future cases account for the changes".

## 2. What is trainable, and how each improves

| Component | Improvement mechanism | Owning metric (gate) |
|---|---|---|
| `tumor_head` (SPEC-05) | Retrain on corrected tile labels | S3-F1 |
| `mitosis_classifier` (SPEC-06 Stage B) | Retrain with corrected positives, negatives and missed figures | NS-M (MIDOG++ breast val) **and** `F1_M` (TCGA val) |
| Stage-A threshold `τ_A`, calibrators | Re-calibrate | NS-M |
| Tubule / pleomorphism heads (SPEC-07 T3/P2/P3/P4) | Retrain on corrected sample-level or slide-level labels | `F1_T` / `F1_P` |
| Histotype MIL (SPEC-07 H2) | Retrain | S5-HT |
| VLM tasks (Gemini) | **No weight updates.** New prompt or definition versions and curated exemplar banks are evaluated as arms through the same gate | Task metric |
| MedGemma | LoRA fine-tuning is **deferred**. It needs its own spec after P2: at least 2k labelled examples per task, plus verification of Vertex tuning support for the deployed variant | Task metric |

## 3. Capture: corrections become examples

Every human edit writes a DecisionRecord with `producer_kind='human'`, and `supersedes_id` points to the model record it changes (SPEC-01 §3.3). The **capture service** (`app/learning/capture.py`) subscribes to those writes and emits `training_examples` rows.

| Source event | Example task | Label | Default weight |
|---|---|---|---|
| Hotspot excluded, with reason `not_tumor` | `tumor_tile` (each tile in the window with `p_tumor_cal ≥ τ_tumor`) | `non_tumor` | 1.0 |
| Hotspot added by pathologist | `tumor_tile` (tiles in the polygon, tissue fraction ≥ 0.5) | `invasive_tumor` | 1.0 |
| Candidate: model `mitosis` → pathologist `not_mitosis` | `mitosis_crop` | negative (model FP) | 1.0 |
| Candidate: model `not_mitosis`/`equivocal` → pathologist `mitosis` | `mitosis_crop` | positive | 1.0 |
| Pathologist-added figure | `mitosis_crop` | positive (model FN) | 1.0 |
| Grading sample override (tubule %, pleo score) | `tubule_sample` / `pleo_field` | overridden value | 1.0 |
| Slide-level component or type override | `tubule_slide` / `pleo_slide` / `histotype_slide` | overridden value | 1.0 |
| Research annotation, `adjudicated` | as per task | annotated value | 1.0 |
| Research annotation, single annotator | as per task | annotated value | 0.5 |
| Implicit confirmation (stage confirmed, entity untouched) | as per task | model value | 0.2, **excluded by default** (`include_implicit=false`; its inclusion is itself an ablation, because of automation-bias risk) |

**Conflicts** on the same entity are resolved in this order:
1. adjudicated annotation
2. latest pathologist review
3. researcher annotation

The losing rows get `retracted_at`.

**Crops are materialised at capture time** into `gs://<training>/examples/<task>/<id>.png`, at the registry's input spec (`mpp`, `size_px`, colour). Examples therefore survive slide deletion and can be reproduced exactly.

## 4. Storage

```sql
CREATE TABLE training_examples (
  id UUID PRIMARY KEY,
  task TEXT NOT NULL,                        -- tumor_tile | mitosis_crop | tubule_sample | pleo_field | tubule_slide | pleo_slide | histotype_slide
  source TEXT NOT NULL,                      -- clinical_review | research_annotation | dataset
  dataset TEXT NOT NULL,                     -- clinical | tcga_brca_dx | bcnb | midogpp_* | ...
  patient_key TEXT NOT NULL,                 -- dataset patient id, or case_id for clinical
  case_id UUID NULL, slide_sha256 CHAR(64) NOT NULL,
  entity_ref JSONB NOT NULL,                 -- {"type":"candidate","id":"m_0042","centroid_um":[..]}
  input_uri TEXT NOT NULL, input_spec JSONB NOT NULL,
  label JSONB NOT NULL, weight REAL NOT NULL,
  labeller_id UUID NULL, labeller_role TEXT NULL,
  model_output JSONB NULL, model_version TEXT NULL,
  decision_record_id UUID NULL REFERENCES decision_records(id),
  quarantined BOOLEAN NOT NULL,              -- TRUE if patient_key is in any locked test split (SPEC-02 §5.2)
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), retracted_at TIMESTAMPTZ NULL
);
CREATE TABLE dataset_snapshots (
  id UUID PRIMARY KEY, task TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  selection JSONB NOT NULL,                  -- filter spec incl. include_implicit, min_weight, datasets
  manifest_uri TEXT NOT NULL, manifest_sha256 CHAR(64) NOT NULL,
  counts JSONB NOT NULL                      -- by label x source x dataset
);
CREATE TABLE promotion_records (
  id UUID PRIMARY KEY, registry_key TEXT NOT NULL, from_version TEXT NOT NULL, to_version TEXT NOT NULL,
  snapshot_id UUID NOT NULL REFERENCES dataset_snapshots(id),
  val_compare_uri TEXT NOT NULL,             -- paired comparison payload (SPEC-08 §4.3)
  gate_result TEXT NOT NULL CHECK (gate_result IN ('pass','fail')),
  shadow_summary_uri TEXT NULL,
  approved_by UUID NULL REFERENCES users(id), approved_at TIMESTAMPTZ NULL,
  registry_commit TEXT NULL
);
```

- **Quarantine** is computed at insert time from `SPLITS.lock`, and recomputed whenever the splits change.
- **Snapshots** always filter `quarantined = false AND retracted_at IS NULL`. They are **immutable**.
- The Parquet manifest lists `(example_id, input_uri, label, weight)` and is hashed.

## 5. Retraining

- **Jobs:** `training/<task>/train.py --snapshot <id> --base <registry_version> --seed <int>` runs as a Cloud Run Job (CPU tasks) or a Vertex AI CustomJob (GPU, `mitosis_classifier`).
- **Data mix:** the snapshot (corrections plus annotations) **plus** the original public training data for that task (for example MIDOG++ train for the mitosis classifier). The mixing ratio is a hyperparameter, recorded in the model card.
- **Determinism:** fixed seeds. `torch.use_deterministic_algorithms(True)` for the CNNs. For the linear heads, the sklearn random_state is recorded.
- **Outputs:**
  - the artefact and its SHA-256
  - the model card (snapshot ID, counts, hyperparameters, val metrics)
  - `val_compare.json` from `eval/metrics.paired_bootstrap_delta` against the current production version, on the **program val split**
- **Triggers:**
  - manual, by an admin; or
  - scheduled weekly, when new non-quarantined examples since the last snapshot reach the task minimum:
    - `tumor_tile` ≥ 500
    - `mitosis_crop` ≥ 200
    - slide-level tasks ≥ 50

## 6. Promotion

1. **Val gate** (SPEC-00 §2.5):
   - The paired ΔF1 on the owning metric has lower bound > 0.
   - No pre-registered slice drops by more than 0.03.
   - INT-PROV and INT-FALL pass.
   - For `mitosis_classifier`, **both** NS-M and `F1_M` must satisfy this.
2. **Shadow** (`RunMode.SHADOW`):
   - The candidate scores live clinical cases in parallel, for at least 14 days **or** at least 50 cases.
   - Its outputs are written only to DecisionRecords with `producer_kind='shadow'`. They are never shown or counted.
   - The shadow summary reports agreement with subsequent pathologist review labels on those cases: F1 against `review_label` for mitosis, and κ for scores.
3. **Approval:**
   - An admin with `model:promote` approves in the Research view, creating a `promotion_records` row.
   - The registry change (`configs/models.yaml`) is committed with the message trailer `Promotion-Record: <id>`.
   - CI rejects registry version changes that lack a `pass` promotion record.
4. **Rollback:** revert the registry commit. Because `config_hash` changes, affected runs are always traceable.

## 7. Monitoring

- **Correction rate:** per task per week (corrections / decisions). It appears in the Research view as the leading indicator of field error rate.
- **Score drift:** population stability index (PSI) of each model's output distribution per scanner and site, compared with the train distribution, computed weekly. PSI > 0.2 raises an `issues` row (category `model`, severity `medium`) automatically.
- **Label-noise audit:** each quarter, 50 random captured examples per task are re-reviewed by a second labeller. Agreement is reported, and examples from a labeller with agreement < 0.8 are down-weighted.

## 8. Acceptance criteria

| # | Criterion |
|---|---|
| AC1 | For each event type in §3, an integration test asserts exactly one `training_examples` row with the correct label, weight, crop spec and `decision_record_id` |
| AC2 | Quarantine: a property test with synthetic splits shows that a snapshot never contains a test-split patient. A CI check re-validates every snapshot manifest against `SPLITS.lock` |
| AC3 | Reproducibility: `tumor_head` retrained twice from the same snapshot and seed gives identical coefficients. The `mitosis_classifier` val NS-M differs by ≤ 0.005 across two runs |
| AC4 | Gate enforcement: a PR changing a registry version without a `pass` promotion record fails CI |
| AC5 | Shadow isolation: E2E test showing shadow outputs appear in no clinical API response and no count |
| AC6 | Monitoring: correction-rate and PSI panels render from fixture data. A PSI breach creates an issue |

## 9. Risks

| Risk | Mitigation |
|---|---|
| Automation bias: pathologists accept wrong model output, so implicit labels are wrong | Implicit confirmations are excluded by default. Only explicit changes and annotations are used |
| Feedback loop overfits to one site | Always mix in public data. Slice gate per site/scanner |
| Leakage into the benchmark | Quarantine plus a CI check. Eval runs never create review labels |
| Too few corrections to matter early | Loop activates in P3. Annotations from SPEC-08 §6 seed it |
