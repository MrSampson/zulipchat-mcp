"""Backend-agnostic DatabaseManager behavior, run against duckdb, sqlite, and
a real Postgres (via the `db` fixture in tests/utils/conftest.py).

These assertions used to be hand-duplicated per backend in test_database.py
(TestDatabaseManager / TestSqliteDatabaseManager), with Postgres covered only
by mocks (TestPostgresDatabaseManager). This file is the single source of
truth for behavior every backend must share; genuinely backend-specific
behavior (DuckDB's file-lock retry, Postgres's engine/URL construction and
SQL translation) stays in test_database.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.zulipchat_mcp.utils.database import DatabaseManager, SqliteDatabaseManager


class TestDatabaseManagerAcrossBackends:
    def test_init_runs_migrations_to_head(self, db: DatabaseManager) -> None:
        assert db._initialized is True
        assert db.query_one("SELECT version_num FROM alembic_version") is not None

    def test_execute_creates_row(self, db: DatabaseManager) -> None:
        db.execute("CREATE TABLE t (x INTEGER)")

        db.execute("INSERT INTO t VALUES (?)", [1])

        assert db.query("SELECT x FROM t") == [(1,)]

    def test_executemany_inserts_all_rows(self, db: DatabaseManager) -> None:
        db.execute("CREATE TABLE t (x INTEGER)")

        db.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])

        assert db.query("SELECT x FROM t ORDER BY x") == [(1,), (2,)]

    def test_executemany_accepts_list_shaped_rows(self, db: DatabaseManager) -> None:
        db.execute("CREATE TABLE t (x INTEGER)")

        db.executemany("INSERT INTO t VALUES (?)", [[1], [2]])

        assert db.query("SELECT x FROM t ORDER BY x") == [(1,), (2,)]

    def test_query_returns_rows_as_tuples(self, db: DatabaseManager) -> None:
        db.execute("CREATE TABLE t (id INTEGER, name VARCHAR)")
        db.execute("INSERT INTO t VALUES (?, ?)", [1, "a"])

        assert db.query("SELECT id, name FROM t") == [(1, "a")]

    def test_query_returns_empty_list_when_no_rows(self, db: DatabaseManager) -> None:
        db.execute("CREATE TABLE t (id INTEGER)")

        assert db.query("SELECT id FROM t") == []

    def test_query_one_returns_single_tuple(self, db: DatabaseManager) -> None:
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("INSERT INTO t VALUES (?)", [1])

        assert db.query_one("SELECT id FROM t") == (1,)

    def test_query_one_returns_none_when_no_rows(self, db: DatabaseManager) -> None:
        db.execute("CREATE TABLE t (id INTEGER)")

        assert db.query_one("SELECT id FROM t") is None

    def test_query_as_dicts_returns_list_of_dicts(self, db: DatabaseManager) -> None:
        db.execute("CREATE TABLE t (id INTEGER, name VARCHAR)")
        db.executemany("INSERT INTO t VALUES (?, ?)", [(1, "a"), (2, "b")])

        result = db.query_as_dicts("SELECT id, name FROM t ORDER BY id")

        assert result == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]

    def test_query_as_dicts_returns_empty_list_when_no_rows(
        self, db: DatabaseManager
    ) -> None:
        db.execute("CREATE TABLE t (id INTEGER)")

        assert db.query_as_dicts("SELECT id FROM t") == []

    def test_query_one_as_dict_returns_dict(self, db: DatabaseManager) -> None:
        db.execute("CREATE TABLE t (id INTEGER, name VARCHAR)")
        db.execute("INSERT INTO t VALUES (?, ?)", [1, "a"])

        assert db.query_one_as_dict("SELECT id, name FROM t") == {"id": 1, "name": "a"}

    def test_query_one_as_dict_returns_none_when_no_rows(
        self, db: DatabaseManager
    ) -> None:
        db.execute("CREATE TABLE t (id INTEGER)")

        assert db.query_one_as_dict("SELECT id FROM t") is None

    def test_execute_strips_tzinfo_from_aware_datetime_params(
        self, db: DatabaseManager
    ) -> None:
        """duckdb and postgres deserialize TIMESTAMP columns back into real
        datetime objects; sqlite3's driver returns the raw stored string
        (no type adapter registered), so this only checks the
        datetime-typed backends. sqlite's write-path stripping is covered
        indirectly: it stores whatever _normalize_params() produces, and
        callers that round-trip the value get back exactly what was stored.
        """
        if isinstance(db, SqliteDatabaseManager):
            pytest.skip(
                "sqlite3 returns TIMESTAMP columns as raw strings, not datetime"
            )

        db.execute("CREATE TABLE t (ts TIMESTAMP)")
        aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        db.execute("INSERT INTO t VALUES (?)", [aware])

        stored = db.query_one("SELECT ts FROM t")[0]
        assert stored.tzinfo is None
        assert stored == aware.replace(tzinfo=None)

    def test_close_disposes_engine_without_error(self, db: DatabaseManager) -> None:
        """close() disposes the engine, but the manager stays usable
        afterward - dispose() only clears idle pooled connections.
        """
        db.close()

        assert db._initialized is True
        db.execute("CREATE TABLE t (x INTEGER)")
        assert db.query("SELECT x FROM t") == []

    def test_upsert_replaces_row_with_same_conflict_column_instead_of_duplicating(
        self, db: DatabaseManager
    ) -> None:
        """Uses agent_profiles (a real schema.py table) rather than an ad-hoc
        one: PostgresDatabaseManager.upsert() builds its statement from
        schema.py's SQLAlchemy metadata and only knows about declared tables,
        unlike the raw-SQL upsert() the file-based backends use.
        """
        columns = [
            "agent_id",
            "agent_name",
            "agent_type",
            "owner_email",
            "stream_name",
            "topic_prefix",
            "created_at",
            "updated_at",
        ]
        now = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
        db.upsert(
            "agent_profiles",
            columns,
            [
                "agent-1",
                "original-name",
                "claude-code",
                "owner@example.com",
                "Agents-Channel",
                "Agents/Session",
                now,
                now,
            ],
            "agent_id",
        )
        db.upsert(
            "agent_profiles",
            columns,
            [
                "agent-1",
                "updated-name",
                "claude-code",
                "owner@example.com",
                "Agents-Channel",
                "Agents/Session",
                now,
                now,
            ],
            "agent_id",
        )

        rows = db.query(
            "SELECT agent_id, agent_name FROM agent_profiles WHERE agent_id = ?",
            ["agent-1"],
        )
        assert rows == [("agent-1", "updated-name")]
