"""Tests for utils/database_manager.py.

TestDatabaseManagerWrapper exercises the wrapper against a real, freshly-
migrated SqliteDatabaseManager (file-based, per-test tmp_path) rather than a
mocked `self._db` - a mock only proves the wrapper calls execute()/upsert()
with SQL that *looks* right; it can't catch a real constraint violation, a
column name typo caught only at execution time, or a SQL string that's
syntactically fine but semantically wrong (e.g. an UPDATE clause built from
**kwargs that doesn't match what get_* actually reads back). SQLite (not
duckdb/postgres) is enough here: this file is testing DatabaseManagerWrapper's
own logic, which is backend-agnostic by construction (it only ever calls
self._db's six generic methods) - the cross-backend behavior of those six
methods themselves is what tests/utils/test_database_backends.py covers.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from src.zulipchat_mcp.config import DatabaseBackend, DatabaseConfig
from src.zulipchat_mcp.utils.database import SqliteDatabaseManager, init_database
from src.zulipchat_mcp.utils.database_manager import DatabaseManager


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[DatabaseManager]:
    """A DatabaseManagerWrapper backed by a real, freshly-migrated sqlite
    file - fresh per test via tmp_path, matching the isolation the other
    DatabaseManager test files get from the same fixture.
    """
    SqliteDatabaseManager._instance = None
    with patch("src.zulipchat_mcp.utils.database._db_manager", None):
        init_database(
            DatabaseConfig(
                backend=DatabaseBackend.SQLITE, path=str(tmp_path / "test.sqlite3")
            )
        )
        yield DatabaseManager()
    SqliteDatabaseManager._instance = None


def _make_profile(manager: DatabaseManager, agent_id: str = "agent-1") -> None:
    manager.upsert_agent_profile(
        agent_id=agent_id,
        agent_name="claude",
        agent_type="claude-code",
        owner_email="owner@example.com",
        stream_name="Agents-Channel",
        topic_prefix="Agents/Session",
    )


def _make_session(
    manager: DatabaseManager, session_id: str = "sess-1", agent_id: str = "agent-1"
) -> None:
    manager.upsert_agent_session(
        session_id=session_id,
        agent_id=agent_id,
        stream_name="Agents-Channel",
        topic_name="Agents/Session/project/claude/cc-123",
        owner_email="owner@example.com",
        status="active",
    )


class TestDatabaseManagerWrapper:
    """Tests for the high-level DatabaseManager wrapper."""

    def test_init(self, manager: DatabaseManager) -> None:
        assert isinstance(manager._db, SqliteDatabaseManager)

    def test_upsert_agent_profile(self, manager: DatabaseManager) -> None:
        result = manager.upsert_agent_profile(
            agent_id="agent-1",
            agent_name="claude",
            agent_type="claude-code",
            owner_email="owner@example.com",
            stream_name="Agents-Channel",
            topic_prefix="Agents/Session",
        )

        assert result["status"] == "success"
        stored = manager.get_agent_profile("agent-1")
        assert stored["agent_id"] == "agent-1"
        assert stored["agent_name"] == "claude"

    def test_upsert_agent_profile_updates_existing_row_in_place(
        self, manager: DatabaseManager
    ) -> None:
        """Same invariant test_database_backends.py's upsert test pins for
        the raw DatabaseManager, one layer up: two upserts with the same
        agent_id replace the row instead of erroring or duplicating.
        """
        _make_profile(manager)

        manager.upsert_agent_profile(
            agent_id="agent-1",
            agent_name="claude-renamed",
            agent_type="claude-code",
            owner_email="owner@example.com",
            stream_name="Agents-Channel",
            topic_prefix="Agents/Session",
        )

        assert manager.get_agent_profile("agent-1")["agent_name"] == "claude-renamed"

    def test_get_agent_profile(self, manager: DatabaseManager) -> None:
        assert manager.get_agent_profile("does-not-exist") is None

        _make_profile(manager)

        result = manager.get_agent_profile("agent-1")
        assert result["agent_id"] == "agent-1"

    def test_upsert_agent_session(self, manager: DatabaseManager) -> None:
        _make_profile(manager)

        result = manager.upsert_agent_session(
            session_id="sess-1",
            agent_id="agent-1",
            external_session_id="cc-123",
            stream_name="Agents-Channel",
            topic_name="Agents/Session/project/claude/cc-123",
            owner_email="owner@example.com",
            project_name="project",
            project_dir="/tmp/project",
            host="localhost",
            status="active",
        )

        assert result["status"] == "success"
        stored = manager.get_agent_session("sess-1")
        assert stored["session_id"] == "sess-1"
        assert stored["agent_id"] == "agent-1"
        assert stored["status"] == "active"

    def test_get_agent_session(self, manager: DatabaseManager) -> None:
        assert manager.get_agent_session("does-not-exist") is None

        _make_profile(manager)
        _make_session(manager)

        result = manager.get_agent_session("sess-1")
        assert result["session_id"] == "sess-1"

    def test_create_agent_request(self, manager: DatabaseManager) -> None:
        _make_profile(manager)
        _make_session(manager)

        result = manager.create_agent_request(
            request_id="req-1",
            agent_id="agent-1",
            session_id="sess-1",
            request_type="approval",
            prompt="Deploy now?",
        )

        assert result["status"] == "success"
        stored = manager.get_agent_request("req-1")
        assert stored["prompt"] == "Deploy now?"
        assert stored["status"] == "pending"

    def test_get_agent_request(self, manager: DatabaseManager) -> None:
        assert manager.get_agent_request("does-not-exist") is None

        _make_profile(manager)
        _make_session(manager)
        manager.create_agent_request(
            request_id="req-1",
            agent_id="agent-1",
            session_id="sess-1",
            request_type="approval",
            prompt="Deploy now?",
        )

        result = manager.get_agent_request("req-1")
        assert result["request_id"] == "req-1"

    def test_update_agent_request(self, manager: DatabaseManager) -> None:
        _make_profile(manager)
        _make_session(manager)
        manager.create_agent_request(
            request_id="req-1",
            agent_id="agent-1",
            session_id="sess-1",
            request_type="approval",
            prompt="Deploy now?",
        )

        manager.update_agent_request("req-1", status="answered")

        assert manager.get_agent_request("req-1")["status"] == "answered"

    def test_update_agent_request_with_no_updates_is_a_no_op(
        self, manager: DatabaseManager
    ) -> None:
        """updates={} takes the early-return branch (no SET clause to
        build); this only matters for a real backend since the mocked
        version could never distinguish 'no-op' from 'ran a UPDATE with an
        empty SET clause', which is a SQL syntax error.
        """
        _make_profile(manager)
        _make_session(manager)
        manager.create_agent_request(
            request_id="req-1",
            agent_id="agent-1",
            session_id="sess-1",
            request_type="approval",
            prompt="Deploy now?",
        )

        result = manager.update_agent_request("req-1")

        assert result == {"status": "success"}
        assert manager.get_agent_request("req-1")["status"] == "pending"

    def test_create_session_event(self, manager: DatabaseManager) -> None:
        result = manager.create_session_event(
            event_id="evt-1",
            agent_id=None,
            session_id=None,
            stream_name="Agents-Channel",
            topic_name="Agents/Session/project/claude/cc-123",
            sender_email="owner@example.com",
            direction="inbound",
            event_type="steer",
            content="please continue",
        )

        assert result["status"] == "success"
        events = manager.get_unacked_session_events()
        assert [e["id"] for e in events] == ["evt-1"]
        assert events[0]["content"] == "please continue"

    def test_get_unacked_session_events(self, manager: DatabaseManager) -> None:
        assert manager.get_unacked_session_events() == []

        manager.create_session_event(
            event_id="evt-1",
            agent_id=None,
            session_id=None,
            direction="inbound",
            event_type="steer",
            content="hello",
        )

        events = manager.get_unacked_session_events(session_id="sess-1")
        assert events == []
        events = manager.get_unacked_session_events()
        assert len(events) == 1

    def test_ack_session_events(self, manager: DatabaseManager) -> None:
        manager.create_session_event(
            event_id="evt-1",
            agent_id=None,
            session_id=None,
            direction="inbound",
            event_type="steer",
            content="hello",
        )

        result = manager.ack_session_events(["evt-1"])

        assert result == {"status": "success"}
        assert manager.get_unacked_session_events() == []

    def test_ack_session_events_with_empty_list_is_a_no_op(
        self, manager: DatabaseManager
    ) -> None:
        """ids=[] takes the early-return branch, avoiding `IN ()` - invalid
        SQL on every backend, unlike update_agent_request's empty-SET case
        above which is sqlite/duckdb-tolerant but still wrong on Postgres.
        """
        result = manager.ack_session_events([])

        assert result == {"status": "success"}

    def test_create_agent_status(self, manager: DatabaseManager) -> None:
        result = manager.create_agent_status("s1", "claude-code", "working")

        assert result["status"] == "success"


def _sql_string_literals_in_source() -> list[str]:
    """Every string literal in database_manager.py that looks like a SQL
    statement (contains a DML keyword), found by parsing the module's own
    source with ast rather than importing it - this only needs the text of
    the literals, not the running module.
    """
    import ast
    import inspect

    from src.zulipchat_mcp.utils import database_manager

    source = inspect.getsource(database_manager)
    tree = ast.parse(source)
    keywords = ("SELECT", "INSERT", "UPDATE", "DELETE")
    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(kw in node.value for kw in keywords):
                literals.append(node.value)
    return literals


def test_no_sql_literal_contains_a_bare_percent_or_a_quoted_question_mark():
    """PostgresDatabaseManager._translate_sql() does a blanket
    sql.replace("?", "%s") on every SQL string in this file, since they're
    all written with duckdb/sqlite's native `?` qmark placeholders (see
    database.py). That textual replace is not SQL-aware: a literal `%` in
    the SQL text (not a bound parameter) would corrupt psycopg's pyformat
    parsing, and a literal `?` inside a quoted string value (e.g. a
    "'unknown?'" default) would be wrongly translated into "%s" too. This
    test pins the invariant _translate_sql's docstring already documents as
    manually verified, so a future SQL string that breaks it fails loudly
    here instead of only against a real Postgres connection.
    """
    literals = _sql_string_literals_in_source()
    assert literals, "expected to find at least one SQL literal to check"

    for sql in literals:
        assert "%" not in sql, f"bare '%' in SQL literal breaks psycopg: {sql!r}"
        # Any single-quoted string *value* embedded in the SQL text itself
        # (not a bound parameter) that contains '?' would be mistranslated.
        for quoted in re.findall(r"'[^']*'", sql):
            assert "?" not in quoted, f"'?' inside a quoted SQL literal: {sql!r}"
