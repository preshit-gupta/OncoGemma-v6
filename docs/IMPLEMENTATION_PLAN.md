# OncoGemma v6 — Implementation Plan

Companion to the specs in [`docs/specs/`](specs/00-program-overview.md). Progress is tracked in [`docs/STATUS.md`](STATUS.md), and delegated work is described by task cards in [`docs/tasks/`](tasks/README.md).

## 1. Execution order

The program owner set the order on 2026-09-28:

1. **Delegate WP task cards first**, so delegated work can start in parallel.
2. **Claude WPs** next.
3. **Delegate+review WPs** last, scheduled by token budget.

Two dependency-driven adjustments were proposed on the same day. The program owner can override either one.

- **WP-1.1 (remove Stage 6) is merged into the WP-1.2 delete card and recommended for the first delegate batch.** Deleting about 6,500 lines early cuts the tokens every later session spends reading code, and it avoids edit conflicts.
- **WP-5.2 (dataset adapters) has a card now and is on the critical path.** Claude WPs 5.4, 6.2, 7.3 and 7.4 need the TCGA, BCSS and MIDOG++ data. If the adapters aren't delegated early, those Claude WPs will wait.
- **WP-2.1 (Alembic baseline) is absorbed into Claude's M2 work**, because WP-2.3 cannot create the `decision_records` table without it.

## 2. Work packages

Sizes: **S** is under 1 session, **M** is about 1 session, **L** is 2–3 sessions.

Owners:
- **D** — Delegate.
- **D+R** — Delegate, and Claude reviews the diff.
- **C** — Claude.

The card column links the delegate task cards.

| WP | Title | Spec | Size | Owner | Depends on | Card |
|---|---|---|---|---|---|---|
| 0.1 | Handoff file, task-card template, `AGENTS.md`, CI (ruff + pytest) | — | S | D+R | — | docs and `AGENTS.md` done now; **CI remains** |
| 1.1 | Remove Stage 6 and rewire the stage list | 08 §2 | M | D+R | — | [WP-1.2](tasks/WP-1.2-slim-down.md) (merged) |
| 1.2 | Delete one-off tools, old CLI, EVAL.md, audit evaluator; archive v5 audit | 11 §2 | S | D | — | [WP-1.2](tasks/WP-1.2-slim-down.md) |
| 1.3 | Per-service requirements, lockfiles, Dockerfiles, strict frontend build | 11 §3 | M | D+R | 1.2 | later |
| 2.1 | Alembic baseline and migration CI | 01 §3.1 | S | **C** (absorbed) | — | — |
| 2.2 | Typed config, `config_hash`, model registry | 01 §3.7–3.8 | M | C | 2.1 | — |
| 2.3 | DecisionRecord, ModelGateway, RunMode, fallback policy | 01 §3.2–3.6 | L | C | 2.2 | — |
| 2.4 | Strict output schemas | 01 §3.5 | S | D | — | [WP-2.4](tasks/WP-2.4-strict-schemas.md) (tests pre-written) |
| 2.5 | Hardcoded-literal scanner and CI hook | 01 §3.8 | S | D | — | [WP-2.5](tasks/WP-2.5-literal-scanner.md) (tests pre-written) |
| 3.1 | `SlideReader`, `read_region_at_mpp` | 04 §3.1 | M | C | 2.2 | — |
| 3.2 | StainProfile / StainTransform, reference builder | 04 §3.3–3.4 | M | C | 3.1 | — |
| 3.3 | TissueMask, specimen type, profiles, QC | 04 §3.2, 3.6–3.7 | M | C | 3.1 | — |
| 3.4 | Migrate every read and normalise call site | 04 | M | C | 3.1–3.3, 2.3 | — |
| 4.1 | Backend SSO, sessions, RBAC, route coverage, webhook OIDC | 03 §3–4 | L | C | 2.1 | — |
| 4.2 | Login page, middleware, header removal, admin users page | 03 §4.3 | M | D | contract | [WP-4.2](tasks/WP-4.2-frontend-auth.md) |
| 4.3 | Safety hardening (routes, key, CSP, audit trigger, rate limits) | 03 §5 | M | D+R | 4.1 | later |
| 5.1 | `eval/metrics.py` | 02 §7 | M | D | — | [WP-5.1](tasks/WP-5.1-metrics.md) (tests pre-written) |
| 5.2 | Dataset adapters and manifests | 02 §3, §5.1 | M | D+R | — | [WP-5.2](tasks/WP-5.2-dataset-adapters.md) (**critical path**) |
| 5.3 | Splits, lock, disjointness checks | 02 §5.2 | S | D | — | [WP-5.3](tasks/WP-5.3-splits.md) (tests pre-written) |
| 5.4 | TCGA report label extraction | 02 §4 | M | C | 5.2 | — |
| 5.5 | Harness controller, runs/items, stage service, CLI, one-shot, batch API | 02 §5–6 | L | C | 2.3, 5.1 | — |
| 6.1 | Tile grid and embedding cache via the gateway | 05 §3 | M | C | 3.x | — |
| 6.2 | Tumour-head training and model card | 05 §4 | M | C | 5.2, 5.3, 6.1 | — |
| 6.3 | Hotspot windows, Chebyshev selection, edit validation | 05 §5 | M | D+R | 6.1 | later |
| 6.4 | Heatmap overlay and TriageViewer | 05 §4.3 | M | D | contract | [WP-6.4](tasks/WP-6.4-triage-viewer.md) |
| 7.1 | MIDOG service v2 contract (separate repo) | 06 §4 | S | D | — | [WP-7.1](tasks/WP-7.1-midog-service.md) |
| 7.2 | Tiling with ownership, detector client, NMS, Stage-A cache | 06 §5.1–5.2, 5.7 | M | C | 3.1, 7.1 | — |
| 7.3 | Attribution study | 06 §6.2 | L | C | 5.2, 7.2 | — |
| 7.4 | Classifier B: train, export, serve | 06 §5.3 | L | C | 5.2, 7.2 | — |
| 7.5 | Referee v2 (definition, markers, post-rule) | 06 §5.4 | M | C | 2.3, 2.4 | — |
| 7.6 | Worker rewrite, detections migration, HPF changes | 06 §5.5–5.8 | M | C | 7.2–7.5 | — |
| 7.7 | MitosisViewer / Gallery UI | 06 §9 | M | D | contract | [WP-7.7](tasks/WP-7.7-mitosis-viewer.md) |
| 8.1 | Tumour-mask sampling, aggregation, bias removal | 07 §4–5.2 | M | C | 6.x | — |
| 8.2 | VLM estimators (Gemini, MedGemma with images) | 07 §5.3, 6.4 | M | C | 2.3 | — |
| 8.3 | StarDist segmenter and nuclear features | 07 §6.2–6.3 | M | D+R | 3.1 | later |
| 8.4 | MIL/ordinal heads, histotype, direct-grade comparator | 07 §5.3, 6.4, 7 | L | C | 5.x, 8.1 | — |
| 8.5 | GradingReviewWorkspace rewrite | 07 §10 | M | D | contract | [WP-8.5](tasks/WP-8.5-grading-workspace.md) |
| 9.1 | Research API | 08 §7 | M | D+R | 5.5 | later |
| 9.2 | Research UI | 08 §3–6 | L | D | contract | [WP-9.2](tasks/WP-9.2-research-ui.md) |
| 9.3 | Label clean-up and label lint | 10 | M | D | 1.1 | [WP-9.3](tasks/WP-9.3-labels.md) |
| 10 | Correction capture (loop capture only) | 09 §3–4 | M | C | 2.3 | — |
| 11 | Audit-1 | 11 §4 | L | C | all P1 | — |

**Phase 2 (measure)** is compute- and cost-bound rather than token-bound. **Phase 3 (improve)** runs SPEC-09 retraining and promotion.

### 2.1 Parallel lanes and conflict map

Delegates only touch files that Claude isn't editing at the same time.

| Lane | WPs | Files owned by the lane |
|---|---|---|
| A — slim-down | 1.2 (+1.1) | Deletions listed in the card; `README.md`; `docs/archive/` |
| B — frontend | 9.3 → 4.2 → 6.4 / 7.7 / 8.5 / 9.2 | `frontend/**` |
| C — eval package | 5.1, 5.3, 5.2 | `backend/eval/**`, `backend/tests/eval/**` |
| D — schemas and tools | 2.4, 2.5 | `backend/app/inference/schemas.py`, `backend/tests/inference/**`, `tools/**` |
| E — detector service | 7.1 | Separate repo `D:\Projects\MIDOG` |
| Claude | M2 → M3 → 4.1 → 5.4/5.5 → M6–M8 → 10 → 11 | `backend/app/**` (except `app/inference/schemas.py`), `backend/pipeline/**`, `backend/worker/**`, `configs/**`, `backend/alembic/**` |

**Frontend contracts.** UI cards (4.2, 6.4, 7.7, 8.5, 9.2) build against the contracts in [`docs/contracts/`](contracts/README.md), using a mock mode (`NEXT_PUBLIC_API_MOCK=1`) with fixtures. The matching Claude backend WP implements the same contract, and any contract change must update the contract file in the same PR.

## 3. Token-conscious working rules

1. **One WP per session.** Start from `docs/STATUS.md` plus the card (or the spec section) for that WP. Never load all 12 specs; they total about 3,000 lines.
2. **Delete before refactoring** (§1).
3. **Read code by symbol or line range.** Large files cost 15–25k tokens per full read: `pipeline/medgemma.py`, `GradingReviewWorkspace.tsx`, `MitosisViewer.tsx`.
4. **Split `pipeline/medgemma.py` during WP-2.3** so that later sessions read small modules.
5. **Tests.** Each WP runs its targeted tests. The full suite (about 2 minutes) runs at milestone ends.
6. **Spec line numbers drift after M2.** From then on, treat `file:line` references in the specs as pointers to symbols. Do not re-verify them line by line.
7. **Review delegate output via diff plus tests**, not by re-reading whole files.
8. **Update `docs/STATUS.md`** at the end of every session: what was done, what is next, and open questions.

## 4. Delegation policy

- **Test-first.** Where correctness is subtle (2.4, 2.5, 5.1, 5.3), Claude writes the tests before delegation. Those tests use `pytest.importorskip`, so they skip until the module exists and then must pass.
- **Bounded cards.** Each card lists allowed files, interfaces, acceptance commands, out-of-scope items and a done checklist. Delegates must not edit `docs/specs/**` or `docs/contracts/**`. They propose changes in the PR description instead.
- **Branch per WP.** Branches are named `wp/<id>-<slug>`, and the work is submitted as a PR to `main`. The program owner merges.
- **Review.** D+R cards get a Claude review of the diff and test results before merge. D cards are merged by the program owner after the acceptance commands pass.
- **Never delegated.** Clinical logic, model selection, security core and cross-cutting refactors (SPEC-00 §6 P0 items owned by C).
- **Delegate conventions** are in [`AGENTS.md`](../AGENTS.md). `GEMINI.md` points there.

## 5. Decision log

| # | Date | Decision | Where recorded |
|---|---|---|---|
| D1 | 2026-09-28 | Datasets: **TCGA-BRCA** (resections) is primary and **BCNB** (core biopsies) is secondary. The north-star metric is **F1** | SPEC-00 §3 |
| D2 | 2026-09-28 | **Fix every issue before validating.** The locked test run happens only after P1 | SPEC-00 §1 |
| D3 | 2026-09-28 | Repo `preshit-gupta/OncoGemma-v6` carries the full v5 history. Tag `v5.0.0-baseline` = `2b87ab7`. The full Apache-2.0 LICENSE comes from the v6 initial commit. The v6 source PDFs stay local and are not committed | git history; SPEC-00 §5 |
| D4 | 2026-09-28 | Specs are highly technical, with F1 as the north star. Issue taxonomy: Biological / Model / Staging / Technical (Vision Doc) | SPEC-00 |
| D5 | 2026-09-28 | Sign-in is **Google Workspace SSO only**. Roles: admin, researcher, pathologist, viewer | SPEC-03 |
| D6 | 2026-09-28 | Pathologist capacity for TCGA mitosis annotation is **not known yet**. The annotation protocol is optional | SPEC-06 §8, SPEC-08 §6 |
| D7 | 2026-09-28 | SPEC-11 interpretation confirmed: lightweight = remove dead code and dependencies; audit = multi-pass with zero open critical/high findings | SPEC-11 |
| D8 | 2026-09-28 | The **mitosis definition is fixed once**, with no recurring sign-off. **A dividing cell counts as 1 mitosis**, including in the ground-truth harmonisation. The same fixed-definition policy covers the tubule and pleomorphism definitions | SPEC-06 §3, SPEC-07 |
| D9 | 2026-09-28 | **KongNet is treated as externally validated.** There is no KongNet-only F1 and no training-overlap audit | SPEC-00 R1, SPEC-06 |
| D10 | 2026-09-28 | BCNB details will be supplied later. The adapter is parameterised and refuses to run until configured | SPEC-02 §3.2 |
| D11 | 2026-09-28 | Licences: an options register was adopted. **StarDist is the default nuclei segmenter.** HoVer-Net/PanNuke is a research-only arm. TUPAC16 and MITOS-ATYPIA-14 are optional | SPEC-00 §3.1, SPEC-07 §6.2 |
| D12 | 2026-09-28 | Targets accepted: **NS-M ≥ 0.70**. Grade floors are set from the P2 baseline | SPEC-00 §2.5 |
| D13 | 2026-09-28 | The program owner performs `git push`. Claude's push was blocked by the Claude Code permission classifier. An allow rule `Bash(git push:*)` in `.claude/settings.local.json` would enable it | — |
| D14 | 2026-09-28 | Order: delegate cards → Claude WPs → delegate+review WPs by token budget, with the §1 adjustments proposed | this file §1 |
| D15 | 2026-09-28 | Delegation is test-first. Cards are bounded, and delegates only touch lanes disjoint from Claude's work | this file §4 |
