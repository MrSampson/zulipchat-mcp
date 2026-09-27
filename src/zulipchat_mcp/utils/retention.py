"""Retention cleanup for the insert-only event/request tables.

agent_events, session_events, agent_requests, user_input_requests, and
tasks are never deleted by their own INSERT/UPDATE call sites (see
database_manager.py) - left alone they grow without bound. This module
deletes rows once they're no longer needed: a terminal state has been
reached (acked, non-pending status, completed) and the row is older than
the configured retention window. Rows still in flight are never deleted
regardless of age.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .database import DatabaseManager
from .logging import get_logger

logger = get_logger(__name__)

_CLEANUP_STATEMENTS: tuple[tuple[str, str], ...] = (
    (
        "agent_events",
        "DELETE FROM agent_events WHERE acked = TRUE AND created_at < ?",
    ),
    (
        "session_events",
        "DELETE FROM session_events WHERE acked = TRUE AND created_at < ?",
    ),
    (
        "agent_requests",
        "DELETE FROM agent_requests WHERE status != 'pending' AND created_at < ?",
    ),
    (
        "user_input_requests",
        "DELETE FROM user_input_requests WHERE status != 'pending' AND created_at < ?",
    ),
    (
        "tasks",
        "DELETE FROM tasks WHERE completed_at IS NOT NULL AND completed_at < ?",
    ),
)


def run_retention_cleanup(db: DatabaseManager, retention_days: int) -> None:
    """Delete terminal-state rows older than `retention_days` from every
    insert-only event/request table.

    A non-positive `retention_days` disables cleanup entirely.
    """
    if retention_days <= 0:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for table, sql in _CLEANUP_STATEMENTS:
        try:
            db.execute(sql, [cutoff])
        except Exception as e:
            logger.error(f"Retention cleanup failed for {table}: {e}")
