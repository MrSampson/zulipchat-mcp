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
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

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


class DatabaseManager(ABC):
    """Abstract base for backend-specific persistence managers.

    Owns the singleton lifecycle, the six query/execute methods, and the
    lock-retry loop shared by every file-based backend. Subclasses provide
    the engine/pool, the migration entry point, and native upsert SQL.
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
            db_path: Backend-specific connection identifier (file path for
                sqlite/duckdb; a redacted display string for postgres).
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
        self._engine: Engine = self._make_engine(db_path)

        self._run_migrations_with_retry()
        self._initialized = True

    @abstractmethod
    def _make_engine(self, db_path: str) -> Engine:
        """Build this backend's SQLAlchemy Engine."""

    @abstractmethod
    def _run_migrations(self) -> None:
        """Run this backend's Alembic migrations against self.db_path."""

    @abstractmethod
    def upsert(
        self,
        table: str,
        columns: Sequence[str],
        values: Sequence[Any],
        conflict_column: str,
    ) -> None:
        """Insert `values` into `table`, replacing the row on conflict."""

    def _translate_sql(self, sql: str) -> str:
        """Translate `?`-style positional placeholders for this backend's
        driver paramstyle. Identity by default - qmark-native drivers
        (duckdb, sqlite3) need no translation; Postgres overrides this.
        """
        return sql

    def _try_clear_stale_lock(self, error: BaseException) -> bool:
        """Check if the lock is held by a dead process and clear it if so.

        Default: never clears (no PID information to parse). DuckDB
        overrides this with its PID-parsing implementation.
        """
        return False

    def _unwrap_lock_error_or_raise(self, error: OperationalError) -> BaseException:
        """Return the unwrapped original error if `error` looks like lock
        contention, else re-raise `error` unmangled (a genuine non-lock
        OperationalError, e.g. a real schema bug, must propagate as itself).

        `.orig` is the underlying driver exception's precise message;
        `str(error)` also includes the SQL statement and a sqlalche.me URL,
        which could coincidentally contain "lock".
        """
        original = error.orig if error.orig is not None else error
        if "lock" not in str(original).lower():
            raise error
        return original

    def _with_lock_retry(self, operation: Callable[[], T]) -> T:
        """Run `operation`, retrying only on lock contention.

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
        self._with_lock_retry(self._run_migrations)

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
        sql = self._translate_sql(sql)

        def _op() -> None:
            with self._engine.begin() as conn:
                conn.exec_driver_sql(sql, _normalize_params(params) if params else ())

        with self._write_lock:  # Thread safety within process
            self._with_lock_retry(_op)

    def executemany(
        self, sql: str, seq_params: Sequence[list[Any] | tuple[Any, ...]]
    ) -> None:
        """Execute multiple write operations in a single transaction.

        Args:
            sql: SQL statement to execute
            seq_params: One parameter sequence per row - list or tuple,
                mirroring execute()'s single-row params (each row is coerced
                to a tuple before reaching the driver, since SQLAlchemy's
                parameter distiller rejects a bare list there)
        """
        sql = self._translate_sql(sql)

        def _op() -> None:
            with self._engine.begin() as conn:
                for params in seq_params:
                    conn.exec_driver_sql(sql, _normalize_params(params))

        with self._write_lock:
            self._with_lock_retry(_op)

    def query(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> list[tuple[Any, ...]]:
        """Execute a read query and return results."""
        sql = self._translate_sql(sql)

        def _op() -> list[tuple[Any, ...]]:
            with self._engine.connect() as conn:
                cursor = conn.exec_driver_sql(
                    sql, _normalize_params(params) if params else ()
                )
                return [tuple(row) for row in cursor.fetchall()]

        return self._with_lock_retry(_op)

    def query_one(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> tuple[Any, ...] | None:
        """Execute a read query and return the first result."""
        sql = self._translate_sql(sql)

        def _op() -> tuple[Any, ...] | None:
            with self._engine.connect() as conn:
                cursor = conn.exec_driver_sql(
                    sql, _normalize_params(params) if params else ()
                )
                row = cursor.fetchone()
                return tuple(row) if row is not None else None

        return self._with_lock_retry(_op)

    def query_as_dicts(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a read query and return results as dictionaries."""
        sql = self._translate_sql(sql)

        def _op() -> list[dict[str, Any]]:
            with self._engine.connect() as conn:
                cursor = conn.exec_driver_sql(
                    sql, _normalize_params(params) if params else ()
                )
                return [dict(row) for row in cursor.mappings()]

        return self._with_lock_retry(_op)

    def query_one_as_dict(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> dict[str, Any] | None:
        """Execute a read query and return the first result as a dictionary."""
        sql = self._translate_sql(sql)

        def _op() -> dict[str, Any] | None:
            with self._engine.connect() as conn:
                cursor = conn.exec_driver_sql(
                    sql, _normalize_params(params) if params else ()
                )
                row = cursor.mappings().first()
                return dict(row) if row is not None else None

        return self._with_lock_retry(_op)

    def close(self) -> None:
        """Dispose the engine's connection pool."""
        self._engine.dispose()

    def __del__(self) -> None:  # noqa: B027 - shared no-op default, not abstract
        """Cleanup (no-op, connections are short-lived)."""
        pass


class DuckDBDatabaseManager(DatabaseManager):
    """DuckDB-backed persistence manager.

    Uses short-lived connections for write operations to support concurrent
    access from multiple MCP server instances. Write operations acquire the
    lock, execute, and release immediately.
    """

    def _make_engine(self, db_path: str) -> Engine:
        try:
            import duckdb_engine  # noqa: F401 - registers the "duckdb" SQLAlchemy dialect
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_BACKEND=duckdb requires the 'duckdb' extra: "
                "install with `pip install zulipchat-mcp[duckdb]` "
                "(or `uv add zulipchat-mcp[duckdb]`)."
            ) from exc
        from .migrations import make_engine

        return make_engine(db_path)

    def _run_migrations(self) -> None:
        from .migrations import run_migrations

        run_migrations(self.db_path)

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

    def upsert(
        self,
        table: str,
        columns: Sequence[str],
        values: Sequence[Any],
        conflict_column: str,
    ) -> None:
        # conflict_column is unused: INSERT OR REPLACE's conflict target is
        # implicitly the table's primary key, which is already conflict_column
        # for every caller today. Kept in the signature for parity with
        # PostgresDatabaseManager.upsert(), which needs it explicitly.
        del conflict_column
        self.execute(_insert_or_replace_sql(table, columns), tuple(values))


class SqliteDatabaseManager(DatabaseManager):
    """SQLite-backed persistence manager. Needs no extra package - stdlib
    sqlite3 + SQLAlchemy's built-in dialect. Same short-lived-connection
    (NullPool) and generic lock-retry behavior as DuckDB; no PID parsing
    since sqlite's lock error carries no PID.
    """

    def _make_engine(self, db_path: str) -> Engine:
        from .migrations import make_sqlite_engine

        return make_sqlite_engine(db_path)

    def _run_migrations(self) -> None:
        from .migrations import run_sqlite_migrations

        run_sqlite_migrations(self.db_path)

    def upsert(
        self,
        table: str,
        columns: Sequence[str],
        values: Sequence[Any],
        conflict_column: str,
    ) -> None:
        del conflict_column  # see DuckDBDatabaseManager.upsert
        self.execute(_insert_or_replace_sql(table, columns), tuple(values))


class PostgresDatabaseManager(DatabaseManager):
    """Postgres-backed persistence manager. Multi-writer, so no file-lock
    retry logic - a real connection pool (QueuePool, SQLAlchemy's default)
    instead of the short-lived NullPool the file-based backends need.
    """

    def __init__(
        self,
        host: str | None,
        port: int,
        dbname: str | None,
        user: str | None,
        password: str | None,
        max_retries: int = 5,
        retry_delay: float = 0.1,
    ) -> None:
        # Stashed before calling super().__init__(): the base class's
        # __init__ immediately calls self._make_engine(db_path), and
        # _make_engine below reads these instead of its db_path argument
        # (which is only a redacted display string, never real connection
        # info - see _make_engine's docstring).
        self._pg_host = host
        self._pg_port = port
        self._pg_dbname = dbname
        self._pg_user = user
        self._pg_password = password
        display = f"postgresql://{user}@{host}:{port}/{dbname}"
        super().__init__(display, max_retries, retry_delay)

    def _make_engine(self, db_path: str) -> Engine:
        """Ignores db_path (a redacted display string, never the real
        connection info - password must never end up in self.db_path,
        which DatabaseLockedError messages and logs may surface verbatim).
        Builds the real URL from the fields __init__ stashed on self.
        """
        del db_path
        from sqlalchemy import create_engine

        try:
            import psycopg  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_BACKEND=postgres requires the 'postgres' extra: "
                "install with `pip install zulipchat-mcp[postgres]` "
                "(or `uv add zulipchat-mcp[postgres]`)."
            ) from exc

        self._url = (
            f"postgresql+psycopg://{self._pg_user}:{self._pg_password}"
            f"@{self._pg_host}:{self._pg_port}/{self._pg_dbname}"
        )
        return create_engine(self._url)

    def _run_migrations(self) -> None:
        from .migrations import run_postgres_migrations

        run_postgres_migrations(self._url)

    def _translate_sql(self, sql: str) -> str:
        """psycopg defaults to pyformat (%s); every call site in
        database_manager.py is written with duckdb/sqlite's native `?`
        qmark placeholders, so translate here rather than rewriting ~40
        call sites. Safe because no SQL string in this codebase contains a
        literal `%` outside a bound parameter value (verified: only `%` in
        the codebase is inside a LIKE pattern passed as a parameter, not
        embedded in SQL text).
        """
        return sql.replace("?", "%s")

    def _with_lock_retry(self, operation: Callable[[], T]) -> T:
        """No retry: Postgres is multi-writer, so there's no single-file OS
        lock to wait out. A real connection error still propagates as itself.
        """
        return operation()

    def upsert(
        self,
        table: str,
        columns: Sequence[str],
        values: Sequence[Any],
        conflict_column: str,
    ) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from .schema import metadata

        table_obj = metadata.tables[table]
        row = dict(zip(columns, values, strict=True))
        stmt = pg_insert(table_obj).values(**row)
        update_cols = {c: stmt.excluded[c] for c in columns if c != conflict_column}
        stmt = stmt.on_conflict_do_update(
            index_elements=[conflict_column], set_=update_cols
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)


def _insert_or_replace_sql(table: str, columns: Sequence[str]) -> str:
    """Shared by DuckDBDatabaseManager and SqliteDatabaseManager - both
    support SQLite's INSERT OR REPLACE INTO syntax natively.
    """
    placeholders = ", ".join(["?"] * len(columns))
    column_list = ", ".join(columns)
    return f"INSERT OR REPLACE INTO {table} ({column_list}) VALUES ({placeholders})"


def _strip_tzinfo(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _normalize_params(params: Sequence[Any]) -> tuple[Any, ...]:
    """Strip tzinfo from aware datetimes so every backend stores the same
    naive wall-clock value into schema.py's naive DateTime columns.
    Postgres in particular would otherwise silently shift the stored value
    by the session's configured TimeZone when casting an aware value to
    `timestamp without time zone` - stripping here, once, centrally, means
    no backend-specific handling is needed at any of the ~40 call sites in
    database_manager.py.
    """
    return tuple(_strip_tzinfo(v) for v in params)


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
        _db_manager = DuckDBDatabaseManager(db_path)
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
    _db_manager = DuckDBDatabaseManager(db_path)
    return _db_manager
