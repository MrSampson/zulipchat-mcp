"""Retention cleanup for the insert-only event/request/task tables.

agent_events, session_events, agent_requests, user_input_requests, and
tasks are never deleted by their own INSERT/UPDATE call sites (see
database_manager.py and tools/agents.py) - left alone they grow without
bound. This module deletes rows once they're old enough that nothing will
look for them.

For session_events, agent_requests, user_input_requests, and tasks, that
means a terminal state (acked, non-pending status, completed) reached
before the retention window - anything still in flight is kept regardless
of age. agent_events is the exception: its `acked` flag is not a reliable
"processed" signal (the only code path that ever sets it - teleport_chat's
wait-for-reply - only acks the one row it matched, out of the most recent
unacked ones, so most rows stay acked=FALSE forever regardless of age), so
it's pruned by age alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .logging import get_logger

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = get_logger(__name__)

_CLEANUP_STATEMENTS: tuple[tuple[str, str], ...] = (
    (
        "agent_events",
        "DELETE FROM agent_events WHERE created_at < ?",
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
