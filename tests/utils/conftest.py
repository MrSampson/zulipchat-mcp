"""Cross-backend fixtures for tests/utils/test_database*.py.

Provides a single `db` fixture, parametrized over duckdb/sqlite/postgres,
that yields a freshly-migrated DatabaseManager instance so the same test
body can assert identical behavior across all three backends. The postgres
variant is marked `integration` (needs Docker) and is backed by a
session-scoped testcontainers Postgres instance, reset to a blank schema
before each test so ad-hoc tables from one test never leak into the next -
the duckdb/sqlite variants get the same isolation for free from pytest's
per-test `tmp_path`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from src.zulipchat_mcp.utils.database import (
    DatabaseManager,
    DuckDBDatabaseManager,
    PostgresDatabaseManager,
    SqliteDatabaseManager,
)

if TYPE_CHECKING:
    from testcontainers.community.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """A session-scoped real Postgres instance.

    Locally, skips every test that requests it (directly or via the `db`
    fixture) when Docker isn't available. In CI, Docker is guaranteed on
    the `ubuntu-latest` runner this project uses, so a failure to start
    there is a real regression - it must fail the run, not silently skip
    it and report a false green on a job that tested nothing.
    """
    from testcontainers.community.postgres import PostgresContainer

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"Postgres testcontainer unavailable (Docker?): {exc}")

    try:
        yield container
    finally:
        container.stop()


def _postgres_url(container: PostgresContainer) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=container.username,
        password=container.password,
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(5432)),
        database=container.dbname,
    )


def _reset_postgres_schema(container: PostgresContainer) -> None:
    """Drop and recreate the public schema so the next PostgresDatabaseManager
    construction runs migrations against a genuinely blank database, matching
    the fresh-file guarantee duckdb/sqlite get from per-test `tmp_path`.
    """
    engine = create_engine(_postgres_url(container))
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture(
    params=[
        "duckdb",
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.integration),
    ]
)
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[DatabaseManager]:
    """A freshly-migrated DatabaseManager for the parametrized backend."""
    backend = request.param
    cls: type[DatabaseManager]
    instance: DatabaseManager

    with patch("src.zulipchat_mcp.utils.database._db_manager", None):
        if backend == "duckdb":
            cls = DuckDBDatabaseManager
            cls._instance = None
            instance = DuckDBDatabaseManager(str(tmp_path / "test.db"))
        elif backend == "sqlite":
            cls = SqliteDatabaseManager
            cls._instance = None
            instance = SqliteDatabaseManager(str(tmp_path / "test.sqlite3"))
        else:
            container = request.getfixturevalue("postgres_container")
            _reset_postgres_schema(container)
            cls = PostgresDatabaseManager
            cls._instance = None
            instance = PostgresDatabaseManager(
                host=container.get_container_host_ip(),
                port=int(container.get_exposed_port(5432)),
                dbname=container.dbname,
                user=container.username,
                password=container.password,
            )

        yield instance

        instance.close()
        cls._instance = None
