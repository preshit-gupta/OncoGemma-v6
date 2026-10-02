"""Tile-grid embeddings through the gateway and their per-slide cache (SPEC-05 §3; WP-6.1)."""
import io
import math

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core.pipeline_config import get_pipeline_config
from app.inference.adapters.base import RawResponse, TransientCallError
from app.inference.errors import ModelUnavailableError
from app.inference.records import DecisionLog
from pipeline.slide_io import SlideReader
from pipeline.tile_embeddings import (
    METADATA_KEY,
    EmbeddingCacheError,
    MissingChecksumError,
    embed_tile_grid,
    embedding_cache_path,
    read_cache_bytes,
    write_cache_bytes,
)
from pipeline.tile_grid import tissue_tile_grid
from pipeline.tissue_mask import TissueMask
from tests.fakes.gateway import FakeAdapter, InMemoryBlobStore, decision_context, make_gateway
from tests.fakes.slide import FakeOpenSlide
from tests.test_triage_worker import EMBEDDING_DIM, embed

WIDTH_PX, HEIGHT_PX, MPP = 2400, 1800, 0.5  # 1200 x 900 µm: a 6 x 5 grid of 224 µm tiles, the edges partial
EXTENT_UM = (WIDTH_PX * MPP, HEIGHT_PX * MPP)
MASK_MPP = 8.0
TILE_UM = 224.0
SHA = "ab" * 32
PRODUCER = "path_foundation"


@pytest.fixture
def reader(monkeypatch):
    import openslide

    monkeypatch.setattr(openslide, "OpenSlide", lambda path: FakeOpenSlide(WIDTH_PX, HEIGHT_PX))
    with SlideReader("fake.svs", MPP, MPP, "svs") as opened:
        yield opened


def mask(left_fraction: float = 1.0) -> TissueMask:
    cells = np.zeros((math.ceil(EXTENT_UM[1] / MASK_MPP), math.ceil(EXTENT_UM[0] / MASK_MPP)), dtype=bool)
    cells[:, : round(cells.shape[1] * left_fraction)] = True
    return TissueMask(cells, MASK_MPP)


def grid_of(tissue: TissueMask, min_fraction: float = 0.25):
    return tissue_tile_grid(tissue, EXTENT_UM, TILE_UM, min_fraction)


class Setup:
    def __init__(self, blobs=None, adapter=None, config=None):
        self.config = config or get_pipeline_config()
        self.adapter = adapter or FakeAdapter(then=embed)
        self.blobs = blobs if blobs is not None else InMemoryBlobStore()
        self.log = DecisionLog()
        self.gateway = make_gateway(self.config, {"vertex_endpoint_raw_predict": self.adapter}, blobs=self.blobs, log=self.log)
        self.ctx = decision_context(stage="triage")

    def run(self, reader, grid, sha=SHA):
        return embed_tile_grid(reader, grid, slide_sha256=sha, producer_id=PRODUCER, gateway=self.gateway, ctx=self.ctx)

    def images_sent(self) -> int:
        return sum(len(request.images) for _, request, _ in self.adapter.calls)

    def cache_paths(self) -> list[str]:
        return [path for path in self.blobs.blobs if path.startswith("embeddings/")]


def test_every_tile_is_embedded_once_per_batch_record_within_the_registry_limits(reader):
    setup = Setup()
    grid = grid_of(mask())
    result = setup.run(reader, grid)

    limits = setup.config.models.models[PRODUCER].limits
    # 6 x 5 tiles: the partial right column (176 of 224 µm on the slide) is in, and the
    # bottom row (a 4 µm sliver of the slide) is not.
    assert grid.n_tiles == 24 and int(grid.i.max()) == 5 and int(grid.j.max()) == 3
    assert result.embeddings.shape == (grid.n_tiles, EMBEDDING_DIM) and result.embeddings.dtype == np.float32
    assert (result.tiles_cached, result.tiles_embedded, result.tiles_sent) == (0, grid.n_tiles, grid.n_tiles)
    sent = [len(request.images) for _, request, _ in setup.adapter.calls]
    assert sum(sent) == grid.n_tiles and max(sent) <= limits.max_batch

    records = setup.log.pending()
    assert len(records) == len(sent)  # one DecisionRecord per batch
    assert all(r["task"] == "pf_embed" and r["entity_type"] == "tile_batch" and r["entity_ids_uri"] for r in records)
    specs = [spec for r in records for spec in r["input_spec"]["images"]]
    assert all(s["mpp"] == 1.0 and s["size_px"] == [224, 224] and s["color"] == "raw" for s in specs)

    # Row k is the embedding of tile k: the fake embedder's answer for that tile's pixels.
    first = embed(setup.adapter.calls[0][1]).data["embeddings"][0]
    np.testing.assert_array_equal(result.embeddings[0], np.asarray(first, dtype=np.float32))


def test_a_second_run_is_served_from_the_cache_without_any_model_call(reader):
    first = Setup()
    grid = grid_of(mask())
    before = first.run(reader, grid)

    again = Setup(blobs=first.blobs)
    after = again.run(reader, grid)

    assert again.adapter.calls == [] and again.log.pending() == []
    assert (after.tiles_cached, after.tiles_embedded, after.tiles_sent) == (grid.n_tiles, 0, 0)
    np.testing.assert_array_equal(after.embeddings, before.embeddings)
    assert after.cache_path == before.cache_path


def test_the_cache_file_has_the_spec_columns_key_and_metadata(reader):
    setup = Setup()
    grid = grid_of(mask())
    result = setup.run(reader, grid)

    version = setup.config.models.version_of(PRODUCER)
    assert version == "models/5848531596314935296@1@2026-09-22"
    assert result.cache_path == f"embeddings/{SHA}/models_5848531596314935296@1@2026-09-22/grid224_v1.parquet"
    table = pq.read_table(io.BytesIO(setup.blobs.blobs[result.cache_path]))
    expected = pa.schema([
        ("i", pa.int32()),
        ("j", pa.int32()),
        ("x_um", pa.float32()),
        ("y_um", pa.float32()),
        ("tissue_fraction", pa.float32()),
        ("emb", pa.list_(pa.float32(), EMBEDDING_DIM)),
    ])
    assert table.schema.equals(expected, check_metadata=False)
    assert table.column("i").to_pylist() == grid.i.tolist() and table.column("j").to_pylist() == grid.j.tolist()
    np.testing.assert_array_equal(table.column("x_um").to_numpy(), (grid.i * TILE_UM).astype(np.float32))
    np.testing.assert_array_equal(table.column("tissue_fraction").to_numpy(), grid.tissue_fraction)
    assert METADATA_KEY in table.schema.metadata


def test_a_grown_grid_embeds_only_the_tiles_the_cache_lacks(reader):
    first = Setup()
    small = grid_of(mask(left_fraction=0.5))
    before = first.run(reader, small)

    again = Setup(blobs=first.blobs)
    large = grid_of(mask())
    after = again.run(reader, large)

    assert small.n_tiles < large.n_tiles
    assert again.images_sent() == after.tiles_embedded == large.n_tiles - small.n_tiles
    assert after.tiles_cached == small.n_tiles
    rows = {(int(i), int(j)): k for k, (i, j) in enumerate(zip(large.i, large.j))}
    for k, (i, j) in enumerate(zip(small.i, small.j)):
        np.testing.assert_array_equal(after.embeddings[rows[(int(i), int(j))]], before.embeddings[k])

    # The merged cache now serves the large grid in full.
    third = Setup(blobs=first.blobs)
    assert third.run(reader, large).tiles_embedded == 0 and third.adapter.calls == []


def test_a_cache_round_trip_is_bit_exact():
    rng = np.random.default_rng(7)
    i, j = np.array([0, 3, 1], dtype=np.int32), np.array([0, 0, 2], dtype=np.int32)
    fractions = rng.random(3).astype(np.float32)
    embeddings = rng.standard_normal((3, EMBEDDING_DIM)).astype(np.float32)
    meta = {"slide_sha256": SHA, "producer_version": "v"}
    rows = read_cache_bytes(write_cache_bytes(i, j, TILE_UM, fractions, embeddings, meta), meta, TILE_UM)
    np.testing.assert_array_equal(rows.i, i)
    np.testing.assert_array_equal(rows.j, j)
    np.testing.assert_array_equal(rows.tissue_fraction, fractions)
    np.testing.assert_array_equal(rows.embeddings, embeddings)


def tamper(setup: Setup, path: str, **changes):
    table = pq.read_table(io.BytesIO(setup.blobs.blobs[path]))
    columns = {name: table.column(name) for name in table.schema.names}
    columns.update(changes)
    buffer = io.BytesIO()
    pq.write_table(pa.table(columns, schema=table.schema), buffer)
    setup.blobs.blobs[path] = buffer.getvalue()


@pytest.mark.parametrize(
    "corruption",
    ["not parquet", "other producer version", "duplicate tile", "moved tile", "nan embedding", "wrong columns"],
)
def test_a_cache_that_does_not_match_raises_instead_of_being_ignored(reader, corruption):
    setup = Setup()
    grid = grid_of(mask())
    path = setup.run(reader, grid).cache_path
    table = pq.read_table(io.BytesIO(setup.blobs.blobs[path]))
    if corruption == "not parquet":
        setup.blobs.blobs[path] = b"garbage"
    elif corruption == "other producer version":
        meta = dict(table.schema.metadata)
        meta[METADATA_KEY] = meta[METADATA_KEY].replace(b"2026-09-22", b"2026-01-01")
        buffer = io.BytesIO()
        pq.write_table(table.replace_schema_metadata(meta), buffer)
        setup.blobs.blobs[path] = buffer.getvalue()
    elif corruption == "duplicate tile":
        tamper(setup, path, i=pa.array([0] * table.num_rows, type=pa.int32()), j=pa.array([0] * table.num_rows, type=pa.int32()))
    elif corruption == "moved tile":
        tamper(setup, path, x_um=pa.array(table.column("x_um").to_numpy() + 1, type=pa.float32()))
    elif corruption == "nan embedding":
        flat = table.column("emb").combine_chunks().flatten().to_numpy().copy()
        flat[0] = np.nan
        tamper(setup, path, emb=pa.FixedSizeListArray.from_arrays(pa.array(flat, type=pa.float32()), EMBEDDING_DIM))
    else:
        buffer = io.BytesIO()
        pq.write_table(table.drop_columns(["tissue_fraction"]), buffer)
        setup.blobs.blobs[path] = buffer.getvalue()

    again = Setup(blobs=setup.blobs)
    with pytest.raises(EmbeddingCacheError):
        again.run(reader, grid)
    assert again.adapter.calls == []


@pytest.mark.parametrize("sha", [None, "", "abc", "AB" * 32])
def test_a_slide_without_a_checksum_has_no_cache_key(reader, sha):
    setup = Setup()
    with pytest.raises(MissingChecksumError, match="ingest records it"):
        setup.run(reader, grid_of(mask()), sha=sha)
    assert setup.adapter.calls == []
    with pytest.raises(MissingChecksumError):
        embedding_cache_path(sha, "v", "grid224_v1")


def test_a_failed_batch_fails_the_call_and_writes_no_cache(reader):
    setup = Setup(adapter=FakeAdapter(embed, then=TransientCallError("503")))
    with pytest.raises(ModelUnavailableError):
        setup.run(reader, grid_of(mask()))
    assert setup.cache_paths() == []


def test_a_short_answer_fails_the_call_and_writes_no_cache(reader):
    def one_row(request):
        return RawResponse(data={"embeddings": [[0.5] * EMBEDDING_DIM]})

    setup = Setup(adapter=FakeAdapter(then=one_row))
    with pytest.raises(ValueError, match="embeddings for"):
        setup.run(reader, grid_of(mask()))
    assert setup.cache_paths() == []


def test_a_normalised_colour_contract_is_refused(reader):
    config = get_pipeline_config()
    entry = config.models.models[PRODUCER]
    normalised = entry.model_copy(update={"input": entry.input.model_copy(update={"color": "normalized"})})
    models = config.models.model_copy(update={"models": {**config.models.models, PRODUCER: normalised}})
    setup = Setup(config=config.model_copy(update={"models": models}))
    with pytest.raises(ValueError, match="raw pixels only"):
        setup.run(reader, grid_of(mask()))
    assert setup.adapter.calls == []
