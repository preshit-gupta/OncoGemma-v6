# SPEC-04 — Stage 2 Rebuild: Single Stain Authority, Specimen Profiles, Resolution Normalisation, Registered Tissue Mask

| Field | Value |
|---|---|
| Spec ID | SPEC-04 |
| Category (V4) | Staging (with Technical infrastructure) |
| Issues covered | N6, A10, A5 (infrastructure part), plus the tissue-mask root cause of N7 |
| Depends on | SPEC-01 (config, registry, gateway input contracts) |
| Blocks | SPEC-02 (BCNB conversion uses `SlideReader`), SPEC-05, SPEC-06, SPEC-07 |

## 1. Answers to the Build Notes question (N6)

**Is staining set up for resection or CNB slides?** Neither, explicitly. No code path uses specimen type, and the concept does not exist in the data model: there is no `specimen_type` on `Case` or `Slide`. The single stain target, `configs/stain_reference.png`, is a 512×512 patch added in commit `e9f8d7d` (v4.4). Its source slide and specimen type are not recorded.

Two parameters are specimen-sensitive in practice, and both are tuned *against* core biopsies:

- **Tissue-mask clean-up.** `worker/triage.py:338` applies a 3×3-cell `binary_opening` on an 80-column grid, and `:344` keeps only components of at least 25 cells. On a typical slide, one cell is roughly 300–400 µm. A 1 mm-wide core is therefore only about 3 cells wide, so the opening erodes much of it. This is the heatmap gap in the Notes screenshot. See SPEC-05 §1.
- **QC tissue-coverage thresholds.** `configs/qc.yaml` sets `fail < 2%` and `warn < 5%` of slide area. A single CNB often covers less than 5% of the slide.

**Is staining also happening in Stage 3?** Yes, and in Stages 4 and 5 and in three API routers as well. §2.1 lists every site.

## 2. Problem

### 2.1 Stain normalisation: 7 call sites, 3 parameterisations

| Site | Source stain matrix | Target | Consequence |
|---|---|---|---|
| `worker/preprocess.py:96` (normalised DZI pyramid) | Fitted on a 50-patch slide mosaic (`pipeline/stain.py` `fit_macenko_stain`) | Fitted on `stain_reference.png` | Consistent, but only up to the ~10× level (`preprocess.py:52-57`) |
| `worker/triage.py:657-741` (hotspot 10×/20×/40× crops) | **Re-estimated by SVD on every patch** (`PureNumpyMacenkoNormalizer.transform`, `stain.py:160-205`), because only `stain_matrix` (the target) is loaded (`worker/triage.py:664-665`) | Target from `stain_params.json` | Patch-to-patch colour drift |
| `worker/mitosis.py:487-573` (HPF patches) | Per-patch SVD, same mechanism | Target | Same drift |
| `worker/grading.py:410, 428` (grading patches sent to the VLMs) | Per-patch SVD | **Unfitted defaults** (`W_target = [[0.644,0.717,0.267],[0.093,0.954,0.283]]`, `maxC = [1.85,1.05]`) | The patches the verifier sees are normalised to a *different* target than every other stage |
| `app/routers/tiles.py:98`, `app/routers/triage.py:520`, `app/routers/mitosis.py:359, 502` | Per-request SVD | Target | The viewer shows colours that differ from the pyramid |

`stain_params.json` does persist `stain_matrix_src` and `max_conc_src` (`stain.py:362-369`), but no consumer loads them.

### 2.2 Resolution

- Workers read level 0 with raw `openslide.read_region` and derive sizes from `mpp`. They bypass `read_region_srgb` (`pipeline/tiles.py:66`), which is documented as the "authoritative single entry point" and is the one that applies ICC-to-sRGB conversion.
- **Mitosis tiles are never resampled** (`worker/mitosis.py:233-241, 255`). A 1024-px tile is 256 µm on a 40× scan and 512 µm on a 20× scan, and KongNet receives half-resolution input on 20× scans. TCGA-BRCA mixes 20× and 40× scans.
- Triage reads `224/mpp` px at level 0 and bilinear-resizes it (`worker/triage.py:379, 434`). At 40× that reads 16× more pixels than needed and ignores pyramid levels.
- Defaults such as `mpp_x=0.25` exist in `pipeline/verify.py:230`, `pipeline/stain.py:246-247` and `pipeline/tiles.py:73-74`.
- All reads are serialised by one global `threading.RLock` (`app/core/openslide_lock.py:5`).

### 2.3 Tissue mask

- `fit_macenko_stain` builds the mask from a **512×512** read of the region `min(50 mm, W) × min(50 mm, H)` (`stain.py:278-281`).
  - Non-square slides are anisotropically squeezed.
  - Slides wider or taller than 50 mm are truncated.
- Consumers map the mask onto the slide by proportional resize, each in its own way:
  - `worker/triage.py:298` (NEAREST to 80×ny)
  - `pipeline/detect.py:500-509`
  - `pipeline/hpf.py:108-135`
  - `worker/grading.py` (`s_x = W_m / slide_w_um`)

## 3. Design

### 3.1 `SlideReader` and `read_region_at_mpp` (single pixel entry point)

```python
# backend/pipeline/slide_io.py
class SlideReader:
    """Thread-safe reader. One OpenSlide handle per thread (threading.local); no global lock."""
    def __init__(self, path: str, mpp_x: float, mpp_y: float, source_format: str): ...
    @property
    def levels(self) -> list[LevelInfo]            # (index, downsample, mpp_x, mpp_y, dims)
    def extent_um(self) -> tuple[float, float]

@dataclass(frozen=True)
class Region:
    rgb: np.ndarray            # uint8 HxWx3, sRGB
    target_mpp: float
    origin_um: tuple[float, float]
    size_um: tuple[float, float]
    native_level: int
    native_mpp: float
    upsampled: bool            # True when native_mpp > target_mpp * (1 + tol)
    icc_applied: bool
    color: Literal["raw", "normalized"]
    stain_profile_id: UUID | None

def read_region_at_mpp(
    reader: SlideReader, x_um: float, y_um: float, w_um: float, h_um: float,
    target_mpp: float, *, color: Literal["raw","normalized"] = "raw",
    stain: StainTransform | None = None, mpp_tolerance: float = 0.02,
) -> Region:
    """
    1. level = finest-resolution level L with mpp_L <= target_mpp*(1+tol); never read a coarser level and upsample
       unless level 0 itself is coarser (then upsampled=True).
    2. read (level-0 coordinates) the minimal box covering the request at level L.
    3. ICC -> sRGB via existing get_icc_transform (pipeline/tiles.py:8); RGBA composited on white.
    4. resample to exact (round(w_um/target_mpp), round(h_um/target_mpp)):
         downsample: Image.Resampling.LANCZOS (area-preserving); upsample: BICUBIC.
    5. if color == "normalized": require stain is not None; rgb = stain.apply(rgb)
    """
```

- Every pixel read in Stages 2–5 and in the routers goes through `read_region_at_mpp`. A static test asserts that `read_region(` appears only in `slide_io.py`.
- `Region` metadata becomes the `input_spec` of the DecisionRecord (SPEC-01 §3.3). `upsampled` powers the 20×/40× slices.
- The global `OPENSLIDE_GLOBAL_LOCK` is replaced by thread-local handles. OpenSlide handles are safe for concurrent reads, but implementation must **verify** this on the Windows dev build and Linux Cloud Run with a concurrency stress test (AC7). The lock stays behind a flag only if that test fails on a platform.
- **Plain-image inputs (BCNB JPG)** are converted to pyramidal TIFF at ingest (SPEC-02 §3.2). `SlideReader` always gets MPP from the `Slide` row, never from file metadata, when `mpp_source != 'file'`.

### 3.2 Specimen type and profiles

**Data model:**
- `cases.specimen_type TEXT NOT NULL CHECK (specimen_type IN ('resection','core_biopsy','unknown'))`, added by an Alembic revision.
- Existing rows become `unknown`.
- Preprocess refuses to run on `unknown` and raises `SpecimenTypeRequired`. The UI prompts for the value, the same way it already handles the `needs_mpp` state.

**Where it is set:**
- The case-creation form (a required select with no default).
- The batch/harness manifest (SPEC-02 §5.1).
- TCGA DX → `resection`; BCNB → `core_biopsy`.

**`configs/specimen_profiles.yaml`** is part of `PipelineConfig` and is hashed. Initial values are proposals, tuned on validation splits in P1:

```yaml
schema_version: 1
profiles:
  resection:
    tissue_mask:  {mpp: 8.0,  otsu_clip: [215, 235], sat_min: 12, open_radius_um: 48,  min_component_um2: 2.0e5, fill_holes_max_um2: 4.0e4}
    stain_fit:    {n_patches: 50, patch_um: 512, fit_mpp: 1.0, min_sat_mean: 0.05, sparse_below: 10}
    stain_target: {ref: configs/stain_refs/tcga_train_median@v1.json}
    qc:           {tissue_area_fail_mm2: 4.0,  tissue_area_warn_mm2: 20.0, focus_vol_threshold: 45.0, focus_fail_ratio: 0.30, focus_warn_ratio: 0.10}
    triage:       {tile_um: 224, tile_mpp: 1.0, min_tissue_fraction: 0.25}
    hotspots:     {k_max: 10, window_um: 600, min_gap_um: 100, min_tumor_fraction: 0.50, allow_fewer: true}
    grading:      {tubule_patches: 48, tubule_patch_um: 512, pleo_fields: 48, pleo_field_um: 128}
  core_biopsy:
    tissue_mask:  {mpp: 4.0,  otsu_clip: [215, 235], sat_min: 12, open_radius_um: 12,  min_component_um2: 2.0e4, fill_holes_max_um2: 1.0e4}
    stain_fit:    {n_patches: 30, patch_um: 256, fit_mpp: 1.0, min_sat_mean: 0.05, sparse_below: 8}
    stain_target: {ref: configs/stain_refs/tcga_train_median@v1.json}
    qc:           {tissue_area_fail_mm2: 0.5,  tissue_area_warn_mm2: 2.0,  focus_vol_threshold: 45.0, focus_fail_ratio: 0.30, focus_warn_ratio: 0.10}
    triage:       {tile_um: 224, tile_mpp: 1.0, min_tissue_fraction: 0.25}
    hotspots:     {k_max: 10, window_um: 600, min_gap_um: 50,  min_tumor_fraction: 0.50, allow_fewer: true}
    grading:      {tubule_patches: 24, tubule_patch_um: 512, pleo_fields: 32, pleo_field_um: 128}
```

- QC tissue thresholds change from a **fraction of slide area** to an **absolute tissue area in mm²**. What matters for grading is how much tumour tissue is available, not how full the glass is.

### 3.3 Stain reference target

The stain target is a *colour standard*. It is independent of the specimen type, so both profiles point to the same reference by default, though a profile may override it.

- **Construction.** `tools/build_stain_reference.py` samples 100 slides from the **TCGA train split**, stratified by TSS.
  1. Fit per-slide Macenko parameters with the Stage-2 fitter.
  2. Take the component-wise **median** of the unit-normalised stain vectors and of the 99th-percentile concentrations.
  3. Write `configs/stain_refs/tcga_train_median@v1.json` with the fields `{W_tgt, maxC_tgt, n_slides, slide_ids_sha256, fitter_version}`.
- **Why not a single reference patch.** A population median is reproducible and documented, whereas an arbitrary 512×512 patch is neither.
- **Evaluation-split leakage.** None: the reference is built from train slides only.

### 3.4 StainProfile and StainTransform (single authority)

Stage 2 fits once per slide and persists the result:

```sql
CREATE TABLE stain_profiles (
  id UUID PRIMARY KEY, slide_id UUID NOT NULL REFERENCES slides(id) ON DELETE CASCADE,
  fitter_version TEXT NOT NULL, reference_id TEXT NOT NULL,
  w_src JSONB NOT NULL, maxc_src JSONB NOT NULL, w_tgt JSONB NOT NULL, maxc_tgt JSONB NOT NULL,
  fit_status TEXT NOT NULL CHECK (fit_status IN ('fitted','sparse','degenerate')),
  n_patches INTEGER NOT NULL, mosaic_sha256 CHAR(64) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

```python
class StainTransform:
    @classmethod
    def from_profile(cls, p: StainProfileRow) -> "StainTransform": ...
    def apply(self, rgb: np.ndarray) -> np.ndarray:
        """Pointwise (per-pixel) mapping; NO statistics computed from the input image.
        OD = -log10(max(rgb,1)/255); C = pinv(W_src^T) @ OD;  C = max(C,0) * (maxC_tgt / maxC_src)
        OD' = W_tgt^T @ C;  rgb' = clip(255 * 10^(-OD'), 0, 255); pixels with max(OD) < beta (0.15) pass through unchanged."""
```

- **Pointwise property.** The transform is a fixed per-pixel function, so `apply(concat(A, B)) == concat(apply(A), apply(B))` exactly. That removes both the tile seams visible in the Notes screenshots and the patch-to-patch colour drift.
- **"Normalise once" (N6).** Parameters are estimated exactly **once**, in Stage 2, and persisted and versioned. Every later stage applies the persisted transform through `read_region_at_mpp(color="normalized", stain=…)` and never re-estimates. The on-the-fly router normalisation (§2.1, last row) is removed. Routers serve the Stage-2 normalised pyramid, or call `read_region_at_mpp` with the persisted transform.
- **Degenerate fits.** `fit_status='degenerate'` (no valid patches) raises a QC warning `stain_fit_degenerate`. Any consumer whose registry input contract requires `color: normalized` then fails with `InputContractError` (SPEC-01). In eval mode the item is recorded as failed, not silently passed.
- **Delete** `PureNumpyMacenkoNormalizer.transform`'s per-image SVD branch (`stain.py:170-205`) and the `get_macenko_normalizer_class` tiatoolbox switch (`stain.py:220-227`). One implementation, pure NumPy, gives deterministic results across platforms.

### 3.5 Colour policy per model (decided by F1, not by assumption)

Each registry model declares `input.color`. The P1 defaults are:

| Model | Default | Rationale | Ablation |
|---|---|---|---|
| `path_foundation` | raw | The foundation encoder is trained on unnormalised data (**verify** in the model documentation) | raw vs normalised: tumour-tile F1 on BCSS val |
| `kongnet_det_midog_1` | raw | MIDOG models are trained for stain and scanner generalisation | raw vs normalised: NS-M on MIDOG++ val |
| `mitosis_classifier` (SPEC-06) | raw + stain augmentation during training | Robustness | — |
| VLM tasks (`mitosis_referee`, `tubule_patch`, `pleo_field`, `histotype`) | normalised | Consistent appearance across sites | raw vs normalised per task, on its F1 |
| Viewer | normalised pyramid | Display | — |

### 3.6 Registered tissue mask

**Computation (Stage 2):**
- Read the full slide extent at `profile.tissue_mask.mpp` with `read_region_at_mpp`, using the coarsest level ≤ target. The output size is `ceil(W_um/mpp) × ceil(H_um/mpp)`, which preserves aspect ratio with no 50 mm cap.
- Tissue = `(gray ≤ clip(otsu(gray), *otsu_clip)) | (S_hsv > sat_min)`, minus pen-mark pixels (QC HSV ranges).
- Then apply, in order:
  1. binary opening with a disk of radius `open_radius_um / mpp` px;
  2. removal of components < `min_component_um2`;
  3. filling of holes < `fill_holes_max_um2`.

**Persistence:**
- `tissue_mask.png` (1-bit)
- `tissue_mask.json`: `{mpp, width_px, height_px, origin_um: [0,0], algorithm_version, profile}`

**Access API:**
```python
class TissueMask:
    mpp: float
    def contains_um(self, x_um, y_um) -> bool
    def fraction_in_box_um(self, x0, y0, x1, y1) -> float   # exact pixel-area fraction
    def fraction_in_disk_um(self, cx, cy, r) -> float
    def tiles(self, tile_um: float, min_fraction: float) -> Iterator[TileRef]   # SPEC-05 grid
```

All proportional-resize mapping code (§2.3) is replaced by `TissueMask` calls.

### 3.7 QC updates

- **Coverage check:** switches to the absolute tissue area (§3.2).
- **Focus check:** samples `sample_max_tiles` tiles *uniformly over the tissue mask* at `1.0 µm/px` through `read_region_at_mpp`.
- **New QC check `resolution`:**
  - Record `native_mpp`.
  - Warn when `native_mpp > 0.30`, because the slide will be upsampled for mitosis. This feeds the SPEC-00 R6 slice.
  - Fail when `native_mpp > 0.55`: mitosis counting is not supported below about 20×.

## 4. Metrics and acceptance criteria

| # | Criterion |
|---|---|
| AC1 | **Single authority.** Static test: normaliser construction and `.transform(`/`.apply(` on stain objects appear only in `pipeline/stain.py`. `read_region(` appears only in `pipeline/slide_io.py` |
| AC2 | **Pointwise.** A property test on random tiles checks that `apply(concat(A,B)) == concat(apply(A),apply(B))` bit-exactly |
| AC3 | **Seam check.** On 10 TCGA val slides, 50 random adjacent tile pairs from the normalised pyramid at the 10× level give mean CIEDE2000 across the 8-px shared strip ≤ 2.0, computed with `skimage.color.deltaE_ciede2000` |
| AC4 | **Mask registration.** A synthetic 60 × 20 mm slide with tissue squares at known µm positions yields mask centroids within 1 mask pixel. Covers slides > 50 mm |
| AC5 | **CNB retention.** A synthetic 1.0 mm × 15 mm diagonal core keeps ≥ 95% of its area in the `core_biopsy` mask. On 10 BCNB val slides, mask IoU against pathologist tumour-plus-tissue polygons (tissue only) is reported as a diagnostic |
| AC6 | **Resolution.** For one region read from a 40× slide and from its 2×-downsampled 20× derivative, `read_region_at_mpp(target=0.25)` returns identical shape and `origin_um`. The 20× read has `upsampled=True` |
| AC7 | **Thread safety.** 16 threads × 500 random `read_region_at_mpp` calls on thread-local handles give byte-identical output to single-threaded reads, with no crash on Linux (Cloud Run image) or Windows (dev) |
| AC8 | **Specimen gating.** Preprocess on `specimen_type='unknown'` fails with `SpecimenTypeRequired`. Changing the profile changes `config_hash` |
| AC9 | **Downstream F1 (tracked, not gating in P1).** Tumour-tile F1 (S3-F1) and NS-M on val computed under raw vs normalised per §3.5. The chosen colour policy is written into `models.yaml` with the run IDs as evidence |

## 5. Test plan

- **Unit:** `read_region_at_mpp` level selection (pyramid fixtures at 0.25/0.5/1.0), LANCZOS vs BICUBIC branch, ICC application flag, `StainTransform` against analytic OD fixtures, and `TissueMask` fraction functions against brute force.
- **Integration:** Stage 2 on one TCGA 40× slide, one TCGA 20× slide and one converted BCNB slide, checking that the persisted artefacts validate against their schemas.
- **Regression images:** golden PNGs of a normalised patch per fixture slide, compared by SSIM ≥ 0.99. Any intentional change bumps `fitter_version`.

## 6. Migration

1. Alembic: add `cases.specimen_type`, create `stain_profiles`, and add `slides.mpp_source` and `slides.native_mpp`.
2. Ship `slide_io.py` and move every read site onto it (triage → mitosis → grading → routers). Each PR deletes that stage's normaliser construction.
3. Build `tcga_train_median@v1.json` after SPEC-02 splits exist. Until then the profile references the v5 reference patch fitted parameters, with `reference_id='v5_patch'`, and `config_hash` differs accordingly.
4. v5 cases lack `stain_profiles` rows, so preprocess must be re-run. They are marked `unknown` specimen, and clinical users are prompted.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Upsampled 20× slides degrade mitosis F1 | Report the 20×/40× slice (SPEC-00 R6). QC warns |
| Median reference poorly represents BCNB staining | The transform is only a display and VLM input choice. §3.5 ablations decide per model. A `bcnb`-specific reference can be added via profile override if the ablation favours it |
| Thread-local OpenSlide memory (one handle per thread) | Cap worker threads (existing pools use 4). Measure RSS in AC7 |
