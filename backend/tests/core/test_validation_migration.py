"""0010_validation: runs, items and run_id on executions and decision records (SPEC-02 §5.3)."""
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
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


def count(engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def seed_execution_with_record(engine) -> None:
    case_id, execution_id = str(uuid.uuid4()), str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id, created_by, status, created_at) VALUES (:id, 'v6', 'open', CURRENT_TIMESTAMP)"), {"id": case_id})
        conn.execute(
            text("INSERT INTO stage_executions (id, case_id, stage, attempt, status) VALUES (:s, :c, 'triage', 1, 'done')"),
            {"s": execution_id, "c": case_id},
        )
        conn.execute(
            text(
                "INSERT INTO decision_records (id, case_id, stage_execution_id, stage, task, entity_type, entity_id, "
                "producer_kind, producer_id, producer_version, input_sha256, input_spec, params, status, latency_ms, "
                "cache_hit, run_mode, config_hash, created_at) VALUES (:id, :c, :s, 'triage', 'tumor_head', 'tile', 't1', "
                "'model', 'head', '1', :h, '{}', '{}', 'ok', 1, 0, 'clinical', :h, CURRENT_TIMESTAMP)"
            ),
            {"id": str(uuid.uuid4()), "c": case_id, "s": execution_id, "h": "0" * 64},
        )


def test_upgrade_keeps_existing_executions_and_records(engine):
    """Adding run_id must not rebuild stage_executions: on SQLite that cascade-deletes its decision records."""
    run(engine, command.upgrade, "0009_auth")
    seed_execution_with_record(engine)
    run(engine, command.upgrade, "0010_validation")
    assert (count(engine, "stage_executions"), count(engine, "decision_records")) == (1, 1)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT run_id FROM stage_executions")).scalar_one() is None
    tables = set(inspect(engine).get_table_names())
    assert {"validation_runs", "validation_items"} <= tables


def test_run_ids_must_name_a_run(engine):
    run(engine, command.upgrade, "0010_validation")
    seed_execution_with_record(engine)
    for table in ("stage_executions", "decision_records"):
        fks = inspect(engine).get_foreign_keys(table)
        assert any(fk["referred_table"] == "validation_runs" and fk["constrained_columns"] == ["run_id"] for fk in fks)
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(f"UPDATE {table} SET run_id = :r"), {"r": str(uuid.uuid4())})


def test_downgrade_drops_the_tables_and_columns_and_keeps_the_rows(engine):
    run(engine, command.upgrade, "0010_validation")
    seed_execution_with_record(engine)
    run(engine, command.downgrade, "0009_auth")
    assert "validation_runs" not in inspect(engine).get_table_names()
    assert "run_id" not in {c["name"] for c in inspect(engine).get_columns("stage_executions")}
    assert not inspect(engine).get_foreign_keys("decision_records") or all(
        fk["referred_table"] != "validation_runs" for fk in inspect(engine).get_foreign_keys("decision_records")
    )
    assert (count(engine, "stage_executions"), count(engine, "decision_records")) == (1, 1)


def test_0011_adds_and_removes_the_controller_lease(engine):
    run(engine, command.upgrade, "0011_run_controller_lease")
    columns = {c["name"] for c in inspect(engine).get_columns("validation_runs")}
    assert {"controller_lease_owner", "controller_lease_until"} <= columns
    run(engine, command.downgrade, "0010_validation")
    columns = {c["name"] for c in inspect(engine).get_columns("validation_runs")}
    assert not {"controller_lease_owner", "controller_lease_until"} & columns
