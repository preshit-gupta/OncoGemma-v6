# WP-7.10 — Stage 4 viewer: HPF circles, what is counted, figure descriptions

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Delegate or Claude | M | SPEC-06 §9; contracts `mitosis_v6` and `grading_v6` as amended by WP-6.5 and WP-7.9 | WP-6.5 and WP-7.9 merged (mock mode can start earlier); WP-9.4 merged (same files) | B (`frontend/**`) |

## Goal

Make Stage 4 show exactly what is counted:
- the 10 HPF circles on the slide, with their padded frames;
- the figures inside a circle, marked differently from figures outside every circle;
- for each counted figure, a morphology description the pathologist can interpret (WP-7.9).

Stages 4 and 5 also say plainly when the tissue was inadequate for 10 HPFs. This is the UI half of owner review issues S3-1 (overcounting as seen), S3-4 and S4-2 (plan §2.4).

**What is wrong today.**
- `MitosisViewer.tsx` draws no HPF circles.
- It marks every counted candidate as in an HPF (`in_hpf: c.counted`), including figures outside every circle, so the screen suggests that figures outside the circles are counted. The server's `count_total` never counted them.
- The decision panel shows a VLM verdict that is always empty while the referee is off.

## Read first (only these)

- `docs/contracts/mitosis_v6.md` (after WP-6.5 and WP-7.9: `Candidate.hpf_seq`, `description`, `description_status`, `Hpf.frame_um`, `summary.hpf_target`, the deleted `replace-hpfs`), `docs/contracts/grading_v6.md` (`mitotic.flags`, `hpf_target`)
- `frontend/components/viewer/MitosisViewer.tsx`, `frontend/components/viewer/MitosisGallery.tsx`, `frontend/components/viewer/OpenSeadragonViewer.tsx` (detection markers; the hotspot overlay)
- `frontend/lib/api/mitosis.ts`, `frontend/lib/api/grading.ts`, `frontend/lib/mock/mitosis.json`, `frontend/lib/mock/grading.json`, `frontend/lib/labels.ts`
- `frontend/components/viewer/GradingReviewWorkspace.tsx`: only the block that renders `mitotic`

## Files you may touch

- `frontend/components/viewer/MitosisViewer.tsx`, `MitosisGallery.tsx`, `OpenSeadragonViewer.tsx`, `GradingReviewWorkspace.tsx` (the mitotic block only)
- `frontend/lib/api/mitosis.ts`, `frontend/lib/api/grading.ts`, `frontend/lib/mock/mitosis.json`, `frontend/lib/mock/grading.json`, `frontend/lib/labels.ts`

## Tasks

1. **Types.**
   - `Candidate.hpf_seq`, `description`, `description_status` (`MitosisDescription` as in the contract).
   - `Hpf.hotspot_id`, `Hpf.frame_um`, `MitosisSummary.hpf_target`.
   - `GradingV6.mitotic.flags` and `hpf_target`.
   - Delete `replaceHpfs` and the "Re-place HPFs" button and label. The route no longer exists.
2. **HPF overlay.**
   - Draw each HPF's circle (`center_um`, `radius_um`), numbered by `seq`, with its padded frame (`frame_um`) dashed. Do not hardcode sizes.
   - Clicking an HPF chip pans to that circle.
3. **Markers.** `in_hpf` is `hpf_seq !== null`; never derive it from `counted`.
   - Counted figure inside a circle: as today's counted marker.
   - Counted figure outside every circle: hollow grey, tooltip "Outside the HPFs, not counted".
   - Gallery and queue rows show "HPF {seq}" or "Outside HPFs".
4. **Description panel.** In the decision-chain panel, replace the empty VLM block while the referee is off:
   - `ok`: a "Morphology description" section. List the six fields as plain words (from `labels.ts`), then the summary, then a fixed note: "Descriptive only. It does not change the label or the count."
   - `unavailable`: "Description unavailable for this figure."
   - `not_requested`: nothing.
   - No model or vendor name (AGENTS rule 6).
   - Keep the existing VLM verdict block for when `vlm` is non-null (eval ablations).
5. **Tissue inadequacy.**
   - Replace `help.hpfCountWarning` with: "Tissue inadequate: {n_hpf} of {hpf_target} HPFs examined ({area_mm2} mm²). The mitotic score is based on this area." `hpf_target` comes from `summary`.
   - Stage 5 (`GradingReviewWorkspace`): when `mitotic.flags` includes `hpf_count_lt_10`, show the same sentence under the mitotic score.
6. **Mock fixtures.**
   - Four HPFs.
   - One counted figure outside every circle.
   - Two described figures: one `ok`, one `unavailable`.
   - A grading fixture with `hpf_count_lt_10`.

## Acceptance (run these)

```powershell
cd frontend
$env:NEXT_PUBLIC_API_MOCK = "1"; npm run build
npx tsc --noEmit; npm run lint:labels
git grep -n "in_hpf: c.counted\|replaceHpfs\|replace-hpfs" -- frontend    # no output
```

Visual check with the owner, in mock mode:
- the circles and frames;
- the hollow marker outside the circles;
- the description panel in its `ok` and `unavailable` states;
- the Stage 4 and Stage 5 tissue-inadequacy sentence.

## Out of scope — do not do

- Backend changes; Stage 3 (WP-6.7).
- Generating descriptions on demand.
- Any client-side count or score: the server's `summary` is the only count.

## Done checklist

- [ ] HPF circles and frames drawn; markers use `hpf_seq`; queue shows the HPF
- [ ] Description panel (three states); no model names
- [ ] "Re-place HPFs" removed; tissue-inadequacy sentence in Stages 4 and 5
- [ ] Build, type check and label lint pass; owner visual check done
