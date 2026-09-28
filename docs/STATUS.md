# STATUS — session handoff

Update this file at the end of every session (SPEC plan §3 rule 8). Keep it under 60 lines.

**Last updated:** 2026-09-28 (Claude)
**Phase:** P1 — Fix

## Done

- v6 repo bootstrapped from v5 history; tag `v5.0.0-baseline`.
- Specs 00–11 (`docs/specs/`); decisions D1–D15 (`docs/IMPLEMENTATION_PLAN.md` §5).
- Delegation pack:
  - `AGENTS.md`, `GEMINI.md`
  - `docs/tasks/` (14 cards covering 13 WPs, plus the template)
  - `docs/contracts/` (auth, triage, mitosis, grading, research)
- Pre-written acceptance tests, validated against throwaway reference implementations. They skip until the delegate's module exists:
  - `backend/tests/inference/test_strict_schemas.py` (WP-2.4)
  - `tools/tests/test_lint_literals.py` (WP-2.5)
  - `backend/tests/eval/test_metrics.py` (WP-5.1)
  - `backend/tests/eval/test_splits.py` (WP-5.3)
- Baseline: 251 backend tests pass on v6 (2026-09-28).

## Ready for delegates now

Lanes are in `docs/tasks/README.md`.

| Lane | Ready |
|---|---|
| A | WP-1.2 (includes Stage 6 removal) |
| C | WP-5.1 → WP-5.3 → WP-5.2 |
| D | WP-2.4, WP-2.5 |
| E | WP-7.1 |
| B | WP-9.3, which waits for WP-1.2 to merge, then 4.2, 6.4, 7.7, 8.5, 9.2 |

## Claude — next

1. **WP-2.1 + 2.2.** Alembic baseline, typed `PipelineConfig`, `config_hash`, `configs/models.yaml`. Stays inside `backend/app/core`, `backend/alembic`, `configs`.
2. **WP-2.3.** DecisionRecord, ModelGateway, RunMode, fallback policy. This is where `pipeline/medgemma.py` gets split.
3. **M3** (3.1 → 3.4), then **4.1**.

**Coordination.** WP-1.2 edits `backend/worker/grading.py` (narrative lines only) and `backend/app/main.py` (report DDL). Claude rebases onto it before touching those files.

## Open items (program owner)

- BCNB details: mpp, magnification, grade field and mapping, annotation format, licence (SPEC-02 §3.2).
- Optional: add `Bash(git push:*)` to `.claude/settings.local.json` so Claude can push; otherwise the owner pushes.
- TUPAC16: one availability attempt (optional, SPEC-00 §3.1).

## Findings to fix

- The v5 test suite authenticates with the developer's real Google ADC credentials: a `google.auth` "end user credentials" warning appears during `pytest`. Some test reaches a live Google client, most likely the Gemini referee path, which the `USE_MOCK_VERTEX_AI` flag does not gate. Close this in WP-2.3: gateway fakes, plus a conftest guard that fails any test creating a real Google client.

## Blockers

None.
