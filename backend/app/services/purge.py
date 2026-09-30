from datetime import datetime, timedelta, timezone
import logging
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.pipeline_config import get_pipeline_config
from app.models.case import Case
from app.models.audit import AuditEvent
from app.routers.cases import delete_single_case_data

logger = logging.getLogger("oncogemma.purge")


def purge_soft_deleted_cases(
    db: Session,
    actor: str = "system:purge_job",
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
