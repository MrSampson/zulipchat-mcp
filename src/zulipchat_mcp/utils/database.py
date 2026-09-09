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
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from .migrations import run_migrations, sqlalchemy_url

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
        self._engine: Engine = create_engine(
            sqlalchemy_url(db_path),
            poolclass=NullPool,
            connect_args={"config": {"access_mode": "READ_WRITE"}},
        )

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

    def _run_migrations_with_retry(self) -> None:
        """Run migrations with retry logic for lock contention.

        Only OperationalErrors that actually look like lock contention get
        retried/relabeled as DatabaseLockedError. A genuine migration
        failure (a real DDL/schema bug, for instance) is also an
        OperationalError but must propagate as itself - mislabeling it as
        "Database is locked" would send debugging down the wrong path.
        """
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                run_migrations(self.db_path)
                return
            except OperationalError as e:
                # e.orig is the underlying duckdb exception's precise
                # message; str(e) also includes the SQL statement and a
                # sqlalche.me URL, which could coincidentally contain "lock".
                original = e.orig if e.orig is not None else e
                if "lock" not in str(original).lower():
                    raise
                last_error = e
                # No backoff here (unlike the branch below): a stale lock
                # from a dead process is already gone, so there's nothing
                # to wait out - retrying immediately is deliberate, not a
                # missing sleep. Same pattern in the other retry loops below.
                if self._try_clear_stale_lock(original):
                    continue
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (2**attempt))
                    continue
                raise DatabaseLockedError(self.db_path, e) from e

        if last_error:
            raise DatabaseLockedError(self.db_path, last_error)

    def _is_retriable_lock_error(self, error: OperationalError) -> BaseException:
        """Return the unwrapped original error if `error` looks like lock
        contention, else re-raise `error` unmangled (a genuine non-lock
        OperationalError, e.g. a real schema bug, must propagate as itself).
        """
        original = error.orig if error.orig is not None else error
        if "lock" not in str(original).lower():
            raise error
        return original

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
        with self._write_lock:  # Thread safety within process
            last_error: OperationalError | None = None
            for attempt in range(self.max_retries):
                try:
                    with self._engine.begin() as conn:
                        conn.exec_driver_sql(sql, tuple(params) if params else ())
                    return
                except OperationalError as e:
                    last_error = e
                    original = self._is_retriable_lock_error(e)
                    if self._try_clear_stale_lock(original):
                        continue
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay * (2**attempt))
                        continue
                    raise DatabaseLockedError(self.db_path, e) from e

            if last_error:
                raise DatabaseLockedError(self.db_path, last_error)

    def executemany(self, sql: str, seq_params: list[tuple[Any, ...]]) -> None:
        """Execute multiple write operations in a single transaction.

        Opens a connection via the engine, executes all statements, and
        releases the connection immediately.

        Args:
            sql: SQL statement to execute
            seq_params: Sequence of parameter tuples
        """
        with self._write_lock:
            last_error: OperationalError | None = None
            for attempt in range(self.max_retries):
                try:
                    with self._engine.begin() as conn:
                        for params in seq_params:
                            conn.exec_driver_sql(sql, params)
                    return
                except OperationalError as e:
                    last_error = e
                    original = self._is_retriable_lock_error(e)
                    if self._try_clear_stale_lock(original):
                        continue
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay * (2**attempt))
                        continue
                    raise DatabaseLockedError(self.db_path, e) from e

            if last_error:
                raise DatabaseLockedError(self.db_path, last_error)

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
        last_error: OperationalError | None = None
        for attempt in range(self.max_retries):
            try:
                with self._engine.connect() as conn:
                    cursor = conn.exec_driver_sql(sql, tuple(params) if params else ())
                    return [tuple(row) for row in cursor.fetchall()]
            except OperationalError as e:
                last_error = e
                original = self._is_retriable_lock_error(e)
                if self._try_clear_stale_lock(original):
                    continue
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (2**attempt))
                    continue
                raise DatabaseLockedError(self.db_path, e) from e

        if last_error:
            raise DatabaseLockedError(self.db_path, last_error)
        return []

    def query_one(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> tuple[Any, ...] | None:
        """Execute a read query and return the first result."""
        last_error: OperationalError | None = None
        for attempt in range(self.max_retries):
            try:
                with self._engine.connect() as conn:
                    cursor = conn.exec_driver_sql(sql, tuple(params) if params else ())
                    row = cursor.fetchone()
                    return tuple(row) if row is not None else None
            except OperationalError as e:
                last_error = e
                original = self._is_retriable_lock_error(e)
                if self._try_clear_stale_lock(original):
                    continue
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (2**attempt))
                    continue
                raise DatabaseLockedError(self.db_path, e) from e

        if last_error:
            raise DatabaseLockedError(self.db_path, last_error)
        return None

    def query_as_dicts(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a read query and return results as dictionaries."""
        last_error: OperationalError | None = None
        for attempt in range(self.max_retries):
            try:
                with self._engine.connect() as conn:
                    cursor = conn.exec_driver_sql(sql, tuple(params) if params else ())
                    return [dict(row) for row in cursor.mappings()]
            except OperationalError as e:
                last_error = e
                original = self._is_retriable_lock_error(e)
                if self._try_clear_stale_lock(original):
                    continue
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (2**attempt))
                    continue
                raise DatabaseLockedError(self.db_path, e) from e

        if last_error:
            raise DatabaseLockedError(self.db_path, last_error)
        return []

    def query_one_as_dict(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> dict[str, Any] | None:
        """Execute a read query and return the first result as a dictionary."""
        last_error: OperationalError | None = None
        for attempt in range(self.max_retries):
            try:
                with self._engine.connect() as conn:
                    cursor = conn.exec_driver_sql(sql, tuple(params) if params else ())
                    row = cursor.mappings().first()
                    return dict(row) if row is not None else None
            except OperationalError as e:
                last_error = e
                original = self._is_retriable_lock_error(e)
                if self._try_clear_stale_lock(original):
                    continue
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (2**attempt))
                    continue
                raise DatabaseLockedError(self.db_path, e) from e

        if last_error:
            raise DatabaseLockedError(self.db_path, last_error)
        return None

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
