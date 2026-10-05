import sys
import time
import asyncio
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from app.auth.deps import public
from app.auth.idempotency import IdempotentReplay, replay_response
from app.auth.rate_limit import RateLimitMiddleware
from app.auth.service import check_auth_settings
from app.core.config import settings
from app.core.geometry import ContractHTTPError, HotspotOverlapError, PolygonValidationError
from app.core.migrations import upgrade_to_head
from app.core.soft_delete import reject_soft_deleted_case
from app.core.pipeline_config import init_pipeline_config
from app.core.gcs import ensure_buckets_exist
from app.core.db import engine
from app.routers import (
    cases_router, cases_test_router, tiles_router, audit_router, triage_router, mitosis_router, grading_router, worker_webhook_router
)
from app.routers.admin import router as admin_router
from app.routers.auth import router as auth_router
from app.routers.batches import router as batches_router
from app.routers.research import router as research_router
from app.routers.users import router as users_router

import logging

logger = logging.getLogger("oncogemma.daemon")

async def background_pipeline_worker():
    """
    Self-healing, always-on background worker daemon running inside Cloud Run.
    Continuously monitors the database for any 'queued' pipeline stages and executes
    them immediately without relying on external task queues.
    Periodically queues again the orphaned 'running' stages of its run modes (settings.STAGE_STALE_AFTER_S).
    """
    logger.info("[Always-On Worker] Initializing in-process pipeline worker daemon...")
    from worker.main import poll_and_execute_single_task, reset_stuck_running_stages
    try:
        await asyncio.to_thread(reset_stuck_running_stages)
    except Exception as e:
        logger.warning(f"[Always-On Worker Reset Note] {e}")

    last_reset_check = time.time()
    while True:
        try:
            # Self-healing watchdog: recover stages stuck in 'running' after container restarts or crashes
            if time.time() - last_reset_check > 60.0:
                try:
                    await asyncio.to_thread(reset_stuck_running_stages)
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


def create_app(env: str | None = None) -> FastAPI:
    runtime_env = env if env is not None else settings.ENV

    application = FastAPI(
        title="OncoGemma v4.5 API",
        description="Breast Cancer Diagnostic Copilot API — Nottingham Grading & CAP-Compliant Synoptic Reporting",
        version="4.5.0",
        lifespan=lifespan
    )

    # Added first so CORS stays the outermost layer and 429 responses carry CORS headers.
    application.add_middleware(RateLimitMiddleware)
    cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_exception_handler(IdempotentReplay, replay_response)

    # Hotspot geometry errors answer with the triage contract's bodies (docs/contracts/triage_v6.md),
    # plus the human-readable ``detail`` every other error carries.
    async def _hotspot_overlap_handler(request, exc: HotspotOverlapError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "hotspot_overlap", "ids": exc.ids, "detail": exc.detail},
        )

    async def _polygon_validation_handler(request, exc: PolygonValidationError):
        content = {"error": "invalid_polygon", "reason": exc.reason, "detail": exc.detail}
        if exc.polygon_id is not None:
            content["id"] = exc.polygon_id
        return JSONResponse(status_code=exc.status_code, content=content)

    async def _contract_error_handler(request, exc: ContractHTTPError):
        return JSONResponse(status_code=exc.status_code, content=exc.body)

    application.add_exception_handler(ContractHTTPError, _contract_error_handler)
    application.add_exception_handler(HotspotOverlapError, _hotspot_overlap_handler)
    application.add_exception_handler(PolygonValidationError, _polygon_validation_handler)

    # Soft-deleted cases answer 404 on every case-scoped route; audit history stays readable.
    case_scoped = [Depends(reject_soft_deleted_case)]
    application.include_router(cases_router, dependencies=case_scoped)
    application.include_router(tiles_router, dependencies=case_scoped)
    application.include_router(audit_router)
    application.include_router(triage_router, dependencies=case_scoped)
    application.include_router(mitosis_router, dependencies=case_scoped)
    application.include_router(grading_router, dependencies=case_scoped)
    application.include_router(worker_webhook_router)
    application.include_router(auth_router)
    application.include_router(users_router)
    application.include_router(batches_router)
    application.include_router(research_router)

    # Destructive endpoints: mounted only when ENV=test (SPEC-03 §5.3.1, AC6)
    if runtime_env == "test":
        application.include_router(admin_router)
        application.include_router(cases_test_router)

    @application.get("/health", dependencies=[Depends(public)])
    @application.get("/api/health", dependencies=[Depends(public)])
    @application.get("/healthz", dependencies=[Depends(public)])
    @application.get("/api/healthz", dependencies=[Depends(public)])
    @application.get("/api/v1/health", dependencies=[Depends(public)])
    @application.get("/api/v1/healthz", dependencies=[Depends(public)])
    async def health_check():
        from starlette.concurrency import run_in_threadpool
        def _ping():
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        try:
            await run_in_threadpool(_ping)
        except Exception as exc:
            logger.error("Database connection failed during health check: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database connection failed"
            )
        return {
            "status": "healthy",
            "version": application.version,
            "env": settings.ENV
        }

    return application


app = create_app()
