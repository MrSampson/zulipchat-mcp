"""Tests for utils/retention.py: deleting terminal-state rows from the
insert-only event/request tables (agent_events, session_events,
agent_requests, user_input_requests, tasks) once they're older than a
configured retention window. Runs against the same cross-backend `db`
fixture as test_database_backends.py (duckdb/sqlite always, postgres when
Docker is available) since the delete rules must hold identically on every
backend.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.zulipchat_mcp.utils.database import DatabaseManager
from src.zulipchat_mcp.utils.retention import run_retention_cleanup

OLD = datetime.now(timezone.utc) - timedelta(days=60)
RECENT = datetime.now(timezone.utc) - timedelta(days=1)


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


class TestRunRetentionCleanup:
    def test_deletes_acked_agent_event_older_than_window(
        self, db: DatabaseManager
    ) -> None:
        db.execute(
            "INSERT INTO agent_events (id, topic, sender_email, content, created_at, acked) "
            "VALUES (?, ?, ?, ?, ?, TRUE)",
            ["evt-1", "topic", "a@b.com", "hi", OLD],
        )

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT id FROM agent_events") == []

    def test_keeps_unacked_agent_event_regardless_of_age(
        self, db: DatabaseManager
    ) -> None:
        db.execute(
            "INSERT INTO agent_events (id, topic, sender_email, content, created_at, acked) "
            "VALUES (?, ?, ?, ?, ?, FALSE)",
            ["evt-1", "topic", "a@b.com", "hi", OLD],
        )

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT id FROM agent_events") == [("evt-1",)]

    def test_keeps_acked_agent_event_within_window(self, db: DatabaseManager) -> None:
        db.execute(
            "INSERT INTO agent_events (id, topic, sender_email, content, created_at, acked) "
            "VALUES (?, ?, ?, ?, ?, TRUE)",
            ["evt-1", "topic", "a@b.com", "hi", RECENT],
        )

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT id FROM agent_events") == [("evt-1",)]

    def test_deletes_acked_session_event_older_than_window(
        self, db: DatabaseManager
    ) -> None:
        db.execute(
            "INSERT INTO session_events (id, direction, event_type, created_at, acked) "
            "VALUES (?, ?, ?, ?, TRUE)",
            ["se-1", "inbound", "message", OLD],
        )

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT id FROM session_events") == []

    def test_keeps_unacked_session_event_regardless_of_age(
        self, db: DatabaseManager
    ) -> None:
        db.execute(
            "INSERT INTO session_events (id, direction, event_type, created_at, acked) "
            "VALUES (?, ?, ?, ?, FALSE)",
            ["se-1", "inbound", "message", OLD],
        )

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT id FROM session_events") == [("se-1",)]

    def test_deletes_non_pending_agent_request_older_than_window(
        self, db: DatabaseManager
    ) -> None:
        _seed_agent_and_session(db)
        db.execute(
            "INSERT INTO agent_requests "
            "(request_id, agent_id, session_id, request_type, prompt, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'timeout', ?)",
            ["req-1", "agent-1", "sess-1", "input", "question?", OLD],
        )

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT request_id FROM agent_requests") == []

    def test_keeps_pending_agent_request_regardless_of_age(
        self, db: DatabaseManager
    ) -> None:
        _seed_agent_and_session(db)
        db.execute(
            "INSERT INTO agent_requests "
            "(request_id, agent_id, session_id, request_type, prompt, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            ["req-1", "agent-1", "sess-1", "input", "question?", OLD],
        )

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT request_id FROM agent_requests") == [("req-1",)]

    def test_deletes_non_pending_user_input_request_older_than_window(
        self, db: DatabaseManager
    ) -> None:
        db.execute(
            "INSERT INTO user_input_requests (request_id, agent_id, question, status, created_at) "
            "VALUES (?, ?, ?, 'answered', ?)",
            ["uir-1", "agent-1", "question?", OLD],
        )

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT request_id FROM user_input_requests") == []

    def test_keeps_pending_user_input_request_regardless_of_age(
        self, db: DatabaseManager
    ) -> None:
        db.execute(
            "INSERT INTO user_input_requests (request_id, agent_id, question, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            ["uir-1", "agent-1", "question?", OLD],
        )

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT request_id FROM user_input_requests") == [("uir-1",)]

    def test_deletes_completed_task_older_than_window(
        self, db: DatabaseManager
    ) -> None:
        db.execute(
            "INSERT INTO tasks (task_id, agent_id, name, status, started_at, completed_at) "
            "VALUES (?, ?, ?, 'completed', ?, ?)",
            ["task-1", "agent-1", "do thing", OLD, OLD],
        )

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT task_id FROM tasks") == []

    def test_keeps_incomplete_task_regardless_of_age(self, db: DatabaseManager) -> None:
        db.execute(
            "INSERT INTO tasks (task_id, agent_id, name, status, started_at) "
            "VALUES (?, ?, ?, 'started', ?)",
            ["task-1", "agent-1", "do thing", OLD],
        )

        run_retention_cleanup(db, retention_days=30)

        assert db.query("SELECT task_id FROM tasks") == [("task-1",)]

    def test_zero_retention_days_disables_cleanup(self, db: DatabaseManager) -> None:
        db.execute(
            "INSERT INTO agent_events (id, topic, sender_email, content, created_at, acked) "
            "VALUES (?, ?, ?, ?, ?, TRUE)",
            ["evt-1", "topic", "a@b.com", "hi", OLD],
        )

        run_retention_cleanup(db, retention_days=0)

        assert db.query("SELECT id FROM agent_events") == [("evt-1",)]

    def test_one_table_failure_does_not_block_cleanup_of_others(
        self, db: DatabaseManager
    ) -> None:
        """A DELETE failing for one table (e.g. future schema drift) must
        not stop the remaining tables' cleanup - this runs once at server
        startup, and one bad statement shouldn't block the rest.
        """
        db.execute(
            "INSERT INTO agent_events (id, topic, sender_email, content, created_at, acked) "
            "VALUES (?, ?, ?, ?, ?, TRUE)",
            ["evt-1", "topic", "a@b.com", "hi", OLD],
        )
        db.execute(
            "INSERT INTO user_input_requests (request_id, agent_id, question, status, created_at) "
            "VALUES (?, ?, ?, 'answered', ?)",
            ["uir-1", "agent-1", "question?", OLD],
        )
        real_execute = db.execute

        def _flaky_execute(sql, params=None):
            if "agent_events" in sql:
                raise RuntimeError("simulated failure")
            return real_execute(sql, params)

        with patch.object(db, "execute", side_effect=_flaky_execute):
            run_retention_cleanup(db, retention_days=30)

        # agent_events delete raised and was skipped - row still present
        assert db.query("SELECT id FROM agent_events") == [("evt-1",)]
        # user_input_requests delete still ran despite the earlier failure
        assert db.query("SELECT request_id FROM user_input_requests") == []
