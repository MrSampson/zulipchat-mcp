"""Alembic-driven schema migrations for the DuckDB backend.

Only the migration step goes through SQLAlchemy/Alembic; DatabaseManager's
regular query/execute hot path keeps using raw duckdb connections (that
wholesale swap is a separate, later piece of work).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from alembic import command
from alembic.config import Config

INITIAL_REVISION = "0001"
IN_MEMORY_DB_PATH = ":memory:"

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _alembic_config(db_path: str) -> Config:
    # DuckDB's ":memory:" is a magic token, not a real path - resolving it
    # would silently create a file literally named ":memory:" on disk.
    url_path = db_path if db_path == IN_MEMORY_DB_PATH else str(Path(db_path).resolve())
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"duckdb:///{url_path}")
    return cfg


def _table_exists(conn: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [table_name],
    ).fetchone()
    return row is not None


def _needs_legacy_stamp(db_path: str) -> bool:
    """True if this is a database from the pre-Alembic hand-rolled migrator:
    it already has all the real tables (tracked via its own schema_migrations
    table at version 1) but no alembic_version table yet.
    """
    conn = duckdb.connect(db_path)
    try:
        if _table_exists(conn, "alembic_version"):
            return False
        if not _table_exists(conn, "schema_migrations"):
            return False
        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 1"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def run_migrations(db_path: str) -> None:
    """Bring the database at db_path up to the latest schema revision.

    Safe to call on a brand new database file, one already at the latest
    revision (no-op), or one created by the old hand-rolled migrator (gets
    stamped at the initial revision instead of replaying its DDL).
    """
    if db_path != IN_MEMORY_DB_PATH:
        dirname = Path(db_path).parent
        if str(dirname):
            dirname.mkdir(parents=True, exist_ok=True)

    cfg = _alembic_config(db_path)
    if _needs_legacy_stamp(db_path):
        command.stamp(cfg, INITIAL_REVISION)
    command.upgrade(cfg, "head")
