# Design: Pluggable persistence backend (SQLite / DuckDB / Postgres)

Date: 2026-09-09
Related: [MrSampson/zulipchat-mcp#4](https://github.com/MrSampson/zulipchat-mcp/issues/4) "Add PostgreSQL backend" (blocks #5 test parametrization, #6 K8s deployment update)

## Problem

Issue #4 asks for a Postgres backend alongside the existing DuckDB one, motivated by
DuckDB's single-writer file lock blocking horizontal scaling (the real driver, per
issue #6). The issue's literal scope is narrow: SQLAlchemy connection pooling for
Postgres, translating 2 `INSERT OR REPLACE INTO` call sites, and a
`DATABASE_BACKEND` env var.

Investigating the current code surfaced two things that widen the real scope:

1. **Every** parameterized SQL statement in `utils/database_manager.py` (~40 call
   sites, not just the 2 upsert ones) is executed via `conn.exec_driver_sql(sql,
   params)`, which sends `?`-style positional placeholders straight to the DBAPI
   driver, bypassing SQLAlchemy's paramstyle translation entirely. This works today
   only because DuckDB's driver happens to accept `?` (qmark) natively. Postgres
   drivers (psycopg) default to `%s` (pyformat) - so a real Postgres backend needs a
   portable placeholder strategy for the whole file, not just the 2 named sites.
2. `utils/schema.py`'s docstring already flags a naive-vs-aware-datetime decision as
   deferred to this issue - all `DateTime` columns are timezone-naive, but the code
   writes `datetime.now(timezone.utc)` (aware) into them.

During design discussion the scope grew further, deliberately: rather than a
two-backend system, this becomes a three-backend pluggable persistence layer
(SQLite, DuckDB, Postgres), with SQLite added as a lightweight, dependency-free
option and as the fixture backend for fast unit tests.

## Decisions

- **Three backends**: SQLite, DuckDB, Postgres. All are first-class, documented,
  user-selectable via `DATABASE_BACKEND` - none is a test-only shortcut.
- **`DATABASE_BACKEND` defaults to `duckdb`** - unchanged from today's behavior, no
  silent data-backend switch for existing users.
- **All three backends are optional at the packaging level**: `duckdb`/`duckdb-engine`
  move from hard dependencies to a `[duckdb]` extra; `psycopg[binary]` ships behind a
  new `[postgres]` extra; SQLite needs nothing extra (stdlib `sqlite3` +
  SQLAlchemy's built-in dialect).
  - A bare `uvx zulipchat-mcp` with no extras has **no working backend** by
    default and fails fast at startup with an actionable message ("install
    `zulipchat-mcp[duckdb]`..."), rather than silently falling back to an empty
    SQLite database while old DuckDB data sits untouched and unused.
  - This is a **one-time breaking change** for existing users: upgrading requires
    adding the `[duckdb]` extra (or switching to `[postgres]`). Call this out
    clearly in the MR description, README, and changelog.
- **Postgres connection config uses discrete env vars**: `POSTGRES_HOST`,
  `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` - not a single
  DSN string.
- **`config.py` has full control**: `ConfigManager` owns backend selection and
  connection parameters; `server.py`/`claude_hooks.py` thread `config_manager.config
  .database` into `init_database()`. `utils/database.py` no longer reads env vars
  directly for backend selection (it still may for other unrelated settings).
- **Datetime columns stay naive** on all three backends (no schema change). Aware UTC
  datetimes get `.replace(tzinfo=None)` stripped once, centrally, before reaching any
  driver - identical stored values everywhere.
- **Testing split**: this ticket (#4) covers unit/mocked tests only (URL building,
  `upsert()` SQL per backend, config parsing, wizard rendering) plus using SQLite's
  `:memory:` as the fast fixture backend for most of the existing
  `tests/utils/test_database*.py` suite. Full cross-backend parametrization of that
  suite and a real Postgres testcontainers fixture remain owned by ticket #5.

## Architecture

`utils/database.py`'s `DatabaseManager` becomes an abstract base with three concrete
subclasses:

- `SqliteDatabaseManager`
- `DuckDBDatabaseManager`
- `PostgresDatabaseManager`

**Base class owns** (shared, unchanged behavior across backends):

- The singleton lifecycle (`__new__`/`_instance`).
- All six query/execute methods (`execute`, `executemany`, `query`, `query_one`,
  `query_as_dicts`, `query_one_as_dict`), reworked to translate `?`-style positional
  params into named binds and execute via `conn.execute(text(...), params_dict)`
  instead of `exec_driver_sql`. SQLAlchemy's compiler then targets whichever
  paramstyle the active DBAPI driver needs. **No call site in
  `database_manager.py` changes for this** - same `?`-SQL-string-plus-positional-list
  calling convention throughout.
- Central datetime normalization (strip `tzinfo` from any aware `datetime` in params).
- The default lock-retry-with-backoff loop (`_with_lock_retry`), needed by both
  file-locked backends.
- `close()`.

**Subclasses override**:

- `_make_engine()` - connection URL and pool class. `NullPool` for sqlite/duckdb
  (short-lived connections, file lock release); SQLAlchemy's default pool
  (`QueuePool`) for Postgres (real connection pooling, no file lock to release).
  Each imports its driver package lazily inside this method, raising a clear
  `RuntimeError` if the backend was selected but its extra isn't installed.
- `upsert(table, columns, values, conflict_column)` - a new method replacing the 2
  raw `INSERT OR REPLACE INTO` statements in `database_manager.py`. SQLite and
  DuckDB share an `INSERT OR REPLACE INTO ... VALUES (...)` implementation (both
  support this syntax natively); Postgres implements it via
  `sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update(index_elements=
  [conflict_column], set_=...)`.
- `_with_lock_retry` - DuckDB additionally overrides this to keep the existing
  PID-based stale-lock-clearing behavior (parses `(PID nnnnn)` from DuckDB's error
  message; SQLite's "database is locked" error has no PID, so it just uses the
  base class's generic backoff retry). Postgres overrides this to a plain
  passthrough (call `operation()` directly, no retry) - multi-writer, no file lock
  contention to retry on.

`config.py` gains:

```python
class DatabaseBackend(StrEnum):
    SQLITE = "sqlite"
    DUCKDB = "duckdb"
    POSTGRES = "postgres"

@dataclass
class DatabaseConfig:
    backend: DatabaseBackend = DatabaseBackend.DUCKDB
    path: str | None = None  # sqlite/duckdb file path
    postgres_host: str | None = None
    postgres_port: int = 5432
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None
```

`ConfigManager` reads this from `DATABASE_BACKEND` (default `duckdb`), the existing
`ZULIPCHAT_DB_PATH` (reused, unchanged name), and the new `POSTGRES_*` vars, exposing
it as `config_manager.config.database`.

## Components touched

| File | Change |
|---|---|
| `utils/database.py` | Split into abstract base + 3 subclasses; add `upsert()`; rework execute/query to use `text()` instead of `exec_driver_sql`; central datetime normalization. |
| `utils/database_manager.py` | Only the 2 upsert methods change, to call `self._db.upsert(...)`. |
| `utils/migrations.py` | `sqlalchemy_url`/`make_engine` become per-backend; Postgres skips the DuckDB-only `_needs_legacy_stamp` check (no pre-Alembic Postgres installs exist). |
| `config.py` | `DatabaseBackend` enum, `DatabaseConfig` dataclass, `ConfigManager` wiring. |
| `setup_wizard.py` | New optional "Database Backend" step (mirrors the existing Bot-identity step); adds the chosen env vars to every client-config renderer's output. |
| `server.py` / `claude_hooks.py` | `init_database()` call sites pass `config_manager.config.database`. |
| `pyproject.toml` | New `[project.optional-dependencies]` group: `duckdb` (duckdb, duckdb-engine - removed from the hard dependency list) and `postgres` (psycopg[binary], new). |

`schema.py` and the existing Alembic revision (`0001_initial_schema.py`) are
unchanged - the versioned migration scripts are already portable `op.create_table()`
calls that run against any of the three backends.

## Data flow

Startup: `init_config_manager()` builds `ConfigManager` -> `config_manager.config
.database` (a `DatabaseConfig`) -> `init_database(db_config)` dispatches to the
matching subclass, builds its engine, and runs the existing Alembic migrations
(unchanged scripts; only the URL-building step is backend-aware). At runtime,
`database_manager.py`'s high-level `DatabaseManager` calls `get_database()` once and
never branches on backend itself - all backend differences are hidden behind the
low-level class's polymorphic methods.

`get_database()` becomes strict: it raises `RuntimeError` if `init_database()` hasn't
run yet, matching how `get_config_manager()` already behaves, rather than lazily
defaulting to a hardcoded DuckDB path as it does today.

## Error handling

- **Lock contention**: SQLite and DuckDB retry with backoff (shared base logic,
  DuckDB additionally clears stale locks from dead processes); Postgres does not
  retry - a real connection error still propagates.
- **Missing driver**: selecting duckdb/postgres without their extra installed raises
  a clear, actionable `RuntimeError` at `init_database()` time. This is caught by the
  existing non-fatal try/except in `server.py` (`database_available` guard already
  there) - agent features degrade with a warning rather than crashing, exactly like
  any other DB init failure today.
- **Datetime handling**: centralized `tzinfo` stripping in the base class means no
  behavior difference between backends and no schema divergence.

## Testing

- Unit tests (no real Postgres/DuckDB needed): URL building per backend, `upsert()`
  SQL generation per backend, backend dispatch in `init_database()`, `DatabaseConfig`
  parsing in `config.py`, the wizard's new step and per-client config rendering.
- SQLite's `:memory:` becomes the fixture backend for most of the existing
  `tests/utils/test_database.py` / `test_database_manager.py` suite - fast, in-process,
  no file or container needed.
- Real Postgres (testcontainers) and full parametrization of the existing suite
  across all three backends are explicitly deferred to ticket #5.

## Backward compatibility note (for the MR description / changelog)

Existing deployments that relied on the implicit DuckDB dependency must add
`DATABASE_BACKEND=duckdb` (already the default, no action needed there) **and**
install the `[duckdb]` extra on upgrade, or startup will fail fast with a message
naming the missing extra. This is a deliberate one-time breaking change in exchange
for clean per-backend packaging (a Postgres-only deployment - the actual motivation
for issue #6 - no longer pulls in DuckDB at all).
