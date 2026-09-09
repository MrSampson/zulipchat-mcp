"""Tests for utils/database.py - short-lived connection pattern."""

import os
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from sqlalchemy.exc import DataError, OperationalError, ProgrammingError
from sqlalchemy.pool import NullPool

from src.zulipchat_mcp.utils.database import (
    DatabaseLockedError,
    DatabaseManager,
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
    """Tests for DatabaseManager with short-lived connections."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        DatabaseManager._instance = None
        # Also reset the global variable in the module
        with patch("src.zulipchat_mcp.utils.database._db_manager", None):
            yield
        DatabaseManager._instance = None

    def test_init_success(self, tmp_path):
        """Test successful initialization runs Alembic migrations against a real
        DB, and that the engine is configured with NullPool so connections are
        genuinely short-lived (opened/closed per call) rather than pooled.
        """
        db_path = str(tmp_path / "test.db")
        db = DatabaseManager(db_path)

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
                "src.zulipchat_mcp.utils.database.run_migrations",
                side_effect=[_lock_error(), _lock_error(), None],
            ) as mock_run_migrations,
            patch.object(DatabaseManager, "_try_clear_stale_lock", return_value=False),
        ):
            db = DatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        assert db._initialized is True
        assert mock_run_migrations.call_count == 3

    def test_init_lock_failure(self, tmp_path):
        """Test initialization raises DatabaseLockedError after retries."""
        db_path = str(tmp_path / "test.db")

        with (
            patch(
                "src.zulipchat_mcp.utils.database.run_migrations",
                side_effect=_lock_error(),
            ) as mock_run_migrations,
            patch.object(DatabaseManager, "_try_clear_stale_lock", return_value=False),
        ):
            with pytest.raises(DatabaseLockedError, match="Database is locked"):
                DatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        assert mock_run_migrations.call_count == 3

    def test_init_stale_lock_cleared_then_succeeds(self, tmp_path):
        """Test the _try_clear_stale_lock == True branch: a lock held by a
        dead process gets cleared and the next attempt succeeds immediately,
        without going through the sleep-and-retry branch.
        """
        db_path = str(tmp_path / "test.db")

        with (
            patch(
                "src.zulipchat_mcp.utils.database.run_migrations",
                side_effect=[_lock_error(), None],
            ) as mock_run_migrations,
            patch.object(
                DatabaseManager, "_try_clear_stale_lock", side_effect=[True]
            ) as mock_clear_stale_lock,
        ):
            db = DatabaseManager(db_path, max_retries=3, retry_delay=0.01)

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
                "src.zulipchat_mcp.utils.database.run_migrations",
                side_effect=_lock_error(),
            ) as mock_run_migrations,
            patch.object(DatabaseManager, "_try_clear_stale_lock", return_value=True),
        ):
            with pytest.raises(DatabaseLockedError, match="Database is locked"):
                DatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        assert mock_run_migrations.call_count == 3

    def test_init_reraises_non_lock_operational_error_unmangled(self, tmp_path):
        """A genuine migration failure (e.g. a real DDL/schema bug) must
        propagate as itself, not get relabeled as DatabaseLockedError just
        because it happens to be a sqlalchemy.exc.OperationalError. Only
        lock contention should ever become a DatabaseLockedError.
        """
        db_path = str(tmp_path / "test.db")

        with patch(
            "src.zulipchat_mcp.utils.database.run_migrations",
            side_effect=_non_lock_operational_error(),
        ) as mock_run_migrations:
            with pytest.raises(OperationalError, match="table already exists"):
                DatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        # Not a lock problem, so it must not have been retried.
        assert mock_run_migrations.call_count == 1

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
        """
        db_path = str(tmp_path / "contended.db")
        duckdb.connect(db_path).close()  # file must exist before contending

        holder = _hold_lock(db_path)
        try:
            time.sleep(0.3)  # let the holder acquire the lock first
            db = DatabaseManager(db_path, max_retries=10, retry_delay=0.2)
            assert db._initialized is True
        finally:
            holder.terminate()
            holder.wait()

    def test_execute_retries_through_real_cross_process_lock_contention(self, tmp_path):
        """Same regression as the init test above, but for the shared
        _with_lock_retry path used by execute()/query()/etc. once the
        DatabaseManager is already up - proves the runtime hot path (not
        just startup) survives genuine cross-process lock contention.
        """
        db_path = str(tmp_path / "contended.db")
        db = DatabaseManager(db_path, max_retries=10, retry_delay=0.2)

        holder = _hold_lock(db_path)
        try:
            time.sleep(0.3)  # let the holder acquire the lock first
            db.execute("CREATE TABLE runtime_t (x INTEGER)")  # must retry, not crash
            assert db.query("SELECT x FROM runtime_t") == []
        finally:
            holder.terminate()
            holder.wait()

    def test_execute_creates_row(self, tmp_path):
        """execute() runs a real write and commits it."""
        db = DatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (x INTEGER)")

        db.execute("INSERT INTO t VALUES (?)", [1])

        assert db.query("SELECT x FROM t") == [(1,)]

    def test_execute_propagates_error_and_leaves_db_usable(self, tmp_path):
        """A failing statement raises, and the connection is still released
        cleanly - later calls against the same DatabaseManager still work.
        """
        db = DatabaseManager(str(tmp_path / "test.db"))

        with pytest.raises(ProgrammingError):
            db.execute("INSERT INTO nonexistent_table VALUES (1)")

        db.execute("CREATE TABLE t (x INTEGER)")
        assert db.query("SELECT x FROM t") == []

    def test_execute_retries_on_lock_then_succeeds(self, tmp_path):
        """execute() retries when the engine reports lock contention."""
        db = DatabaseManager(str(tmp_path / "test.db"), max_retries=3, retry_delay=0.01)

        success_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.begin.side_effect = [
            _lock_error(),
            _lock_error(),
            _mock_context_manager(success_conn),
        ]
        db._engine = mock_engine

        with patch.object(DatabaseManager, "_try_clear_stale_lock", return_value=False):
            db.execute("INSERT INTO t VALUES (?)", [1])

        assert mock_engine.begin.call_count == 3
        success_conn.exec_driver_sql.assert_called_once_with(
            "INSERT INTO t VALUES (?)", (1,)
        )

    def test_execute_raises_database_locked_error_after_exhausting_retries(
        self, tmp_path
    ):
        """execute() gives up and raises DatabaseLockedError after max_retries."""
        db = DatabaseManager(str(tmp_path / "test.db"), max_retries=3, retry_delay=0.01)

        mock_engine = MagicMock()
        mock_engine.begin.side_effect = _lock_error()
        db._engine = mock_engine

        with patch.object(DatabaseManager, "_try_clear_stale_lock", return_value=False):
            with pytest.raises(DatabaseLockedError, match="Database is locked"):
                db.execute("INSERT INTO t VALUES (?)", [1])

        assert mock_engine.begin.call_count == 3

    def test_execute_reraises_non_lock_operational_error_unmangled(self, tmp_path):
        """A genuine non-lock OperationalError (e.g. a real schema bug) must
        propagate as itself, not get relabeled as DatabaseLockedError and not
        get retried - mirrors test_init_reraises_non_lock_operational_error_
        unmangled, but for the execute() retry loop instead of init's.
        """
        db = DatabaseManager(str(tmp_path / "test.db"), max_retries=3, retry_delay=0.01)

        mock_engine = MagicMock()
        mock_engine.begin.side_effect = _non_lock_operational_error()
        db._engine = mock_engine

        with pytest.raises(OperationalError, match="table already exists"):
            db.execute("INSERT INTO t VALUES (?)", [1])

        assert mock_engine.begin.call_count == 1

    def test_query_reraises_non_lock_operational_error_unmangled(self, tmp_path):
        """Same invariant as above, for the query() retry loop."""
        db = DatabaseManager(str(tmp_path / "test.db"), max_retries=3, retry_delay=0.01)

        mock_engine = MagicMock()
        mock_engine.connect.side_effect = _non_lock_operational_error()
        db._engine = mock_engine

        with pytest.raises(OperationalError, match="table already exists"):
            db.query("SELECT *")

        assert mock_engine.connect.call_count == 1

    def test_executemany_inserts_all_rows(self, tmp_path):
        """executemany() writes every row in a single transaction."""
        db = DatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (x INTEGER)")

        db.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])

        assert db.query("SELECT x FROM t ORDER BY x") == [(1,), (2,)]

    def test_executemany_rolls_back_all_on_partial_failure(self, tmp_path):
        """A failure partway through executemany() rolls back the whole
        transaction - the first (successful) insert must not persist either.
        """
        db = DatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (x INTEGER)")

        with pytest.raises(DataError):
            db.executemany("INSERT INTO t VALUES (?)", [(1,), ("not-an-int",)])

        assert db.query("SELECT x FROM t") == []

    def test_query_returns_rows_as_tuples(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (id INTEGER, name VARCHAR)")
        db.execute("INSERT INTO t VALUES (?, ?)", [1, "a"])

        assert db.query("SELECT id, name FROM t") == [(1, "a")]

    def test_query_returns_empty_list_when_no_rows(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (id INTEGER)")

        assert db.query("SELECT id FROM t") == []

    def test_query_retries_on_lock_then_succeeds(self, tmp_path):
        """query() retries when the engine reports lock contention."""
        db = DatabaseManager(str(tmp_path / "test.db"), max_retries=3, retry_delay=0.01)

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

        with patch.object(DatabaseManager, "_try_clear_stale_lock", return_value=False):
            result = db.query("SELECT *")

        assert result == [(1,)]
        assert mock_engine.connect.call_count == 3

    def test_query_one_returns_single_tuple(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("INSERT INTO t VALUES (?)", [1])

        assert db.query_one("SELECT id FROM t") == (1,)

    def test_query_one_returns_none_when_no_rows(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (id INTEGER)")

        assert db.query_one("SELECT id FROM t") is None

    def test_query_as_dicts_returns_list_of_dicts(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (id INTEGER, name VARCHAR)")
        db.executemany("INSERT INTO t VALUES (?, ?)", [(1, "a"), (2, "b")])

        result = db.query_as_dicts("SELECT id, name FROM t ORDER BY id")

        assert result == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]

    def test_query_as_dicts_returns_empty_list_when_no_rows(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (id INTEGER)")

        assert db.query_as_dicts("SELECT id FROM t") == []

    def test_query_one_as_dict_returns_dict(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (id INTEGER, name VARCHAR)")
        db.execute("INSERT INTO t VALUES (?, ?)", [1, "a"])

        assert db.query_one_as_dict("SELECT id, name FROM t") == {"id": 1, "name": "a"}

    def test_query_one_as_dict_returns_none_when_no_rows(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (id INTEGER)")

        assert db.query_one_as_dict("SELECT id FROM t") is None

    def test_close_disposes_engine_without_error(self, tmp_path):
        """close() disposes the engine. Under NullPool there's nothing
        pooled to discard, but the engine stays usable afterward - dispose()
        only clears idle pooled connections, it doesn't tear down the engine.
        """
        db = DatabaseManager(str(tmp_path / "test.db"))

        db.close()

        assert db._initialized is True
        db.execute("CREATE TABLE t (x INTEGER)")
        assert db.query("SELECT x FROM t") == []

    def test_clear_stale_lock_removes_wal_when_holder_is_dead(
        self, tmp_path, monkeypatch
    ):
        """_try_clear_stale_lock is fully mocked out by every test above (the
        PID it must parse is unpredictable at record time) - test it
        directly instead, with no mocking of the class under test.
        """
        db = DatabaseManager(str(tmp_path / "test.db"))
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

    def test_clear_stale_lock_declines_when_holder_is_alive(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "test.db"))

        cleared = db._try_clear_stale_lock(
            Exception(f"Conflicting lock is held in /x (PID {os.getpid()})")
        )

        assert cleared is False

    def test_clear_stale_lock_declines_when_message_has_no_pid(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "test.db"))

        cleared = db._try_clear_stale_lock(Exception("IO Error: disk full"))

        assert cleared is False

    def test_global_instances(self):
        """Test global instance helpers."""
        db = init_database(IN_MEMORY_DB_PATH)
        assert db is not None

        db2 = get_database()
        assert db2 is db

        # Verify calling DatabaseManager() directly also returns the same instance
        db3 = DatabaseManager(IN_MEMORY_DB_PATH)
        assert db3 is db
