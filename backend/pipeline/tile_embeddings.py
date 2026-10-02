"""Embeddings of the Stage 3 tile grid, through the model gateway, with a per-slide cache (SPEC-05 §3).

The cache is one Parquet file per slide, embedder version and grid version::

    embeddings/<slide_sha256>/<embedder version>/<grid version>.parquet

with columns ``i:int32, j:int32, x_um:float32, y_um:float32, tissue_fraction:float32,
emb:fixed_size_list<float32, D>``. A tile's embedding depends only on the slide's pixels, the
grid and the embedder, so the key is the slide checksum (not a case or slide id), and a grid that
grows (a new tissue mask) embeds only the tiles the cache lacks. A cache that does not match what
the run expects raises EmbeddingCacheError; it is never ignored or silently rebuilt.
"""
import io
import json
import re
from dataclasses import dataclass

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from app.core.run_context import DecisionContext
from app.core.tasks import EntityType, Task
from app.inference.batching import plan_batches
from app.inference.gateway import EntityRef, ImageInput, InputSpec, ModelGateway, ModelInputs
from app.inference.outputs import EmbeddingBatch
from pipeline.slide_io import SlideReader, read_region_at_mpp
from pipeline.tile_grid import TileGrid

CACHE_FORMAT = "tile_embeddings_v1"
METADATA_KEY = b"oncogemma.tile_embeddings"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9@._-]")
COLUMNS = ("i", "j", "x_um", "y_um", "tissue_fraction", "emb")


class EmbeddingCacheError(RuntimeError):
    """A cached embedding file is unreadable or does not match the slide, grid or embedder."""


class MissingChecksumError(ValueError):
    """The slide has no SHA-256, so its embeddings have no cache key (ingest records it)."""


@dataclass(frozen=True)
class GridEmbeddings:
    embeddings: np.ndarray  # (n_tiles, D) float32, in the grid's tile order
    cache_path: str  # in the gateway's blob store (the artifacts bucket in the worker)
    tiles_cached: int  # served by this cache
    tiles_embedded: int  # sent through the gateway
    tiles_sent: int  # ... of which the gateway did not serve from its own output cache


def embedding_cache_path(slide_sha256: str | None, producer_version: str, grid_version: str) -> str:
    if not slide_sha256 or not SHA256_HEX.match(slide_sha256):
        raise MissingChecksumError(
            f"the slide's checksum {slide_sha256!r} is not a SHA-256, so its tile embeddings have no cache key (ingest records it)"
        )
    return f"embeddings/{slide_sha256}/{UNSAFE_PATH_CHARS.sub('_', producer_version)}/{grid_version}.parquet"


def _schema(dim: int, metadata: dict) -> pa.Schema:
    return pa.schema(
        [
            ("i", pa.int32()),
            ("j", pa.int32()),
            ("x_um", pa.float32()),
            ("y_um", pa.float32()),
            ("tissue_fraction", pa.float32()),
            ("emb", pa.list_(pa.float32(), dim)),
        ],
        metadata={METADATA_KEY: json.dumps(metadata, sort_keys=True).encode("utf-8")},
    )


def write_cache_bytes(i, j, tile_um: float, tissue_fraction, embeddings: np.ndarray, metadata: dict) -> bytes:
    """The Parquet file of the rows (i, j, fraction, embedding), positions derived from (i, j)."""
    embeddings = np.ascontiguousarray(embeddings, dtype=np.float32)
    n, dim = embeddings.shape
    i, j = np.asarray(i, dtype=np.int32), np.asarray(j, dtype=np.int32)
    table = pa.Table.from_arrays(
        [
            pa.array(i, type=pa.int32()),
            pa.array(j, type=pa.int32()),
            pa.array((i * tile_um).astype(np.float32), type=pa.float32()),
            pa.array((j * tile_um).astype(np.float32), type=pa.float32()),
            pa.array(np.asarray(tissue_fraction, dtype=np.float32), type=pa.float32()),
            pa.FixedSizeListArray.from_arrays(pa.array(embeddings.reshape(n * dim), type=pa.float32()), dim),
        ],
        schema=_schema(dim, metadata),
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    return buffer.getvalue()


@dataclass(frozen=True)
class CachedRows:
    i: np.ndarray
    j: np.ndarray
    tissue_fraction: np.ndarray
    embeddings: np.ndarray


def read_cache_bytes(data: bytes, metadata: dict, tile_um: float) -> CachedRows:
    """The rows of a cache file, after checking its schema, metadata, keys and values."""
    try:
        table = pq.read_table(io.BytesIO(data))
    except (pa.ArrowException, OSError) as exc:
        raise EmbeddingCacheError(f"the embedding cache is not a readable Parquet file: {exc}") from exc
    found = json.loads((table.schema.metadata or {}).get(METADATA_KEY, b"null"))
    if found != metadata:
        raise EmbeddingCacheError(f"the embedding cache was written for {found}, not {metadata}")
    emb_type = table.schema.field("emb").type if "emb" in table.schema.names else None
    if not isinstance(emb_type, pa.FixedSizeListType) or not table.schema.equals(_schema(emb_type.list_size, metadata), check_metadata=False):
        raise EmbeddingCacheError(f"the embedding cache has schema {table.schema}, not the {CACHE_FORMAT} columns {COLUMNS}")
    n, dim = table.num_rows, emb_type.list_size
    i = table.column("i").to_numpy()
    j = table.column("j").to_numpy()
    embeddings = table.column("emb").combine_chunks().flatten().to_numpy().reshape(n, dim)
    keys = i.astype(np.int64) * (int(j.max(initial=0)) + 1) + j
    if np.unique(keys).size != n:
        raise EmbeddingCacheError("the embedding cache has duplicate (i, j) tiles")
    if n and (i.min() < 0 or j.min() < 0):
        raise EmbeddingCacheError("the embedding cache has negative tile indices")
    x_um, y_um = table.column("x_um").to_numpy(), table.column("y_um").to_numpy()
    if not (np.array_equal(x_um, (i * tile_um).astype(np.float32)) and np.array_equal(y_um, (j * tile_um).astype(np.float32))):
        raise EmbeddingCacheError(f"the embedding cache's tile positions are not (i, j) x {tile_um:g} µm")
    if not np.isfinite(embeddings).all():
        raise EmbeddingCacheError("the embedding cache has NaN or infinite embeddings")
    return CachedRows(i, j, table.column("tissue_fraction").to_numpy(), embeddings)


def _png_input(rgb: np.ndarray, target_mpp: float, color: str) -> ImageInput:
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, "PNG")
    height_px, width_px = rgb.shape[:2]
    return ImageInput(buffer.getvalue(), InputSpec(mpp=target_mpp, size_px=(width_px, height_px), color=color, format="png"))


def embed_tile_grid(
    reader: SlideReader,
    grid: TileGrid,
    *,
    slide_sha256: str | None,
    producer_id: str,
    gateway: ModelGateway,
    ctx: DecisionContext,
) -> GridEmbeddings:
    """Embeddings of every tile of ``grid``: from the cache, and through the gateway for the rest.

    Tiles are read at the embedder's input contract and sent in batches within its request
    limits, one DecisionRecord per batch. At most ``max_batch`` tiles are held in memory at a
    time. The merged cache is written only after every batch succeeded.
    """
    registry = gateway.registry
    entry = registry.models[producer_id]
    contract = entry.input
    size_w, size_h = contract.size_px
    if size_w != size_h:
        raise ValueError(f"{producer_id} takes {contract.size_px} px images; the tile grid needs square tiles")
    if contract.color != "raw":
        raise ValueError(
            f"{producer_id} takes {contract.color} colour; the tile embedding cache is keyed for raw pixels only "
            "(normalised tiles would need the stain profile in the key)"
        )
    if contract.format != "png":
        raise ValueError(f"{producer_id} takes {contract.format}; tiles are sent as lossless PNG")
    target_mpp = grid.tile_um / size_w
    producer_version = registry.version_of(producer_id)
    metadata = {
        "format": CACHE_FORMAT,
        "slide_sha256": slide_sha256,
        "producer": producer_id,
        "producer_version": producer_version,
        "grid_version": grid.version,
        "tile_um": grid.tile_um,
        "mpp": target_mpp,
        "size_px": size_w,
        "color": contract.color,
    }
    path = embedding_cache_path(slide_sha256, producer_version, grid.version)
    blobs = gateway.blobs

    rows: dict[tuple[int, int], tuple[float, np.ndarray]] = {}
    cached_bytes = blobs.read(path)
    if cached_bytes is not None:
        cached = read_cache_bytes(cached_bytes, metadata, grid.tile_um)
        for k in range(cached.i.size):
            rows[(int(cached.i[k]), int(cached.j[k]))] = (float(cached.tissue_fraction[k]), cached.embeddings[k])

    keys = list(zip(grid.i.tolist(), grid.j.tolist()))
    tile_ids = grid.tile_ids()
    missing = [k for k, key in enumerate(keys) if key not in rows]
    limits = entry.limits
    tiles_sent, n_batch = 0, 0
    for start in range(0, len(missing), limits.max_batch):
        chunk = missing[start : start + limits.max_batch]
        images = [
            _png_input(
                read_region_at_mpp(reader, float(grid.x_um[k]), float(grid.y_um[k]), grid.tile_um, grid.tile_um, target_mpp, color="raw").rgb,
                target_mpp,
                contract.color,
            )
            for k in chunk
        ]
        for batch in plan_batches([len(image.data) for image in images], limits.max_batch, limits.max_request_bytes):
            result = gateway.invoke(
                Task.PF_EMBED,
                producer_id,
                ModelInputs(images=tuple(images[b] for b in batch)),
                ctx,
                EntityRef(EntityType.TILE_BATCH, f"tb_{n_batch:05d}", ids=tuple(tile_ids[chunk[b]] for b in batch)),
                EmbeddingBatch,
            )
            n_batch += 1
            vectors = result.output.as_array()
            if vectors.shape[0] != len(batch):
                raise ValueError(f"{producer_id} returned {vectors.shape[0]} embeddings for {len(batch)} tiles")
            for b, vector in zip(batch, vectors):
                k = chunk[b]
                rows[keys[k]] = (float(grid.tissue_fraction[k]), vector)
            if not result.cache_hit:
                tiles_sent += len(batch)

    dims = {vector.shape[0] for _, vector in rows.values()}
    if len(dims) != 1:
        raise EmbeddingCacheError(f"{producer_id} embeddings have different widths {sorted(dims)} (cache and new tiles disagree)")
    if missing:
        ordered = sorted(rows, key=lambda key: (key[1], key[0]))  # row-major, as the grid
        blobs.write(
            path,
            write_cache_bytes(
                [key[0] for key in ordered],
                [key[1] for key in ordered],
                grid.tile_um,
                [rows[key][0] for key in ordered],
                np.stack([rows[key][1] for key in ordered]),
                metadata,
            ),
            "application/octet-stream",
        )
    embeddings = np.stack([rows[key][1] for key in keys]).astype(np.float32)
    return GridEmbeddings(
        embeddings=embeddings,
        cache_path=path,
        tiles_cached=len(keys) - len(missing),
        tiles_embedded=len(missing),
        tiles_sent=tiles_sent,
    )
