# STATUS — session handoff

Update this file at the end of every session (SPEC plan §3 rule 8). Keep it under 60 lines.

**Last updated:** 2026-09-29 (Claude)
**Phase:** P1 — Fix

## Done

- v6 repo bootstrapped from v5 history; tag `v5.0.0-baseline`. Specs 00–11; decisions D1–D15 (`docs/IMPLEMENTATION_PLAN.md` §5).
- Delegation pack: `AGENTS.md`, `GEMINI.md`, `docs/tasks/` (14 cards), `docs/contracts/`, and pre-written acceptance tests for WP-2.4, 2.5, 5.1, 5.3 (they skip until the module exists).
- Merged into `main`: WP-1.2, 2.4, 2.5, 5.1, 5.2, 5.3, 9.3 (`ff65fe0`), then WP-2.1 + 2.2 (`5124d9f`, PR #1), then the WP-4.3 database-password slice (`c800bd1`, PR #2), then the suite fixes (`6611ceb`, PR #3), then the frontend lane (WP-4.2, 6.4, 7.7, 8.5, 9.2) and WP-1.3 (PR #9).
- **Deployed 2026-09-28** (`cloudbuild.yaml`: API `oncogemma-api-00095-s7k`, frontend, worker job). Production database stamped `0001_v5_baseline` before the deploy; startup upgraded it to `0002_drop_v5_reports`; `alembic check` clean. Later revisions apply at API startup.
- **Cloud SQL password rotated and redeployed 2026-09-28** (PR #2 runbook; images built from `6611ceb`). The `oncogemma` password is a random value held only in Secret Manager `og-db-password` (version 1). The old password, which is in git history, no longer works. API `oncogemma-api-00099-rk4` (100% of traffic, image `api:main-6611ceb`) and the worker job (`worker:main-6611ceb`) read it as `DB_PASSWORD` via `--set-secrets`, next to `CLOUD_SQL_CONNECTION_NAME`. The job's plaintext Terraform `DATABASE_URL` was removed. `/health` returns 200. New DB connections failed from 15:59 to 16:43 UTC during the rotation.
- **WP-2.1 + 2.2:**
  - `backend/alembic/` (`0001_v5_baseline`, `0002_drop_v5_reports`; the owner approved deleting `reports` on 2026-09-28), CI `.github/workflows/migrations.yml`. API startup (`ENV != test`) runs `upgrade_to_head` under a Postgres advisory lock and refuses unversioned databases. `create_all` and the v5 startup DDL are gone.
  - `PipelineConfig` (every `configs/*.yaml` and prompt, strict, hashed; load failure aborts startup) and `ModelRegistry` (`configs/models.yaml`; Vertex versions are verified deployments `models/<id>@<ver>@<deploy date>`; `${NAME}` from an allowlist of Settings fields).
- **Suite fixes** (PR #3): the fake GCS store uses extended-length paths on Windows; `RUN_IN_PROCESS_WORKER` (off in conftest) stops the lifespan worker racing `TestClient`. Suite on `main` (`6611ceb`): 439 passed, 1 skipped.
- **WP-2.3 is complete and on `main`** (2026-09-29): #6, then #11 (API image hotfix), #12 (lands #7, #8, #10), #13 (WP-2.3e) and #14 (TCGA metadata fails loudly). Images `api`/`worker:wp2.3e-4c1b34e` are built; the owner deploys. All other branches were deleted; only `main` remains.
- **WP-2.3a** (PR #6): gateway foundations.
  - `0003_decision_records`, `0004_stage_run_mode` (existing rows become `clinical`), verified on Postgres 15. `app/core/{run_context,tasks,fallbacks}.py`, empty `configs/fallbacks.yaml`, registry `call_policy`, VLM `params`/`schema_retries`, Gemini `region`.
  - `app/inference/gateway.py`: contract checks (incl. decoded image size), transport-only retries with full jitter, strict parsing, cache, one record per attempt, EVAL refuses Gemini aliases, `invoke_or_fallback`. `adapters/vertex_genai.py` uses `response_json_schema`.
  - `worker/execution.py`: one `execute_stage` for poll loop, Cloud Run job and webhook (which now stamps `config_hash`); handlers get a `StageRuntime`; records survive a failed stage; `error` is JSON. conftest `forbid_real_google_credentials` fails any test asking for ADC. Suite: 587 passed, 1 skipped.
- **WP-2.3b** (PR #7, stacked on #6): triage on the gateway. PF (`path_foundation_v1`, raw predict, batches within registry `limits`), probe (`local_sklearn`, sha256-pinned artifact) and the tumour referee (MedGemma `medgemma_chat_v1`, `tumor_verification@v2.md`, strict `TumorVerdict`). MedGemma's request format was verified with one owner-approved test call (2026-09-28). Deleted: random/mock embeddings, parquet cache (now the gateway cache), runtime probe training, synthetic crops and thumbnails, the colour-threshold tumour fallback. The API image gets `models/` in #11. Suite: 645 passed, 1 skipped; literal baseline 319 → 295.
- **WP-2.3c** (PR #8, stacked on #7): mitosis on the gateway. KongNet in 512 px PNG patches (`kongnet_midog_v1`, the request the deployed v5 service reads; `kongnet_midog_v2` codec ready for the WP-7.1 service). Gemini referee with the v5 prompt and strict `MitosisVerdict` (SPEC-06 arm A2); labels and `label_source` come from the verdict. Deleted: YOLO/OD detector fallback, verifier gating and forced confidences, swallowed referee errors, the lenient mitosis schema and morphometric referee, the unconfirmed-triage hotspot fallback, silently skipped tiles and crops. The v5 heuristics moved to `pipeline/heuristics/` (registry, ablation only; allowlisted). A 20× slide now fails at the detector contract (AC5) until M3 resamples. Suite: 655 passed, 1 skipped; baseline 295 → 218.
- **WP-2.3d** (PR #10, stacked on #8): grading on the gateway (tubule per patch, pleomorphism per field, histotype over the first 6 patches; strict schemas, v5 prompts, no MedGemma anchor, SPEC-07 §5.4). `pipeline/medgemma.py` deleted. Failed estimates are never defaulted (v5: tubule 20, pleo 2, IDC-NST); allowed clinical fallbacks leave them `null` and flag `needs_human`. Grading needs Stage 4 HPFs (no more mitotic score 1 by default). `0005_grading_histotype_nullable` (Postgres-checked). Removed Settings `USE_MOCK_VERTEX_AI`, `USE_GEMINI_FLASH_REFEREE`, `GEMINI_API_KEY`, `VERTEX_MEDGEMMA_MODEL_VERSION`, `MEDGEMMA_*`. The grading confirm endpoint no longer defaults the type to IDC-NST. Suite: 666 passed, 1 skipped.
- **WP-2.3e**: config injection. `pipeline/{scoring,grading,qc_checks}.py` take typed `MitosisScoringConfig` / `ScoringConfig` / `QcConfig` (YAML loaders, `DEFAULT_*`, the QC default dict and its md5 hash deleted; QC results carry the pipeline `config_hash`). Routers read `get_pipeline_config()`; every handler takes the runtime (`_without_runtime` gone). No more invented values: mitotic score 1 without HPFs in the grading router (now `needs_human`, flag `no_mitotic_score`), 10 HPFs of 262 µm, grade-preview scores of 2 (now 400), unknown-confidence weight 1.0. Gemini: `gemini-2.5-flash` is a stable release (Vertex, 2026-09-29), so EVAL accepts it; records name the served `model_version`. AC3: `tests/test_fallback_paths_eval.py` (every §1.1 row raises in EVAL against a policy allowing every fallback). AC4: `tests/inference/test_schema_properties.py` (Hypothesis). Static: `tools/ruff_fail_loud.toml` (BLE001/S110/PLW0603; the v5 files still swallowing errors are listed and may only shrink). `USE_GEMINI_FLASH_REFEREE` removed from `ops/` and `infra/`. Literal baseline 150 → 121. Suite: 721 passed, 1 skipped; `tools/tests` 39 passed.

## Ready for delegates now

Lanes are in `docs/tasks/README.md`. Lane B (frontend) merged WP-4.2, 6.4, 7.7, 8.5 and 9.2. Lane E: WP-7.1.

## Claude — next

1. **M3** (3.1 → 3.4), then **4.1**. M3 takes over from WP-2.3 the swallowed tissue-mask and stain-normaliser loads (triage, mitosis, grading), per-patch normalisation falling back to the original, the centre-region tissue fallback, 0.25 µm/px resampling for the detector, and the QC checks' slide-read fallbacks (`pipeline/qc_checks.py`: grey thumbnail, 2048 px dimensions, skipped focus tiles). Each file leaves `tools/ruff_fail_loud.toml` when clean.

## Open items (program owner)

- Record in the plan §5: D16, drop the v5 `reports` table (2026-09-28).
- WP-2.3a proposal: add `tumor_referee` to the SPEC-01 §3.3 task catalogue (the SPEC-05 §5.4 referee had no task).
- **MIDOG v2 is not deployed** (2026-09-29): only `midog-detector:v1` exists and the endpoint serves the v5 model, so `kongnet_det_midog_1` stays on `kongnet_midog_v1`. After the WP-7.1 `deploy.ps1` run, switch the registry entry's `wire_format` to `kongnet_midog_v2` and its `version`/deploy date.
- `docs/contracts/mitosis_v6.md` types `mitotic_score` as `1 | 2 | 3`, so a mitosis summary with zero HPFs still reports score 1 (with `n_hpf: 0`). Proposal: allow `null` there.
- Is `configs/stain_reference.png` part of `config_hash`? It changes outputs but is not YAML; SPEC-04 §3.3 may replace it.
- Use one git worktree per agent. Delegates share `D:\Projects\OncoGemma v6`, and the branch switched under Claude mid-task. Tests also share the fake-GCS directory under the system temp dir, so concurrent `pytest` runs on one machine interfere.
- BCNB details (SPEC-02 §3.2). Optional: `Bash(git push:*)` allow rule. Optional: TUPAC16 availability attempt.
- After the worker job's next real execution, confirm that `gcloud run jobs logs read oncogemma-worker-job` shows `[DB Core] Using Cloud SQL instance` and no `password authentication failed`. Future password rotations: add a secret version, then set the database password *from the secret* (`gcloud secrets versions access latest`), then roll new revisions. Never generate it separately: on 2026-09-28 a regenerated `$pw` drifted from the secret and the first deploy failed.

## Findings to fix

- Cloud Run's front end reserves `/healthz` and answers it with a Google 404 before the request reaches the app, so the PR #2 runbook's `/healthz` check is wrong: use `/health`. The health endpoints also return the raw DB exception text to unauthenticated callers (WP-4.3).
- Production Cloud SQL is Postgres 16; the migrations CI job uses 15 (SPEC-01). Consider moving CI to 16. Also, `backend/tests` and `tools/tests` are both packages named `tests`: run them in separate `pytest` runs.

## Blockers

None.
