"""Alembic environment (SPEC-01 §3.1).

The engine comes from ``app.core.db``, so the CLI and the API migrate the same
database. ``app.core.migrations.upgrade_to_head`` passes its own connection in
``config.attributes["connection"]``; the CLI opens one from the app engine.
"""
from logging.config import fileConfig

from alembic import context

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.core.db import Base, engine
from app.models.base import GUID

config = context.config
shared_connection = config.attributes.get("connection")

# Only the CLI configures logging; inside the API the app's logging is kept.
if config.config_file_name is not None and shared_connection is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Render the app's GUID type by name so migrations import it explicitly."""
    if type_ == "type" and isinstance(obj, GUID):
        autogen_context.imports.add("from app.models.base import GUID")
        return "GUID()"
    return False


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_item=render_item,
        render_as_batch=connection.dialect.name == "sqlite",
    )


def run_migrations_offline() -> None:
    context.configure(
        dialect_name=engine.dialect.name,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    if shared_connection is not None:
        _configure(shared_connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    with engine.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
