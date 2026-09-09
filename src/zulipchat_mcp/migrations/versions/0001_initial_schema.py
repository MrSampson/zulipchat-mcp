"""Initial schema

Frozen as of this revision, deliberately not built from utils/schema.py's
live metadata object: a revision must describe the schema at its point in
history, not follow schema.py forward. Importing metadata.create_all() here
would mean this migration silently gains tomorrow's tables and columns the
moment schema.py changes for a future revision - it would create them on
every fresh install before that later revision's own op.create_table() runs,
and collide with it. utils/schema.py stays the source of truth for what a
migrated database should look like; tests/utils/test_schema.py's
test_schema_matches_the_ddl_database_py_actually_executes guards that this
revision (and any later ones) still produces exactly that shape.

Revision ID: 0001
Revises:
Create Date: 2026-09-09

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "afk_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("is_afk", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("auto_return_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agents",
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("agent_type", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_table(
        "agent_instances",
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("project_dir", sa.Text(), nullable=True),
        sa.Column("host", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
        sa.PrimaryKeyConstraint("instance_id"),
    )
    op.create_table(
        "user_input_requests",
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("options", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("responded_at", sa.DateTime(), nullable=True),
        sa.Column("response", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("outputs", sa.Text(), nullable=True),
        sa.Column("metrics", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_table(
        "agent_status",
        sa.Column("status_id", sa.Text(), nullable=False),
        sa.Column("agent_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("status_id"),
    )
    op.create_table(
        "streams_cache",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "users_cache",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "agent_events",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("zulip_message_id", sa.Integer(), nullable=True),
        sa.Column("topic", sa.Text(), nullable=True),
        sa.Column("sender_email", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column(
            "acked", sa.Boolean(), server_default=sa.text("FALSE"), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "listener_state",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=False,
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("queue_id", sa.Text(), nullable=True),
        sa.Column("last_event_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agent_profiles",
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("agent_type", sa.Text(), nullable=False),
        sa.Column("owner_email", sa.Text(), nullable=False),
        sa.Column("stream_name", sa.Text(), nullable=False),
        sa.Column("topic_prefix", sa.Text(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_table(
        "agent_sessions",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("external_session_id", sa.Text(), nullable=True),
        sa.Column("stream_name", sa.Text(), nullable=False),
        sa.Column("topic_name", sa.Text(), nullable=False),
        sa.Column("owner_email", sa.Text(), nullable=False),
        sa.Column("project_name", sa.Text(), nullable=True),
        sa.Column("project_dir", sa.Text(), nullable=True),
        sa.Column("host", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_profiles.agent_id"]),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_table(
        "agent_requests",
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("request_type", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("options", sa.Text(), nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source_event", sa.Text(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("responded_at", sa.DateTime(), nullable=True),
        sa.Column("response", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_profiles.agent_id"]),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.session_id"]),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_table(
        "session_events",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("stream_name", sa.Text(), nullable=True),
        sa.Column("topic_name", sa.Text(), nullable=True),
        sa.Column("sender_email", sa.Text(), nullable=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("normalized_content", sa.Text(), nullable=True),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column(
            "acked", sa.Boolean(), server_default=sa.text("FALSE"), nullable=True
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_profiles.agent_id"]),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.session_id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("session_events")
    op.drop_table("agent_requests")
    op.drop_table("agent_sessions")
    op.drop_table("agent_profiles")
    op.drop_table("listener_state")
    op.drop_table("agent_events")
    op.drop_table("users_cache")
    op.drop_table("streams_cache")
    op.drop_table("agent_status")
    op.drop_table("tasks")
    op.drop_table("user_input_requests")
    op.drop_table("agent_instances")
    op.drop_table("agents")
    op.drop_table("afk_state")
