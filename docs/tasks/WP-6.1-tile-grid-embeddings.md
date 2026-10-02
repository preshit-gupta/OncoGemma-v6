# WP-6.1 — Tile grid and embedding cache via the gateway

| Owner | Size | Spec | Depends on | Lane |
|---|---|---|---|---|
| Claude | M | SPEC-05 §3 (and §1.2 for the defects it removes) | WP-3.1–3.4 (`read_region_at_mpp`, `TissueMask`, specimen profiles), WP-2.3 (gateway) | C |

## Goal

Stage 3 embeds **every** tissue tile of a global 224 µm grid, with no sample cap, so the heatmap has a defined value over all tissue. Embeddings are cached per slide checksum, Path Foundation version and grid version, so a re-run or a later stage (WP-6.2 training, eval replays) never pays for the same tile twice.

This WP replaces the v5 "Smart Scout" sampler (2,048-patch budget, 80% by thumbnail darkness, 20% strided). The tumour head, the OD/margin fusion, the 80-column overview grid and the IDW fill are **not** changed here: WP-6.2 replaces them.

## Read first (only these)

- `docs/specs/05-stage3-triage-hotspots.md` §1.2, §3
- `backend/worker/triage.py`
- `backend/pipeline/tissue_mask.py` (`TissueMask.fractions_of_boxes_um`)
- `backend/app/inference/gateway.py` (`invoke`, `EntityRef`), `backend/app/inference/blobs.py`, `backend/app/inference/batching.py`
- `configs/models.yaml` (`path_foundation`), `configs/triage.yaml`, `configs/specimen_profiles.yaml`

## Files you may touch

- Create `backend/pipeline/tile_grid.py`, `backend/pipeline/tile_embeddings.py`.
- `backend/worker/triage.py`.
- `backend/app/core/pipeline_config.py` (triage and specimen-profile models only).
- `configs/triage.yaml`, `configs/specimen_profiles.yaml`, `configs/models.yaml` (`path_foundation.input.color`).
- Tests: create `backend/tests/pipeline/test_tile_grid.py`, `backend/tests/pipeline/test_tile_embeddings.py`. Edit `backend/tests/test_triage_worker.py`, the triage tests in `backend/tests/test_read_sites_workers.py`, and the triage seed helpers of other tests only to give seeded slides a checksum and to clear the embedding cache where a test is about the gateway cache. Delete `backend/tests/test_batch10_triage_hybrid.py`, which re-implements the deleted sampler inline and imports no production code.
- `docs/tasks/WP-6.1-tile-grid-embeddings.md` (this card), `docs/STATUS.md`.

## Tasks

1. **Grid** (`pipeline/tile_grid.py`).
   - The grid is global and anchored at the slide origin. Tile `(i, j)` covers `[i·t, (i+1)·t) × [j·t, (j+1)·t)` µm, with `t = patch_size_px × mpp_target` (224 µm). `i` is the column, `j` the row.
   - It spans `ceil(W/t) × ceil(H/t)` tiles, so a partial tile at the right or bottom edge is a candidate. Area off the slide counts as non-tissue, and pixels off the slide are read as white (`read_region_at_mpp` pads).
   - A tile is included iff `TissueMask.fractions_of_boxes_um(tile) ≥ profile.triage.min_tissue_fraction`. **No cap.** Zero included tiles raises.
   - The grid version is `grid<t>_v1` (`grid224_v1`).
2. **Embedding cache** (`pipeline/tile_embeddings.py`).
   - **Path Foundation sees raw scanner colour** (owner decision, 2026-10-02): it was trained on unnormalised H&E, so `configs/models.yaml` keeps `path_foundation.input.color: raw`.
     - Normalised tiles are supported but off. Setting `color: normalized` sends each tile through the slide's persisted `StainTransform` and gives it its own cache. Switch it on only if WP-6.2 shows a paired S3-F1 gain on val.
   - The cache lives in the gateway's blob store (the artifacts bucket in the worker), at `embeddings/<slide_sha256>/<pf_version>/<grid_version>.parquet`, the SPEC-05 path.
     - A normalised embedder writes to `embeddings/<slide_sha256>/<pf_version>/stain_<sha256>/<grid_version>.parquet` instead. `stain_<sha256>` hashes the transform's exact parameters (source and target stain vectors and maxima, `od_beta`), so a refitted profile or a new reference stain gets its own cache.
     - `<pf_version>` is the registry version with every character outside `[A-Za-z0-9@._-]` replaced by `_`.
   - Columns, exactly: `i:int32, j:int32, x_um:float32, y_um:float32, tissue_fraction:float32, emb:fixed_size_list<float32, D>`. `D` is the embedder's output width (384).
   - The Parquet key-value metadata records `slide_sha256`, `producer`, `producer_version`, `grid_version`, `tile_um`, `mpp`, `size_px`, `color` and `stain_sha256`.
   - A read **validates** the file: schema, metadata equal to what the run expects, unique `(i, j)`, finite values, and positions consistent with `(i, j)`. Any mismatch raises `EmbeddingCacheError`. A bad cache is never silently re-embedded or ignored.
   - A slide without `checksum_sha256` has no cache key, so the stage raises (ingest records the checksum).
3. **Embedding through the gateway.** `embed_tile_grid(...)`:
   - Loads the cache and embeds only the grid tiles it lacks.
   - Reads each tile with `read_region_at_mpp` at the embedder's contract `mpp`, size and `color`, passing the stain transform when the colour is `normalized`. A normalised contract without a transform raises, and so does a raw contract given one. Each record's `input_spec` carries the `stain_profile_id`.
   - Batches per `registry.limits` (`max_batch`, `max_request_bytes`), reading at most `max_batch` tiles at a time so memory does not grow with the tile count. Each request is one `gateway.invoke(Task.PF_EMBED, …, EntityRef(TILE_BATCH, ids=tile ids))`, i.e. one DecisionRecord per batch.
   - Writes the merged cache (the old rows plus the new ones), then returns the embeddings in grid order, with counts of cached, embedded and sent tiles.
   - A gateway error propagates, and no cache is written for that call.
4. **Worker** (`worker/triage.py`).
   - Load the stain transform before embedding. With a normalised embedder, a degenerate fit raises `DegenerateStainProfileError`. With the raw default, such a slide is triaged as before.
   - Delete the Smart Scout sampler and its use of the thumbnail stain map for sampling.
   - Embed the full grid through `embed_tile_grid`.
   - Each tile probability enters its overview cell as the **mean** of the tiles whose centre falls in that cell (v5's "last write wins" goes, SPEC-05 §1.2). The fusion and IDW stay until WP-6.2.
   - `output.json` gains `tile_grid` (`version`, `tile_um`, `n_cols`, `n_rows`, `n_tiles`) and `embedding_cache` (`uri`, `tiles_cached`, `tiles_embedded`).
5. **Config.**
   - `configs/triage.yaml` loses `max_sample_patches`.
   - `specimen_profiles.yaml` gains `triage.min_tissue_fraction` (0.25) per profile.
   - `PipelineConfig` checks at load that `mpp_target` and `patch_size_px` match the embedder's input contract (`mpp` within `mpp_tolerance`, square `size_px`).
   - `tissue_threshold_pct` now only gates overview cells, until WP-6.2 deletes the overview grid.

## Interfaces

```python
# backend/pipeline/tile_grid.py
class EmptyTileGridError(ValueError): ...
def grid_version(tile_um: float) -> str                      # "grid224_v1"
@dataclass(frozen=True)
class TileGrid:
    tile_um: float
    n_cols: int
    n_rows: int
    i: np.ndarray               # int32, column of each included tile, row-major order
    j: np.ndarray               # int32, row
    tissue_fraction: np.ndarray # float32
    # properties: version, n_tiles, x_um, y_um; tile_ids() -> ["t_<i>_<j>", ...]
def tissue_tile_grid(mask: TissueMask, extent_um: tuple[float, float], tile_um: float, min_fraction: float) -> TileGrid

# backend/pipeline/tile_embeddings.py
class EmbeddingCacheError(RuntimeError): ...
class MissingChecksumError(ValueError): ...
def embedding_cache_path(slide_sha256: str | None, producer_version: str, grid_version: str, stain_sha256: str | None = None) -> str
def write_cache_bytes(i, j, tile_um, tissue_fraction, embeddings, metadata) -> bytes
def read_cache_bytes(data: bytes, metadata: dict, tile_um: float) -> CachedRows   # validates; raises EmbeddingCacheError
@dataclass(frozen=True)
class GridEmbeddings:
    embeddings: np.ndarray      # (n_tiles, D) float32, in grid order
    cache_path: str             # in the gateway's blob store
    tiles_cached: int
    tiles_embedded: int
    tiles_sent: int             # embedded and not served by the gateway's own cache
def stain_fingerprint(stain: StainTransform) -> str          # SHA-256 of the transform's parameters
def embed_tile_grid(reader, grid, *, slide_sha256, producer_id, gateway, ctx, stain=None) -> GridEmbeddings
```

## Acceptance (run these)

```powershell
python -m pytest backend/tests/pipeline/test_tile_grid.py backend/tests/pipeline/test_tile_embeddings.py -q -p no:cacheprovider
python -m pytest backend/tests -q -p no:cacheprovider
python -m pytest tools/tests -q -p no:cacheprovider
python tools/lint_literals.py   # findings must be identical to main's (main already has 15)
```

The new tests must show:
- The grid equals a brute-force `fraction_in_box_um` per tile, partial edge tiles included, on a thin diagonal band (the CNB case in SPEC-05 §1.2). The band keeps every tile that has enough tissue, so S3-COV is 1.0 by construction.
- There is no cap: a fully tissue mask gives `ceil(W/t)·ceil(H/t)` tiles, more than the old budget of 2,048.
- The registry embedder reads raw tiles under the SPEC-05 path. With the normalised option, the embedder receives exactly `StainTransform.apply` of the raw tile, another stain mapping gets its own cache, and a degenerate fit fails the stage.
- A cache round trip is bit-exact, with the schema and metadata exactly as specified.
- A second run makes zero embedder calls. A grown grid embeds only the new tiles.
- A tampered or mismatched cache raises.
- A missing checksum raises.
- A gateway failure writes no cache.
- One DecisionRecord per batch, each within `max_batch`.

## Out of scope — do not do

- The tumour head, calibration, `tiles.parquet`, `heatmap.json` and the deletion of the fusion, IDW and 80-column grid (WP-6.2).
- Hotspot windows and selection (WP-6.3).
- The node-hour cost model in `configs/pricing.yaml` (SPEC-05 §3 "Cost"). It needs the endpoint's machine type and actual billing, which the owner verifies. The per-1k-patch estimate stays, computed on the tiles actually sent.
- Parallel tile reads or concurrent embedding requests (`qps` is not enforced yet, SPEC-02 §6.2).

## Done checklist

- [ ] Grid, cache and worker as above. Smart Scout deleted.
- [ ] New tests pass. Full backend suite and `tools/tests` pass.
- [ ] `lint_literals.py` finds nothing new (output identical to `main`); the baseline does not grow.
- [ ] `docs/STATUS.md` updated.
- [ ] PR lists assumptions.
