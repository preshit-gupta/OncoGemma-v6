"""0007_stain_profiles and 0008_case_specimen_type (SPEC-04 §3.2, §3.4)."""
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.core.migrations import alembic_config, upgrade_to_head


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


def count(engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def seed_case_with_children(engine) -> str:
    case_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id, created_by, status, created_at) VALUES (:id, 'v5', 'open', CURRENT_TIMESTAMP)"), {"id": case_id})
        conn.execute(text("INSERT INTO stage_executions (id, case_id, stage, attempt, status) VALUES (:s, :c, 'triage', 1, 'done')"), {"s": str(uuid.uuid4()), "c": case_id})
        conn.execute(
            text("INSERT INTO slides (id, case_id, gcs_uri_original, created_at) VALUES (:s, :c, 'gs://b/s.svs', CURRENT_TIMESTAMP)"),
            {"s": str(uuid.uuid4()), "c": case_id},
        )
    return case_id


def test_existing_cases_become_unknown_and_keep_their_children(engine):
    """Adding the column must not rebuild `cases`: on SQLite that cascade-deletes every child row."""
    run(engine, command.upgrade, "0007_stain_profiles")
    case_id = seed_case_with_children(engine)
    run(engine, command.upgrade, "0008_case_specimen_type")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT specimen_type FROM cases")).scalar_one() == "unknown"
    assert (count(engine, "cases"), count(engine, "stage_executions"), count(engine, "slides")) == (1, 1, 1)
    assert case_id


def test_downgrade_removes_the_column_and_keeps_the_children(engine):
    run(engine, command.upgrade, "0008_case_specimen_type")
    seed_case_with_children(engine)
    run(engine, command.downgrade, "0007_stain_profiles")
    assert "specimen_type" not in {c["name"] for c in inspect(engine).get_columns("cases")}
    assert (count(engine, "cases"), count(engine, "stage_executions"), count(engine, "slides")) == (1, 1, 1)


def test_new_cases_default_to_unknown_in_the_database_too(engine):
    upgrade_to_head(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id, created_by, status, created_at) VALUES ('c1', 'u', 'open', CURRENT_TIMESTAMP)"))
        assert conn.execute(text("SELECT specimen_type FROM cases WHERE id = 'c1'")).scalar_one() == "unknown"


def test_stain_profiles_reference_slides_and_go_with_them(engine):
    upgrade_to_head(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("stain_profiles")}
    assert columns == {
        "id", "slide_id", "fitter_version", "reference_id", "w_src", "maxc_src", "w_tgt", "maxc_tgt",
        "fit_status", "n_patches", "mosaic_sha256", "created_at",
    }
    fks = inspect(engine).get_foreign_keys("stain_profiles")
    assert [(fk["referred_table"], fk["options"].get("ondelete")) for fk in fks] == [("slides", "CASCADE")]
    run(engine, command.downgrade, "0006_slide_mpp_source")
    assert "stain_profiles" not in inspect(engine).get_table_names()
