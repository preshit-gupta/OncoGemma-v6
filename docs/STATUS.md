# STATUS — session handoff

Update this file at the end of every session (SPEC plan §3 rule 8). Keep it under 60 lines.

**Last updated:** 2026-09-28 (Claude)
**Phase:** P1 — Fix

## Done

- v6 repo bootstrapped from v5 history; tag `v5.0.0-baseline`. Specs 00–11; decisions D1–D15 (`docs/IMPLEMENTATION_PLAN.md` §5).
- Delegation pack: `AGENTS.md`, `GEMINI.md`, `docs/tasks/` (14 cards), `docs/contracts/`, and pre-written acceptance tests for WP-2.4, 2.5, 5.1, 5.3 (they skip until the module exists).
- Merged into `main`: WP-1.2, 2.4, 2.5, 5.1, 5.2, 5.3, 9.3 (`ff65fe0`), then WP-2.1 + 2.2 (`5124d9f`, PR #1), then the WP-4.3 database-password slice (`c800bd1`, PR #2), then the suite fixes (`6611ceb`, PR #3).
- **Deployed 2026-09-28** (`cloudbuild.yaml`: API `oncogemma-api-00095-s7k`, frontend, worker job). Production database stamped `0001_v5_baseline` before the deploy; startup upgraded it to `0002_drop_v5_reports`; `alembic check` clean. Later revisions apply at API startup.
- **Cloud SQL password rotated and redeployed 2026-09-28** (PR #2 runbook; images built from `6611ceb`). The `oncogemma` password is a random value held only in Secret Manager `og-db-password` (version 1). The old password, which is in git history, no longer works. API `oncogemma-api-00099-rk4` (100% of traffic, image `api:main-6611ceb`) and the worker job (`worker:main-6611ceb`) read it as `DB_PASSWORD` via `--set-secrets`, next to `CLOUD_SQL_CONNECTION_NAME`. The job's plaintext Terraform `DATABASE_URL` was removed. `/health` returns 200. New DB connections failed from 15:59 to 16:43 UTC during the rotation.
- **WP-2.1 + 2.2:**
  - `backend/alembic/` (`0001_v5_baseline`, `0002_drop_v5_reports`; the owner approved deleting `reports` on 2026-09-28), CI `.github/workflows/migrations.yml`. API startup (`ENV != test`) runs `upgrade_to_head` under a Postgres advisory lock and refuses unversioned databases. `create_all` and the v5 startup DDL are gone.
  - `PipelineConfig` (every `configs/*.yaml` and prompt, strict, hashed; load failure aborts startup) and `ModelRegistry` (`configs/models.yaml`; Vertex versions are verified deployments `models/<id>@<ver>@<deploy date>`; `${NAME}` from an allowlist of Settings fields).
- **Suite fixes** (PR #3): the fake GCS store uses extended-length paths on Windows; `RUN_IN_PROCESS_WORKER` (off in conftest) stops the lifespan worker racing `TestClient`. Suite on `main` (`6611ceb`): 439 passed, 1 skipped.
- **WP-2.3a** (PR #6, open): gateway foundations.
  - `0003_decision_records`, `0004_stage_run_mode` (existing rows become `clinical`), verified on Postgres 15. `app/core/{run_context,tasks,fallbacks}.py`, empty `configs/fallbacks.yaml`, registry `call_policy`, VLM `params`/`schema_retries`, Gemini `region`.
  - `app/inference/gateway.py`: contract checks (incl. decoded image size), transport-only retries with full jitter, strict parsing, cache, one record per attempt, EVAL refuses Gemini aliases, `invoke_or_fallback`. `adapters/vertex_genai.py` uses `response_json_schema`.
  - `worker/execution.py`: one `execute_stage` for poll loop, Cloud Run job and webhook (which now stamps `config_hash`); handlers get a `StageRuntime`; records survive a failed stage; `error` is JSON. conftest `forbid_real_google_credentials` fails any test asking for ADC. Suite: 587 passed, 1 skipped.
- **WP-2.3b** (PR open, stacked on #6): triage on the gateway. PF (`path_foundation_v1`, raw predict, batches within registry `limits`), probe (`local_sklearn`, sha256-pinned artifact) and the tumour referee (MedGemma `medgemma_chat_v1`, `tumor_verification@v2.md`, strict `TumorVerdict`). MedGemma's request format was verified with one owner-approved test call (2026-09-28). Deleted: random/mock embeddings, parquet cache (now the gateway cache), runtime probe training, synthetic crops and thumbnails, the colour-threshold tumour fallback. `Dockerfile.api` copies `models/`. Suite: 645 passed, 1 skipped; literal baseline 319 → 295.

## Ready for delegates now

Lanes are in `docs/tasks/README.md`. Lane B (frontend) merged WP-4.2, 6.4, 7.7, 8.5 and 9.2. Lane E: WP-7.1.

## Claude — next

1. **WP-2.3c** (mitosis: KongNet `kongnet_midog_v1`, Gemini referee with strict `MitosisVerdict`) then **2.3d** (grading, delete `pipeline/medgemma.py`, `Grading.histologic_type` nullable, remove `USE_MOCK_VERTEX_AI` and Settings model fields). Carry-overs:
   - Handlers read `runtime.config`; remove `yaml.safe_load` in `worker/mitosis.py`, `pipeline/{scoring,grading,qc_checks}.py`, `routers/grading.py` and the `medgemma.py` prompt loader.
   - Left for M3 (WP-3.3/3.4) in triage: the swallowed tissue-mask and stain-normaliser loads, per-patch normalisation falling back to the original, and the centre-region tissue fallback.
   - `gemini_referee` uses the alias `gemini-2.5-flash`, so EVAL refuses it until the owner pins a version.
2. **M3** (3.1 → 3.4), then **4.1**.

## Open items (program owner)

- Record in the plan §5: D16, drop the v5 `reports` table (2026-09-28).
- WP-2.3a proposals: add `tumor_referee` to the SPEC-01 §3.3 task catalogue (the SPEC-05 §5.4 referee had no task); choose a pinned Gemini version ID for EVAL runs.
- Is `configs/stain_reference.png` part of `config_hash`? It changes outputs but is not YAML; SPEC-04 §3.3 may replace it.
- Use one git worktree per agent. Delegates share `D:\Projects\OncoGemma v6`, and the branch switched under Claude mid-task. Tests also share the fake-GCS directory under the system temp dir, so concurrent `pytest` runs on one machine interfere.
- BCNB details (SPEC-02 §3.2). Optional: `Bash(git push:*)` allow rule. Optional: TUPAC16 availability attempt.
- After the worker job's next real execution, confirm that `gcloud run jobs logs read oncogemma-worker-job` shows `[DB Core] Using Cloud SQL instance` and no `password authentication failed`. Future password rotations: add a secret version, then set the database password *from the secret* (`gcloud secrets versions access latest`), then roll new revisions. Never generate it separately: on 2026-09-28 a regenerated `$pw` drifted from the secret and the first deploy failed.

## Findings to fix

- `backend/requirements.txt` has `sqlalchemy>=2.0.28` with only `psycopg2-binary`. SQLAlchemy 2.1 (what a fresh build resolves) maps plain `postgresql://` to psycopg 3, so such URLs fail with `No module named 'psycopg'`. The Cloud SQL socket path names `+psycopg2` and is unaffected. Fix it in WP-1.3.
- Cloud Run's front end reserves `/healthz` and answers it with a Google 404 before the request reaches the app, so the PR #2 runbook's `/healthz` check is wrong: use `/health`. The health endpoints also return the raw DB exception text to unauthenticated callers (WP-4.3).
- Production Cloud SQL is Postgres 16; the migrations CI job uses 15 (SPEC-01). Consider moving CI to 16. Also, `backend/tests` and `tools/tests` are both packages named `tests`: run them in separate `pytest` runs.

## Blockers

None.
