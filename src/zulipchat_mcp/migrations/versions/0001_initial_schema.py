"""Initial schema

Seeded from utils/schema.py's SQLAlchemy Core metadata rather than
hand-written op.create_table() calls, so this migration can never drift
from the canonical schema definitions the same way a hand transcription
could.

Revision ID: 0001
Revises:
Create Date: 2026-09-09

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from zulipchat_mcp.utils.schema import metadata

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
