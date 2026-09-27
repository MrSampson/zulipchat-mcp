"""Tests for utils/retention.py: deleting rows from the insert-only
event/request/task tables (agent_events, session_events, agent_requests,
user_input_requests, tasks) once they're older than a configured retention
window. Runs against the same cross-backend `db` fixture as
test_database_backends.py (duckdb/sqlite always, postgres when Docker is
available) since the delete rules must hold identically on every backend.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from src.zulipchat_mcp.utils.database import DatabaseManager
from src.zulipchat_mcp.utils.retention import run_retention_cleanup

OLD = datetime.now(timezone.utc) - timedelta(days=60)
RECENT = datetime.now(timezone.utc) - timedelta(days=1)
PAST_CEILING = datetime.now(timezone.utc) - timedelta(days=100)


def _insert_agent_event(
    db: DatabaseManager, event_id: str, created_at: datetime, acked: bool
) -> None:
    flag = "TRUE" if acked else "FALSE"
    db.execute(
        "INSERT INTO agent_events (id, topic, sender_email, content, created_at, acked) "
        f"VALUES (?, ?, ?, ?, ?, {flag})",
        [event_id, "topic", "a@b.com", "hi", created_at],
    )


def _insert_session_event(
    db: DatabaseManager, event_id: str, created_at: datetime, acked: bool
) -> None:
    flag = "TRUE" if acked else "FALSE"
    db.execute(
        "INSERT INTO session_events (id, direction, event_type, created_at, acked) "
        f"VALUES (?, ?, ?, ?, {flag})",
        [event_id, "inbound", "message", created_at],
    )


def _seed_agent_and_session(
    db: DatabaseManager, agent_id: str = "agent-1", session_id: str = "sess-1"
) -> None:
    """agent_requests.agent_id/session_id are NOT NULL foreign keys, so
    inserting a test row requires real parent rows first.
    """
    now = datetime.now(timezone.utc)
    db.execute(
        """
        INSERT INTO agent_profiles
        (agent_id, agent_name, agent_type, owner_email, stream_name, topic_prefix, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            agent_id,
            "claude",
            "claude-code",
            "owner@example.com",
            "Agents-Channel",
            "Agents/Session",
            now,
            now,
        ],
    )
    db.execute(
        """
        INSERT INTO agent_sessions
        (session_id, agent_id, stream_name, topic_name, owner_email, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            session_id,
            agent_id,
            "Agents-Channel",
            "Agents/Session/x",
            "owner@example.com",
            "active",
            now,
            now,
        ],
    )


def _insert_agent_request(
    db: DatabaseManager, request_id: str, created_at: datetime, status: str
) -> None:
    _seed_agent_and_session(db)
    db.execute(
        "INSERT INTO agent_requests "
        "(request_id, agent_id, session_id, request_type, prompt, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [request_id, "agent-1", "sess-1", "input", "question?", status, created_at],
    )


def _insert_user_input_request(
    db: DatabaseManager, request_id: str, created_at: datetime, status: str
) -> None:
    db.execute(
        "INSERT INTO user_input_requests (request_id, agent_id, question, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [request_id, "agent-1", "question?", status, created_at],
    )


def _insert_task(
    db: DatabaseManager,
    task_id: str,
    created_at: datetime,
    completed_at: datetime | None,
) -> None:
    if completed_at is None:
        db.execute(
            "INSERT INTO tasks (task_id, agent_id, name, status, started_at) "
            "VALUES (?, ?, ?, 'started', ?)",
            [task_id, "agent-1", "do thing", created_at],
        )
    else:
        db.execute(
            "INSERT INTO tasks (task_id, agent_id, name, status, started_at, completed_at) "
            "VALUES (?, ?, ?, 'completed', ?, ?)",
            [task_id, "agent-1", "do thing", created_at, completed_at],
        )


class TestRunRetentionCleanup:
    """agent_events has no reliable "processed" signal: the only code path
    that ever sets acked=TRUE (teleport_chat's wait-for-reply) only acks the
    one row it matched, out of the most recent unacked ones - most rows stay
    acked=FALSE forever regardless of age. So, unlike every other table
    here, agent_events is pruned by age alone, not gated on acked.
    """

    def test_deletes_acked_agent_event_older_than_window(
        self, db: DatabaseManager
    ) -> None:
        _insert_agent_event(db, "evt-1", OLD, acked=True)

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT id FROM agent_events") == []

    def test_deletes_unacked_agent_event_older_than_window(
        self, db: DatabaseManager
    ) -> None:
        _insert_agent_event(db, "evt-1", OLD, acked=False)

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT id FROM agent_events") == []

    def test_keeps_agent_event_within_window_regardless_of_acked(
        self, db: DatabaseManager
    ) -> None:
        _insert_agent_event(db, "evt-1", RECENT, acked=True)
        _insert_agent_event(db, "evt-2", RECENT, acked=False)

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT id FROM agent_events ORDER BY id") == [
            ("evt-1",),
            ("evt-2",),
        ]

    def test_deletes_acked_session_event_older_than_window(
        self, db: DatabaseManager
    ) -> None:
        _insert_session_event(db, "se-1", OLD, acked=True)

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT id FROM session_events") == []

    def test_keeps_unacked_session_event_regardless_of_age(
        self, db: DatabaseManager
    ) -> None:
        _insert_session_event(db, "se-1", OLD, acked=False)

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT id FROM session_events") == [("se-1",)]

    def test_keeps_acked_session_event_within_window(self, db: DatabaseManager) -> None:
        _insert_session_event(db, "se-1", RECENT, acked=True)

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT id FROM session_events") == [("se-1",)]

    def test_deletes_non_pending_agent_request_older_than_window(
        self, db: DatabaseManager
    ) -> None:
        _insert_agent_request(db, "req-1", OLD, status="timeout")

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT request_id FROM agent_requests") == []

    def test_keeps_pending_agent_request_regardless_of_age(
        self, db: DatabaseManager
    ) -> None:
        _insert_agent_request(db, "req-1", OLD, status="pending")

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT request_id FROM agent_requests") == [("req-1",)]

    def test_keeps_non_pending_agent_request_within_window(
        self, db: DatabaseManager
    ) -> None:
        _insert_agent_request(db, "req-1", RECENT, status="answered")

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT request_id FROM agent_requests") == [("req-1",)]

    def test_deletes_non_pending_user_input_request_older_than_window(
        self, db: DatabaseManager
    ) -> None:
        _insert_user_input_request(db, "uir-1", OLD, status="answered")

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT request_id FROM user_input_requests") == []

    def test_keeps_pending_user_input_request_regardless_of_age(
        self, db: DatabaseManager
    ) -> None:
        _insert_user_input_request(db, "uir-1", OLD, status="pending")

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT request_id FROM user_input_requests") == [("uir-1",)]

    def test_keeps_non_pending_user_input_request_within_window(
        self, db: DatabaseManager
    ) -> None:
        _insert_user_input_request(db, "uir-1", RECENT, status="answered")

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT request_id FROM user_input_requests") == [("uir-1",)]

    def test_deletes_completed_task_older_than_window(
        self, db: DatabaseManager
    ) -> None:
        _insert_task(db, "task-1", OLD, completed_at=OLD)

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT task_id FROM tasks") == []

    def test_keeps_incomplete_task_regardless_of_age(self, db: DatabaseManager) -> None:
        _insert_task(db, "task-1", OLD, completed_at=None)

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT task_id FROM tasks") == [("task-1",)]

    def test_keeps_completed_task_within_window(self, db: DatabaseManager) -> None:
        _insert_task(db, "task-1", RECENT, completed_at=RECENT)

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT task_id FROM tasks") == [("task-1",)]

    def test_zero_retention_days_disables_cleanup(self, db: DatabaseManager) -> None:
        _insert_agent_event(db, "evt-1", OLD, acked=True)

        run_retention_cleanup(db, retention_days=0)

        assert db.query("SELECT id FROM agent_events") == [("evt-1",)]

    def test_stale_ceiling_deletes_pending_agent_request_past_ceiling(
        self, db: DatabaseManager
    ) -> None:
        _insert_agent_request(db, "req-1", PAST_CEILING, status="pending")

        run_retention_cleanup(db, retention_days=30, stale_ceiling_days=90)

        assert db.query("SELECT request_id FROM agent_requests") == []

    def test_stale_ceiling_keeps_pending_agent_request_within_ceiling(
        self, db: DatabaseManager
    ) -> None:
        _insert_agent_request(db, "req-1", OLD, status="pending")

        run_retention_cleanup(db, retention_days=30, stale_ceiling_days=90)

        assert db.query("SELECT request_id FROM agent_requests") == [("req-1",)]

    def test_stale_ceiling_deletes_pending_user_input_request_past_ceiling(
        self, db: DatabaseManager
    ) -> None:
        _insert_user_input_request(db, "uir-1", PAST_CEILING, status="pending")

        run_retention_cleanup(db, retention_days=30, stale_ceiling_days=90)

        assert db.query("SELECT request_id FROM user_input_requests") == []

    def test_stale_ceiling_keeps_pending_user_input_request_within_ceiling(
        self, db: DatabaseManager
    ) -> None:
        _insert_user_input_request(db, "uir-1", OLD, status="pending")

        run_retention_cleanup(db, retention_days=30, stale_ceiling_days=90)

        assert db.query("SELECT request_id FROM user_input_requests") == [("uir-1",)]

    def test_stale_ceiling_deletes_unacked_session_event_past_ceiling(
        self, db: DatabaseManager
    ) -> None:
        _insert_session_event(db, "se-1", PAST_CEILING, acked=False)

        run_retention_cleanup(db, retention_days=30, stale_ceiling_days=90)

        assert db.query("SELECT id FROM session_events") == []

    def test_stale_ceiling_keeps_unacked_session_event_within_ceiling(
        self, db: DatabaseManager
    ) -> None:
        _insert_session_event(db, "se-1", OLD, acked=False)

        run_retention_cleanup(db, retention_days=30, stale_ceiling_days=90)

        assert db.query("SELECT id FROM session_events") == [("se-1",)]

    def test_stale_ceiling_deletes_incomplete_task_past_ceiling(
        self, db: DatabaseManager
    ) -> None:
        _insert_task(db, "task-1", PAST_CEILING, completed_at=None)

        run_retention_cleanup(db, retention_days=30, stale_ceiling_days=90)

        assert db.query("SELECT task_id FROM tasks") == []

    def test_stale_ceiling_keeps_incomplete_task_within_ceiling(
        self, db: DatabaseManager
    ) -> None:
        _insert_task(db, "task-1", OLD, completed_at=None)

        run_retention_cleanup(db, retention_days=30, stale_ceiling_days=90)

        assert db.query("SELECT task_id FROM tasks") == [("task-1",)]

    def test_zero_stale_ceiling_days_disables_ceiling(
        self, db: DatabaseManager
    ) -> None:
        _insert_agent_request(db, "req-1", PAST_CEILING, status="pending")

        run_retention_cleanup(db, retention_days=30, stale_ceiling_days=0)

        assert db.query("SELECT request_id FROM agent_requests") == [("req-1",)]

    def test_stale_ceiling_defaults_to_disabled(self, db: DatabaseManager) -> None:
        """Callers that don't pass stale_ceiling_days keep the pre-ceiling
        behavior of retaining non-terminal rows regardless of age."""
        _insert_agent_request(db, "req-1", PAST_CEILING, status="pending")

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT request_id FROM agent_requests") == [("req-1",)]

    def test_one_table_failure_does_not_block_cleanup_of_others(
        self, db: DatabaseManager
    ) -> None:
        """A DELETE failing for one table (e.g. future schema drift) must
        not stop the remaining tables' cleanup - this runs once at server
        startup, and one bad statement shouldn't block the rest.
        """
        _insert_agent_event(db, "evt-1", OLD, acked=True)
        _insert_user_input_request(db, "uir-1", OLD, status="answered")
        real_execute = db.execute

        def _flaky_execute(
            sql: str, params: list[Any] | tuple[Any, ...] | None = None
        ) -> None:
            if "agent_events" in sql:
                raise RuntimeError("simulated failure")
            real_execute(sql, params)

        with patch.object(db, "execute", side_effect=_flaky_execute):
            run_retention_cleanup(db, retention_days=30)

        # agent_events delete raised and was skipped - row still present
        assert db.query("SELECT id FROM agent_events") == [("evt-1",)]
        # user_input_requests delete still ran despite the earlier failure
        assert db.query("SELECT request_id FROM user_input_requests") == []
