"""Database URL resolution: no default password, no silent SQLite fallback (SPEC-03 §5.4).

On Cloud Run the password comes only from DB_PASSWORD, which the deploy fills
from Secret Manager (--set-secrets). Tests never touch a real database.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.db import DatabaseConfigError, resolve_database_url

BACKEND_DIR = Path(__file__).resolve().parents[2]
CONNECTION_NAME = "test-project:test-region:test-instance"
# Characters that break a password interpolated into a URL by hand.
AWKWARD_PASSWORD = "n3w/p@ss:w0rd?#%&="


def make_settings(database_url: str = "", connection_name: str = "") -> Settings:
    return Settings(
        DATABASE_URL=database_url,
        CLOUD_SQL_CONNECTION_NAME=connection_name,
        DB_USER="app_user",
        DB_NAME="app_db",
    )


def test_database_url_is_used_as_given():
    url = resolve_database_url(make_settings(database_url="sqlite:///:memory:"), {})
    assert url.get_backend_name() == "sqlite"
    assert url.database == ":memory:"


def test_cloud_sql_url_uses_socket_and_password_from_environment():
    url = resolve_database_url(make_settings(connection_name=CONNECTION_NAME), {"DB_PASSWORD": AWKWARD_PASSWORD})
    assert url.drivername == "postgresql+psycopg2"
    assert url.username == "app_user"
    assert url.password == AWKWARD_PASSWORD
    assert url.database == "app_db"
    assert url.host is None
    assert dict(url.query) == {"host": f"/cloudsql/{CONNECTION_NAME}"}
    assert make_url(url.render_as_string(hide_password=False)).password == AWKWARD_PASSWORD
    assert AWKWARD_PASSWORD not in repr(url)


@pytest.mark.parametrize("environ", [{}, {"DB_PASSWORD": ""}])
def test_cloud_sql_without_password_raises(environ):
    with pytest.raises(DatabaseConfigError, match="DB_PASSWORD is not set"):
        resolve_database_url(make_settings(connection_name=CONNECTION_NAME), environ)


@pytest.mark.parametrize("password", ["secret\n", " secret", "secret\r\n"])
def test_cloud_sql_password_with_surrounding_whitespace_raises(password):
    with pytest.raises(DatabaseConfigError, match="whitespace"):
        resolve_database_url(make_settings(connection_name=CONNECTION_NAME), {"DB_PASSWORD": password})


def test_nothing_configured_raises_instead_of_falling_back_to_sqlite():
    with pytest.raises(DatabaseConfigError, match="No database is configured"):
        resolve_database_url(make_settings(), {"DB_PASSWORD": "unused"})


def test_database_url_and_cloud_sql_together_raise():
    settings = make_settings(database_url="sqlite:///:memory:", connection_name=CONNECTION_NAME)
    with pytest.raises(DatabaseConfigError, match="exactly one"):
        resolve_database_url(settings, {"DB_PASSWORD": "unused"})


def import_db_module(tmp_path, **env_overrides) -> subprocess.CompletedProcess:
    """Import app.core.db in a fresh interpreter, as API and worker startup do."""
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "CLOUD_SQL_CONNECTION_NAME", "DB_PASSWORD")}
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env.update(env_overrides)
    # cwd is an empty directory, so no stray .env file feeds Settings.
    return subprocess.run(
        [sys.executable, "-c", "import app.core.db"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )


def test_startup_fails_on_cloud_sql_without_password(tmp_path):
    result = import_db_module(tmp_path, CLOUD_SQL_CONNECTION_NAME=CONNECTION_NAME)
    assert result.returncode != 0
    assert "DatabaseConfigError" in result.stderr
    assert "DB_PASSWORD is not set" in result.stderr


def test_startup_fails_without_any_database(tmp_path):
    result = import_db_module(tmp_path)
    assert result.returncode != 0
    assert "No database is configured" in result.stderr
