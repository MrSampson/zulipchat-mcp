# Pluggable Database Backend (SQLite / DuckDB / Postgres) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the DuckDB-only persistence layer with a pluggable backend (SQLite, DuckDB, Postgres), selected via `DATABASE_BACKEND`, so Postgres can be used for multi-replica deployments (issue #6) while existing DuckDB users keep working unchanged.

**Architecture:** `utils/database.py`'s `DatabaseManager` becomes an abstract base (singleton lifecycle, all six query/execute methods, lock-retry loop, a `_translate_sql` placeholder hook) with three concrete subclasses (`DuckDBDatabaseManager`, `SqliteDatabaseManager`, `PostgresDatabaseManager`) that differ only in engine/pool construction, `upsert()`, and (Postgres only) lock-retry behavior and placeholder translation. `config.py` owns backend selection (`DatabaseBackend` enum + `DatabaseConfig`), threaded into `init_database()` from `server.py`/`claude_hooks.py`.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.x, Alembic, DuckDB + duckdb-engine, stdlib `sqlite3`, psycopg (v3) for Postgres, pytest.

**Spec:** `docs/superpowers/specs/2026-09-09-postgres-backend-design.md`

## Global Constraints

- `DATABASE_BACKEND` defaults to `duckdb` (no change to today's behavior for existing users).
- All three backend drivers are optional at the packaging level: `duckdb`/`duckdb-engine` move to a `[duckdb]` extra, `psycopg[binary]` ships behind a new `[postgres]` extra, SQLite needs nothing extra (stdlib). The `dev` dependency group keeps all three drivers installed so the full test suite can exercise every backend regardless of what a production install chooses.
- Postgres connection config uses discrete env vars: `POSTGRES_HOST`, `POSTGRES_PORT` (default 5432), `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` — not a single DSN.
- `config.py`'s `ConfigManager` has full control: it owns `DatabaseConfig`, and `init_database()` is always called with it (no more direct env-var reads inside `utils/database.py` for backend selection).
- Datetime columns stay naive (no schema change) on all three backends; aware `datetime.now(timezone.utc)` values get `.replace(tzinfo=None)` stripped once, centrally.
- No call site in `utils/database_manager.py` other than the 2 upsert methods changes.
- This ticket (#4) ships unit/mocked tests only for Postgres — no real Postgres connection anywhere in this plan. Full cross-backend parametrization of `tests/utils/test_database*.py` and a real Postgres testcontainers fixture are ticket #5's scope.
- Every `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src` invocation in this plan must pass before moving to the next task.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/zulipchat_mcp/config.py` | `DatabaseBackend` enum, `DatabaseConfig` dataclass, `ConfigManager` reads `DATABASE_BACKEND`/`ZULIPCHAT_DB_PATH`/`POSTGRES_*`. |
| `src/zulipchat_mcp/utils/database.py` | Abstract `DatabaseManager` base + `DuckDBDatabaseManager`/`SqliteDatabaseManager`/`PostgresDatabaseManager`; `init_database()`/`get_database()` factory. |
| `src/zulipchat_mcp/utils/database_manager.py` | High-level API, unchanged except the 2 upsert methods. |
| `src/zulipchat_mcp/utils/migrations.py` | Per-backend URL/engine builders and migration entry points, sharing one internal `_run_migrations_for_url` helper. |
| `src/zulipchat_mcp/server.py`, `src/zulipchat_mcp/claude_hooks.py` | `init_database(config_manager.config.database)` call sites. |
| `src/zulipchat_mcp/setup_wizard.py` | New "Database Backend" step; env vars plumbed into every client-config renderer. |
| `pyproject.toml` | `[project.optional-dependencies]` with `duckdb`/`postgres` extras; dev group keeps all drivers. |
| `README.md` | Multi-backend install/config docs, backward-compat callout. |

---

### Task 1: Config - `DatabaseBackend` enum and `DatabaseConfig`

**Files:**
- Modify: `src/zulipchat_mcp/config.py`
- Test: `tests/test_config.py` (create if it doesn't already cover `ConfigManager` env parsing — check first; if a `tests/test_config.py` exists, add to it instead)

**Interfaces:**
- Produces: `DatabaseBackend(StrEnum)` with members `DUCKDB = "duckdb"`, `SQLITE = "sqlite"`, `POSTGRES = "postgres"`; `DatabaseConfig` dataclass with fields `backend: DatabaseBackend`, `path: str | None`, `postgres_host: str | None`, `postgres_port: int`, `postgres_db: str | None`, `postgres_user: str | None`, `postgres_password: str | None`; `ZulipConfig.database: DatabaseConfig`.

- [ ] **Step 1: Check for an existing config test file**

Run: `ls tests/test_config.py 2>&1 || find tests -iname "*config*"`

If a file exists, read it first to match its existing test style before adding to it.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_config.py`:

```python
import os

import pytest

from zulipchat_mcp.config import ConfigManager, DatabaseBackend, DatabaseConfig


def test_database_config_defaults_to_duckdb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_BACKEND", raising=False)
    monkeypatch.delenv("ZULIPCHAT_DB_PATH", raising=False)

    config = ConfigManager()

    assert config.config.database.backend is DatabaseBackend.DUCKDB
    assert config.config.database.path == ".mcp/zulipchat/zulipchat.duckdb"


def test_database_config_reads_sqlite_backend_with_default_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.delenv("ZULIPCHAT_DB_PATH", raising=False)

    config = ConfigManager()

    assert config.config.database.backend is DatabaseBackend.SQLITE
    assert config.config.database.path == ".mcp/zulipchat/zulipchat.sqlite3"


def test_database_config_reads_explicit_path_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "duckdb")
    monkeypatch.setenv("ZULIPCHAT_DB_PATH", "/tmp/custom.duckdb")

    config = ConfigManager()

    assert config.config.database.path == "/tmp/custom.duckdb"


def test_database_config_reads_postgres_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "zulipchat")
    monkeypatch.setenv("POSTGRES_USER", "mcp")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    config = ConfigManager()

    db = config.config.database
    assert db.backend is DatabaseBackend.POSTGRES
    assert db.postgres_host == "db.internal"
    assert db.postgres_port == 6543
    assert db.postgres_db == "zulipchat"
    assert db.postgres_user == "mcp"
    assert db.postgres_password == "secret"


def test_database_config_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "mongodb")

    with pytest.raises(ValueError, match="DATABASE_BACKEND"):
        ConfigManager()


def test_database_config_postgres_port_defaults_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.delenv("POSTGRES_PORT", raising=False)

    config = ConfigManager()

    assert config.config.database.postgres_port == 5432
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k database_config -v`
Expected: FAIL with `ImportError: cannot import name 'DatabaseBackend'`

- [ ] **Step 4: Implement `DatabaseBackend` and `DatabaseConfig`**

In `src/zulipchat_mcp/config.py`, add near the top (after the existing imports, before `@dataclass class ZulipConfig`):

```python
from enum import StrEnum


class DatabaseBackend(StrEnum):
    """Selectable persistence backends for ZulipChat MCP state."""

    DUCKDB = "duckdb"
    SQLITE = "sqlite"
    POSTGRES = "postgres"


def _default_db_path(backend: DatabaseBackend) -> str:
    """DuckDB and SQLite each get their own default file, so switching
    DATABASE_BACKEND never silently points at the other backend's file.
    """
    if backend is DatabaseBackend.SQLITE:
        return ".mcp/zulipchat/zulipchat.sqlite3"
    return ".mcp/zulipchat/zulipchat.duckdb"


@dataclass
class DatabaseConfig:
    """Persistence backend selection and connection settings."""

    backend: DatabaseBackend = DatabaseBackend.DUCKDB
    path: str | None = None  # sqlite/duckdb file path
    postgres_host: str | None = None
    postgres_port: int = 5432
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None
```

Add `database: DatabaseConfig` as a field on `ZulipConfig` (after `bot_config_file: str | None = None`):

```python
    bot_config_file: str | None = None
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
```

This needs `field` imported: change `from dataclasses import dataclass` to `from dataclasses import dataclass, field`.

Add a loader method to `ConfigManager` (after `_get_bot_config_file`):

```python
    def _load_database_config(self) -> DatabaseConfig:
        """Read DATABASE_BACKEND/ZULIPCHAT_DB_PATH/POSTGRES_* into a DatabaseConfig.

        Raises ValueError on an unrecognized DATABASE_BACKEND rather than
        silently falling back to duckdb - a typo here should fail loudly at
        startup, not switch someone's data to a different empty backend.
        """
        backend_raw = self._env("DATABASE_BACKEND") or DatabaseBackend.DUCKDB.value
        try:
            backend = DatabaseBackend(backend_raw.lower())
        except ValueError as exc:
            valid = ", ".join(b.value for b in DatabaseBackend)
            raise ValueError(
                f"Invalid DATABASE_BACKEND={backend_raw!r}. Must be one of: {valid}"
            ) from exc

        port_raw = self._env("POSTGRES_PORT")
        return DatabaseConfig(
            backend=backend,
            path=self._env("ZULIPCHAT_DB_PATH") or _default_db_path(backend),
            postgres_host=self._env("POSTGRES_HOST"),
            postgres_port=int(port_raw) if port_raw else 5432,
            postgres_db=self._env("POSTGRES_DB"),
            postgres_user=self._env("POSTGRES_USER"),
            postgres_password=self._env("POSTGRES_PASSWORD"),
        )
```

Wire it into `_load_config`'s return value (after `bot_config_file=final_bot_config_file,`):

```python
            bot_config_file=final_bot_config_file,
            database=self._load_database_config(),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -k database_config -v`
Expected: PASS (all 6 tests)

- [ ] **Step 6: Run the full existing config test suite to check for regressions**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 7: Lint and type-check**

Run: `uv run ruff check src/zulipchat_mcp/config.py tests/test_config.py && uv run mypy src/zulipchat_mcp/config.py`
Expected: no errors

- [ ] **Step 8: Commit**

```bash
git add src/zulipchat_mcp/config.py tests/test_config.py
git commit -m "feat: add DatabaseBackend/DatabaseConfig to ConfigManager"
```

---

### Task 2: Split `utils/database.py` into an abstract base + `DuckDBDatabaseManager`

This is a behavior-preserving refactor: `DuckDBDatabaseManager` must do exactly what today's `DatabaseManager` does. No test assertion about DuckDB behavior should need to change other than the class name.

**Files:**
- Modify: `src/zulipchat_mcp/utils/database.py`
- Modify: `tests/utils/test_database.py` (rename `DatabaseManager` -> `DuckDBDatabaseManager` throughout)
- Modify: `tests/utils/test_schema.py:14,365-367,423-425` (same rename)

**Interfaces:**
- Consumes: nothing new.
- Produces: `DatabaseManager` (abstract base, in the same module, same public name so `DatabaseLockedError`/module-level singleton machinery keep working) with abstract methods `_make_engine(self, db_path: str) -> Engine`, `_run_migrations(self) -> None`, `upsert(self, table: str, columns: Sequence[str], values: Sequence[Any], conflict_column: str) -> None` (declared now, implemented starting Task 4); concrete hook `_translate_sql(self, sql: str) -> str` returning `sql` unchanged by default; concrete hook `_try_clear_stale_lock(self, error: BaseException) -> bool` returning `False` by default. `DuckDBDatabaseManager` implementing `_make_engine`/`_run_migrations`/`_try_clear_stale_lock` with today's exact logic.

- [ ] **Step 1: Rename `DatabaseManager` to `DuckDBDatabaseManager` in both test files**

Run:
```bash
sed -i 's/\bDatabaseManager\b/DuckDBDatabaseManager/g' tests/utils/test_database.py
sed -i 's/from src\.zulipchat_mcp\.utils\.database import DuckDBDatabaseManager/from src.zulipchat_mcp.utils.database import DuckDBDatabaseManager/' tests/utils/test_database.py
```

Then manually fix the import line in `tests/utils/test_database.py` (the sed above already renamed inside the `from ... import (...)` block, so `DatabaseLockedError`, `get_database`, `init_database` stay as-is and only `DatabaseManager` becomes `DuckDBDatabaseManager` in that same import - verify with `grep -n "^from src.zulipchat_mcp.utils.database import" -A4 tests/utils/test_database.py`).

In `tests/utils/test_schema.py`, change line 14 from `from src.zulipchat_mcp.utils.database import DatabaseManager` to `from src.zulipchat_mcp.utils.database import DuckDBDatabaseManager`, and change every `DatabaseManager._instance` / `DatabaseManager(db_path)` at lines 365-367 and 423-425 to `DuckDBDatabaseManager`.

- [ ] **Step 2: Run the tests to verify they fail on the missing class (not on logic)**

Run: `uv run pytest tests/utils/test_database.py tests/utils/test_schema.py -v 2>&1 | tail -20`
Expected: FAIL with `ImportError: cannot import name 'DuckDBDatabaseManager'`

- [ ] **Step 3: Restructure `utils/database.py`**

Replace the `class DatabaseManager:` block (lines 43-284 of the current file) with an abstract base plus a `DuckDBDatabaseManager` subclass. Full replacement:

```python
from abc import ABC, abstractmethod

# ... (keep existing imports; add the abstractmethod import above alongside
# the existing `import logging` etc. block)


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
                conn.exec_driver_sql(sql, tuple(params) if params else ())

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
                    conn.exec_driver_sql(sql, tuple(params))

        with self._write_lock:
            self._with_lock_retry(_op)

    def query(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> list[tuple[Any, ...]]:
        """Execute a read query and return results."""
        sql = self._translate_sql(sql)

        def _op() -> list[tuple[Any, ...]]:
            with self._engine.connect() as conn:
                cursor = conn.exec_driver_sql(sql, tuple(params) if params else ())
                return [tuple(row) for row in cursor.fetchall()]

        return self._with_lock_retry(_op)

    def query_one(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> tuple[Any, ...] | None:
        """Execute a read query and return the first result."""
        sql = self._translate_sql(sql)

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
        sql = self._translate_sql(sql)

        def _op() -> list[dict[str, Any]]:
            with self._engine.connect() as conn:
                cursor = conn.exec_driver_sql(sql, tuple(params) if params else ())
                return [dict(row) for row in cursor.mappings()]

        return self._with_lock_retry(_op)

    def query_one_as_dict(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> dict[str, Any] | None:
        """Execute a read query and return the first result as a dictionary."""
        sql = self._translate_sql(sql)

        def _op() -> dict[str, Any] | None:
            with self._engine.connect() as conn:
                cursor = conn.exec_driver_sql(sql, tuple(params) if params else ())
                row = cursor.mappings().first()
                return dict(row) if row is not None else None

        return self._with_lock_retry(_op)

    def close(self) -> None:
        """Dispose the engine's connection pool."""
        self._engine.dispose()

    def __del__(self) -> None:
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
        self.execute(_insert_or_replace_sql(table, columns), values)


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
```

Add `from datetime import datetime` to the imports at the top of `utils/database.py` (it isn't imported there today).

Now go back through the six methods added in this task's Step 3 (`execute`, `executemany`, `query`, `query_one`, `query_as_dicts`, `query_one_as_dict`) and replace every `tuple(params) if params else ()` with `_normalize_params(params) if params else ()`, and `executemany`'s per-row `conn.exec_driver_sql(sql, tuple(params))` with `conn.exec_driver_sql(sql, _normalize_params(params))`.

- [ ] **Step 3b: Write the failing tzinfo-stripping test**

Add to `tests/utils/test_database.py` (in `TestDuckDBDatabaseManager` - the behavior is backend-agnostic, and this class already has the fixtures):

```python
    def test_execute_strips_tzinfo_from_aware_datetime_params(self, tmp_path):
        from datetime import datetime, timezone

        db = DuckDBDatabaseManager(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE t (ts TIMESTAMP)")
        aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        db.execute("INSERT INTO t VALUES (?)", [aware])

        stored = db.query_one("SELECT ts FROM t")[0]
        assert stored.tzinfo is None
        assert stored == aware.replace(tzinfo=None)
```

Run: `uv run pytest tests/utils/test_database.py -k strips_tzinfo -v`
Expected: FAIL first (before Step 3's edits are applied), then PASS once `_normalize_params` is wired into `execute()`/`query_one()`.

- [ ] **Step 3c: Write the failing "missing extra" test**

Add to `tests/utils/test_database.py` in `TestDuckDBDatabaseManager`:

```python
    def test_make_engine_raises_actionable_error_when_duckdb_engine_missing(
        self, tmp_path, monkeypatch
    ):
        import sys

        monkeypatch.setitem(sys.modules, "duckdb_engine", None)

        with pytest.raises(RuntimeError, match=r"\[duckdb\]"):
            DuckDBDatabaseManager(str(tmp_path / "test.db"))
```

(Setting a `sys.modules` entry to `None` makes the import system raise `ImportError` for that module - the standard way to simulate a missing package without actually uninstalling it.)

Run: `uv run pytest tests/utils/test_database.py -k duckdb_engine_missing -v`
Expected: FAIL until Step 3's `_make_engine` edit above lands, then PASS.

At the top of the file, change the existing `from .migrations import make_engine, run_migrations` line - **remove it**. The imports now happen lazily inside `DuckDBDatabaseManager._make_engine`/`_run_migrations` (see above) so that `utils/database.py` itself never fails to import when duckdb-engine isn't installed - only actually selecting the duckdb backend does. Add `from abc import ABC, abstractmethod` to the imports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/utils/test_database.py tests/utils/test_schema.py -v`
Expected: PASS (every test, including the two `@pytest.mark.slow` cross-process ones - run with `-m ""` if your default pytest config excludes `slow`)

- [ ] **Step 5: Run the full test suite for regressions elsewhere**

Run: `uv run pytest -q -m "not slow and not integration"`
Expected: PASS (the only files touched are `utils/database.py` and its two direct test files; nothing else references the old class name per the earlier grep)

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check src/zulipchat_mcp/utils/database.py tests/utils/test_database.py tests/utils/test_schema.py && uv run mypy src/zulipchat_mcp/utils/database.py`
Expected: no errors (note the abstract `upsert()`/`_run_migrations()`/`_make_engine()` methods have no body other than a docstring - that's valid for `@abstractmethod`, mypy won't complain)

- [ ] **Step 7: Commit**

```bash
git add src/zulipchat_mcp/utils/database.py tests/utils/test_database.py tests/utils/test_schema.py
git commit -m "refactor: split DatabaseManager into abstract base + DuckDBDatabaseManager"
```

---

### Task 3: Add `SqliteDatabaseManager` and sqlite migrations

**Files:**
- Modify: `src/zulipchat_mcp/utils/migrations.py`
- Modify: `src/zulipchat_mcp/utils/database.py`
- Test: `tests/utils/test_migrations.py` (add sqlite coverage)
- Test: `tests/utils/test_database.py` (add a `TestSqliteDatabaseManager` class covering the same behavioral surface as `TestDuckDBDatabaseManager` minus the duckdb-specific stale-lock-PID tests)

**Interfaces:**
- Consumes: `DatabaseManager` base from Task 2 (`_make_engine`, `_run_migrations`, `upsert`, `_translate_sql`, `_try_clear_stale_lock`); `_insert_or_replace_sql` from Task 2.
- Produces: `run_sqlite_migrations(db_path: str) -> None` in `migrations.py`; `SqliteDatabaseManager` in `database.py`.

- [ ] **Step 1: Write the failing migrations test**

Add to `tests/utils/test_migrations.py`:

```python
import sqlite3

from src.zulipchat_mcp.utils.migrations import run_sqlite_migrations


def _sqlite_table_names(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _sqlite_alembic_version(db_path: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def test_sqlite_fresh_database_creates_all_real_tables(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fresh.sqlite3")

    run_sqlite_migrations(db_path)

    assert _sqlite_table_names(db_path) >= _REAL_TABLE_NAMES


def test_sqlite_fresh_database_ends_up_at_the_initial_revision(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fresh.sqlite3")

    run_sqlite_migrations(db_path)

    assert _sqlite_alembic_version(db_path) == "0001"


def test_sqlite_in_memory_database_does_not_leak_a_literal_memory_file_to_disk(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    run_sqlite_migrations(IN_MEMORY_DB_PATH)

    assert not (tmp_path / IN_MEMORY_DB_PATH).exists()


def test_sqlite_running_twice_on_the_same_database_does_not_raise(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fresh.sqlite3")

    run_sqlite_migrations(db_path)
    run_sqlite_migrations(db_path)

    assert _sqlite_table_names(db_path) >= _REAL_TABLE_NAMES
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/utils/test_migrations.py -k sqlite -v`
Expected: FAIL with `ImportError: cannot import name 'run_sqlite_migrations'`

- [ ] **Step 3: Refactor `migrations.py` to share the URL-driven core, and add the sqlite entry point**

Replace the body of `migrations.py` (keep `INITIAL_REVISION`, `IN_MEMORY_DB_PATH`, `_MIGRATIONS_DIR` as-is) with:

```python
def sqlalchemy_url(db_path: str) -> str:
    """Build the duckdb_engine URL for db_path.

    DuckDB's ":memory:" is a magic token, not a real path - resolving it
    would silently create a file literally named ":memory:" on disk.
    """
    url_path = db_path if db_path == IN_MEMORY_DB_PATH else str(Path(db_path).resolve())
    return f"duckdb:///{url_path}"


def sqlite_sqlalchemy_url(db_path: str) -> str:
    """Build the sqlite3 dialect URL for db_path. Same ':memory:' guard as
    sqlalchemy_url() above - sqlite has its own native ':memory:' syntax,
    but we keep the shared IN_MEMORY_DB_PATH constant and guard identically
    across both file-based backends rather than special-casing per backend.
    """
    if db_path == IN_MEMORY_DB_PATH:
        return "sqlite:///:memory:"
    return f"sqlite:///{Path(db_path).resolve()}"


def make_engine(db_path: str) -> Engine:
    """Build the shared duckdb_engine Engine for db_path.

    NullPool means every checkout is a fresh connection - callers must
    never hold a connection open longer than one call, so the file lock
    is released for other processes sharing this DuckDB file.
    """
    return create_engine(
        sqlalchemy_url(db_path),
        poolclass=NullPool,
        connect_args={"config": {"access_mode": "READ_WRITE"}},
    )


def make_sqlite_engine(db_path: str) -> Engine:
    """Build the shared sqlite3 Engine for db_path. NullPool for the same
    file-lock-release reason as make_engine() above.
    """
    return create_engine(sqlite_sqlalchemy_url(db_path), poolclass=NullPool)


def _alembic_config_for_url(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _alembic_config(db_path: str) -> Config:
    return _alembic_config_for_url(sqlalchemy_url(db_path))


def _table_exists(connection: Connection, table_name: str) -> bool:
    row = connection.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :name"),
        {"name": table_name},
    ).fetchone()
    return row is not None


def _needs_legacy_stamp(db_path: str) -> bool:
    """True if this is a database from the pre-Alembic hand-rolled migrator:
    it already has all the real tables (tracked via its own schema_migrations
    table at version 1) but no alembic_version table yet.

    DuckDB-only: sqlite and postgres are new backends with no pre-Alembic
    installs to detect.

    Goes through the same SQLAlchemy/duckdb_engine path as the rest of
    run_migrations (rather than a raw duckdb connection) so lock contention
    here surfaces as the same sqlalchemy.exc.OperationalError
    _run_migrations_with_retry already catches, instead of an unhandled
    duckdb.IOException bypassing that retry loop entirely.
    """
    engine = make_engine(db_path)
    try:
        with engine.connect() as connection:
            if _table_exists(connection, "alembic_version"):
                return False
            if not _table_exists(connection, "schema_migrations"):
                return False
            row = connection.execute(
                text("SELECT version FROM schema_migrations WHERE version = 1")
            ).fetchone()
            return row is not None
    finally:
        engine.dispose()


def _run_migrations_for_url(url: str, *, check_legacy_stamp_path: str | None) -> None:
    """Shared upgrade-to-head core for every backend.

    check_legacy_stamp_path: pass the duckdb db_path to run the DuckDB-only
    legacy-stamp check first; pass None for backends that never had a
    pre-Alembic install (sqlite, postgres).
    """
    cfg = _alembic_config_for_url(url)
    if check_legacy_stamp_path is not None and _needs_legacy_stamp(check_legacy_stamp_path):
        command.stamp(cfg, INITIAL_REVISION)
    command.upgrade(cfg, "head")


def run_migrations(db_path: str) -> None:
    """Bring the DuckDB database at db_path up to the latest schema revision.

    Safe to call on a brand new database file, one already at the latest
    revision (no-op), or one created by the old hand-rolled migrator (gets
    stamped at the initial revision instead of replaying its DDL).
    """
    if db_path != IN_MEMORY_DB_PATH:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _run_migrations_for_url(sqlalchemy_url(db_path), check_legacy_stamp_path=db_path)


def run_sqlite_migrations(db_path: str) -> None:
    """Bring the SQLite database at db_path up to the latest schema revision.

    SQLite is a new backend - there are no pre-Alembic installs to stamp.
    """
    if db_path != IN_MEMORY_DB_PATH:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _run_migrations_for_url(sqlite_sqlalchemy_url(db_path), check_legacy_stamp_path=None)
```

- [ ] **Step 4: Run the migrations tests to verify they pass**

Run: `uv run pytest tests/utils/test_migrations.py -v`
Expected: PASS (both the pre-existing duckdb tests and the new sqlite ones)

- [ ] **Step 5: Write the failing `SqliteDatabaseManager` tests**

In `tests/utils/test_database.py`, add a second test class mirroring `TestDuckDBDatabaseManager`'s non-duckdb-specific behavior (skip the PID/stale-lock tests - sqlite has no PID in its lock error and inherits the base's always-`False` `_try_clear_stale_lock`):

```python
import sqlite3

from src.zulipchat_mcp.utils.database import SqliteDatabaseManager


class TestSqliteDatabaseManager:
    """Tests for SqliteDatabaseManager - mirrors TestDuckDBDatabaseManager's
    backend-agnostic behavior. Lock-retry and stale-lock-PID tests stay
    DuckDB-only (sqlite's "database is locked" error carries no PID).
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

    def test_execute_creates_row(self, tmp_path):
        db = SqliteDatabaseManager(str(tmp_path / "test.sqlite3"))
        db.execute("CREATE TABLE t (x INTEGER)")

        db.execute("INSERT INTO t VALUES (?)", [1])

        assert db.query("SELECT x FROM t") == [(1,)]

    def test_executemany_inserts_all_rows(self, tmp_path):
        db = SqliteDatabaseManager(str(tmp_path / "test.sqlite3"))
        db.execute("CREATE TABLE t (x INTEGER)")

        db.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])

        assert db.query("SELECT x FROM t ORDER BY x") == [(1,), (2,)]

    def test_query_as_dicts_returns_list_of_dicts(self, tmp_path):
        db = SqliteDatabaseManager(str(tmp_path / "test.sqlite3"))
        db.execute("CREATE TABLE t (id INTEGER, name VARCHAR)")
        db.executemany("INSERT INTO t VALUES (?, ?)", [(1, "a"), (2, "b")])

        result = db.query_as_dicts("SELECT id, name FROM t ORDER BY id")

        assert result == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
```

(The factory functions `init_database()`/`get_database()` aren't wired for sqlite until Task 6, so this task's tests construct `SqliteDatabaseManager` directly - the four tests above are sufficient for this task's scope.)

- [ ] **Step 6: Run the new tests to verify they fail**

Run: `uv run pytest tests/utils/test_database.py -k Sqlite -v`
Expected: FAIL with `ImportError: cannot import name 'SqliteDatabaseManager'`

- [ ] **Step 7: Implement `SqliteDatabaseManager`**

In `src/zulipchat_mcp/utils/database.py`, add after `DuckDBDatabaseManager`:

```python
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
        self.execute(_insert_or_replace_sql(table, columns), values)
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/utils/test_database.py -k Sqlite -v`
Expected: PASS

- [ ] **Step 9: Run the full suite for regressions**

Run: `uv run pytest -q -m "not slow and not integration"`
Expected: PASS

- [ ] **Step 10: Lint and type-check**

Run: `uv run ruff check src/zulipchat_mcp/utils/migrations.py src/zulipchat_mcp/utils/database.py tests/utils/test_migrations.py tests/utils/test_database.py && uv run mypy src/zulipchat_mcp/utils/migrations.py src/zulipchat_mcp/utils/database.py`
Expected: no errors

- [ ] **Step 11: Commit**

```bash
git add src/zulipchat_mcp/utils/migrations.py src/zulipchat_mcp/utils/database.py tests/utils/test_migrations.py tests/utils/test_database.py
git commit -m "feat: add SqliteDatabaseManager and sqlite migrations"
```

---

### Task 4: Route the 2 `INSERT OR REPLACE` call sites through `upsert()`

**Files:**
- Modify: `src/zulipchat_mcp/utils/database_manager.py:51-68,135-158`
- Modify: `tests/utils/test_database_manager.py:24-39,47-66`

**Interfaces:**
- Consumes: `upsert(table, columns, values, conflict_column)` from `DuckDBDatabaseManager`/`SqliteDatabaseManager` (Tasks 2-3).
- Produces: nothing new - this task only changes call sites.

- [ ] **Step 1: Update the failing test expectations first**

In `tests/utils/test_database_manager.py`, change `test_upsert_agent_profile` (lines 24-39):

```python
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
```

And `test_upsert_agent_session` (lines 47-66):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/utils/test_database_manager.py -k upsert -v`
Expected: FAIL - `mock_db.upsert.call_args` is `None` because `upsert_agent_profile`/`upsert_agent_session` still call `mock_db.execute`

- [ ] **Step 3: Update the two call sites**

In `src/zulipchat_mcp/utils/database_manager.py`, replace the `self._db.execute(...)` call inside `upsert_agent_profile` (lines 51-68) with:

```python
            self._db.upsert(
                "agent_profiles",
                [
                    "agent_id",
                    "agent_name",
                    "agent_type",
                    "owner_email",
                    "stream_name",
                    "topic_prefix",
                    "metadata",
                    "created_at",
                    "updated_at",
                ],
                [
                    agent_id,
                    agent_name,
                    agent_type,
                    owner_email,
                    stream_name,
                    topic_prefix,
                    metadata,
                    created_at,
                    now,
                ],
                "agent_id",
            )
```

And inside `upsert_agent_session` (lines 135-158):

```python
            self._db.upsert(
                "agent_sessions",
                [
                    "session_id",
                    "agent_id",
                    "external_session_id",
                    "stream_name",
                    "topic_name",
                    "owner_email",
                    "project_name",
                    "project_dir",
                    "host",
                    "status",
                    "metadata",
                    "created_at",
                    "updated_at",
                    "ended_at",
                ],
                [
                    session_id,
                    agent_id,
                    external_session_id,
                    stream_name,
                    topic_name,
                    owner_email,
                    project_name,
                    project_dir,
                    host,
                    status,
                    metadata,
                    created_at,
                    now,
                    None if status not in {"completed", "failed", "cancelled"} else now,
                ],
                "session_id",
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/utils/test_database_manager.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Run the full suite for regressions**

Run: `uv run pytest -q -m "not slow and not integration"`
Expected: PASS

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check src/zulipchat_mcp/utils/database_manager.py tests/utils/test_database_manager.py && uv run mypy src/zulipchat_mcp/utils/database_manager.py`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add src/zulipchat_mcp/utils/database_manager.py tests/utils/test_database_manager.py
git commit -m "refactor: route agent_profiles/agent_sessions upserts through DatabaseManager.upsert()"
```

---

### Task 5: Add `PostgresDatabaseManager`

**Files:**
- Modify: `src/zulipchat_mcp/utils/database.py`
- Modify: `src/zulipchat_mcp/utils/migrations.py`
- Modify: `pyproject.toml` (add `psycopg[binary]` to the `dev` dependency group only - the `[postgres]` extra itself is Task 7)
- Test: `tests/utils/test_database.py` (new `TestPostgresDatabaseManager` class, mocked engine only - no real Postgres)

**Interfaces:**
- Consumes: `DatabaseManager` base (Task 2), `run_postgres_migrations` (this task, in `migrations.py`).
- Produces: `PostgresDatabaseManager` in `database.py`.

- [ ] **Step 1: Add `psycopg[binary]` to the dev dependency group**

In `pyproject.toml`, add to the `[dependency-groups] dev` list:

```toml
    "psycopg[binary]>=3.2",
```

Run: `uv sync`
Expected: installs psycopg without error

- [ ] **Step 2: Write the failing migrations test**

Add to `tests/utils/test_migrations.py`:

```python
from src.zulipchat_mcp.utils.migrations import run_postgres_migrations


def test_run_postgres_migrations_builds_config_without_legacy_check(monkeypatch):
    """No real Postgres needed: prove run_postgres_migrations never calls
    the DuckDB-only legacy-stamp check, by making that check raise if
    called - if run_postgres_migrations tried to call it, this test fails
    loudly instead of silently connecting to a bogus DuckDB path.
    """
    def _boom(db_path: str) -> bool:
        raise AssertionError("_needs_legacy_stamp must not run for postgres")

    monkeypatch.setattr(
        "src.zulipchat_mcp.utils.migrations._needs_legacy_stamp", _boom
    )
    calls = []
    monkeypatch.setattr(
        "src.zulipchat_mcp.utils.migrations.command.upgrade",
        lambda cfg, rev: calls.append(rev),
    )

    run_postgres_migrations("postgresql+psycopg://u:p@host:5432/db")

    assert calls == ["head"]
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/utils/test_migrations.py -k postgres -v`
Expected: FAIL with `ImportError: cannot import name 'run_postgres_migrations'`

- [ ] **Step 4: Add `run_postgres_migrations` to `migrations.py`**

Append to `src/zulipchat_mcp/utils/migrations.py`:

```python
def run_postgres_migrations(url: str) -> None:
    """Bring the Postgres database at url up to the latest schema revision.

    Postgres is a new backend - there are no pre-Alembic installs to stamp,
    and no local directory to create (a connection string, not a file path).
    """
    _run_migrations_for_url(url, check_legacy_stamp_path=None)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/utils/test_migrations.py -k postgres -v`
Expected: PASS

- [ ] **Step 6: Write the failing `PostgresDatabaseManager` tests**

Add to `tests/utils/test_database.py`:

```python
from src.zulipchat_mcp.utils.database import PostgresDatabaseManager
from sqlalchemy.pool import QueuePool


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
            host="db.internal", port=6543, dbname="zulipchat",
            user="mcp", password="s3cret",
        )

        assert isinstance(db._engine.pool, QueuePool)
        assert db._engine.url.drivername == "postgresql+psycopg"
        assert db._engine.url.host == "db.internal"
        assert db._engine.url.port == 6543
        assert db._engine.url.database == "zulipchat"
        assert db._engine.url.username == "mcp"

    def test_db_path_display_string_excludes_password(self):
        db = PostgresDatabaseManager(
            host="db.internal", port=5432, dbname="zulipchat",
            user="mcp", password="s3cret",
        )

        assert "s3cret" not in db.db_path

    def test_translate_sql_converts_qmark_to_pyformat(self):
        db = PostgresDatabaseManager(
            host="h", port=5432, dbname="d", user="u", password="p",
        )

        assert db._translate_sql("SELECT * FROM t WHERE a = ? AND b = ?") == (
            "SELECT * FROM t WHERE a = %s AND b = %s"
        )

    def test_with_lock_retry_calls_operation_once_without_retrying(self):
        db = PostgresDatabaseManager(
            host="h", port=5432, dbname="d", user="u", password="p",
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
                host="h", port=5432, dbname="d", user="u", password="p",
            )

    def test_upsert_builds_on_conflict_do_update(self):
        db = PostgresDatabaseManager(
            host="h", port=5432, dbname="d", user="u", password="p",
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
            executed[0].compile(dialect=__import__(
                "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
            ).dialect())
        )
        assert "ON CONFLICT" in compiled
        assert "agent_profiles" in compiled
```

- [ ] **Step 7: Run the tests to verify they fail**

Run: `uv run pytest tests/utils/test_database.py -k Postgres -v`
Expected: FAIL with `ImportError: cannot import name 'PostgresDatabaseManager'`

- [ ] **Step 8: Implement `PostgresDatabaseManager`**

In `src/zulipchat_mcp/utils/database.py`, add after `SqliteDatabaseManager`:

```python
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
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run pytest tests/utils/test_database.py -k Postgres -v`
Expected: PASS

- [ ] **Step 10: Run the full suite for regressions**

Run: `uv run pytest -q -m "not slow and not integration"`
Expected: PASS

- [ ] **Step 11: Lint and type-check**

Run: `uv run ruff check src/zulipchat_mcp/utils/database.py src/zulipchat_mcp/utils/migrations.py tests/utils/test_database.py tests/utils/test_migrations.py && uv run mypy src/zulipchat_mcp/utils/database.py src/zulipchat_mcp/utils/migrations.py`
Expected: no errors

- [ ] **Step 12: Commit**

```bash
git add pyproject.toml uv.lock src/zulipchat_mcp/utils/database.py src/zulipchat_mcp/utils/migrations.py tests/utils/test_database.py tests/utils/test_migrations.py
git commit -m "feat: add PostgresDatabaseManager (unit-tested, no real Postgres)"
```

---

### Task 6: Wire `DatabaseConfig` through `init_database()`/`get_database()`

**Files:**
- Modify: `src/zulipchat_mcp/utils/database.py:287-317` (the `get_database`/`init_database` module-level functions)
- Modify: `src/zulipchat_mcp/server.py:159`
- Modify: `src/zulipchat_mcp/claude_hooks.py:308`
- Modify: `tests/utils/test_database.py` (the `test_global_instances` test and any other direct `init_database(path)`/`get_database()` callers)

**Interfaces:**
- Consumes: `DatabaseConfig`/`DatabaseBackend` from Task 1.
- Produces: `init_database(config: DatabaseConfig) -> DatabaseManager`; `get_database() -> DatabaseManager` (now raises `RuntimeError` if uninitialized, matching `get_config_manager()`'s existing contract).

- [ ] **Step 1: Update the failing test**

In `tests/utils/test_database.py`, find `test_global_instances` (around line 516) and replace it:

```python
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
```

Also remove the old test's final two lines that constructed `DatabaseManager(IN_MEMORY_DB_PATH)` directly and asserted singleton identity via the base class name (that identity check is already covered by `TestDuckDBDatabaseManager`'s `reset_singleton` fixture pattern elsewhere; the two new tests above cover what this test needs to prove for the factory specifically).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/utils/test_database.py -k global_instances -v`
Expected: FAIL - `init_database()` still takes a raw path string, not a `DatabaseConfig`

- [ ] **Step 3: Rewrite `get_database()`/`init_database()`**

Replace lines 287-317 of `src/zulipchat_mcp/utils/database.py` (the `# Global database manager instance` section through the end of the file) with:

```python
# Global database manager instance
_db_manager: DatabaseManager | None = None


def init_database(config: DatabaseConfig) -> DatabaseManager:
    """Initialize the global database manager for the given backend config.

    Must be called once at server startup (mirrors init_config_manager()).
    Subsequent calls reinitialize (useful for testing).
    """
    global _db_manager
    backend = config.backend
    if backend is DatabaseBackend.DUCKDB:
        _db_manager = DuckDBDatabaseManager(config.path)
    elif backend is DatabaseBackend.SQLITE:
        _db_manager = SqliteDatabaseManager(config.path)
    elif backend is DatabaseBackend.POSTGRES:
        _db_manager = PostgresDatabaseManager(
            host=config.postgres_host,
            port=config.postgres_port,
            dbname=config.postgres_db,
            user=config.postgres_user,
            password=config.postgres_password,
        )
    else:
        raise ValueError(f"Unsupported DATABASE_BACKEND: {backend}")
    return _db_manager


def get_database() -> DatabaseManager:
    """Get the global database manager instance.

    Raises:
        RuntimeError: If init_database() was not called first.
    """
    if _db_manager is None:
        raise RuntimeError(
            "Database not initialized. Call init_database() first."
        )
    return _db_manager
```

Add a top-level import for the config types (no cycle: `config.py` has no dependency on `utils/database.py`, directly or transitively):

```python
from ..config import DatabaseBackend, DatabaseConfig
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/utils/test_database.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Update `server.py` and `claude_hooks.py` call sites**

In `src/zulipchat_mcp/server.py`, change line 159 from:

```python
            init_database()
```

to:

```python
            init_database(config_manager.config.database)
```

In `src/zulipchat_mcp/claude_hooks.py`, change line 308 from:

```python
    init_database()
```

to:

```python
    init_database(config_manager.config.database)
```

- [ ] **Step 6: Run the full suite to find any other broken call sites**

Run: `uv run pytest -q -m "not slow and not integration" 2>&1 | tail -60`

If other tests fail with `RuntimeError: Database not initialized` (e.g. tests exercising `tools/agents.py`, `core/agent_control.py`, `services/message_listener.py`, or `core/service_manager.py` that relied on `get_database()`'s old lazy-default), check whether they already mock `get_database`/`database_manager.get_database` (most do, per the grep in Task 4's prep) - if a test genuinely constructs the real database layer without going through `init_database()`, add a call to `init_database(DatabaseConfig(backend=DatabaseBackend.SQLITE, path=IN_MEMORY_DB_PATH))` in that test's setup (sqlite in-memory is the fastest option for this).

- [ ] **Step 7: Run the full suite again to confirm green**

Run: `uv run pytest -q -m "not slow and not integration"`
Expected: PASS

- [ ] **Step 8: Lint and type-check**

Run: `uv run ruff check src/zulipchat_mcp/utils/database.py src/zulipchat_mcp/server.py src/zulipchat_mcp/claude_hooks.py tests/utils/test_database.py && uv run mypy src/zulipchat_mcp/utils/database.py src/zulipchat_mcp/server.py src/zulipchat_mcp/claude_hooks.py`
Expected: no errors

- [ ] **Step 9: Commit**

```bash
git add src/zulipchat_mcp/utils/database.py src/zulipchat_mcp/server.py src/zulipchat_mcp/claude_hooks.py tests/utils/test_database.py
git commit -m "feat: thread DatabaseConfig through init_database(); get_database() now raises if uninitialized"
```

---

### Task 7: Packaging - `[duckdb]`/`[postgres]` extras

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: nothing new.
- Produces: `zulipchat-mcp[duckdb]`, `zulipchat-mcp[postgres]` installable extras.

- [ ] **Step 1: Move `duckdb`/`duckdb-engine` out of the hard dependency list and add the extras group**

In `pyproject.toml`, remove these two lines from `dependencies`:

```toml
    "duckdb>=1.3.0",
```
```toml
    "duckdb-engine==0.17.0",
```

Add a new section after the `dependencies = [...]` block:

```toml
[project.optional-dependencies]
duckdb = [
    "duckdb>=1.3.0",
    "duckdb-engine==0.17.0",
]
postgres = [
    "psycopg[binary]>=3.2",
]
```

In `[dependency-groups] dev`, add `duckdb` and `duckdb-engine` explicitly (they're no longer pulled in transitively via `dependencies`, and the dev group needs them so the full test suite keeps exercising the duckdb backend):

```toml
    "duckdb>=1.3.0",
    "duckdb-engine==0.17.0",
```

(`psycopg[binary]` is already there from Task 5.)

- [ ] **Step 2: Re-sync and run the full suite**

Run: `uv sync && uv run pytest -q -m "not slow and not integration"`
Expected: PASS - the dev environment still has every driver

- [ ] **Step 3: Verify the extras are installable standalone**

Run: `uv pip install -e ".[duckdb]" --dry-run 2>&1 | tail -20 && uv pip install -e ".[postgres]" --dry-run 2>&1 | tail -20`
Expected: both resolve without error (dry-run, no actual install needed - this just proves the extras syntax and version constraints are valid)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: move duckdb to [duckdb] extra, add [postgres] extra"
```

---

### Task 8: `setup_wizard.py` - Database Backend step

**Files:**
- Modify: `src/zulipchat_mcp/setup_wizard.py`
- Test: `tests/test_setup_wizard.py` (check if it exists first: `find . -iname "test_setup_wizard*"`; if it doesn't exist, create it)

**Interfaces:**
- Consumes: `DatabaseBackend` from Task 1.
- Produces: `prompt_database_backend() -> dict[str, str]` returning the env vars to plumb into generated configs; `_build_args`/`generate_claude_code_command`/`generate_mcp_config` all accept an `env: dict[str, str] | None = None` and thread it through.

- [ ] **Step 1: Check for an existing wizard test file**

Run: `find . -iname "test_setup_wizard*" -not -path "*/node_modules/*"`

Read it first if found, to match existing test style/fixtures.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_setup_wizard.py`:

```python
from unittest.mock import patch

from zulipchat_mcp.setup_wizard import (
    _build_args,
    generate_claude_code_command,
    generate_mcp_config,
    prompt_database_backend,
)


def test_prompt_database_backend_defaults_to_skip_returns_no_env(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")

    env = prompt_database_backend()

    assert env == {}


def test_prompt_database_backend_duckdb_returns_backend_env(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "1")

    env = prompt_database_backend()

    assert env == {"DATABASE_BACKEND": "duckdb"}


def test_prompt_database_backend_postgres_prompts_for_connection_fields(monkeypatch):
    answers = iter(["3", "db.internal", "6543", "zulipchat", "mcp", "s3cret"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    env = prompt_database_backend()

    assert env == {
        "DATABASE_BACKEND": "postgres",
        "POSTGRES_HOST": "db.internal",
        "POSTGRES_PORT": "6543",
        "POSTGRES_DB": "zulipchat",
        "POSTGRES_USER": "mcp",
        "POSTGRES_PASSWORD": "s3cret",
    }


def test_generate_mcp_config_includes_env_when_provided():
    user_config = {"path": "/home/u/.zuliprc"}

    config = generate_mcp_config(
        user_config, extended_tools=False, use_uvx=True,
        env={"DATABASE_BACKEND": "postgres"},
    )

    assert config["env"] == {"DATABASE_BACKEND": "postgres"}


def test_generate_claude_code_command_includes_database_env_flags():
    user_config = {"path": "/home/u/.zuliprc"}

    command = generate_claude_code_command(
        user_config, env={"DATABASE_BACKEND": "postgres", "POSTGRES_HOST": "db"},
    )

    assert "-e DATABASE_BACKEND=postgres" in command
    assert "-e POSTGRES_HOST=db" in command
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_setup_wizard.py -k "database_backend or includes_env or includes_database" -v`
Expected: FAIL with `ImportError: cannot import name 'prompt_database_backend'`

- [ ] **Step 4: Implement `prompt_database_backend`**

Add to `src/zulipchat_mcp/setup_wizard.py`, after `_select_tool_mode`:

```python
def prompt_database_backend() -> dict[str, str]:
    """Prompt for a database backend and return the env vars to plumb into
    the generated MCP config. Empty dict means "don't set anything" - the
    server's own DATABASE_BACKEND default (duckdb) applies.
    """
    print(f"\n{BOLD}Step: Database Backend (Optional){RESET}")
    print("  1. DuckDB (default - no setup needed)")
    print("  2. SQLite (no extra dependency)")
    print("  3. Postgres (for multi-replica deployments)")
    print("  4. Skip (use the server's default)")
    choice = prompt("Choice", default="4")

    if choice == "1":
        return {"DATABASE_BACKEND": "duckdb"}
    if choice == "2":
        return {"DATABASE_BACKEND": "sqlite"}
    if choice == "3":
        host = prompt("Postgres host")
        port = prompt("Postgres port", default="5432")
        dbname = prompt("Postgres database name")
        user = prompt("Postgres user")
        password = prompt("Postgres password")
        return {
            "DATABASE_BACKEND": "postgres",
            "POSTGRES_HOST": host,
            "POSTGRES_PORT": port,
            "POSTGRES_DB": dbname,
            "POSTGRES_USER": user,
            "POSTGRES_PASSWORD": password,
        }
    return {}
```

- [ ] **Step 5: Thread `env` through the config generators**

Change `_build_args`'s signature and body (it stays args-only; env vars for the `claude mcp add` form are separate `-e` flags, added in `generate_claude_code_command` below - `_build_args` itself needs no change).

Change `generate_mcp_config`:

```python
def generate_mcp_config(
    user_config: dict[str, Any],
    bot_config: dict[str, Any] | None = None,
    *,
    extended_tools: bool = False,
    use_uvx: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Generate MCP server configuration."""
    command = shutil.which("uv") or "uv"
    args = _build_args(
        user_config,
        bot_config,
        extended_tools=extended_tools,
        use_uvx=use_uvx,
    )
    config: dict[str, Any] = {
        "command": command,
        "args": args,
    }
    if env:
        config["env"] = env
    return config
```

Change `generate_claude_code_command`:

```python
def generate_claude_code_command(
    user_config: dict[str, Any],
    bot_config: dict[str, Any] | None = None,
    *,
    extended_tools: bool = False,
    env: dict[str, str] | None = None,
) -> str:
    """Generate `claude mcp add` command for Claude Code."""
    parts = ["claude mcp add zulipchat"]
    parts.append(f"-e ZULIP_CONFIG_FILE={user_config['path']}")

    if bot_config:
        parts.append(f"-e ZULIP_BOT_CONFIG_FILE={bot_config['path']}")

    for key, value in (env or {}).items():
        parts.append(f"-e {key}={value}")

    cmd_tail = "-- uvx zulipchat-mcp"
    if extended_tools:
        cmd_tail += " --extended-tools"
    parts.append(cmd_tail)

    return " \\\n  ".join(parts)
```

- [ ] **Step 6: Wire the new step and env into `main()`**

In `main()`, after the `extended_tools = _select_tool_mode()` line, add:

```python
    database_env = prompt_database_backend()
```

Change the `generate_mcp_config(...)` call a few lines below to pass `env=database_env`:

```python
    mcp_config = generate_mcp_config(
        user_config,
        bot_config,
        extended_tools=extended_tools,
        use_uvx=True,
        env=database_env,
    )
```

And the `client_choice == "1"` branch's `generate_claude_code_command(...)` call to pass `env=database_env`:

```python
            generate_claude_code_command(
                user_config,
                bot_config,
                extended_tools=extended_tools,
                env=database_env,
            )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_setup_wizard.py -v`
Expected: PASS (full file)

- [ ] **Step 8: Run the full suite for regressions**

Run: `uv run pytest -q -m "not slow and not integration"`
Expected: PASS

- [ ] **Step 9: Lint and type-check**

Run: `uv run ruff check src/zulipchat_mcp/setup_wizard.py tests/test_setup_wizard.py && uv run mypy src/zulipchat_mcp/setup_wizard.py`
Expected: no errors

- [ ] **Step 10: Commit**

```bash
git add src/zulipchat_mcp/setup_wizard.py tests/test_setup_wizard.py
git commit -m "feat: add Database Backend step to setup wizard"
```

---

### Task 9: Documentation staleness pass + full verification

**Files:**
- Modify: `README.md:175,259,263`
- Modify: `CLAUDE.md` (the "Database Migrations" section, if it references DuckDB-only assumptions that are now backend-general)

**Interfaces:** none (docs only).

- [ ] **Step 1: Update README.md's multi-replica note (line 175)**

Replace:

```markdown
> **Note on Multi-Replica Deployments**: DuckDB state persistence is single-writer. When deploying multiple HTTP replicas, ensure each instance points to a distinct DuckDB path or run a single-instance deployment.
```

with:

```markdown
> **Note on Multi-Replica Deployments**: the default DuckDB/SQLite backends are single-writer file databases. When deploying multiple HTTP replicas, either point each instance at a distinct file, run a single-instance deployment, or set `DATABASE_BACKEND=postgres` (install with the `postgres` extra) for a real multi-writer backend.
```

- [ ] **Step 2: Update README.md's architecture description (lines 259, 263)**

Replace:

```markdown
├── utils/          # Logging, DuckDB persistence, metrics
```

with:

```markdown
├── utils/          # Logging, pluggable persistence (SQLite/DuckDB/Postgres), metrics
```

Replace:

```markdown
Built on [FastMCP](https://github.com/PrefectHQ/fastmcp) with async-first design, [DuckDB](https://duckdb.org) for agent state persistence, and smart user/stream caching for fast fuzzy resolution.
```

with:

```markdown
Built on [FastMCP](https://github.com/PrefectHQ/fastmcp) with async-first design, a pluggable persistence backend (SQLite, [DuckDB](https://duckdb.org), or Postgres via `DATABASE_BACKEND`) for agent state, and smart user/stream caching for fast fuzzy resolution.
```

- [ ] **Step 3: Add a backend configuration section to README.md**

Find the existing environment variables section (search `grep -n "^### Environment\|^## Configuration" README.md`) and add a subsection documenting: `DATABASE_BACKEND` (default `duckdb`), `ZULIPCHAT_DB_PATH`, `POSTGRES_HOST`/`PORT`/`DB`/`USER`/`PASSWORD`, and the extras (`pip install zulipchat-mcp[duckdb]` / `[postgres]`) - including the backward-compatibility callout from the spec: existing users must add the `[duckdb]` extra on upgrade, or startup fails with an actionable error naming the missing extra.

- [ ] **Step 4: Check `CLAUDE.md`'s Database Migrations section for staleness**

Run: `grep -n "DuckDB\|duckdb" CLAUDE.md`

For each hit describing DuckDB-only behavior that's no longer accurate project-wide (e.g. anything implying migrations only ever target DuckDB), add a brief note that the same Alembic revisions now also run against SQLite and Postgres via `run_sqlite_migrations`/`run_postgres_migrations` in `utils/migrations.py`. Leave DuckDB-specific gotchas (the `duckdb_engine`/`autoincrement=False` notes) as-is - those remain accurate for the duckdb backend specifically.

- [ ] **Step 5: Full verification sweep**

Run each of these and confirm clean output before proceeding:

```bash
uv run pytest -q
uv run pytest --cov=src --cov-fail-under=60
uv run ruff check .
changed_py=$(git diff main --name-only -- '*.py')
[ -z "$changed_py" ] || uv run black --check $changed_py
uv run mypy src
```

Expected: all pass. If `pytest -q` (the full suite, including `slow`/`integration`-marked tests) surfaces anything beyond what `-m "not slow and not integration"` already caught, fix it here before moving on - this is the last checkpoint before the MR.

- [ ] **Step 6: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document pluggable database backend and extras"
```

---

## After This Plan

Per this repo's workflow (`CLAUDE.md`): push the branch, open the MR with `glab`... wait, this is a GitHub fork (`MrSampson/zulipchat-mcp`), so use `gh pr create` instead, and reference issue #4. Do not merge without review. Ticket #5 (parametrize the DB test suite across backends with a real Postgres testcontainers fixture) and #6 (update the K8s deployment for Postgres) remain as follow-up work, now unblocked.
