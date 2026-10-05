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
| 6.1 | Tile grid and embedding cache via the gateway | 05 §3 | M | C | 3.x | [WP-6.1](tasks/WP-6.1-tile-grid-embeddings.md) **done** (#39) |
| 6.2 | Tumour-head training and model card | 05 §4 | M | C | 5.2, 5.3, 6.1 | **done** (#41) |
| 6.3 | Hotspot windows, Chebyshev selection, edit validation | 05 §5 | M | D+R | 6.1 | **done** (#42) |
| 6.4 | Heatmap overlay and TriageViewer | 05 §4.3 | M | D | contract | [WP-6.4](tasks/WP-6.4-triage-viewer.md) |
| 6.5 | HPF sites: 0.5 mm circles in 0.6 mm frames, site edits, tissue-inadequacy gate | 05 §5, 06 §5.8 (D22) | L | C | — | [WP-6.5](tasks/WP-6.5-hpf-sites.md) (§2.4) |
| 6.6 | HPF sites at the tumour periphery first (arm H1P) | 05 §5.2 (D22) | M | C | 6.5 | [WP-6.6](tasks/WP-6.6-periphery-ranking.md) (§2.4) |
| 6.7 | Stage 3 viewer: HPF circles, Pin HPF, tissue inadequacy | 05 §5.5 | M | D | 6.5, 6.6 | [WP-6.7](tasks/WP-6.7-triage-hpf-pins.md) (§2.4) |
| 7.1 | MIDOG service v2 contract (separate repo) | 06 §4 | S | D | — | [WP-7.1](tasks/WP-7.1-midog-service.md) **done** (MIDOG-microservice #1, #2; deployed) |
| 7.2 | Tiling with ownership, detector client, NMS, raw Stage A | 06 §5.1–5.2, 5.7 | M | C | 3.1, 7.1 | **done** (#20) |
| 7.6a | Detections migration, `mitosis_v6` API, HPF fixes (baseline end to end) | 06 §5.6–5.8 | M | C | 7.2 | [WP-7.6a](tasks/WP-7.6a-detections-and-contract.md) (**production UI broken until done**) |
| 7.7 | MitosisViewer / Gallery UI | 06 §9 | M | D | contract | [WP-7.7](tasks/WP-7.7-mitosis-viewer.md) **done** (#9) |
| 7.7b | Remove the v5 mitosis client; show an ungated `in_tumor` | 06 §9 | S | D | 7.6a | [WP-7.7b](tasks/WP-7.7b-mitosis-client-cleanup.md) |
| 7.8 | Validate the baseline on MIDOG++ breast (fixed settings, no tuning) | 02 §5, 06 §3.1, 6.1 | M | C | 5.5 | [WP-7.8](tasks/WP-7.8-baseline-validation.md) |
| 7.6b | Tumour-cell gate and HPF tumour constraints | 06 §5.5, 5.8 | M | C | 6.2 (merged), 6.3 (merged), 7.6a | [WP-7.6b](tasks/WP-7.6b-tumour-gate.md) |
| 7.9 | Morphology descriptions of mitotic figures (never a decision) | 06 §5.6 (D22) | M | C | 6.5 | [WP-7.9](tasks/WP-7.9-mitosis-descriptions.md) (§2.4) |
| 7.10 | Stage 4 viewer: HPF circles, counted vs outside, descriptions; Stage 5 tissue note | 06 §9 | M | D | 6.5, 7.9, 9.4 | [WP-7.10](tasks/WP-7.10-mitosis-viewer-hpfs.md) (§2.4) |
| 7.3, 7.4, 7.5 | Attribution study, classifier B, referee v2, definition file | 06 §3, 5.3–5.4, 6.2 | — | — | — | **deferred to the next iteration** (D19) |
| 8.6 | `grading_v6` API, hotspot-framed stratified sampling, separate tubule/pleomorphism reads, unbiased aggregation (baseline end to end) | 07 §4–5.2, 6.1, 7.1, 7.3 | L | C | 7.6a, 6.2/6.3 (merged), 8.5 (merged) | [WP-8.6](tasks/WP-8.6-grading-v6-api.md) **done** (#49) |
| 8.7 | Validate the grading baseline on TCGA-BRCA val (fixed settings, no tuning) | 02 §5, 07 §7.1, 8.2 (metrics) | M | C | 8.6 (#49), 5.6 IDC source (#54) | [WP-8.7](tasks/WP-8.7-grading-baseline-validation.md) |
| 8.1 | Tumour-mask sampling, aggregation, bias removal | 07 §4–5.2 | — | — | — | **absorbed into 8.6** (sampling, aggregation, B2–B5); the whole-tumour frame is deferred (§2.3, D21) |
| 8.2, 8.3, 8.4 | VLM arms (T1 `@v2`, T1-MG, P1), StarDist and nuclear features, MIL/ordinal heads, H2, direct-grade comparator, attribution study | 07 §5.3–5.4, 6.2–6.4, 7.2, 8.2 | — | — | — | **deferred to the next iteration** (§2.3, D21) |
| 8.5 | GradingReviewWorkspace rewrite | 07 §10 | M | D | contract | [WP-8.5](tasks/WP-8.5-grading-workspace.md) **done** (#9) |
| 9.1 | Research API | 08 §7 | M | D+R | 5.5 | later |
| 9.2 | Research UI | 08 §3–6 | L | D | contract | [WP-9.2](tasks/WP-9.2-research-ui.md) |
| 9.3 | Label clean-up and label lint | 10 | M | D | 1.1 | [WP-9.3](tasks/WP-9.3-labels.md) |
| 9.4 | Quick fixes: Stage 5 scrolling, Stage 4 HPF chips | 07 §10, 06 §9 | S | D | — | [WP-9.4](tasks/WP-9.4-review-quick-fixes.md) (§2.4) |
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

### 2.2 WP-7 re-plan (2026-10-02): baseline first

The program owner set the scope on 2026-10-02: **this iteration establishes and validates the baseline. It does not test new hypotheses** (D19).

**The baseline (arm A1, D17).** KongNet (`KongNet_Det_MIDOG_1`, pretrained TIAToolbox weights, pinned SHA-256) runs at 0.25 µm/px in 512 px tiles with ownership. τ is 0.75, NMS 7.5 µm, and the **referee is off**. KongNet needs no training data to predict: it is inference-only, and labelled data is used only to measure it.

**What was measured** (MIDOG++ image 094, 82 figures; one image, one scanner, `reports/baseline/mitosis_midogpp_094.md`):

| Finding | Result |
|---|---|
| tiatoolbox 2.0.1 patch mode returned x/y transposed (every deployment until 2026-09-29) | v5 detector F1 0.12; fixed server-side (MIDOG-microservice #2) |
| KongNet at the 0.5 µm/px of its IO config | F1 0.27; native 0.25 µm/px gives 0.87 |
| Baseline (τ 0.75, NMS 7.5 µm) | F1 0.862 (P 0.815, R 0.915); 0.851 through the M3 read path |
| Gemini referee (v5 prompt) on every candidate | F1 0.676 (rejected 25 true figures): **referee off** |

**This iteration:**

1. **WP-7.6a** ships the baseline end to end. The WP-7.7 viewer calls `mitosis_v6` routes the backend does not serve yet, so the deployed Stage 4 review screen fails outside mock mode.
2. **WP-7.8** validates the baseline, with its settings fixed, on MIDOG++ breast beyond one image. No calibration, threshold tuning or new arms.
3. **WP-7.7b** (lane B) and **WP-7.6b** (tumour-cell gate) follow 7.6a. The tumour mask (WP-6.2) and the hotspot windows (WP-6.3) are merged; Stage 4 tests pass on that `main` (51 passed, 2026-10-02).

**Deferred to the next iteration** (hypotheses, tested against the validated baseline): the attribution study (7.3), classifier B and all training code (7.4), referee v2 with the definition file, prompt and post-rule (7.5), `p_A` calibration and `τ_A`, the Stage-A cache, and the research `/curves/mitosis` and `/errors/mitosis` endpoints. SPEC-06 §6.2–6.3 and AC1/AC4/AC7/AC8 (referee and classifier parts) apply then.

Notes for the next iteration:
- The A2 referee run was a mismatched setup. The v1 prompt asks for the v5 fields while Gemini is constrained to `MitosisVerdict`, it captions the images in the wrong order, and it promises a marker that is never drawn.
- RC11 (transposed coordinates) is the largest v5 cause and is not in SPEC-06 §1.

### 2.3 WP-8 re-plan (2026-10-04): baseline first

**Confirmed by the program owner on 2026-10-04 (D21).** It applies D19's baseline-first scope to Stage 5, following the owner's WP-8.6 decisions of 2026-10-02.

**Where Stage 5 is.**
- Already removed in WP-2.3: the v5 doer, the numeric anchors and the silent defaults (B3 anchoring, B5, B6). Estimates go through the gateway with strict schemas, and a failed estimate becomes `null` plus `needs_human`.
- Still in place: density sampling (B2), pleomorphism at 1.0 µm/px (B4), confidence weights, and the tie-to-max mode.
- The backend still serves the v5 grading API, so the merged WP-8.5 screen fails outside mock mode.

**This iteration:**
1. **WP-8.6** ships the baseline end to end. It covers the `grading_v6` API, stratified samples inside the confirmed hotspots (owner, 2026-10-02), tubule at 512 µm @ 1.0 µm/px, pleomorphism at 128 µm @ 0.25 µm/px, area-weighted T%, mode P, no confidence weights, and M only through `pipeline/scoring.py`. It starts after WP-7.6a merges, because both edit the grading readers and `scoring.py`.
2. **WP-8.7** validates that baseline on the locked TCGA val split with fixed settings. It reports NS-G, the band metrics, and the per-band signed error that tests SPEC-07 §1's upward-bias hypothesis. It needs the harness to read TCGA slides in place, and an owner go-ahead for the live run.

**Deferred to the next iteration.** These are tested against the validated baseline:
- WP-8.1's whole-tumour sampling frame (SPEC-07 §4) versus the hotspot frame;
- the attribution study from G0 = v5 (§8.2);
- WP-8.2 arms: T1 with `tubule@v2`/`pleo@v2` and the definition files, T1-MG, T1-chain, P1 `p75`;
- WP-8.3 StarDist and the §6.3 features (`PleoField.nuclei` stays `null`);
- WP-8.4: T3/P4 ABMIL, P2/P3 ordinal, H2, and the §7.2 direct-grade comparator;
- T4 cut-point calibration.

**Owner decisions (2026-10-04):**
- The split above is confirmed.
- When pleomorphism field scores tie, P takes the highest score.
- The WP-8.7 live run is approved.
- The owner supplies the Thennavan et al. histotype labels after the other WP-8 parts are done; a follow-up adds S5-HT.

### 2.4 Owner review of Stages 3–5 (2026-10-05)

The program owner reviewed Stages 3–5 and raised seven issues. Each was traced to the code on `main` (c7e448c).

| # | Issue (owner) | Cause found | Cards |
|---|---|---|---|
| S3-1 | **10 HPFs must total 2 mm².** HPFs are circles 0.5 mm across, padded by 0.05 mm on each side to a 0.6 mm square, and only figures inside the circle count. Today the area equals 18 HPFs, and small sections get fewer than 10 HPFs. | Stage 3 qualifies and separates 600 µm squares: 0.36 mm² each, 3.6 mm² for 10. Stage 4 fits a 524 µm disk into each square and moves it towards the figures. The viewer draws no circles and marks every counted figure as in an HPF. | 6.5, 7.10 |
| S3-2 | HPFs preferably at the tumour periphery. | The ranking (H1, mean tumour probability) favours the tumour's core. | 6.6 |
| S3-3 | "Pin ROI" fails. | Each edit request replaces the stored edits. A pinned 600 µm square overlaps the tightly packed model windows; the 422 message asks for a 100 µm gap, but the gap is 0. Pins get v5 fields and a fixed id, and "Restore" does nothing. Stage 4 orders windows by `prob_mean`, which v6 hotspots lack, so a pin can be dropped. | 6.5, 6.7 |
| S3-4 | At least 10 HPFs, with a message when the tissue is inadequate. | Stage 3 can be confirmed with any number of hotspots. Only Stage 4 flags `hpf_count_lt_10`, with a generic message, and Stage 5 says nothing. | 6.5, 6.7, 7.10 |
| S4-1 | HPF counts also show tissue and tumour fractions, which are not needed. | The chip row in `MitosisViewer.tsx`. | 9.4 |
| S4-2 | Figures need a description for the pathologist to interpret, not a judgement. | The referee, which gave a verdict and a rationale, is off (D17), and nothing replaced the rationale. | 7.9, 7.10 |
| S5-1 | The Stage 5 report does not scroll. | The workspace has no scroll container, and its parent is `overflow-hidden`. | 9.4 |

**Order.**
1. WP-9.4 now (frontend, no dependency).
2. WP-6.5 (Claude). Then WP-6.6 and WP-7.9 (Claude), which follow 6.5 because they edit the same worker and contracts.
3. WP-6.7 after 6.5 and 6.6, and WP-7.10 after 6.5, 7.9 and 9.4. The frontend cards may start in mock mode from the interfaces in the backend cards, but they merge after their backend.

**Deploy.**
- WP-6.5 deletes `replace-hpfs` and changes the triage edit grammar, so the backend ships with WP-6.7 and WP-7.10 in one release.
- Then re-run Stages 3 and 4 and grading for open cases. Old triage outputs answer `409 triage_rerun_required`.

**Effect on WP-8.7.** The HPF geometry and ranking change the mitotic count, so they change M and the grade.
- If the WP-8.7 val run has not been made, make it after WP-6.5 and 6.6 merge, so that it measures what pathologists will use.
- A run that was already made measures the old geometry, and its report says so.

**Spec changes proposed** (for owner approval; the cards implement them):
- **SPEC-05 §5.1.** A hotspot is an HPF site: a circle `hpf_diameter_um` (500 µm) across, in a square frame padded by `frame_padding_um` (50 µm). Validity is measured on the circle, and the lattice step is d/4.
- **SPEC-05 §5.2.** Arm H1P (periphery first, 1 mm band) is the production ranking. This is the owner's clinical choice (D22), not an evidence-selected arm.
- **SPEC-05 §5.3.** Non-overlap applies to circles (Euclidean distance `≥ d + gap`). Frames may overlap.
- **SPEC-05 §5.5.**
  - Edits are site operations by centre: `add`, `move`, `exclude`, `restore`, `delete`.
  - Edits accumulate, and at most `k_max` sites may be active.
  - Confirming fewer than `k_max` sites needs `accept_fewer_hpfs`.
  - The 0.1–4.0 mm² polygon rule no longer applies.
- **SPEC-06 §5.8.** An HPF is the confirmed circle (r 250 µm, 0.196 mm²). There is no placement search and no `replace-hpfs`. The score is per mm² over the circles examined.
- **SPEC-06 §5.6.** A new task, `mitosis_describe` (Gemini), describes the counted and equivocal figures inside HPFs. It is never a decision.

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
| D17 | 2026-09-30 | KongNet is served at native **0.25 µm/px**, not the 0.5 µm/px of its TIAToolbox IO config (measured F1 0.87 vs 0.27). Production arm is **A1** (τ 0.75, NMS 7.5 µm) with the **referee off**, until an arm beats it under SPEC-06 §6.3 | `configs/mitosis.yaml`, `reports/baseline/mitosis_midogpp_094.md` |
| D18 | 2026-10-02 | WP-7 re-planned from the measurements: 7.6 split into 7.6a/7.6b, 7.8 added | this file §2.2 |
| D19 | 2026-10-02 | **Baseline first.** This iteration validates the baseline (KongNet, τ 0.75, NMS 7.5 µm, referee off). The attribution study, classifier B and training code, referee v2, and calibration are deferred to the next iteration | this file §2.2 |
| D20 | 2026-10-02 | **This version is built for TCGA-BRCA only** (narrows D1). BCNB is deferred, with its core-biopsy checks (SPEC-05 AC8, per-profile `τ_tumor` from BCNB val, the BCNB binary loss term); the `core_biopsy` profile stays in code. BCSS counts as TCGA-BRCA: its expert ROI masks on TCGA-BRCA slides are the tumour-head tile labels, read in raw colour from the GDC slides by HTTP range requests (the BCSS release has only colour-normalised RGBs). MIDOG++ stays as the mitosis detector's validation set | SPEC-05 §4, WP-6.2 |
| D21 | 2026-10-04 | **Stage 5, baseline first.** WP-8.6 ships the baseline, and WP-8.7 validates it on TCGA val with fixed settings. WP-8.1 is absorbed into 8.6. WP-8.2–8.4 and the SPEC-07 attribution study are deferred. Pleomorphism ties take the highest score | this file §2.3 |
| D22 | 2026-10-05 | **HPFs after the owner's review.**<br>• An HPF is a circle 0.5 mm across (10 HPFs = 1.96 mm²), centred in a 0.6 mm frame with 0.05 mm padding. Figures count only inside the circle.<br>• Circles never overlap; frames may.<br>• Sites at the tumour periphery rank first: within 1 mm of the invasive front, glass edges excluded. Interior sites fill the rest.<br>• With fewer than 10 sites, the score is per mm² over the circles examined. Stages 3–5 say that the tissue is inadequate, and the pathologist acknowledges it at Stage 3.<br>• Gemini describes the counted figures for interpretation. The description never changes a label or the count. | this file §2.4; WP-6.5, 6.6, 7.9 |
