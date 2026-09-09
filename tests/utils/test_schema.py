"""Tests for utils/schema.py - canonical SQLAlchemy Core schema definitions.

Verifies the SQLAlchemy Core Table objects mirror the hand-written DuckDB DDL in
database.py exactly: same tables, same columns, same nullability, same primary
keys, same foreign keys. No behavior change - nothing consumes this schema yet.
"""

from sqlalchemy import Boolean, DateTime, Integer, Table, Text

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
            actual = set(table.columns.keys())
            expected = set(spec["columns"].keys())  # type: ignore[union-attr]
            assert actual == expected, f"{table_name}: {actual} != {expected}"

    def test_column_types(self) -> None:
        for table_name, spec in EXPECTED_TABLES.items():
            table = metadata.tables[table_name]
            columns = spec["columns"]
            assert isinstance(columns, dict)
            for column_name, expected_type in columns.items():
                actual_type = table.c[column_name].type
                assert isinstance(actual_type, expected_type), (
                    f"{table_name}.{column_name}: {actual_type!r} is not "
                    f"{expected_type.__name__}"
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
                    assert (
                        column.server_default is not None
                    ), f"{table_name}.{column.name}: missing server_default"
                    assert str(column.server_default.arg) == expected, (
                        f"{table_name}.{column.name}: "
                        f"{column.server_default.arg!r} != {expected!r}"
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
