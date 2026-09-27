"""Retention cleanup for the insert-only event/request/task tables.

agent_events, session_events, agent_requests, user_input_requests, and
tasks are never deleted by their own INSERT/UPDATE call sites (see
database_manager.py and tools/agents.py) - left alone they grow without
bound. This module deletes rows once they're old enough that nothing will
look for them.

For session_events, agent_requests, user_input_requests, and tasks, that
normally means a terminal state (acked, non-pending status, completed)
reached before the retention window - anything still in flight is kept
regardless of age. agent_events is the exception: its `acked` flag is not
a reliable "processed" signal (the only code path that ever sets it -
teleport_chat's wait-for-reply - only acks the one row it matched, out of
the most recent unacked ones, so most rows stay acked=FALSE forever
regardless of age), so it's pruned by age alone.

A row can also be stuck non-terminal forever through no fault of the
retention rule: the process waiting on it can crash mid-wait, or nobody
ever polls/answers it. The `stale_ceiling_days` cutoff bounds that - past
this much older age, still-non-terminal rows are deleted regardless of
their age-window position, on the theory that anything both in-flight and
that old is abandoned rather than genuinely pending. For session_events,
agent_requests, and user_input_requests this cutoff applies unconditionally
(the same `created_at` column drives both the terminal-state check and the
ceiling, so a row surviving to ceiling age is necessarily still
non-terminal). tasks is the exception: its terminal check keys off
`completed_at`, a different column from the ceiling's `started_at`, so the
ceiling statement is gated on `completed_at IS NULL` - otherwise a
long-running but completed task would be deleted for having started long
ago, despite having finished normally.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .logging import get_logger

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = get_logger(__name__)

_CLEANUP_STATEMENTS: tuple[tuple[str, str, str | None], ...] = (
    (
        "agent_events",
        "DELETE FROM agent_events WHERE created_at < ?",
        None,
    ),
    (
        "session_events",
        "DELETE FROM session_events WHERE acked = TRUE AND created_at < ?",
        "DELETE FROM session_events WHERE created_at < ?",
    ),
    (
        "agent_requests",
        "DELETE FROM agent_requests WHERE status != 'pending' AND created_at < ?",
        "DELETE FROM agent_requests WHERE created_at < ?",
    ),
    (
        "user_input_requests",
        "DELETE FROM user_input_requests WHERE status != 'pending' AND created_at < ?",
        "DELETE FROM user_input_requests WHERE created_at < ?",
    ),
    (
        "tasks",
        "DELETE FROM tasks WHERE completed_at IS NOT NULL AND completed_at < ?",
        "DELETE FROM tasks WHERE completed_at IS NULL AND started_at < ?",
    ),
)


def run_retention_cleanup(
    db: DatabaseManager, retention_days: int, stale_ceiling_days: int = 0
) -> None:
    """Delete terminal-state rows older than `retention_days`, and rows past
    the much longer `stale_ceiling_days` regardless of terminal state, from
    every insert-only event/request table.

    A non-positive `retention_days` disables the terminal-state cleanup
    entirely. A non-positive `stale_ceiling_days` disables the ceiling
    cleanup entirely, independent of `retention_days`.
    """
    now = datetime.now(timezone.utc)
    retention_cutoff = now - timedelta(days=retention_days)
    ceiling_cutoff = now - timedelta(days=stale_ceiling_days)

    for table, sql, ceiling_sql in _CLEANUP_STATEMENTS:
        if retention_days > 0:
            try:
                db.execute(sql, [retention_cutoff])
            except Exception as e:
                logger.error(f"Retention cleanup failed for {table}: {e}")

        if stale_ceiling_days > 0 and ceiling_sql is not None:
            try:
                db.execute(ceiling_sql, [ceiling_cutoff])
            except Exception as e:
                logger.error(f"Stale-ceiling cleanup failed for {table}: {e}")
