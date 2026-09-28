"""
The API lifespan starts the in-process polling worker only when settings.RUN_IN_PROCESS_WORKER is on.

The worker polls the database from another thread. Under the suite's in-memory SQLite StaticPool that is
the connection the request uses, which made TestClient tests flaky ("no such table: cases",
"Could not refresh instance"). conftest turns it off; deployments keep the default (on).
"""
import asyncio

from app import main
from app.core.config import Settings


def _stub_worker(monkeypatch):
    started = []

    async def fake_worker():
        started.append(True)

    monkeypatch.setattr(main, "ensure_buckets_exist", lambda: None)
    monkeypatch.setattr(main, "background_pipeline_worker", fake_worker)
    return started


def test_in_process_worker_is_on_by_default(monkeypatch):
    monkeypatch.delenv("RUN_IN_PROCESS_WORKER", raising=False)
    assert Settings(_env_file=None).RUN_IN_PROCESS_WORKER is True


def test_lifespan_starts_worker_when_enabled(monkeypatch):
    started = _stub_worker(monkeypatch)
    monkeypatch.setattr(main.settings, "RUN_IN_PROCESS_WORKER", True)
    asyncio.run(main._async_init_and_worker())
    assert started == [True]


def test_lifespan_skips_worker_when_disabled(monkeypatch):
    started = _stub_worker(monkeypatch)
    monkeypatch.setattr(main.settings, "RUN_IN_PROCESS_WORKER", False)
    asyncio.run(main._async_init_and_worker())
    assert started == []
