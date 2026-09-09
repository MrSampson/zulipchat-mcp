"""DuckDB-backed persistence for ZulipChat MCP state and cache.

Uses short-lived connections for write operations to allow concurrent access
from multiple MCP server instances. Each write operation opens a connection,
executes, and closes it immediately to release the file lock.

Connections are opened through a SQLAlchemy Engine (duckdb_engine) configured
with NullPool, so the engine/pool machinery is what actually manages
connect/disconnect - NullPool just means every checkout is a fresh
connection, preserving the short-lived-connection behavior above.
"""

import logging
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from .migrations import make_engine, run_migrations

T = TypeVar("T")

logger = logging.getLogger(__name__)


class DatabaseLockedError(Exception):
    """Raised when the database is locked by another process after retries."""

    def __init__(self, db_path: str, original_error: Exception):
        self.db_path = db_path
        self.original_error = original_error
        super().__init__(
            f"Database is locked after max retries: {db_path}. "
            f"Original error: {original_error}"
        )


class DatabaseManager:
    """DuckDB database manager for ZulipChat MCP.

    Uses short-lived connections for write operations to support concurrent
    access from multiple MCP server instances. Write operations acquire the
    lock, execute, and release immediately.
    """

    _instance = None

    def __new__(cls, *args: Any, **kwargs: Any) -> "DatabaseManager":
        """Ensure singleton instance within a single process."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self, db_path: str, max_retries: int = 5, retry_delay: float = 0.1
    ) -> None:
        """Initialize database manager.

        Args:
            db_path: Path to the DuckDB database file
            max_retries: Maximum number of retry attempts on lock contention
            retry_delay: Base delay between retries (uses exponential backoff)
        """
        # Skip initialization if already initialized
        if hasattr(self, "_initialized") and self._initialized:
            return

        self.db_path = db_path
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._write_lock = threading.RLock()  # Thread safety within process
        self._initialized: bool = False
        self._engine: Engine = make_engine(db_path)

        # run_migrations() creates db_path's parent directory itself.
        self._run_migrations_with_retry()
        self._initialized = True

    def _try_clear_stale_lock(self, error: BaseException) -> bool:
        """Check if the lock is held by a dead process and clear it if so.

        DuckDB error messages include the locking PID, e.g.:
        'Conflicting lock is held in ... (PID 12345)'

        Returns True if a stale lock was cleared and the caller should retry.
        """
        match = re.search(r"\(PID\s+(\d+)\)", str(error))
        if not match:
            return False

        pid = int(match.group(1))
        try:
            os.kill(pid, 0)  # signal 0 checks if process exists
            # Process is alive; lock is legitimate
            return False
        except ProcessLookupError:
            pass  # PID doesn't exist
        except PermissionError:
            # Process exists but we can't signal it; lock is legitimate
            return False

        # Stale lock: the locking process is dead
        wal_path = self.db_path + ".wal"
        if os.path.exists(wal_path):
            try:
                os.remove(wal_path)
                logger.warning(
                    f"Removed stale WAL file from dead process (PID {pid}): {wal_path}"
                )
            except OSError as rm_err:
                logger.error(f"Failed to remove stale WAL file: {rm_err}")
                return False
        else:
            logger.info(f"Locking process (PID {pid}) is dead; retrying connect")
        return True

    def _unwrap_lock_error_or_raise(self, error: OperationalError) -> BaseException:
        """Return the unwrapped original error if `error` looks like lock
        contention, else re-raise `error` unmangled (a genuine non-lock
        OperationalError, e.g. a real schema bug, must propagate as itself).

        `.orig` is the underlying duckdb exception's precise message;
        `str(error)` also includes the SQL statement and a sqlalche.me URL,
        which could coincidentally contain "lock".
        """
        original = error.orig if error.orig is not None else error
        if "lock" not in str(original).lower():
            raise error
        return original

    def _with_lock_retry(self, operation: Callable[[], T]) -> T:
        """Run `operation`, retrying only on DuckDB lock contention.

        Shared by every short-lived-connection call (execute/query/...) and
        by migration startup. A genuine non-lock OperationalError propagates
        as itself. Lock contention retries with exponential backoff, except
        when a stale lock from a dead process was cleared - that retries
        immediately since there's nothing left to wait out.
        """
        last_error: OperationalError | None = None
        for attempt in range(self.max_retries):
            try:
                return operation()
            except OperationalError as e:
                last_error = e
                original = self._unwrap_lock_error_or_raise(e)
                if self._try_clear_stale_lock(original):
                    continue
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (2**attempt))
                    continue
                raise DatabaseLockedError(self.db_path, e) from e

        raise DatabaseLockedError(
            self.db_path, last_error or RuntimeError("max_retries must be >= 1")
        )

    def _run_migrations_with_retry(self) -> None:
        """Run migrations with retry logic for lock contention."""
        self._with_lock_retry(lambda: run_migrations(self.db_path))

    def execute(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> None:
        """Execute a single write operation with short-lived connection.

        Opens a connection via the engine, executes the statement in a
        transaction, and releases the connection immediately (NullPool) to
        release the file lock.

        Args:
            sql: SQL statement to execute
            params: Parameters for the SQL statement
        """

        def _op() -> None:
            with self._engine.begin() as conn:
                conn.exec_driver_sql(sql, tuple(params) if params else ())

        with self._write_lock:  # Thread safety within process
            self._with_lock_retry(_op)

    def executemany(
        self, sql: str, seq_params: Sequence[list[Any] | tuple[Any, ...]]
    ) -> None:
        """Execute multiple write operations in a single transaction.

        Opens a connection via the engine, executes all statements, and
        releases the connection immediately.

        Args:
            sql: SQL statement to execute
            seq_params: One parameter sequence per row - list or tuple,
                mirroring execute()'s single-row params (each row is coerced
                to a tuple before reaching the driver, since SQLAlchemy's
                parameter distiller rejects a bare list there)
        """

        def _op() -> None:
            with self._engine.begin() as conn:
                for params in seq_params:
                    conn.exec_driver_sql(sql, tuple(params))

        with self._write_lock:
            self._with_lock_retry(_op)

    def query(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> list[tuple[Any, ...]]:
        """Execute a read query and return results.

        Uses short-lived connection with retry for lock contention.

        Args:
            sql: SQL query to execute
            params: Parameters for the SQL query

        Returns:
            List of result tuples
        """

        def _op() -> list[tuple[Any, ...]]:
            with self._engine.connect() as conn:
                cursor = conn.exec_driver_sql(sql, tuple(params) if params else ())
                return [tuple(row) for row in cursor.fetchall()]

        return self._with_lock_retry(_op)

    def query_one(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> tuple[Any, ...] | None:
        """Execute a read query and return the first result."""

        def _op() -> tuple[Any, ...] | None:
            with self._engine.connect() as conn:
                cursor = conn.exec_driver_sql(sql, tuple(params) if params else ())
                row = cursor.fetchone()
                return tuple(row) if row is not None else None

        return self._with_lock_retry(_op)

    def query_as_dicts(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a read query and return results as dictionaries."""

        def _op() -> list[dict[str, Any]]:
            with self._engine.connect() as conn:
                cursor = conn.exec_driver_sql(sql, tuple(params) if params else ())
                return [dict(row) for row in cursor.mappings()]

        return self._with_lock_retry(_op)

    def query_one_as_dict(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> dict[str, Any] | None:
        """Execute a read query and return the first result as a dictionary."""

        def _op() -> dict[str, Any] | None:
            with self._engine.connect() as conn:
                cursor = conn.exec_driver_sql(sql, tuple(params) if params else ())
                row = cursor.mappings().first()
                return dict(row) if row is not None else None

        return self._with_lock_retry(_op)

    def close(self) -> None:
        """Dispose the engine's connection pool.

        Safe to call even though connections are short-lived: NullPool has
        nothing pooled to discard, and the engine stays usable afterward -
        dispose() only clears idle pooled connections, it doesn't tear down
        the engine.
        """
        self._engine.dispose()

    def __del__(self) -> None:
        """Cleanup (no-op, connections are short-lived)."""
        pass


# Global database manager instance
_db_manager: DatabaseManager | None = None


def get_database() -> DatabaseManager:
    """Get or create the global database manager instance.

    Returns:
        Global DatabaseManager instance
    """
    global _db_manager
    if _db_manager is None:
        db_path = os.getenv("ZULIPCHAT_DB_PATH", ".mcp/zulipchat/zulipchat.duckdb")
        _db_manager = DatabaseManager(db_path)
    return _db_manager


def init_database(db_path: str | None = None) -> DatabaseManager:
    """Initialize the global database manager with a specific path.

    Args:
        db_path: Path to the database file, uses default if None

    Returns:
        Initialized DatabaseManager instance
    """
    global _db_manager
    if db_path is None:
        db_path = os.getenv("ZULIPCHAT_DB_PATH", ".mcp/zulipchat/zulipchat.duckdb")
    _db_manager = DatabaseManager(db_path)
    return _db_manager
