"""Tests for utils/schema.py - canonical SQLAlchemy Core schema definitions.

Verifies the SQLAlchemy Core Table objects mirror the hand-written DuckDB DDL in
database.py exactly: same tables, same columns, same nullability, same primary
keys, same foreign keys. No behavior change - nothing consumes this schema yet.
"""

from sqlalchemy import Table

from src.zulipchat_mcp.utils.schema import metadata

EXPECTED_TABLES: dict[str, dict[str, object]] = {
    "schema_migrations": {
        "columns": {"version": True, "applied_at": True},
        "not_null": set(),
        "primary_key": {"version"},
        "foreign_keys": {},
    },
    "afk_state": {
        "columns": {
            "id": True,
            "is_afk": True,
            "reason": True,
            "auto_return_at": True,
            "updated_at": True,
        },
        "not_null": {"is_afk", "updated_at"},
        "primary_key": {"id"},
        "foreign_keys": {},
    },
    "agents": {
        "columns": {
            "agent_id": True,
            "agent_type": True,
            "created_at": True,
            "metadata": True,
        },
        "not_null": {"agent_type", "created_at"},
        "primary_key": {"agent_id"},
        "foreign_keys": {},
    },
    "agent_instances": {
        "columns": {
            "instance_id": True,
            "agent_id": True,
            "session_id": True,
            "project_dir": True,
            "host": True,
            "started_at": True,
        },
        "not_null": {"agent_id", "started_at"},
        "primary_key": {"instance_id"},
        "foreign_keys": {"agent_id": "agents.agent_id"},
    },
    "user_input_requests": {
        "columns": {
            "request_id": True,
            "agent_id": True,
            "question": True,
            "context": True,
            "options": True,
            "status": True,
            "created_at": True,
            "responded_at": True,
            "response": True,
        },
        "not_null": {"agent_id", "question", "status", "created_at"},
        "primary_key": {"request_id"},
        "foreign_keys": {},
    },
    "tasks": {
        "columns": {
            "task_id": True,
            "agent_id": True,
            "name": True,
            "description": True,
            "status": True,
            "progress": True,
            "started_at": True,
            "completed_at": True,
            "outputs": True,
            "metrics": True,
        },
        "not_null": {"agent_id", "name", "status", "started_at"},
        "primary_key": {"task_id"},
        "foreign_keys": {},
    },
    "agent_status": {
        "columns": {
            "status_id": True,
            "agent_type": True,
            "status": True,
            "message": True,
            "created_at": True,
        },
        "not_null": {"agent_type", "status", "created_at"},
        "primary_key": {"status_id"},
        "foreign_keys": {},
    },
    "streams_cache": {
        "columns": {"key": True, "payload": True, "fetched_at": True},
        "not_null": {"payload", "fetched_at"},
        "primary_key": {"key"},
        "foreign_keys": {},
    },
    "users_cache": {
        "columns": {"key": True, "payload": True, "fetched_at": True},
        "not_null": {"payload", "fetched_at"},
        "primary_key": {"key"},
        "foreign_keys": {},
    },
    "agent_events": {
        "columns": {
            "id": True,
            "zulip_message_id": True,
            "topic": True,
            "sender_email": True,
            "content": True,
            "created_at": True,
            "acked": True,
        },
        "not_null": set(),
        "primary_key": {"id"},
        "foreign_keys": {},
    },
    "listener_state": {
        "columns": {
            "id": True,
            "queue_id": True,
            "last_event_id": True,
            "updated_at": True,
        },
        "not_null": {"updated_at"},
        "primary_key": {"id"},
        "foreign_keys": {},
    },
    "agent_profiles": {
        "columns": {
            "agent_id": True,
            "agent_name": True,
            "agent_type": True,
            "owner_email": True,
            "stream_name": True,
            "topic_prefix": True,
            "metadata": True,
            "created_at": True,
            "updated_at": True,
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
    },
    "agent_sessions": {
        "columns": {
            "session_id": True,
            "agent_id": True,
            "external_session_id": True,
            "stream_name": True,
            "topic_name": True,
            "owner_email": True,
            "project_name": True,
            "project_dir": True,
            "host": True,
            "status": True,
            "metadata": True,
            "created_at": True,
            "updated_at": True,
            "ended_at": True,
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
    },
    "agent_requests": {
        "columns": {
            "request_id": True,
            "agent_id": True,
            "session_id": True,
            "request_type": True,
            "prompt": True,
            "options": True,
            "context": True,
            "status": True,
            "source_event": True,
            "metadata": True,
            "created_at": True,
            "responded_at": True,
            "response": True,
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
    },
    "session_events": {
        "columns": {
            "id": True,
            "agent_id": True,
            "session_id": True,
            "stream_name": True,
            "topic_name": True,
            "sender_email": True,
            "direction": True,
            "event_type": True,
            "content": True,
            "normalized_content": True,
            "command": True,
            "decision": True,
            "request_id": True,
            "metadata": True,
            "created_at": True,
            "acked": True,
        },
        "not_null": {"direction", "event_type", "created_at"},
        "primary_key": {"id"},
        "foreign_keys": {
            "agent_id": "agent_profiles.agent_id",
            "session_id": "agent_sessions.session_id",
        },
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
