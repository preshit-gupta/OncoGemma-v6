"""0012_research: issues, gt_annotations, run_metrics, annotation_tasks, qa_items (SPEC-08 §5–7)."""
import pytest
from alembic import command
from sqlalchemy import create_engine, inspect

from app.core.migrations import alembic_config


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'oncogemma_test.db'}")
    yield engine
    engine.dispose()


def run(engine, fn, *args):
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        fn(cfg, *args)


def test_research_migration_upgrade_and_downgrade(engine):
    # Upgrade to 0013_research
    run(engine, command.upgrade, "0013_research")
    tables = set(inspect(engine).get_table_names())
    assert {"issues", "gt_annotations", "run_metrics", "annotation_tasks", "qa_items"} <= tables
    qa_columns = {c["name"]: c["nullable"] for c in inspect(engine).get_columns("qa_items")}
    assert qa_columns["dataset"] is False and qa_columns["protocol_version"] is False

    # Downgrade back to 0012_safety_hardening
    run(engine, command.downgrade, "0012_safety_hardening")
    tables_after = set(inspect(engine).get_table_names())
    assert not ({"issues", "gt_annotations", "run_metrics", "annotation_tasks", "qa_items"} & tables_after)
