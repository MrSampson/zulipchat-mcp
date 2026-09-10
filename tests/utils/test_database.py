"""Tests for utils/database.py - short-lived connection pattern."""

import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError, OperationalError, ProgrammingError
from sqlalchemy.pool import NullPool, QueuePool

from src.zulipchat_mcp.utils.database import (
    DatabaseLockedError,
    DuckDBDatabaseManager,
    PostgresDatabaseManager,
    SqliteDatabaseManager,
    get_database,
    init_database,
)
from src.zulipchat_mcp.utils.migrations import IN_MEMORY_DB_PATH


def _lock_error() -> OperationalError:
    """A fresh instance each call - exceptions accumulate traceback/context
    state when raised, so a single shared instance reused across many tests
    (some raising it more than once) would carry stale state between them.
    """
    return OperationalError(
        "stmt", None, Exception("IO Error: Could not set lock on file")
    )


def _non_lock_operational_error() -> OperationalError:
    return OperationalError(
        "stmt", None, Exception("Catalog Error: table already exists")
    )


def _mock_context_manager(return_value: MagicMock) -> MagicMock:
    """Build a MagicMock usable as a `with ... as x:` block yielding return_value."""
    cm = MagicMock()
    cm.__enter__.return_value = return_value
    cm.__exit__.return_value = False
    return cm


def _hold_lock(db_path: str) -> subprocess.Popen:
    """Start a subprocess that holds a real DuckDB write lock on db_path for
    1.5s, so a test can prove real cross-process lock-contention retry.
    """
    holder_script = (
        "import duckdb, time\n"
        f"conn = duckdb.connect({db_path!r})\n"
        'conn.execute("CREATE TABLE IF NOT EXISTS t(x INTEGER)")\n'
        "time.sleep(1.5)\n"
    )
    return subprocess.Popen([sys.executable, "-c", holder_script])


class TestDatabaseManager:
    """Tests for DuckDBDatabaseManager behavior specific to it: file-lock
    retry/stale-lock handling and short-lived-connection details. Shared
    backend-agnostic behavior (execute/query/upsert/etc.) lives in
    test_database_backends.py, parametrized across every backend.
    """

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        DuckDBDatabaseManager._instance = None
        # Also reset the global variable in the module
        with patch("src.zulipchat_mcp.utils.database._db_manager", None):
            yield
        DuckDBDatabaseManager._instance = None

    def test_init_success(self, tmp_path):
        """Test successful initialization runs Alembic migrations against a real
        DB, and that the engine is configured with NullPool so connections are
        genuinely short-lived (opened/closed per call) rather than pooled.
        """
        db_path = str(tmp_path / "test.db")
        db = DuckDBDatabaseManager(db_path)

        assert db._initialized is True
        assert db.db_path == db_path
        assert isinstance(db._engine.pool, NullPool)
        conn = duckdb.connect(db_path, read_only=True)
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        finally:
            conn.close()
        assert row == ("0001",)

    def test_init_lock_retry_success(self, tmp_path):
        """Test initialization retries on lock and succeeds."""
        db_path = str(tmp_path / "test.db")

        # Fail twice with a lock error (as run_migrations raises it - a
        # SQLAlchemy OperationalError, not a raw duckdb.IOException), then
        # succeed. _try_clear_stale_lock is forced to False so the retry
        # goes through the sleep-and-retry branch rather than the stale-PID
        # short-circuit, decoupling this test from real PID liveness.
        with (
            patch(
                "src.zulipchat_mcp.utils.migrations.run_migrations",
                side_effect=[_lock_error(), _lock_error(), None],
            ) as mock_run_migrations,
            patch.object(
                DuckDBDatabaseManager, "_try_clear_stale_lock", return_value=False
            ),
        ):
            db = DuckDBDatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        assert db._initialized is True
        assert mock_run_migrations.call_count == 3

    def test_init_lock_failure(self, tmp_path):
        """Test initialization raises DatabaseLockedError after retries."""
        db_path = str(tmp_path / "test.db")

        with (
            patch(
                "src.zulipchat_mcp.utils.migrations.run_migrations",
                side_effect=_lock_error(),
            ) as mock_run_migrations,
            patch.object(
                DuckDBDatabaseManager, "_try_clear_stale_lock", return_value=False
            ),
        ):
            with pytest.raises(DatabaseLockedError, match="Database is locked"):
                DuckDBDatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        assert mock_run_migrations.call_count == 3

    def test_init_stale_lock_cleared_then_succeeds(self, tmp_path):
        """Test the _try_clear_stale_lock == True branch: a lock held by a
        dead process gets cleared and the next attempt succeeds immediately,
        without going through the sleep-and-retry branch.
        """
        db_path = str(tmp_path / "test.db")

        with (
            patch(
                "src.zulipchat_mcp.utils.migrations.run_migrations",
                side_effect=[_lock_error(), None],
            ) as mock_run_migrations,
            patch.object(
                DuckDBDatabaseManager, "_try_clear_stale_lock", side_effect=[True]
            ) as mock_clear_stale_lock,
        ):
            db = DuckDBDatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        assert db._initialized is True
        assert mock_run_migrations.call_count == 2
        mock_clear_stale_lock.assert_called_once()

    def test_init_stale_lock_repeatedly_detected_still_exhausts_retries(self, tmp_path):
        """Test the post-loop DatabaseLockedError fallthrough: if
        _try_clear_stale_lock keeps returning True, the retry loop never
        takes the in-loop 'raise DatabaseLockedError' branch (only reached
        when it returns False) - it just keeps retrying immediately until
        max_retries is exhausted, falling through to the loop's own
        DatabaseLockedError after the for loop ends.
        """
        db_path = str(tmp_path / "test.db")

        with (
            patch(
                "src.zulipchat_mcp.utils.migrations.run_migrations",
                side_effect=_lock_error(),
            ) as mock_run_migrations,
            patch.object(
                DuckDBDatabaseManager, "_try_clear_stale_lock", return_value=True
            ),
        ):
            with pytest.raises(DatabaseLockedError, match="Database is locked"):
                DuckDBDatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        assert mock_run_migrations.call_count == 3

    def test_init_reraises_non_lock_operational_error_unmangled(self, tmp_path):
        """A genuine migration failure (e.g. a real DDL/schema bug) must
        propagate as itself, not get relabeled as DatabaseLockedError just
        because it happens to be a sqlalchemy.exc.OperationalError. Only
        lock contention should ever become a DatabaseLockedError.
        """
        db_path = str(tmp_path / "test.db")

        with patch(
            "src.zulipchat_mcp.utils.migrations.run_migrations",
            side_effect=_non_lock_operational_error(),
        ) as mock_run_migrations:
            with pytest.raises(OperationalError, match="table already exists"):
                DuckDBDatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        # Not a lock problem, so it must not have been retried.
        assert mock_run_migrations.call_count == 1

    @pytest.mark.slow
    def test_init_retries_through_real_cross_process_lock_contention(self, tmp_path):
        """Regression test using a real (unmocked) lock from another OS
        process, not the mocked run_migrations used above.

        _needs_legacy_stamp (utils/migrations.py) goes through the same
        SQLAlchemy/duckdb_engine connection as the rest of run_migrations,
        so lock contention there surfaces as the same retriable
        sqlalchemy.exc.OperationalError. This test proves that end-to-end
        against a genuinely locked file, not a mock: it previously caught a
        real bug where that check used a separate raw duckdb connection
        whose duckdb.IOException bypassed _run_migrations_with_retry's
        except clause entirely and crashed startup instead of retrying. A
        regression back to a raw connection there would fail this test the
        same way.

        The elapsed-time assertion proves contention actually happened -
        without it, this would pass identically if the holder never
        acquired the lock in time, silently decaying into a no-op test.
        """
        db_path = str(tmp_path / "contended.db")
        duckdb.connect(db_path).close()  # file must exist before contending

        holder = _hold_lock(db_path)
        try:
            time.sleep(0.3)  # let the holder acquire the lock first
            start = time.monotonic()
            db = DuckDBDatabaseManager(db_path, max_retries=10, retry_delay=0.2)
            assert db._initialized is True
            assert time.monotonic() - start > 0.5, "expected to block on the holder"
        finally:
            holder.terminate()
            holder.wait()

    @pytest.mark.slow
    def test_execute_retries_through_real_cross_process_lock_contention(self, tmp_path):
        """Same regression as the init test above, but for the shared
        _with_lock_retry path used by execute()/query()/etc. once the
        DuckDBDatabaseManager is already up - proves the runtime hot path (not
        just startup) survives genuine cross-process lock contention.
        """
        db_path = str(tmp_path / "contended.db")
        db = DuckDBDatabaseManager(db_path, max_retries=10, retry_delay=0.2)

        holder = _hold_lock(db_path)
        try:
            time.sleep(0.3)  # let the holder acquire the lock first
            start = time.monotonic()
            db.execute("CREATE TABLE runtime_t (x INTEGER)")  # must retry, not crash
            assert time.monotonic() - start > 0.5, "expected to block on the holder"
            assert db.query("SELECT x FROM runtime_t") == []
        finally:
            holder.terminate()
            holder.wait()

    def test_execute_propagates_error_and_leaves_db_usable(self, tmp_path):
        """A failing statement raises, and the connection is still released
        cleanly - later calls against the same DuckDBDatabaseManager still work.
        """
        db = DuckDBDatabaseManager(str(tmp_path / "test.db"))

        with pytest.raises(ProgrammingError):
            db.execute("INSERT INTO nonexistent_table VALUES (1)")

        db.execute("CREATE TABLE t (x INTEGER)")
        assert db.query("SELECT x FROM t") == []

    def test_execute_retries_on_lock_then_succeeds(self, tmp_path):
        """execute() retries when the engine reports lock contention."""
        db = DuckDBDatabaseManager(
            str(tmp_path / "test.db"), max_retries=3, retry_delay=0.01
        )

        success_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.begin.side_effect = [
            _lock_error(),
            _lock_error(),
            _mock_context_manager(success_conn),
        ]
        db._engine = mock_engine

        with patch.object(
            DuckDBDatabaseManager, "_try_clear_stale_lock", return_value=False
        ):
            db.execute("INSERT INTO t VALUES (?)", [1])

        assert mock_engine.begin.call_count == 3
        success_conn.exec_driver_sql.assert_called_once_with(
            "INSERT INTO t VALUES (?)", (1,)
        )

    def test_execute_raises_database_locked_error_after_exhausting_retries(
        self, tmp_path
    ):
        """execute() gives up and raises DatabaseLockedError after max_retries."""
        db = DuckDBDatabaseManager(
            str(tmp_path / "test.db"), max_retries=3, retry_delay=0.01
        )

        mock_engine = MagicMock()
        mock_engine.begin.side_effect = _lock_error()
        db._engine = mock_engine

        with patch.object(
            DuckDBDatabaseManager, "_try_clear_stale_lock", return_value=False
        ):
            with pytest.raises(DatabaseLockedError, match="Database is locked"):
                db.execute("INSERT INTO t VALUES (?)", [1])

        assert mock_engine.begin.call_count == 3

    def test_execute_reraises_non_lock_operational_error_unmangled(self, tmp_path):
        """A genuine non-lock OperationalError (e.g. a real schema bug) must
        propagate as itself, not get relabeled as DatabaseLockedError and not
        get retried - mirrors test_init_reraises_non_lock_operational_error_
        unmangled, but for the execute() retry loop instead of init's.
        """
        db = DuckDBDatabaseManager(
            str(tmp_path / "test.db"), max_retries=3, retry_delay=0.01
        )

        mock_engine = MagicMock()
        mock_engine.begin.side_effect = _non_lock_operational_error()
        db._engine = mock_engine

        with pytest.raises(OperationalError, match="table already exists"):
            db.execute("INSERT INTO t VALUES (?)", [1])

        assert mock_engine.begin.call_count == 1

    def test_query_reraises_non_lock_operational_error_unmangled(self, tmp_path):
        """Same invariant as above, for the query() retry loop."""
        db = DuckDBDatabaseManager(
            str(tmp_path / "test.db"), max_retries=3, retry_delay=0.01
        )

        mock_engine = MagicMock()
        mock_engine.connect.side_effect = _non_lock_operational_error()
        db._engine = mock_engine

        with pytest.raises(OperationalError, match="table already exists"):
            db.query("SELECT *")

        assert mock_engine.connect.call_count == 1

    def test_executemany_rolls_back_all_on_partial_failure(self, tmp_path):
        """A failure partway through executemany() rolls back the whole
        transaction - the first (successful) insert must not persist either.
        """
        db = DuckDBDatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (x INTEGER)")

        with pytest.raises(DataError):
            db.executemany("INSERT INTO t VALUES (?)", [(1,), ("not-an-int",)])

        assert db.query("SELECT x FROM t") == []

    def test_query_retries_on_lock_then_succeeds(self, tmp_path):
        """query() retries when the engine reports lock contention."""
        db = DuckDBDatabaseManager(
            str(tmp_path / "test.db"), max_retries=3, retry_delay=0.01
        )

        cursor = MagicMock()
        cursor.fetchall.return_value = [(1,)]
        success_conn = MagicMock()
        success_conn.exec_driver_sql.return_value = cursor
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = [
            _lock_error(),
            _lock_error(),
            _mock_context_manager(success_conn),
        ]
        db._engine = mock_engine

        with patch.object(
            DuckDBDatabaseManager, "_try_clear_stale_lock", return_value=False
        ):
            result = db.query("SELECT *")

        assert result == [(1,)]
        assert mock_engine.connect.call_count == 3

    def test_clear_stale_lock_removes_wal_when_holder_is_dead(
        self, tmp_path, monkeypatch
    ):
        """_try_clear_stale_lock is fully mocked out by every test above (the
        PID it must parse is unpredictable at record time) - test it
        directly instead, with no mocking of the class under test.
        """
        db = DuckDBDatabaseManager(str(tmp_path / "test.db"))
        wal_path = db.db_path + ".wal"
        with open(wal_path, "w"):
            pass

        def fake_kill(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(os, "kill", fake_kill)

        cleared = db._try_clear_stale_lock(
            Exception("Conflicting lock is held in /x (PID 424242)")
        )

        assert cleared is True
        assert not os.path.exists(wal_path)

    def test_clear_stale_lock_clears_when_holder_is_dead_and_no_wal_present(
        self, tmp_path, monkeypatch
    ):
        """The no-WAL-file branch: DuckDB can hold a lock with no .wal on
        disk, and that's still a stale-lock-cleared case, not a decline.
        """
        db = DuckDBDatabaseManager(str(tmp_path / "test.db"))
        assert not os.path.exists(db.db_path + ".wal")

        def fake_kill(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(os, "kill", fake_kill)

        cleared = db._try_clear_stale_lock(
            Exception("Conflicting lock is held in /x (PID 424242)")
        )

        assert cleared is True

    def test_clear_stale_lock_declines_when_holder_is_alive(self, tmp_path):
        db = DuckDBDatabaseManager(str(tmp_path / "test.db"))

        cleared = db._try_clear_stale_lock(
            Exception(f"Conflicting lock is held in /x (PID {os.getpid()})")
        )

        assert cleared is False

    def test_clear_stale_lock_declines_when_message_has_no_pid(self, tmp_path):
        db = DuckDBDatabaseManager(str(tmp_path / "test.db"))

        cleared = db._try_clear_stale_lock(Exception("IO Error: disk full"))

        assert cleared is False

    def test_global_instances(self):
        """Test global instance helpers."""
        from src.zulipchat_mcp.config import DatabaseBackend, DatabaseConfig

        db = init_database(
            DatabaseConfig(backend=DatabaseBackend.DUCKDB, path=IN_MEMORY_DB_PATH)
        )
        assert db is not None
        assert isinstance(db, DuckDBDatabaseManager)

        db2 = get_database()
        assert db2 is db

    def test_get_database_raises_before_init_database_called(self):
        with pytest.raises(RuntimeError, match="not initialized"):
            get_database()

    def test_make_engine_raises_actionable_error_when_duckdb_engine_missing(
        self, tmp_path, monkeypatch
    ):
        import sys

        monkeypatch.setitem(sys.modules, "duckdb_engine", None)

        with pytest.raises(RuntimeError, match=r"\[duckdb\]"):
            DuckDBDatabaseManager(str(tmp_path / "test.db"))


class TestSqliteDatabaseManager:
    """Tests for SqliteDatabaseManager behavior that's specific to it, or
    that pins the base class's default for a hook DuckDB overrides. Shared
    backend-agnostic behavior (execute/query/upsert/etc.) lives in
    test_database_backends.py, parametrized across every backend.
    """

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        SqliteDatabaseManager._instance = None
        with patch("src.zulipchat_mcp.utils.database._db_manager", None):
            yield
        SqliteDatabaseManager._instance = None

    def test_init_success(self, tmp_path):
        db_path = str(tmp_path / "test.sqlite3")
        db = SqliteDatabaseManager(db_path)

        assert db._initialized is True
        assert db.db_path == db_path
        assert isinstance(db._engine.pool, NullPool)
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        finally:
            conn.close()
        assert row == ("0001",)

    def test_try_clear_stale_lock_uses_base_class_default_of_false(self, tmp_path):
        """SqliteDatabaseManager doesn't override _try_clear_stale_lock (no
        PID in sqlite's lock error to parse) - it relies on the base
        class's default, which must always decline rather than silently
        skip the retry-with-backoff path.
        """
        db = SqliteDatabaseManager(str(tmp_path / "test.sqlite3"))

        assert db._try_clear_stale_lock(Exception("database is locked")) is False

    def test_init_database_dispatches_to_sqlite_manager(self):
        """init_database() is the sole production entry point for every
        backend - it must actually map DatabaseConfig.backend=SQLITE onto
        SqliteDatabaseManager, not just SqliteDatabaseManager's direct
        constructor (which every other test in this class uses).
        """
        from src.zulipchat_mcp.config import DatabaseBackend, DatabaseConfig

        db = init_database(
            DatabaseConfig(backend=DatabaseBackend.SQLITE, path=IN_MEMORY_DB_PATH)
        )

        assert isinstance(db, SqliteDatabaseManager)
        assert get_database() is db


class TestPostgresDatabaseManager:
    """Unit tests only - no real Postgres connection. Engine construction
    and SQL generation are tested directly; execute()/query() behavior is
    already covered generically by the base class tests on the other two
    backends, so this class focuses on what's actually different: engine
    URL/pool, placeholder translation, no lock retry, and upsert SQL shape.
    """

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        PostgresDatabaseManager._instance = None
        with patch("src.zulipchat_mcp.utils.database._db_manager", None):
            yield
        PostgresDatabaseManager._instance = None

    @pytest.fixture(autouse=True)
    def no_real_migrations(self):
        """Every test in this class constructs a real Engine (to prove the
        URL/pool are right) but must never actually run migrations against
        a real network connection.
        """
        with patch.object(PostgresDatabaseManager, "_run_migrations"):
            yield

    def test_make_engine_uses_queue_pool_and_correct_url(self):
        db = PostgresDatabaseManager(
            host="db.internal",
            port=6543,
            dbname="zulipchat",
            user="mcp",
            password="s3cret",
        )

        assert isinstance(db._engine.pool, QueuePool)
        assert db._engine.url.drivername == "postgresql+psycopg"
        assert db._engine.url.host == "db.internal"
        assert db._engine.url.port == 6543
        assert db._engine.url.database == "zulipchat"
        assert db._engine.url.username == "mcp"

    def test_db_path_display_string_excludes_password(self):
        db = PostgresDatabaseManager(
            host="db.internal",
            port=5432,
            dbname="zulipchat",
            user="mcp",
            password="s3cret",
        )

        assert "s3cret" not in db.db_path

    def test_init_database_dispatches_to_postgres_manager_with_correct_field_mapping(
        self,
    ):
        """init_database() is the sole production entry point for every
        backend, and it renames DatabaseConfig's postgres_* fields onto
        PostgresDatabaseManager's host/port/dbname/user/password
        constructor kwargs - a transposed keyword here (e.g. postgres_db
        landing on `user` instead of `dbname`) would ship green in every
        other test in this class, which all construct
        PostgresDatabaseManager directly with already-correct kwargs.
        """
        from src.zulipchat_mcp.config import DatabaseBackend, DatabaseConfig

        db = init_database(
            DatabaseConfig(
                backend=DatabaseBackend.POSTGRES,
                postgres_host="db.internal",
                postgres_port=6543,
                postgres_db="zulipchat",
                postgres_user="mcp",
                postgres_password="s3cret",
            )
        )

        assert isinstance(db, PostgresDatabaseManager)
        assert get_database() is db
        assert db._engine.url.host == "db.internal"
        assert db._engine.url.port == 6543
        assert db._engine.url.database == "zulipchat"
        assert db._engine.url.username == "mcp"
        assert db._engine.url.password == "s3cret"

    def test_translate_sql_converts_qmark_to_pyformat(self):
        db = PostgresDatabaseManager(
            host="h",
            port=5432,
            dbname="d",
            user="u",
            password="p",
        )

        assert db._translate_sql("SELECT * FROM t WHERE a = ? AND b = ?") == (
            "SELECT * FROM t WHERE a = %s AND b = %s"
        )

    def test_with_lock_retry_calls_operation_once_without_retrying(self):
        db = PostgresDatabaseManager(
            host="h",
            port=5432,
            dbname="d",
            user="u",
            password="p",
        )
        calls = []

        def _op():
            calls.append(1)
            return "ok"

        assert db._with_lock_retry(_op) == "ok"
        assert calls == [1]

    def test_make_engine_raises_actionable_error_when_psycopg_missing(
        self, monkeypatch
    ):
        import sys

        monkeypatch.setitem(sys.modules, "psycopg", None)

        with pytest.raises(RuntimeError, match=r"\[postgres\]"):
            PostgresDatabaseManager(
                host="h",
                port=5432,
                dbname="d",
                user="u",
                password="p",
            )

    def test_upsert_builds_on_conflict_do_update(self):
        db = PostgresDatabaseManager(
            host="h",
            port=5432,
            dbname="d",
            user="u",
            password="p",
        )
        executed = []
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = lambda stmt: executed.append(stmt)
        db._engine = MagicMock()
        db._engine.begin.return_value = _mock_context_manager(mock_conn)

        db.upsert(
            "agent_profiles",
            ["agent_id", "agent_name"],
            ["agent-1", "claude"],
            "agent_id",
        )

        compiled = str(
            executed[0].compile(
                dialect=__import__(
                    "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
                ).dialect()
            )
        )
        assert "ON CONFLICT" in compiled
        assert "agent_profiles" in compiled

    def test_upsert_strips_tzinfo_from_aware_datetimes(self):
        """Postgres casts an aware datetime to the session TimeZone when
        storing it into schema.py's naive `timestamp without time zone`
        columns, silently shifting the stored value. upsert() builds its
        statement directly (not via self.execute()), so it must apply the
        same central _normalize_params() stripping the other backends get.
        """
        db = PostgresDatabaseManager(
            host="h",
            port=5432,
            dbname="d",
            user="u",
            password="p",
        )
        executed = []
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = lambda stmt: executed.append(stmt)
        db._engine = MagicMock()
        db._engine.begin.return_value = _mock_context_manager(mock_conn)

        aware = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        db.upsert(
            "agent_profiles",
            ["agent_id", "created_at"],
            ["agent-1", aware],
            "agent_id",
        )

        params = executed[0].compile(dialect=postgresql.dialect()).params
        stored = params["created_at"]
        assert isinstance(stored, datetime)
        assert stored.tzinfo is None
        assert stored == aware.replace(tzinfo=None)

    def test_make_engine_parses_password_with_at_sign_correctly(self):
        """Naive f-string interpolation of `p@ss` into the URL reparses as
        host='ss@db.internal', password='p' - a silent misconnection.
        """
        db = PostgresDatabaseManager(
            host="db.internal",
            port=5432,
            dbname="zulipchat",
            user="mcp",
            password="p@ss:word/x",
        )

        assert db._engine.url.host == "db.internal"
        assert db._engine.url.password == "p@ss:word/x"
        assert db._engine.url.username == "mcp"
        assert db._engine.url.database == "zulipchat"
        assert db._engine.url.port == 5432


def test_postgres_manager_to_alembic_url_survives_special_characters(monkeypatch):
    """End-to-end across the database.py -> migrations.py seam that both
    critical URL bugs lived on: a password containing '@', ':', '/' and '%'
    must reach both the engine and the Alembic config intact, without
    raising, and must never appear in db_path (which DatabaseLockedError
    messages and logs surface verbatim).
    """
    from src.zulipchat_mcp.utils import migrations

    PostgresDatabaseManager._instance = None
    password = "p@ss%wo/rd:x"
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        migrations.command,
        "upgrade",
        lambda cfg, rev: captured.update(url=cfg.get_main_option("sqlalchemy.url")),
    )

    try:
        db = PostgresDatabaseManager(
            host="db.internal",
            port=5432,
            dbname="zulipchat",
            user="mcp",
            password=password,
        )

        assert db._engine.url.host == "db.internal"
        assert db._engine.url.password == password
        assert password not in db.db_path

        alembic_url = make_url(captured["url"])
        assert alembic_url.host == "db.internal"
        assert alembic_url.password == password
        assert alembic_url.database == "zulipchat"
    finally:
        PostgresDatabaseManager._instance = None
