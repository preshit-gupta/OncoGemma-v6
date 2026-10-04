"""Buffer for DecisionRecords written during one stage execution (SPEC-01 §3.3), and the non-model
``mitosis_count`` record (SPEC-06 AC8).

The gateway may be called from worker threads, so rows are buffered under a lock.
The stage execution wrapper writes them with the stage's final status: on success in
the same commit as the stage outputs, and on failure after the rollback, so the
records of a failed stage survive while its partial outputs do not.
"""
import hashlib
import threading
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.pipeline_config import canonical_json
from app.core.tasks import DecisionStatus, EntityType, ProducerKind, Task
from app.models.decision_record import DecisionRecord


class DecisionLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] = []

    def add(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._pending.append(row)

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._pending)

    def flush(self, session: Session) -> int:
        """Add every buffered row to ``session`` (the caller commits). Returns the count."""
        with self._lock:
            rows, self._pending = self._pending, []
        session.add_all(DecisionRecord(**row) for row in rows)
        return len(rows)


def mitosis_count_record(
    *,
    case_id: uuid.UUID,
    stage_execution_id: uuid.UUID,
    run_id: uuid.UUID | None,
    run_mode: str,
    config_hash: str,
    slide_id: str,
    candidates: list[dict[str, Any]],
    hpfs: list[dict[str, Any]],
    summary: dict[str, Any],
    thresholds: dict[str, float],
    supersedes_id: uuid.UUID | None = None,
) -> DecisionRecord:
    """The Stage 4 count as a decision (SPEC-06 AC8): one per Stage 4 run and one per recompute.

    Not a model call: the producer is the counting rule in ``pipeline/scoring.py``, versioned by the
    configuration that holds its thresholds. ``input_spec`` names every candidate and the decision
    records of the counted ones (``mitosis_detect`` -> ... -> ``mitosis_count``).
    """
    counted = [c for c in candidates if c["counted"]]
    count_input = {
        "candidate_ids": sorted(c["id"] for c in candidates),
        "counted_ids": sorted(c["id"] for c in counted),
        "parent_record_ids": sorted({rid for c in counted for rid in (c.get("record_ids") or [])}),
        "hpfs": [{"seq": h["seq"], "center_um": list(h["center_um"]), "radius_um": h["radius_um"]} for h in hpfs],
    }
    return DecisionRecord(
        id=uuid.uuid4(),
        case_id=case_id,
        stage_execution_id=stage_execution_id,
        run_id=run_id,
        stage="mitosis",
        task=Task.MITOSIS_COUNT.value,
        entity_type=EntityType.SLIDE.value,
        entity_id=slide_id,
        entity_ids_uri=None,
        producer_kind=ProducerKind.HEURISTIC.value,
        producer_id="mitosis_count",
        producer_version=config_hash,
        endpoint=None,
        prompt_id=None,
        prompt_sha256=None,
        input_sha256=hashlib.sha256(canonical_json(count_input).encode("utf-8")).hexdigest(),
        input_spec=count_input,
        params={"thresholds": thresholds},
        output=summary,
        raw_output_uri=None,
        status=DecisionStatus.OK.value,
        error_class=None,
        error_detail=None,
        latency_ms=0,
        cost_usd=None,
        cache_hit=False,
        run_mode=run_mode,
        config_hash=config_hash,
        supersedes_id=supersedes_id,
    )
