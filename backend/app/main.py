import sys
import time
import asyncio
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from app.auth.deps import public
from app.auth.service import check_auth_settings
from app.core.config import settings
from app.core.migrations import upgrade_to_head
from app.core.pipeline_config import init_pipeline_config
from app.core.gcs import ensure_buckets_exist
from app.core.db import engine
from app.routers import (
    cases_router, tiles_router, audit_router, triage_router, mitosis_router, grading_router, worker_webhook_router
)
from app.routers.admin import router as admin_router
from app.routers.auth import router as auth_router
from app.routers.users import router as users_router

import logging

logger = logging.getLogger("oncogemma.daemon")

async def background_pipeline_worker():
    """
    Self-healing, always-on background worker daemon running inside Cloud Run.
    Continuously monitors the database for any 'queued' pipeline stages and executes
    them immediately without relying on external task queues.
    Periodically checks for and recovers any orphaned 'running' stages (> 300s).
    """
    logger.info("[Always-On Worker] Initializing in-process pipeline worker daemon...")
    from worker.main import poll_and_execute_single_task, reset_stuck_running_stages
    try:
        await asyncio.to_thread(reset_stuck_running_stages, 300)
    except Exception as e:
        logger.warning(f"[Always-On Worker Reset Note] {e}")

    last_reset_check = time.time()
    while True:
        try:
            # Self-healing watchdog: recover stages stuck in 'running' after container restarts or crashes
            if time.time() - last_reset_check > 60.0:
                try:
                    await asyncio.to_thread(reset_stuck_running_stages, 300)
                except Exception as r_err:
                    logger.warning(f"[Always-On Worker Periodic Reset Note] {r_err}")
                last_reset_check = time.time()

            had_work = await asyncio.to_thread(poll_and_execute_single_task)
            if not had_work:
                await asyncio.sleep(1.5)
            else:
                # Chained stages execute immediately with minimal latency
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            logger.info("[Always-On Worker] Background worker cancelled gracefully.")
            break
        except Exception as exc:
            logger.error(f"[Always-On Worker Exception] {exc}")
            await asyncio.sleep(2.0)


async def _async_init_and_worker():
    """Check buckets and start the background worker non-blockingly."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, ensure_buckets_exist)
    except Exception as e:
        logger.warning(f"[GCS Bucket Check Note] {e}")
    if settings.RUN_IN_PROCESS_WORKER:
        await background_pipeline_worker()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Configuration and schema are prerequisites: any failure here aborts startup (SPEC-01 §3.1, §3.8).
    init_pipeline_config()
    check_auth_settings()
    if settings.ENV != "test":
        await asyncio.to_thread(upgrade_to_head, engine)
    # Launch bucket checks and the worker daemon in a background task.
    init_task = asyncio.create_task(_async_init_and_worker())
    try:
        yield
    finally:
        init_task.cancel()
        try:
            await init_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="OncoGemma v4.5 API",
    description="Breast Cancer Diagnostic Copilot API — Nottingham Grading & CAP-Compliant Synoptic Reporting",
    version="4.5.0",
    lifespan=lifespan
)

cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(cases_router)
app.include_router(tiles_router)
app.include_router(audit_router)
app.include_router(triage_router)
app.include_router(mitosis_router)
app.include_router(grading_router)
app.include_router(worker_webhook_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(users_router)

@app.get("/health", dependencies=[Depends(public)])
@app.get("/api/health", dependencies=[Depends(public)])
@app.get("/healthz", dependencies=[Depends(public)])
@app.get("/api/healthz", dependencies=[Depends(public)])
@app.get("/api/v1/health", dependencies=[Depends(public)])
@app.get("/api/v1/healthz", dependencies=[Depends(public)])
async def health_check():
    from starlette.concurrency import run_in_threadpool
    def _ping():
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    try:
        await run_in_threadpool(_ping)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database connection failed: {e}"
        )
    return {
        "status": "healthy",
        "version": app.version,
        "env": settings.ENV
    }
