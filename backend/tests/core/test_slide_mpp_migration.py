"""0006_slide_mpp_source: provenance and native resolution of a slide's MPP (SPEC-04 §3.1, §6)."""
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.core.migrations import alembic_config


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'oncogemma.db'}")
    yield engine
    engine.dispose()


def run(engine, fn, *args):
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        fn(cfg, *args)


def insert_slide(conn, mpp_x, mpp_y) -> str:
    case_id, slide_id = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO cases (id, created_by, status, created_at) VALUES (:id, 'u', 'open', CURRENT_TIMESTAMP)"),
        {"id": case_id},
    )
    conn.execute(
        text(
            "INSERT INTO slides (id, case_id, gcs_uri_original, mpp_x, mpp_y, created_at) "
            "VALUES (:id, :case, 'gs://b/s.svs', :x, :y, CURRENT_TIMESTAMP)"
        ),
        {"id": slide_id, "case": case_id, "x": mpp_x, "y": mpp_y},
    )
    return slide_id


def test_existing_slides_get_their_native_resolution_and_no_recorded_source(engine):
    run(engine, command.upgrade, "0005_grading_histotype_nullable")
    with engine.begin() as conn:
        aniso = insert_slide(conn, 0.25, 0.30)
        needs_mpp = insert_slide(conn, None, None)
    run(engine, command.upgrade, "0006_slide_mpp_source")
    with engine.connect() as conn:
        rows = {
            row.id.replace("-", ""): (row.mpp_source, row.native_mpp)
            for row in conn.execute(text("SELECT id, mpp_source, native_mpp FROM slides"))
        }
    assert rows[aniso.replace("-", "")] == (None, 0.30)  # the coarser axis
    assert rows[needs_mpp.replace("-", "")] == (None, None)


def test_the_source_is_limited_to_the_spec_02_vocabulary(engine):
    run(engine, command.upgrade, "0006_slide_mpp_source")
    with engine.begin() as conn:
        slide_id = insert_slide(conn, 0.25, 0.25)
        for source in ("file", "dataset_doc", "manual", None):
            conn.execute(text("UPDATE slides SET mpp_source = :s WHERE id = :id"), {"s": source, "id": slide_id})
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("UPDATE slides SET mpp_source = 'guess' WHERE id = :id"), {"id": slide_id})


def test_downgrade_removes_the_columns(engine):
    run(engine, command.upgrade, "0006_slide_mpp_source")
    run(engine, command.downgrade, "0005_grading_histotype_nullable")
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(slides)"))}
    assert not {"mpp_source", "native_mpp"} & columns
