"""0012_research: issues, gt_annotations, run_metrics, annotation_tasks, qa_items (SPEC-08 §5–7)."""
import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

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

    # Downgrade back to 0012_safety_hardening
    run(engine, command.downgrade, "0012_safety_hardening")
    tables_after = set(inspect(engine).get_table_names())
    assert not ({"issues", "gt_annotations", "run_metrics", "annotation_tasks", "qa_items"} & tables_after)


def test_qa_items_dataset_protocol_migration(engine):
    assert len("0014_qa_items_dataset_protocol") <= 32  # alembic_version.version_num is VARCHAR(32)
    run(engine, command.upgrade, "0014_qa_items_dataset_protocol")
    columns = {c["name"]: c["nullable"] for c in inspect(engine).get_columns("qa_items")}
    assert columns["dataset"] is False and columns["protocol_version"] is False

    run(engine, command.downgrade, "0013_research")
    columns = {c["name"] for c in inspect(engine).get_columns("qa_items")}
    assert not ({"dataset", "protocol_version"} & columns)


def test_qa_items_migration_refuses_to_invent_a_dataset_for_existing_rows(engine):
    run(engine, command.upgrade, "0013_research")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO qa_items (patient_id, report_text_url) VALUES ('TCGA-01', 'u')"))
    with pytest.raises(RuntimeError, match="1 rows without a dataset"):
        run(engine, command.upgrade, "0014_qa_items_dataset_protocol")
    assert "dataset" not in {c["name"] for c in inspect(engine).get_columns("qa_items")}
