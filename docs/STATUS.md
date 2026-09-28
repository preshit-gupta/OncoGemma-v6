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
  - `backend/alembic/`: `0001_v5_baseline` (the v5 schema), then `0002_drop_v5_reports`. The owner approved deleting the `reports` data on 2026-09-28. Verified on Postgres 15: upgrade, `alembic check`, downgrade and upgrade again, plus the v5 stamp procedure below. CI: `.github/workflows/migrations.yml`.
  - API startup (`ENV != test`) runs `app.core.migrations.upgrade_to_head` under a Postgres advisory lock. It refuses any database that has tables but no `alembic_version`. `create_all` and the v5 startup DDL are gone.
  - `app.core.pipeline_config.PipelineConfig`: every `configs/*.yaml` plus `configs/prompts/*`, `extra="forbid"`, strict types, range and cross-file checks, duplicate-YAML-key check. `config_hash()` is stamped on every stage execution by `worker/main.py` and `worker/cloud_job_entry.py`. Load failure aborts startup.
  - `configs/models.yaml` → `app.core.model_registry.ModelRegistry`. Vertex endpoint versions are verified deployments (`models/<id>@<ver>@<deploy date>`, read with `gcloud ai endpoints describe` on 2026-09-28). `${NAME}` resolves from an allowlist of Settings fields.
  - Backend suite: 423 passed, 1 skipped, 1 failed (`test_run_triage_stage_e2e`, failing on `main` too; `main` has 362 passed). `tools/tests`: 31 passed. `tools/lint_literals.py`: clean.
- **Suite fixes** (PR #3). The triage failure was not a v6 regression (v5 fails the same way): with a private TMP longer than 77 characters, the fake-GCS path of the embedding cache exceeded Windows MAX_PATH. The fake store now uses extended-length paths on Windows. `RUN_IN_PROCESS_WORKER` (off in conftest) stops the lifespan worker racing `TestClient` requests. Backend suite on `main` (`6611ceb`): 439 passed, 1 skipped.

## Ready for delegates now

Lanes are in `docs/tasks/README.md`.

| Lane | Ready |
|---|---|
| E | WP-7.1 |
| B | WP-4.2 (in progress), then 6.4, 7.7, 8.5, 9.2 |

## Claude — next

1. **WP-2.3.** DecisionRecord (revision 0003), ModelGateway, RunMode, fallback policy; split `pipeline/medgemma.py`. Carry-overs from 2.2:
   - Handlers take `PipelineConfig` by injection; remove `yaml.safe_load` in `worker/mitosis.py`, `worker/triage.py`, `pipeline/{scoring,grading,qc_checks}.py`, `routers/grading.py` and the `medgemma.py` prompt loader.
   - `model_versions` come from `registry.version_of()`, so the grading worker stops recording the v5 label `VERTEX_MEDGEMMA_MODEL_VERSION`. Remove the Settings endpoint and model fields once the gateway reads the registry.
   - EVAL refuses floating model aliases (SPEC-01 §3.7).
   - `worker/triage.py` swallows Path Foundation cache read and write errors (`except Exception`), which hid the MAX_PATH failure above. Raise instead.
2. **M3** (3.1 → 3.4), then **4.1**.

## Open items (program owner)

- Record in the plan §5: D16, drop the v5 `reports` table (2026-09-28).
- Is `configs/stain_reference.png` part of `config_hash`? It changes outputs but is not YAML; SPEC-04 §3.3 may replace it.
- Use one git worktree per agent. Delegates share `D:\Projects\OncoGemma v6`, and the branch switched under Claude mid-task. Tests also share the fake-GCS directory under the system temp dir, so concurrent `pytest` runs on one machine interfere.
- BCNB details (SPEC-02 §3.2). Optional: `Bash(git push:*)` allow rule. Optional: TUPAC16 availability attempt.
- After the worker job's next real execution, confirm that `gcloud run jobs logs read oncogemma-worker-job` shows `[DB Core] Using Cloud SQL instance` and no `password authentication failed`. Future password rotations: add a secret version, then set the database password *from the secret* (`gcloud secrets versions access latest`), then roll new revisions. Never generate it separately: on 2026-09-28 a regenerated `$pw` drifted from the secret and the first deploy failed.

## Findings to fix

- The v5 test suite authenticates with the developer's real Google ADC credentials (a `google.auth` "end user credentials" warning). Close this in WP-2.3 with gateway fakes and a conftest guard that fails any test creating a real Google client.
- `backend/requirements.txt` has `sqlalchemy>=2.0.28` with only `psycopg2-binary`. SQLAlchemy 2.1 (what a fresh build resolves) maps plain `postgresql://` to psycopg 3, so such URLs fail with `No module named 'psycopg'`. The Cloud SQL socket path names `+psycopg2` and is unaffected. Fix it in WP-1.3.
- `backend/tests` and `tools/tests` are both packages named `tests`, so one `pytest` run cannot collect both. Run them separately.
- Cloud Run's front end reserves `/healthz` and answers it with a Google 404 before the request reaches the app, so the PR #2 runbook's `/healthz` check is wrong: use `/health`. The health endpoints also return the raw DB exception text to unauthenticated callers (WP-4.3).
- Production Cloud SQL is Postgres 16; the migrations CI job uses 15 (SPEC-01). Consider moving CI to 16.

## Blockers

None.
