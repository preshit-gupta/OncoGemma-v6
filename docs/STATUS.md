# STATUS — session handoff

Update this file at the end of every session (SPEC plan §3 rule 8). Keep it under 60 lines.

**Last updated:** 2026-09-28 (Claude)
**Phase:** P1 — Fix

## Done

- v6 repo bootstrapped from v5 history; tag `v5.0.0-baseline`. Specs 00–11; decisions D1–D15 (`docs/IMPLEMENTATION_PLAN.md` §5).
- Delegation pack: `AGENTS.md`, `GEMINI.md`, `docs/tasks/` (14 cards), `docs/contracts/`, and pre-written acceptance tests for WP-2.4, 2.5, 5.1, 5.3 (they skip until the module exists).
- **WP-2.1 + 2.2** on branch `wp/2.1-2.2-alembic-typed-config` (not merged):
  - `backend/alembic/` with `0001_v5_baseline`. `alembic upgrade head` + `alembic check` pass on an empty Postgres 15. A v5 `create_all` database stamped at 0001 has an identical `pg_dump` schema. CI: `.github/workflows/migrations.yml`.
  - API startup (`ENV != test`) runs `app.core.migrations.upgrade_to_head` under a Postgres advisory lock. It refuses a database that has tables but no `alembic_version`. `create_all` is gone from startup.
  - `app.core.pipeline_config.PipelineConfig`: every `configs/*.yaml` plus `configs/prompts/*`, `extra="forbid"`, strict types, range and cross-file checks, duplicate-YAML-key check. `config_hash()` is stamped on every stage execution by `worker/main.py` and `worker/cloud_job_entry.py`. Load failure aborts startup.
  - `configs/models.yaml` → `app.core.model_registry.ModelRegistry`. `${NAME}` resolves from an allowlist of Settings fields.
- Backend suite on that branch: 310 passed, 3 skipped (the 251 baseline plus 59 new tests in `backend/tests/core/`).

## Ready for delegates now

Lanes are in `docs/tasks/README.md`.

| Lane | Ready |
|---|---|
| A | WP-1.2 (includes Stage 6 removal) — in progress |
| C | WP-5.1 → WP-5.3 → WP-5.2 |
| D | WP-2.4, WP-2.5 |
| E | WP-7.1 |
| B | WP-9.3, which waits for WP-1.2 to merge, then 4.2, 6.4, 7.7, 8.5, 9.2 |

## Claude — next

1. **Merge order: WP-1.2 first, then WP-2.1/2.2 rebased onto it.** The 0001 baseline contains `reports`, and WP-1.2 deletes the `Report` model, so `alembic check` fails until a 0002 revision handles that table. After the rebase: add that revision, drop the `cap_elements` / `staging` fields from `PipelineConfig`, and delete `ensure_schema_up_to_date` and the unused `Base` import in `app/main.py`. The hunks do not overlap WP-1.2's.
2. **WP-2.3.** DecisionRecord (revision 0002/0003), ModelGateway, RunMode, fallback policy; split `pipeline/medgemma.py`. Carry-overs from 2.2:
   - Handlers take `PipelineConfig` by injection; remove `yaml.safe_load` in `worker/mitosis.py`, `worker/triage.py`, `pipeline/{scoring,grading,qc_checks}.py`, `routers/grading.py` and the `medgemma.py` prompt loader.
   - `model_versions` come from `registry.version_of()`. Remove the Settings endpoint and model fields once the gateway reads the registry.
   - EVAL refuses registry versions containing `@unverified` and floating model aliases (SPEC-01 §3.7).
3. **M3** (3.1 → 3.4), then **4.1**.

## Open items (program owner)

- **Before deploying WP-2.1:** stamp production once, from `backend/`: `alembic stamp 0001_v5_baseline`, then `alembic check`. Until then, API startup stops with `UnversionedDatabaseError` (intended).
- Decide for the 0002 revision: **drop** the v5 `reports` table, or **rename** it to an archive table kept out of `alembic check`.
- Fill in the registry versions marked `@unverified` in `configs/models.yaml` (`path_foundation`, `kongnet_det_midog_1`) from `gcloud ai endpoints describe`.
- Is `configs/stain_reference.png` part of `config_hash`? It changes outputs but is not YAML; SPEC-04 §3.3 may replace it.
- Use one git worktree per agent. On 2026-09-28 a WP-1.2 delegate and Claude shared one checkout, and the branch switched under Claude mid-task.
- BCNB details (SPEC-02 §3.2). Optional: `Bash(git push:*)` allow rule. Optional: TUPAC16 availability attempt.

## Findings to fix

- The v5 test suite authenticates with the developer's real Google ADC credentials (a `google.auth` "end user credentials" warning). Close this in WP-2.3 with gateway fakes and a conftest guard that fails any test creating a real Google client.
- `backend/requirements.txt` has `sqlalchemy>=2.0.28` with only `psycopg2-binary`. SQLAlchemy 2.1 (what a fresh build resolves) maps plain `postgresql://` to psycopg 3, so such URLs fail with `No module named 'psycopg'`. The Cloud SQL socket path names `+psycopg2` and is unaffected. Fix it in WP-1.3 (pin, or name the driver everywhere).

## Blockers

None.
