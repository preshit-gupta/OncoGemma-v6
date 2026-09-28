"""Buffer for DecisionRecords written during one stage execution (SPEC-01 §3.3).

The gateway may be called from worker threads, so rows are buffered under a lock.
The stage execution wrapper writes them with the stage's final status: on success in
the same commit as the stage outputs, and on failure after the rollback, so the
records of a failed stage survive while its partial outputs do not.
"""
import threading
from typing import Any

from sqlalchemy.orm import Session

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
