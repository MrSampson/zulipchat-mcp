"""Tests for utils/database_manager.py."""

import re
from unittest.mock import MagicMock, patch

import pytest

from src.zulipchat_mcp.utils.database_manager import DatabaseManager


class TestDatabaseManagerWrapper:
    """Tests for the high-level DatabaseManager wrapper."""

    @pytest.fixture
    def mock_db(self):
        with patch("src.zulipchat_mcp.utils.database_manager.get_database") as mock_get:
            db_instance = MagicMock()
            mock_get.return_value = db_instance
            yield db_instance

    def test_init(self, mock_db):
        manager = DatabaseManager()
        assert manager._db == mock_db

    def test_upsert_agent_profile(self, mock_db):
        manager = DatabaseManager()
        mock_db.query_one_as_dict.return_value = None

        result = manager.upsert_agent_profile(
            agent_id="agent-1",
            agent_name="claude",
            agent_type="claude-code",
            owner_email="owner@example.com",
            stream_name="Agents-Channel",
            topic_prefix="Agents/Session",
        )

        assert result["status"] == "success"
        table, columns, values, conflict_column = mock_db.upsert.call_args[0]
        assert table == "agent_profiles"
        assert conflict_column == "agent_id"
        assert dict(zip(columns, values, strict=True))["agent_id"] == "agent-1"

    def test_get_agent_profile(self, mock_db):
        manager = DatabaseManager()
        mock_db.query_one_as_dict.return_value = {"agent_id": "agent-1"}
        result = manager.get_agent_profile("agent-1")
        assert result["agent_id"] == "agent-1"

    def test_upsert_agent_session(self, mock_db):
        manager = DatabaseManager()
        mock_db.query_one_as_dict.return_value = None

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
        table, columns, values, conflict_column = mock_db.upsert.call_args[0]
        assert table == "agent_sessions"
        assert conflict_column == "session_id"
        assert dict(zip(columns, values, strict=True))["session_id"] == "sess-1"

    def test_get_agent_session(self, mock_db):
        manager = DatabaseManager()
        mock_db.query_one_as_dict.return_value = {"session_id": "sess-1"}
        result = manager.get_agent_session("sess-1")
        assert result["session_id"] == "sess-1"

    def test_create_agent_request(self, mock_db):
        manager = DatabaseManager()
        result = manager.create_agent_request(
            request_id="req-1",
            agent_id="agent-1",
            session_id="sess-1",
            request_type="approval",
            prompt="Deploy now?",
        )
        assert result["status"] == "success"
        sql = mock_db.execute.call_args[0][0]
        assert "INSERT INTO agent_requests" in sql

    def test_get_agent_request(self, mock_db):
        manager = DatabaseManager()
        mock_db.query_one_as_dict.return_value = {"request_id": "req-1"}
        result = manager.get_agent_request("req-1")
        assert result["request_id"] == "req-1"

    def test_update_agent_request(self, mock_db):
        manager = DatabaseManager()
        manager.update_agent_request("req-1", status="answered")
        sql = mock_db.execute.call_args[0][0]
        assert "UPDATE agent_requests" in sql

    def test_create_session_event(self, mock_db):
        manager = DatabaseManager()
        result = manager.create_session_event(
            event_id="evt-1",
            agent_id="agent-1",
            session_id="sess-1",
            stream_name="Agents-Channel",
            topic_name="Agents/Session/project/claude/cc-123",
            sender_email="owner@example.com",
            direction="inbound",
            event_type="steer",
            content="please continue",
        )
        assert result["status"] == "success"
        sql = mock_db.execute.call_args[0][0]
        assert "INSERT INTO session_events" in sql

    def test_get_unacked_session_events(self, mock_db):
        manager = DatabaseManager()
        mock_db.query_as_dicts.return_value = [{"id": "evt-1"}]
        events = manager.get_unacked_session_events(session_id="sess-1")
        assert events == [{"id": "evt-1"}]

    def test_ack_session_events(self, mock_db):
        manager = DatabaseManager()
        manager.ack_session_events(["evt-1"])
        sql = mock_db.execute.call_args[0][0]
        assert "UPDATE session_events SET acked = TRUE" in sql

    def test_create_agent_status(self, mock_db):
        manager = DatabaseManager()
        manager.create_agent_status("s1", "claude-code", "working")
        mock_db.execute.assert_called()


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
