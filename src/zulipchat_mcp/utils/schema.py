"""Canonical SQLAlchemy Core schema definitions for ZulipChat MCP persistence.

This is the source of truth Alembic's initial migration (``migrations/versions/
0001_initial_schema.py``) builds the database from - see ``utils/migrations.py``.
The future Postgres backend (issue #4) will build on it too.

Integer primary keys declare ``autoincrement=False`` deliberately: without it,
SQLAlchemy's default single-column-integer-PK heuristic compiles to ``SERIAL``
under duckdb_engine's Postgres-derived dialect, which the installed DuckDB
version doesn't support (``Type with name SERIAL does not exist``). The real
DDL these tables mirror never used autoincrement in the first place.

All ``DateTime`` columns here are timezone-naive (matching the hand-written
DDL, and DuckDB's own naive TIMESTAMP), even though database.py writes
``datetime.now(timezone.utc)`` into them. Postgres's ``TIMESTAMP WITHOUT TIME
ZONE`` will inherit the same naive/aware mismatch - a decision to make in
issue #4 (Add PostgreSQL backend), not a bug in this extraction.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    text,
)

metadata = MetaData()

afk_state = Table(
    "afk_state",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("is_afk", Boolean, nullable=False),
    Column("reason", Text),
    Column("auto_return_at", DateTime),
    Column("updated_at", DateTime, nullable=False),
)

agents = Table(
    "agents",
    metadata,
    Column("agent_id", Text, primary_key=True),
    Column("agent_type", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("metadata", Text),
)

agent_instances = Table(
    "agent_instances",
    metadata,
    Column("instance_id", Text, primary_key=True),
    Column("agent_id", Text, ForeignKey("agents.agent_id"), nullable=False),
    Column("session_id", Text),
    Column("project_dir", Text),
    Column("host", Text),
    Column("started_at", DateTime, nullable=False),
)

user_input_requests = Table(
    "user_input_requests",
    metadata,
    Column("request_id", Text, primary_key=True),
    Column("agent_id", Text, nullable=False),
    Column("question", Text, nullable=False),
    Column("context", Text),
    Column("options", Text),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("responded_at", DateTime),
    Column("response", Text),
)

tasks = Table(
    "tasks",
    metadata,
    Column("task_id", Text, primary_key=True),
    Column("agent_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text),
    Column("status", Text, nullable=False),
    Column("progress", Integer),
    Column("started_at", DateTime, nullable=False),
    Column("completed_at", DateTime),
    Column("outputs", Text),
    Column("metrics", Text),
)

agent_status = Table(
    "agent_status",
    metadata,
    Column("status_id", Text, primary_key=True),
    Column("agent_type", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("message", Text),
    Column("created_at", DateTime, nullable=False),
)

streams_cache = Table(
    "streams_cache",
    metadata,
    Column("key", Text, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("fetched_at", DateTime, nullable=False),
)

users_cache = Table(
    "users_cache",
    metadata,
    Column("key", Text, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("fetched_at", DateTime, nullable=False),
)

agent_events = Table(
    "agent_events",
    metadata,
    Column("id", Text, primary_key=True),
    Column("zulip_message_id", Integer),
    Column("topic", Text),
    Column("sender_email", Text),
    Column("content", Text),
    Column("created_at", DateTime),
    Column("acked", Boolean, server_default=text("FALSE")),
)

listener_state = Table(
    "listener_state",
    metadata,
    Column(
        "id", Integer, primary_key=True, autoincrement=False, server_default=text("1")
    ),
    Column("queue_id", Text),
    Column("last_event_id", Integer),
    Column("updated_at", DateTime, nullable=False),
)

agent_profiles = Table(
    "agent_profiles",
    metadata,
    Column("agent_id", Text, primary_key=True),
    Column("agent_name", Text, nullable=False),
    Column("agent_type", Text, nullable=False),
    Column("owner_email", Text, nullable=False),
    Column("stream_name", Text, nullable=False),
    Column("topic_prefix", Text, nullable=False),
    Column("metadata", Text),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

agent_sessions = Table(
    "agent_sessions",
    metadata,
    Column("session_id", Text, primary_key=True),
    Column("agent_id", Text, ForeignKey("agent_profiles.agent_id"), nullable=False),
    Column("external_session_id", Text),
    Column("stream_name", Text, nullable=False),
    Column("topic_name", Text, nullable=False),
    Column("owner_email", Text, nullable=False),
    Column("project_name", Text),
    Column("project_dir", Text),
    Column("host", Text),
    Column("status", Text, nullable=False),
    Column("metadata", Text),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("ended_at", DateTime),
)

agent_requests = Table(
    "agent_requests",
    metadata,
    Column("request_id", Text, primary_key=True),
    Column("agent_id", Text, ForeignKey("agent_profiles.agent_id"), nullable=False),
    Column("session_id", Text, ForeignKey("agent_sessions.session_id"), nullable=False),
    Column("request_type", Text, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("options", Text),
    Column("context", Text),
    Column("status", Text, nullable=False),
    Column("source_event", Text),
    Column("metadata", Text),
    Column("created_at", DateTime, nullable=False),
    Column("responded_at", DateTime),
    Column("response", Text),
)

session_events = Table(
    "session_events",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_id", Text, ForeignKey("agent_profiles.agent_id")),
    Column("session_id", Text, ForeignKey("agent_sessions.session_id")),
    Column("stream_name", Text),
    Column("topic_name", Text),
    Column("sender_email", Text),
    Column("direction", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("content", Text),
    Column("normalized_content", Text),
    Column("command", Text),
    Column("decision", Text),
    Column("request_id", Text),
    Column("metadata", Text),
    Column("created_at", DateTime, nullable=False),
    Column("acked", Boolean, server_default=text("FALSE")),
)
