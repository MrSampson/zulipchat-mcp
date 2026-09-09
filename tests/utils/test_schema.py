"""Tests for utils/schema.py - canonical SQLAlchemy Core schema definitions.

Verifies the SQLAlchemy Core Table objects mirror the hand-written DuckDB DDL in
database.py exactly: same tables, same columns, same nullability, same primary
keys, same foreign keys. No behavior change - nothing consumes this schema yet.
"""

from pathlib import Path

import duckdb
from sqlalchemy import Boolean, DateTime, DefaultClause, Integer, Table, Text

from src.zulipchat_mcp.utils.database import DatabaseManager
from src.zulipchat_mcp.utils.schema import metadata

EXPECTED_TABLES: dict[str, dict[str, object]] = {
    "schema_migrations": {
        "columns": {"version": Integer, "applied_at": DateTime},
        "not_null": set(),
        "primary_key": {"version"},
        "foreign_keys": {},
        "server_defaults": {},
    },
    "afk_state": {
        "columns": {
            "id": Integer,
            "is_afk": Boolean,
            "reason": Text,
            "auto_return_at": DateTime,
            "updated_at": DateTime,
        },
        "not_null": {"is_afk", "updated_at"},
        "primary_key": {"id"},
        "foreign_keys": {},
        "server_defaults": {},
    },
    "agents": {
        "columns": {
            "agent_id": Text,
            "agent_type": Text,
            "created_at": DateTime,
            "metadata": Text,
        },
        "not_null": {"agent_type", "created_at"},
        "primary_key": {"agent_id"},
        "foreign_keys": {},
        "server_defaults": {},
    },
    "agent_instances": {
        "columns": {
            "instance_id": Text,
            "agent_id": Text,
            "session_id": Text,
            "project_dir": Text,
            "host": Text,
            "started_at": DateTime,
        },
        "not_null": {"agent_id", "started_at"},
        "primary_key": {"instance_id"},
        "foreign_keys": {"agent_id": "agents.agent_id"},
        "server_defaults": {},
    },
    "user_input_requests": {
        "columns": {
            "request_id": Text,
            "agent_id": Text,
            "question": Text,
            "context": Text,
            "options": Text,
            "status": Text,
            "created_at": DateTime,
            "responded_at": DateTime,
            "response": Text,
        },
        "not_null": {"agent_id", "question", "status", "created_at"},
        "primary_key": {"request_id"},
        "foreign_keys": {},
        "server_defaults": {},
    },
    "tasks": {
        "columns": {
            "task_id": Text,
            "agent_id": Text,
            "name": Text,
            "description": Text,
            "status": Text,
            "progress": Integer,
            "started_at": DateTime,
            "completed_at": DateTime,
            "outputs": Text,
            "metrics": Text,
        },
        "not_null": {"agent_id", "name", "status", "started_at"},
        "primary_key": {"task_id"},
        "foreign_keys": {},
        "server_defaults": {},
    },
    "agent_status": {
        "columns": {
            "status_id": Text,
            "agent_type": Text,
            "status": Text,
            "message": Text,
            "created_at": DateTime,
        },
        "not_null": {"agent_type", "status", "created_at"},
        "primary_key": {"status_id"},
        "foreign_keys": {},
        "server_defaults": {},
    },
    "streams_cache": {
        "columns": {"key": Text, "payload": Text, "fetched_at": DateTime},
        "not_null": {"payload", "fetched_at"},
        "primary_key": {"key"},
        "foreign_keys": {},
        "server_defaults": {},
    },
    "users_cache": {
        "columns": {"key": Text, "payload": Text, "fetched_at": DateTime},
        "not_null": {"payload", "fetched_at"},
        "primary_key": {"key"},
        "foreign_keys": {},
        "server_defaults": {},
    },
    "agent_events": {
        "columns": {
            "id": Text,
            "zulip_message_id": Integer,
            "topic": Text,
            "sender_email": Text,
            "content": Text,
            "created_at": DateTime,
            "acked": Boolean,
        },
        "not_null": set(),
        "primary_key": {"id"},
        "foreign_keys": {},
        "server_defaults": {"acked": "FALSE"},
    },
    "listener_state": {
        "columns": {
            "id": Integer,
            "queue_id": Text,
            "last_event_id": Integer,
            "updated_at": DateTime,
        },
        "not_null": {"updated_at"},
        "primary_key": {"id"},
        "foreign_keys": {},
        "server_defaults": {"id": "1"},
    },
    "agent_profiles": {
        "columns": {
            "agent_id": Text,
            "agent_name": Text,
            "agent_type": Text,
            "owner_email": Text,
            "stream_name": Text,
            "topic_prefix": Text,
            "metadata": Text,
            "created_at": DateTime,
            "updated_at": DateTime,
        },
        "not_null": {
            "agent_name",
            "agent_type",
            "owner_email",
            "stream_name",
            "topic_prefix",
            "created_at",
            "updated_at",
        },
        "primary_key": {"agent_id"},
        "foreign_keys": {},
        "server_defaults": {},
    },
    "agent_sessions": {
        "columns": {
            "session_id": Text,
            "agent_id": Text,
            "external_session_id": Text,
            "stream_name": Text,
            "topic_name": Text,
            "owner_email": Text,
            "project_name": Text,
            "project_dir": Text,
            "host": Text,
            "status": Text,
            "metadata": Text,
            "created_at": DateTime,
            "updated_at": DateTime,
            "ended_at": DateTime,
        },
        "not_null": {
            "agent_id",
            "stream_name",
            "topic_name",
            "owner_email",
            "status",
            "created_at",
            "updated_at",
        },
        "primary_key": {"session_id"},
        "foreign_keys": {"agent_id": "agent_profiles.agent_id"},
        "server_defaults": {},
    },
    "agent_requests": {
        "columns": {
            "request_id": Text,
            "agent_id": Text,
            "session_id": Text,
            "request_type": Text,
            "prompt": Text,
            "options": Text,
            "context": Text,
            "status": Text,
            "source_event": Text,
            "metadata": Text,
            "created_at": DateTime,
            "responded_at": DateTime,
            "response": Text,
        },
        "not_null": {
            "agent_id",
            "session_id",
            "request_type",
            "prompt",
            "status",
            "created_at",
        },
        "primary_key": {"request_id"},
        "foreign_keys": {
            "agent_id": "agent_profiles.agent_id",
            "session_id": "agent_sessions.session_id",
        },
        "server_defaults": {},
    },
    "session_events": {
        "columns": {
            "id": Text,
            "agent_id": Text,
            "session_id": Text,
            "stream_name": Text,
            "topic_name": Text,
            "sender_email": Text,
            "direction": Text,
            "event_type": Text,
            "content": Text,
            "normalized_content": Text,
            "command": Text,
            "decision": Text,
            "request_id": Text,
            "metadata": Text,
            "created_at": DateTime,
            "acked": Boolean,
        },
        "not_null": {"direction", "event_type", "created_at"},
        "primary_key": {"id"},
        "foreign_keys": {
            "agent_id": "agent_profiles.agent_id",
            "session_id": "agent_sessions.session_id",
        },
        "server_defaults": {"acked": "FALSE"},
    },
}


def test_metadata_contains_exactly_the_expected_tables() -> None:
    assert set(metadata.tables.keys()) == set(EXPECTED_TABLES.keys())


def _foreign_keys_by_column(table: Table) -> dict[str, str]:
    result: dict[str, str] = {}
    for fk in table.foreign_keys:
        result[fk.parent.name] = f"{fk.column.table.name}.{fk.column.name}"
    return result


class TestTableShapes:
    """Each table's columns, nullability, primary key and foreign keys match the DDL."""

    def test_column_names(self) -> None:
        for table_name, spec in EXPECTED_TABLES.items():
            table = metadata.tables[table_name]
            columns = spec["columns"]
            assert isinstance(columns, dict)
            actual = set(table.columns.keys())
            expected = set(columns.keys())
            assert actual == expected, f"{table_name}: {actual} != {expected}"

    def test_column_types(self) -> None:
        for table_name, spec in EXPECTED_TABLES.items():
            table = metadata.tables[table_name]
            columns = spec["columns"]
            assert isinstance(columns, dict)
            for column_name, expected_type in columns.items():
                actual_type = table.c[column_name].type
                # Exact type, not isinstance: a subclass like BigInteger would
                # otherwise silently pass an Integer check despite being a
                # different DDL type (BIGINT vs INTEGER).
                assert type(actual_type) is expected_type, (
                    f"{table_name}.{column_name}: {actual_type!r} is not "
                    f"exactly {expected_type.__name__}"
                )

    def test_server_defaults(self) -> None:
        for table_name, spec in EXPECTED_TABLES.items():
            table = metadata.tables[table_name]
            expected_defaults = spec["server_defaults"]
            assert isinstance(expected_defaults, dict)
            for column in table.columns:
                expected = expected_defaults.get(column.name)
                if expected is None:
                    assert column.server_default is None, (
                        f"{table_name}.{column.name}: unexpected server_default "
                        f"{column.server_default!r}"
                    )
                else:
                    server_default = column.server_default
                    assert isinstance(
                        server_default, DefaultClause
                    ), f"{table_name}.{column.name}: missing server_default"
                    assert str(server_default.arg) == expected, (
                        f"{table_name}.{column.name}: "
                        f"{server_default.arg!r} != {expected!r}"
                    )

    def test_not_null_columns(self) -> None:
        # Primary key columns are implicitly NOT NULL (SQL semantics), so the
        # expected set is the declared not-null columns plus the primary key.
        for table_name, spec in EXPECTED_TABLES.items():
            table = metadata.tables[table_name]
            actual = {c.name for c in table.columns if not c.nullable}
            expected = spec["not_null"] | spec["primary_key"]  # type: ignore[operator]
            assert actual == expected, f"{table_name}: {actual}"

    def test_primary_keys(self) -> None:
        for table_name, spec in EXPECTED_TABLES.items():
            table = metadata.tables[table_name]
            actual = {c.name for c in table.primary_key.columns}
            assert actual == spec["primary_key"], f"{table_name}: {actual}"

    def test_foreign_keys(self) -> None:
        for table_name, spec in EXPECTED_TABLES.items():
            table = metadata.tables[table_name]
            actual = _foreign_keys_by_column(table)
            assert actual == spec["foreign_keys"], f"{table_name}: {actual}"


# DuckDB's catalog reports its own native type names, not what SQLAlchemy's
# generic dialect would compile them to (e.g. Text() compiles generically to
# "TEXT", but DuckDB's catalog reports "VARCHAR"). duckdb_engine - added in
# issue #3 - will handle this properly via a real dialect; hand-mapped here
# since nothing wires schema.py to a DuckDB SQLAlchemy dialect yet.
_DUCKDB_CATALOG_TYPE_NAME: dict[type, str] = {
    Integer: "INTEGER",
    Text: "VARCHAR",
    Boolean: "BOOLEAN",
    DateTime: "TIMESTAMP",
}


def test_schema_matches_the_ddl_database_py_actually_executes(tmp_path: Path) -> None:
    """schema.py mirrors the real DDL DatabaseManager runs - not just EXPECTED_TABLES,
    which is itself hand-transcribed from schema.py and can't catch a transcription
    error made identically in both places.
    """
    db_path = str(tmp_path / "schema_check.duckdb")
    DatabaseManager._instance = None
    DatabaseManager(db_path)
    DatabaseManager._instance = None

    conn = duckdb.connect(db_path, read_only=True)
    try:
        for table_name, table in metadata.tables.items():
            rows = conn.execute(
                "SELECT column_name, data_type, is_nullable FROM duckdb_columns() "
                "WHERE table_name = ? ORDER BY column_index",
                [table_name],
            ).fetchall()
            assert rows, f"{table_name} is missing from the DDL database.py executes"

            expected_names = [c.name for c in table.columns]
            expected_types = [
                _DUCKDB_CATALOG_TYPE_NAME[type(c.type)] for c in table.columns
            ]
            expected_nullable = [c.nullable for c in table.columns]

            assert [r[0] for r in rows] == expected_names, table_name
            assert [r[1] for r in rows] == expected_types, table_name
            assert [bool(r[2]) for r in rows] == expected_nullable, table_name
    finally:
        conn.close()
