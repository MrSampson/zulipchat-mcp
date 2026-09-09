"""Alembic-driven schema migrations for the DuckDB backend."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.pool import NullPool

INITIAL_REVISION = "0001"
IN_MEMORY_DB_PATH = ":memory:"

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def sqlalchemy_url(db_path: str) -> str:
    """Build the duckdb_engine URL for db_path.

    DuckDB's ":memory:" is a magic token, not a real path - resolving it
    would silently create a file literally named ":memory:" on disk.
    """
    url_path = db_path if db_path == IN_MEMORY_DB_PATH else str(Path(db_path).resolve())
    return f"duckdb:///{url_path}"


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


def _alembic_config(db_path: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url(db_path))
    return cfg


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


def run_migrations(db_path: str) -> None:
    """Bring the database at db_path up to the latest schema revision.

    Safe to call on a brand new database file, one already at the latest
    revision (no-op), or one created by the old hand-rolled migrator (gets
    stamped at the initial revision instead of replaying its DDL).
    """
    if db_path != IN_MEMORY_DB_PATH:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    cfg = _alembic_config(db_path)
    if _needs_legacy_stamp(db_path):
        command.stamp(cfg, INITIAL_REVISION)
    command.upgrade(cfg, "head")
