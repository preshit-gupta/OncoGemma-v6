"""Alembic baseline and startup migration (SPEC-01 §3.1, AC8).

CI also runs `alembic upgrade head` and `alembic check` on an empty Postgres 15
(.github/workflows/migrations.yml); these tests cover the same path on SQLite.
"""
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

import app.main as main
from app.core.db import Base
from app.core.migrations import (
    BASELINE_REVISION,
    UnversionedDatabaseError,
    alembic_config,
    upgrade_to_head,
)
from app.core.pipeline_config import ConfigLoadError


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'oncogemma.db'}")
    yield engine
    engine.dispose()


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def run(engine, fn, *args):
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        fn(cfg, *args)


def test_single_linear_history_starting_at_the_v5_baseline():
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert [rev.revision for rev in script.walk_revisions()][-1] == BASELINE_REVISION


def test_upgrade_creates_every_model_table(engine):
    upgrade_to_head(engine)
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    assert current_revision(engine) == head_revision()


def test_migrations_match_the_models(engine):
    """`alembic check`: no drift between the models and the migration history."""
    upgrade_to_head(engine)
    run(engine, command.check)


def test_upgrade_is_idempotent(engine):
    upgrade_to_head(engine)
    upgrade_to_head(engine)
    assert current_revision(engine) == head_revision()


def test_downgrade_to_base_and_back(engine):
    upgrade_to_head(engine)
    run(engine, command.downgrade, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    upgrade_to_head(engine)
    assert current_revision(engine) == head_revision()


def test_unversioned_v5_database_is_refused(engine):
    Base.metadata.create_all(engine)
    with pytest.raises(UnversionedDatabaseError, match=BASELINE_REVISION):
        upgrade_to_head(engine)
    assert "alembic_version" not in inspect(engine).get_table_names()


def test_database_with_only_retired_tables_is_refused(engine):
    """Detection must not depend on the current models: `reports` is not one of them any more."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE reports (case_id VARCHAR PRIMARY KEY)"))
    with pytest.raises(UnversionedDatabaseError, match="reports"):
        upgrade_to_head(engine)


def build_v5_database(engine) -> None:
    """The schema v5's create_all built (revision 0001), without migration history."""
    run(engine, command.upgrade, BASELINE_REVISION)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))


def test_v5_database_stamped_at_baseline_upgrades_without_drift(engine):
    """The operator procedure in UnversionedDatabaseError: stamp, upgrade head, check."""
    build_v5_database(engine)
    with pytest.raises(UnversionedDatabaseError):
        upgrade_to_head(engine)
    run(engine, command.stamp, BASELINE_REVISION)
    upgrade_to_head(engine)
    run(engine, command.check)
    assert current_revision(engine) == head_revision()


def test_0002_drops_the_v5_reports_table(engine):
    run(engine, command.upgrade, BASELINE_REVISION)
    assert "reports" in inspect(engine).get_table_names()
    run(engine, command.upgrade, "0002_drop_v5_reports")
    assert "reports" not in inspect(engine).get_table_names()
    run(engine, command.downgrade, BASELINE_REVISION)
    assert "reports" in inspect(engine).get_table_names()


def test_0003_0004_add_decision_records_and_mark_existing_executions_clinical(engine):
    run(engine, command.upgrade, "0002_drop_v5_reports")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id, created_by, status, created_at) VALUES ('c1', 'v5', 'open', '2026-09-01')"))
        conn.execute(text(
            "INSERT INTO stage_executions (id, case_id, stage, attempt, status) VALUES ('s1', 'c1', 'triage', 1, 'done')"
        ))
    upgrade_to_head(engine)
    assert "decision_records" in inspect(engine).get_table_names()
    with engine.begin() as conn:
        assert conn.execute(text("SELECT run_mode FROM stage_executions WHERE id = 's1'")).scalar_one() == "clinical"
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("UPDATE stage_executions SET run_mode = 'batch' WHERE id = 's1'"))
    run(engine, command.downgrade, "0002_drop_v5_reports")
    assert "decision_records" not in inspect(engine).get_table_names()
    assert "run_mode" not in {c["name"] for c in inspect(engine).get_columns("stage_executions")}


def test_0005_makes_histologic_type_nullable_and_downgrade_refuses_to_invent_one(engine):
    upgrade_to_head(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id, created_by, status, created_at) VALUES ('c1', 'v6', 'open', '2026-09-29')"))
        conn.execute(text(
            "INSERT INTO gradings (case_id, histologic_type, type_confirmed_by, machine, overrides, created_at, updated_at) "
            "VALUES ('c1', NULL, 'unconfirmed', '{}', '{}', '2026-09-29', '2026-09-29')"
        ))
    with pytest.raises(RuntimeError, match="1 gradings have no histologic type"):
        run(engine, command.downgrade, "0004_stage_run_mode")


# --- API startup ------------------------------------------------------------------

def test_startup_migrates_outside_test_env(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "upgrade_to_head", calls.append)
    monkeypatch.setattr(main.settings, "ENV", "production")
    with TestClient(main.app):
        pass
    assert calls == [main.engine]


def test_startup_does_not_migrate_in_test_env(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "upgrade_to_head", calls.append)
    with TestClient(main.app):
        pass
    assert calls == []


def test_startup_aborts_on_invalid_config(monkeypatch):
    def broken_config():
        raise ConfigLoadError("qc.yaml: invalid")

    monkeypatch.setattr(main, "init_pipeline_config", broken_config)
    with pytest.raises(ConfigLoadError):
        with TestClient(main.app):
            pass


def test_startup_aborts_on_migration_failure(monkeypatch):
    def refuse(engine):
        raise UnversionedDatabaseError("no alembic_version")

    monkeypatch.setattr(main, "upgrade_to_head", refuse)
    monkeypatch.setattr(main.settings, "ENV", "production")
    with pytest.raises(UnversionedDatabaseError):
        with TestClient(main.app):
            pass
