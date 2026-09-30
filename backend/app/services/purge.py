"""Retention jobs (SPEC-03 §5.3): hard-delete expired soft-deleted cases and expired idempotency keys.

Run daily as a scheduled job: ``python -m app.services.purge``.
"""
from datetime import datetime, timedelta, timezone
import logging
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.pipeline_config import get_pipeline_config
from app.models.case import Case
from app.models.audit import AuditEvent
from app.models.idempotency import IdempotencyKeyRecord
from app.routers.cases import delete_single_case_data

logger = logging.getLogger("oncogemma.purge")

PURGE_ACTOR = "system:purge_job"


def purge_soft_deleted_cases(
    db: Session,
    actor: str = PURGE_ACTOR,
    retention_days: int | None = None
) -> int:
    """
    Hard-deletes cases soft-deleted longer ago than safety.soft_delete.retention_days (SPEC-03 §5.3.1).
    """
    safety_cfg = get_pipeline_config().safety
    if retention_days is None:
        retention_days = safety_cfg.soft_delete.retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    stmt = select(Case).where(Case.deleted_at.is_not(None), Case.deleted_at <= cutoff)
    expired_cases = db.scalars(stmt).all()
    count = 0

    for case in expired_cases:
        case_id = case.id
        case_str = str(case_id)
        deleted_at_iso = case.deleted_at.isoformat() if case.deleted_at else None
        logger.info(f"[Purge] Hard-deleting expired soft-deleted case {case_str}")
        delete_single_case_data(case_id, db)
        audit = AuditEvent(
            case_id=case_str,
            actor=actor,
            event_type="case_purged",
            payload={"retention_days": retention_days, "soft_deleted_at": deleted_at_iso}
        )
        db.add(audit)
        db.commit()
        count += 1

    return count


def purge_expired_idempotency_keys(db: Session) -> int:
    """Deletes idempotency keys past their ``expires_at`` (SPEC-03 §5.3.3)."""
    result = db.execute(
        delete(IdempotencyKeyRecord).where(IdempotencyKeyRecord.expires_at <= datetime.now(timezone.utc))
    )
    db.commit()
    return result.rowcount


def main() -> None:
    from app.core.db import SessionLocal
    from app.core.pipeline_config import init_pipeline_config

    logging.basicConfig(level=logging.INFO)
    init_pipeline_config()
    with SessionLocal() as db:
        cases = purge_soft_deleted_cases(db)
        keys = purge_expired_idempotency_keys(db)
    logger.info("[Purge] removed %d soft-deleted case(s) and %d expired idempotency key(s)", cases, keys)


if __name__ == "__main__":
    main()
