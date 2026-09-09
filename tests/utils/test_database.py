"""Tests for utils/database.py - short-lived connection pattern."""

import subprocess
import sys
import time
from unittest.mock import MagicMock, call, patch

import duckdb
import pytest
from sqlalchemy.exc import OperationalError

from src.zulipchat_mcp.utils.database import (
    DatabaseLockedError,
    DatabaseManager,
    get_database,
    init_database,
)


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

    @pytest.fixture
    def mock_duckdb(self):
        with patch("src.zulipchat_mcp.utils.database.duckdb") as mock:
            conn = MagicMock()
            mock.connect.return_value = conn
            mock.IOException = duckdb.IOException
            yield mock

    def test_init_success(self, tmp_path):
        """Test successful initialization runs Alembic migrations against a real DB."""
        db_path = str(tmp_path / "test.db")
        db = DatabaseManager(db_path)

        assert db._initialized is True
        assert db.db_path == db_path
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
        lock_error = OperationalError(
            "stmt", None, Exception("IO Error: Could not set lock on file")
        )

        with (
            patch(
                "src.zulipchat_mcp.utils.database.run_migrations",
                side_effect=[lock_error, lock_error, None],
            ) as mock_run_migrations,
            patch.object(DatabaseManager, "_try_clear_stale_lock", return_value=False),
        ):
            db = DatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        assert db._initialized is True
        assert mock_run_migrations.call_count == 3

    def test_init_lock_failure(self, tmp_path):
        """Test initialization raises DatabaseLockedError after retries."""
        db_path = str(tmp_path / "test.db")
        lock_error = OperationalError(
            "stmt", None, Exception("IO Error: Could not set lock on file")
        )

        with (
            patch(
                "src.zulipchat_mcp.utils.database.run_migrations",
                side_effect=lock_error,
            ) as mock_run_migrations,
            patch.object(DatabaseManager, "_try_clear_stale_lock", return_value=False),
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
        schema_error = OperationalError(
            "stmt", None, Exception("Catalog Error: table already exists")
        )

        with patch(
            "src.zulipchat_mcp.utils.database.run_migrations",
            side_effect=schema_error,
        ) as mock_run_migrations:
            with pytest.raises(OperationalError, match="table already exists"):
                DatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        # Not a lock problem, so it must not have been retried.
        assert mock_run_migrations.call_count == 1

    @pytest.mark.slow
    def test_init_retries_through_real_cross_process_lock_contention(self, tmp_path):
        """Regression test for a real (unmocked) lock, not the mocked
        run_migrations used above.

        _needs_legacy_stamp (utils/migrations.py) opens its own raw
        duckdb.connect() to check for the old schema_migrations table,
        separately from the SQLAlchemy engine Alembic itself uses. If that
        raw connection ever raises duckdb.IOException instead of the
        sqlalchemy.exc.OperationalError the rest of run_migrations raises,
        it must still be caught here - _run_migrations_with_retry existing
        to retry lock contention is the whole point of this test.
        """
        db_path = str(tmp_path / "contended.db")
        duckdb.connect(db_path).close()  # file must exist before contending

        holder_script = (
            "import duckdb, time\n"
            f"conn = duckdb.connect({db_path!r})\n"
            'conn.execute("CREATE TABLE IF NOT EXISTS t(x INTEGER)")\n'
            "time.sleep(1.5)\n"
        )
        holder = subprocess.Popen([sys.executable, "-c", holder_script])
        try:
            time.sleep(0.3)  # let the holder acquire the lock first
            db = DatabaseManager(db_path, max_retries=10, retry_delay=0.2)
            assert db._initialized is True
        finally:
            holder.terminate()
            holder.wait()

    def test_execute_opens_closes_connection(self, mock_duckdb, tmp_path):
        """Test execute opens and closes connection for each operation."""
        db_path = str(tmp_path / "test.db")
        db = DatabaseManager(db_path)

        # Reset mock to clear init calls
        mock_duckdb.connect.reset_mock()
        conn = MagicMock()
        mock_duckdb.connect.return_value = conn

        db.execute("INSERT INTO t VALUES (?)", [1])

        # Verify connection opened
        mock_duckdb.connect.assert_called_once()
        # Verify transaction calls
        calls = conn.execute.call_args_list
        assert call("BEGIN") in calls
        assert call("INSERT INTO t VALUES (?)", [1]) in calls
        assert call("COMMIT") in calls
        # Verify connection closed
        conn.close.assert_called_once()

    def test_execute_retry_on_lock(self, mock_duckdb, tmp_path):
        """Test execute retries on lock contention."""
        db_path = str(tmp_path / "test.db")
        db = DatabaseManager(db_path, max_retries=3, retry_delay=0.01)

        mock_duckdb.connect.reset_mock()

        # First two connections fail with lock, third succeeds
        lock_error = duckdb.IOException("IO Error: lock")
        conn_success = MagicMock()
        mock_duckdb.connect.side_effect = [lock_error, lock_error, conn_success]

        db.execute("INSERT", [1])

        assert mock_duckdb.connect.call_count == 3
        conn_success.close.assert_called_once()

    def test_execute_rollback_on_error(self, mock_duckdb, tmp_path):
        """Test execute rolls back on error."""
        db_path = str(tmp_path / "test.db")
        db = DatabaseManager(db_path)

        mock_duckdb.connect.reset_mock()
        conn = MagicMock()
        mock_duckdb.connect.return_value = conn

        # Simulate error on the INSERT
        conn.execute.side_effect = [
            None,  # BEGIN
            Exception("Fail"),  # INSERT fails
            None,  # ROLLBACK
        ]

        with pytest.raises(Exception, match="Fail"):
            db.execute("INSERT", [1])

        # Verify rollback was called
        assert call("ROLLBACK") in conn.execute.call_args_list
        conn.close.assert_called()

    def test_executemany(self, mock_duckdb, tmp_path):
        """Test executemany uses short-lived connection."""
        db_path = str(tmp_path / "test.db")
        db = DatabaseManager(db_path)

        mock_duckdb.connect.reset_mock()
        conn = MagicMock()
        mock_duckdb.connect.return_value = conn

        db.executemany("INSERT", [(1,), (2,)])

        calls = conn.execute.call_args_list
        assert call("BEGIN") in calls
        assert call("INSERT", (1,)) in calls
        assert call("INSERT", (2,)) in calls
        assert call("COMMIT") in calls
        conn.close.assert_called_once()

    def test_query(self, mock_duckdb, tmp_path):
        """Test query uses short-lived connection."""
        db_path = str(tmp_path / "test.db")
        db = DatabaseManager(db_path)

        mock_duckdb.connect.reset_mock()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(1,)]
        conn.execute.return_value = cursor
        mock_duckdb.connect.return_value = conn

        res = db.query("SELECT *")

        assert res == [(1,)]
        conn.close.assert_called_once()

    def test_query_one(self, mock_duckdb, tmp_path):
        """Test query_one uses short-lived connection."""
        db_path = str(tmp_path / "test.db")
        db = DatabaseManager(db_path)

        mock_duckdb.connect.reset_mock()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)
        conn.execute.return_value = cursor
        mock_duckdb.connect.return_value = conn

        res = db.query_one("SELECT *")

        assert res == (1,)
        conn.close.assert_called_once()

    def test_query_as_dicts(self, mock_duckdb, tmp_path):
        """Test query_as_dicts returns list of dicts."""
        db_path = str(tmp_path / "test.db")
        db = DatabaseManager(db_path)

        mock_duckdb.connect.reset_mock()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(1, "a"), (2, "b")]
        cursor.description = [("id",), ("name",)]
        conn.execute.return_value = cursor
        mock_duckdb.connect.return_value = conn

        res = db.query_as_dicts("SELECT *")

        assert res == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        conn.close.assert_called_once()

    def test_query_one_as_dict(self, mock_duckdb, tmp_path):
        """Test query_one_as_dict returns single dict."""
        db_path = str(tmp_path / "test.db")
        db = DatabaseManager(db_path)

        mock_duckdb.connect.reset_mock()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (1, "a")
        cursor.description = [("id",), ("name",)]
        conn.execute.return_value = cursor
        mock_duckdb.connect.return_value = conn

        res = db.query_one_as_dict("SELECT *")

        assert res == {"id": 1, "name": "a"}
        conn.close.assert_called_once()

    def test_close_is_noop(self, mock_duckdb, tmp_path):
        """Test close is a no-op since connections are short-lived."""
        db_path = str(tmp_path / "test.db")
        db = DatabaseManager(db_path)

        # close should not raise and should be a no-op
        db.close()
        assert db._initialized is True

    def test_global_instances(self, mock_duckdb):
        """Test global instance helpers."""
        db = init_database(":memory:")
        assert db is not None

        db2 = get_database()
        assert db2 is db

        # Verify calling DatabaseManager() directly also returns the same instance
        db3 = DatabaseManager(":memory:")
        assert db3 is db
