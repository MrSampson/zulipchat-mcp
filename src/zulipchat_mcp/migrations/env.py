"""Alembic environment script.

Alembic loads this file by path via its own script loader rather than
Python's normal package import machinery, so relative imports (`from ..`)
don't resolve here - the absolute import below is required, not a style
choice.
"""

from __future__ import annotations

from alembic import context
from alembic.ddl.impl import DefaultImpl
from sqlalchemy import create_engine, pool

from zulipchat_mcp.utils.schema import metadata

config = context.config
target_metadata = metadata


class DuckDBImpl(DefaultImpl):
    """Neither Alembic nor duckdb_engine registers a DDL impl for the
    "duckdb" dialect name, so Alembic can't find one without this. The
    generic DefaultImpl behavior is sufficient for what this project's
    migrations do (CREATE/DROP TABLE via SQLAlchemy Core).
    """

    __dialect__ = "duckdb"


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("sqlalchemy.url is not set on the Alembic config")
    connectable = create_engine(url, poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
