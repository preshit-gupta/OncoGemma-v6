import os
import sqlite3
from collections.abc import Mapping
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.core.config import Settings, settings

# Cloud Run mounts each --add-cloudsql-instances socket under this directory.
CLOUD_SQL_SOCKET_DIR = "/cloudsql"


class DatabaseConfigError(RuntimeError):
    """The database connection settings are missing or contradictory."""


def resolve_database_url(settings: Settings, environ: Mapping[str, str]) -> URL:
    """Return the database URL from exactly one configured source, or raise.

    - ``DATABASE_URL``: used as given (the test suite sets ``sqlite:///:memory:``).
    - ``CLOUD_SQL_CONNECTION_NAME`` (``project:region:instance``): Postgres over the
      Cloud Run unix socket, as ``DB_USER`` on ``DB_NAME``. The password comes only
      from the ``DB_PASSWORD`` environment variable, which Cloud Run fills from
      Secret Manager (``--set-secrets=DB_PASSWORD=<secret>:latest``).

    There is no default password and no SQLite fallback.
    """
    database_url = settings.DATABASE_URL
    connection_name = settings.CLOUD_SQL_CONNECTION_NAME
    if database_url and connection_name:
        raise DatabaseConfigError(
            "Both DATABASE_URL and CLOUD_SQL_CONNECTION_NAME are set. Set exactly one."
        )
    if database_url:
        return make_url(database_url)
    if not connection_name:
        raise DatabaseConfigError(
            "No database is configured. Set DATABASE_URL, or on Cloud Run set "
            "CLOUD_SQL_CONNECTION_NAME and the DB_PASSWORD secret."
        )

    password = environ.get("DB_PASSWORD", "")
    if not password:
        raise DatabaseConfigError(
            f"CLOUD_SQL_CONNECTION_NAME is {connection_name!r} but DB_PASSWORD is not set. "
            "Deploy with --set-secrets=DB_PASSWORD=<secret>:latest so Cloud Run reads it from Secret Manager."
        )
    if password != password.strip():
        raise DatabaseConfigError(
            "DB_PASSWORD has leading or trailing whitespace, usually a newline stored in the secret. "
            "Add a secret version without it."
        )
    return URL.create(
        "postgresql+psycopg2",
        username=settings.DB_USER,
        password=password,
        database=settings.DB_NAME,
        query={"host": f"{CLOUD_SQL_SOCKET_DIR}/{connection_name}"},
    )


# Register SQLite listener for PRAGMA foreign_keys=ON on all SQLite engines (Issue #8)
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

db_url = resolve_database_url(settings, os.environ)
if settings.CLOUD_SQL_CONNECTION_NAME:
    print(f"[DB Core] Using Cloud SQL instance {settings.CLOUD_SQL_CONNECTION_NAME} via unix socket")

if db_url.get_backend_name() == "sqlite":
    connect_args = {"check_same_thread": False}
    engine_kwargs = {"connect_args": connect_args}
    if db_url.database == ":memory:":
        engine_kwargs["poolclass"] = StaticPool
    else:
        connect_args["timeout"] = 30
    engine = create_engine(db_url, **engine_kwargs)
else:
    # PostgreSQL engine: do NOT catch connection failures or fallback to SQLite (Issue #6, #288, #350)
    engine = create_engine(
        db_url,
        pool_pre_ping=True,
        pool_size=30,
        max_overflow=50,
        pool_timeout=60,
        pool_recycle=1800,
        connect_args={"connect_timeout": 10}
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
