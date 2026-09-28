"""Schema migrations run by the API entrypoint (SPEC-01 §3.1).

Startup runs ``alembic upgrade head`` when ``ENV != test``. ``create_all``
survives only in test fixtures.
"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.core.db import Base

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
BASELINE_REVISION = "0001_v5_baseline"

# Key for pg_advisory_xact_lock, so concurrent API instances migrate one at a time.
# Any fixed bigint works; this one is the ASCII bytes of "ONCOGEMM".
MIGRATION_LOCK_KEY = 0x4F4E434F47454D4D


class UnversionedDatabaseError(RuntimeError):
    """The database has application tables but no alembic_version table."""


def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def upgrade_to_head(engine: Engine) -> None:
    """Upgrade the database to the latest revision, or raise.

    A v5 database (built by ``create_all``) is refused rather than guessed at:
    the operator verifies it matches the baseline and stamps it once.
    """
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY})

        existing = set(inspect(conn).get_table_names())
        app_tables = existing & set(Base.metadata.tables)
        if "alembic_version" not in existing and app_tables:
            raise UnversionedDatabaseError(
                f"Database has application tables {sorted(app_tables)} but no alembic_version table. "
                f"It was built without migrations (v5 create_all). From backend/, run "
                f"`alembic stamp {BASELINE_REVISION}`, then `alembic check` to confirm the schema matches "
                f"the models, then restart."
            )

        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
