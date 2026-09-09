"""Tests for utils/migrations.py - Alembic-driven schema migrations.

Covers the two real-world entry states `run_migrations` must handle: a
brand new database file, and one already created by the old hand-rolled
`schema_migrations`-table migrator that predates Alembic.
"""

from pathlib import Path

import duckdb
import pytest
from alembic import command
from sqlalchemy import create_engine

from src.zulipchat_mcp.utils.migrations import (
    IN_MEMORY_DB_PATH,
    _alembic_config,
    run_migrations,
)
from src.zulipchat_mcp.utils.schema import metadata

_REAL_TABLES = metadata.sorted_tables
_REAL_TABLE_NAMES = {t.name for t in _REAL_TABLES}


def _table_names(db_path: str) -> set[str]:
    conn = duckdb.connect(db_path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _alembic_version(db_path: str) -> str | None:
    conn = duckdb.connect(db_path, read_only=True)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _seed_legacy_database(db_path: str) -> None:
    """Recreate what the pre-Alembic hand-rolled migrator would have left
    behind: all real tables present, plus its own version-tracking table.
    """
    engine = create_engine(f"duckdb:///{db_path}")
    try:
        with engine.begin() as connection:
            metadata.create_all(bind=connection, tables=_REAL_TABLES)
    finally:
        engine.dispose()

    conn = duckdb.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TIMESTAMP)"
        )
        conn.execute("INSERT INTO schema_migrations VALUES (1, now())")
    finally:
        conn.close()


def test_in_memory_database_does_not_leak_a_literal_memory_file_to_disk(
    tmp_path: Path, monkeypatch
) -> None:
    """':memory:' is DuckDB's magic in-memory token, not a real path. Naively
    resolving it as one (e.g. via Path(db_path).resolve()) silently creates a
    file literally named ':memory:' in the current working directory.
    """
    monkeypatch.chdir(tmp_path)

    run_migrations(IN_MEMORY_DB_PATH)

    assert not (tmp_path / IN_MEMORY_DB_PATH).exists()


def test_fresh_database_creates_all_real_tables(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fresh.duckdb")

    run_migrations(db_path)

    assert _table_names(db_path) >= _REAL_TABLE_NAMES


def test_fresh_database_does_not_create_the_obsolete_schema_migrations_table(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "fresh.duckdb")

    run_migrations(db_path)

    assert "schema_migrations" not in _table_names(db_path)


def test_fresh_database_ends_up_at_the_initial_revision(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fresh.duckdb")

    run_migrations(db_path)

    assert _alembic_version(db_path) == "0001"


def test_running_twice_on_the_same_database_does_not_raise(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fresh.duckdb")

    run_migrations(db_path)
    run_migrations(db_path)

    assert _table_names(db_path) >= _REAL_TABLE_NAMES


def test_legacy_pre_alembic_database_is_stamped_not_replayed(tmp_path: Path) -> None:
    """A database from the old hand-rolled migrator already has every table.
    If run_migrations tried to replay the initial migration's DDL against it,
    CREATE TABLE would fail because the tables already exist - this proves
    it takes the stamp-only path instead.
    """
    db_path = str(tmp_path / "legacy.duckdb")
    _seed_legacy_database(db_path)

    run_migrations(db_path)  # must not raise

    assert _alembic_version(db_path) == "0001"


def test_offline_mode_generates_sql_without_touching_the_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """run_migrations() (production's only entry point) never uses offline
    mode - it's a dev-only path documented in alembic.ini as how to see the
    exact DDL to transcribe into a new hand-written revision (`alembic
    upgrade head --sql`). Prove it actually works rather than leaving it
    silently untested and unused.
    """
    db_path = str(tmp_path / "offline_probe.duckdb")
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "head", sql=True)

    generated_sql = capsys.readouterr().out
    assert "CREATE TABLE afk_state" in generated_sql
    assert not Path(db_path).exists()


def test_downgrade_from_head_drops_every_real_table(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fresh.duckdb")
    run_migrations(db_path)

    command.downgrade(_alembic_config(db_path), "base")

    assert _table_names(db_path).isdisjoint(_REAL_TABLE_NAMES)
