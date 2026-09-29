# STATUS — session handoff

Update this file at the end of every session (SPEC plan §3 rule 8). Keep it under 60 lines.

**Last updated:** 2026-09-29 (Claude)
**Phase:** P1 — Fix

## Done

- v6 repo bootstrapped from v5 history; tag `v5.0.0-baseline`. Specs 00–11; decisions D1–D15 (`docs/IMPLEMENTATION_PLAN.md` §5).
- Delegation pack: `AGENTS.md`, `GEMINI.md`, `docs/tasks/` (14 cards), `docs/contracts/`, and pre-written acceptance tests for WP-2.4, 2.5, 5.1, 5.3.
- Merged into `main`: WP-1.2, 2.4, 2.5, 5.1, 5.2, 5.3, 9.3, then WP-2.1 + 2.2 (PR #1), the WP-4.3 database-password slice (PR #2), the suite fixes (PR #3), and the frontend lane (WP-4.2, 6.4, 7.7, 8.5, 9.2) with WP-1.3 (PR #9).
- **Deployed 2026-09-28** (`cloudbuild.yaml`: API, frontend, worker job); production stamped `0001_v5_baseline`, startup upgraded it to `0002`; later revisions apply at API startup. **Cloud SQL password rotated 2026-09-28**: a random value only in Secret Manager `og-db-password`, read as `DB_PASSWORD` via `--set-secrets`; the old one in git history no longer works; `/health` returns 200.
- **WP-2.1 + 2.2:** Alembic (`0001`, `0002_drop_v5_reports`), CI `migrations.yml`, startup `upgrade_to_head` under an advisory lock. `PipelineConfig` (every `configs/*.yaml` and prompt, strict, hashed) and `ModelRegistry` (`configs/models.yaml`, verified Vertex deployments).
- **WP-2.3 is complete on `main`** (#6, #11, #12, #13, #14, #15): DecisionRecord + `ModelGateway` (contract checks, transport-only retries, strict parsing, cache, one record per attempt, EVAL refuses aliases, `invoke_or_fallback`), one `execute_stage`, and triage, mitosis and grading on the gateway with every v5 fallback deleted (random embeddings, forced confidences, defaulted scores and types, unconfirmed-hotspot fallback). Config injection (`QcConfig`, `ScoringConfig`, `MitosisScoringConfig`); AC3 `tests/test_fallback_paths_eval.py`, AC4 Hypothesis schemas; fail-loud ruff (`tools/ruff_fail_loud.toml`, may only shrink). Images `wp2.3e-4c1b34e` are built; the owner deploys.
- **M3 = WP-3.1–3.4 (SPEC-04): four cumulative PRs, #16 → #17 → #18 → #19 (`wp/3.1-slide-reader` … `3.4-migrate-read-sites`); merge in order, one at a time. #16 and #17 (3.1, 3.2) are merged (owner, 2026-09-30); #18 and #19 (3.3, 3.4) are open.** WP-7.2 (`wp/7.2-detector-client`, depends on 3.1) runs in parallel in its own worktree. Suite at 3.4: 997 passed, 1 skipped (main was 726 passed, 1 skipped; 3.1 784, 3.2 850, 3.3 945); `tools/tests` 49 passed; literal baseline 121 → 77.
  - 3.1 `pipeline/slide_io.py`: thread-safe `SlideReader` (one handle per thread, resolution from the `Slide` row) and `read_region_at_mpp` (coarsest fine-enough level, ICC, exact size, `Region` metadata); AC6/AC7 on real pyramidal TIFFs. `0006` adds `slides.mpp_source`/`native_mpp`.
  - 3.2 `StainTransform` (pointwise, bit-exact, AC2), `fit_stain_profile`, `stain_profiles` (`0007`), `configs/specimen_profiles.yaml` + hashed `configs/stain_refs/*.json` (`v5_patch@v1`), `tools/build_stain_reference.py`.
  - 3.3 `TissueMask` (full extent, exact area queries; AC4/AC5), `cases.specimen_type` (`0008`; `unknown` refused, AC8; `PATCH /cases/{id}/specimen-type`), Stage 2 and QC rebuilt (mm² by specimen, focus over the mask, `resolution` check).
  - 3.4 every stage and router reads through `read_region_at_mpp` and normalises with the persisted profile; the v5 normaliser, lock and thumbnail/mask fallbacks are deleted; detector tiles are resampled to 0.25 µm/px (`detector_upsampled` recorded); AC1 is `tests/test_single_authority.py`.

## Ready for delegates now

Lanes are in `docs/tasks/README.md`. **New, blocks deploying M3:** a frontend card for the specimen type (required select on case creation, a prompt for `unknown` cases like `needs_mpp`, `PATCH .../specimen-type` then retry preprocess). Lane E: WP-7.1.

## Claude — next

1. Owner merges 3.3 and 3.4 (3.1 and 3.2 are in), the `migrations` CI job checks `0006`–`0008` on Postgres 15, then **4.1**. Before any deploy: run `pytest backend/tests/pipeline/test_slide_io.py::test_concurrent_reads_are_byte_identical_to_single_threaded_reads` on Linux (AC7 has only run on Windows).

## Open items (program owner)

- **TODO after the M3 deployment is finished (owner): check the pen-mark rule on real slides.** A pen mark is a pen-coloured connected component of at least 1 mm² (`qc.yaml` `pen_marks.min_component_area_mm2`), not a pen-coloured pixel (SPEC-04 §3.6 says pixels): hematoxylin falls in the blue pen range (h 90–130) and removing it by colour deleted tissue in the synthetic test. Look for bluish tumour dropped from the mask, and for ink left in it (AC5 BCNB diagnostic).
- **M3 must not be deployed before the frontend can state a specimen type** (preprocess refuses `unknown`, and today's UI sends none). Existing cases need a specimen type and a preprocess re-run (mask json + stain profile). Slides that need MPP have no case thumbnail (409); the owner accepted that on 2026-09-30.
- Real-slide checks M3 could not run (synthetic tissue only): AC3 seams on 10 TCGA val slides, AC5 BCNB mask IoU, AC9 raw vs normalised F1, tuning of the mask/QC/stain proposals. The mitosis referee now sees normalised, 0.25 µm/px crops (SPEC-04 §3.5; `color:` in `mitosis.yaml`). The pen rule's departure from SPEC-04's wording is the TODO above.
- Record in the plan §5: D16, drop the v5 `reports` table (2026-09-28). WP-2.3a: add `tumor_referee` to the SPEC-01 §3.3 task catalogue.
- **MIDOG v2 is not deployed**: `kongnet_det_midog_1` stays on `kongnet_midog_v1`; after the WP-7.1 `deploy.ps1` run switch `wire_format` to `kongnet_midog_v2` and its `version`/deploy date.
- `docs/contracts/mitosis_v6.md` types `mitotic_score` as `1 | 2 | 3`; a summary with zero HPFs still reports 1. Proposal: allow `null`.
- Use one git worktree per agent; tests share the fake-GCS dir under the system temp dir, so concurrent `pytest` runs need private `TMP`. BCNB details (SPEC-02 §3.2). Optional: `Bash(git push:*)` allow rule, TUPAC16.
- After the worker job's next real execution, confirm its logs show `[DB Core] Using Cloud SQL instance` and no `password authentication failed`. Rotate passwords from the secret (`gcloud secrets versions access latest`), never a separately generated value.

## Findings to fix

- Cloud Run reserves `/healthz` (Google 404): use `/health`. Health endpoints return raw DB exception text to unauthenticated callers (WP-4.3).
- Production Cloud SQL is Postgres 16; the migrations CI job uses 15. `backend/tests` and `tools/tests` are both packages named `tests`: run them separately.
- Ingest's raw DeepZoom pyramid (`DeepZoomGenerator`) applies no ICC conversion. OpenSlide's `DeepZoomGenerator.get_tile` returns one pixel less than its `get_tile_dimensions` on some levels.
- SQLite `batch_alter_table` on `cases` cascade-deletes children when foreign keys are enforced (`0008` avoids it); avoid batch mode on root tables.

## Blockers

None.
