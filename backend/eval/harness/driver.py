"""Workers drive validation runs and batches (SPEC-02 §6.1–6.2; owner decision 2026-09-30, option A).

Every worker calls ``drive_active_runs`` on a timer. A run is driven by at most one worker at a
time: the worker takes the run's lease with one conditional UPDATE (free, expired, or already
its own) and renews it on every tick. A worker that dies simply stops renewing, and after
``lease_s`` another worker takes the run over; the controller's decisions come from the
database, so the new controller repeats nothing (AC2).
"""
from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.validation import ValidationRun
from eval.harness.controller import RunController

ACTIVE_RUN = ("created", "running")


def worker_identity() -> str:
    return f"worker:{socket.gethostname()}:{os.getpid()}"


def take_lease(session: Session, run_id, owner: str, *, lease_s: float, now: datetime) -> bool:
    """Take or renew the run's controller lease. Commits; True when ``owner`` holds it."""
    result = session.execute(
        update(ValidationRun)
        .where(
            ValidationRun.id == run_id,
            ValidationRun.status.in_(ACTIVE_RUN),
            or_(
                ValidationRun.controller_lease_until.is_(None),
                ValidationRun.controller_lease_until < now,
                ValidationRun.controller_lease_owner == owner,
            ),
        )
        .values(controller_lease_owner=owner, controller_lease_until=now + timedelta(seconds=lease_s))
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount == 1


def release_lease(session: Session, run_id, owner: str) -> None:
    session.execute(
        update(ValidationRun)
        .where(ValidationRun.id == run_id, ValidationRun.controller_lease_owner == owner)
        .values(controller_lease_owner=None, controller_lease_until=None)
        .execution_options(synchronize_session=False)
    )
    session.commit()


def fail_run(session: Session, run_id, owner: str, exc: BaseException) -> None:
    """A controller error stops the run loudly: status failed, the error in the audit log.
    The caller re-raises, so the worker logs it; other runs are driven on the next tick."""
    session.rollback()
    run = session.get(ValidationRun, run_id)
    run.status = "failed"
    run.finished_at = datetime.now(timezone.utc)
    session.add(AuditEvent(
        actor=owner, event_type="validation_run_failed",
        payload={"run_id": str(run_id), "class": type(exc).__name__, "detail": str(exc)},
    ))
    session.commit()


def drive_active_runs(
    session_factory: Callable[[], Session],
    owner: str,
    *,
    lease_s: float,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[str]:
    """One controller pass over every active run this worker holds or can take. Returns their ids."""
    session = session_factory()
    driven = []
    try:
        run_ids = session.scalars(select(ValidationRun.id).where(ValidationRun.status.in_(ACTIVE_RUN))).all()
        for run_id in run_ids:
            if not take_lease(session, run_id, owner, lease_s=lease_s, now=now()):
                continue
            try:
                RunController(session, run_id).step()
            except Exception as exc:  # the run's boundary: record the run as failed, then re-raise
                fail_run(session, run_id, owner, exc)
                raise
            driven.append(str(run_id))
            if session.get(ValidationRun, run_id).status not in ACTIVE_RUN:
                release_lease(session, run_id, owner)
    finally:
        session.close()
    return driven
