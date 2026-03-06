"""Add page_index table for cross-reference resolution.

Revision ID: 002_page_index
Revises: 001_initial
Create Date: 2026-03-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision: str = "002_page_index"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "page_index",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("header_text", sa.String(500), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_page_index_source_page", "page_index", ["source_hash", "page_number"])
    op.create_index("ix_page_index_source_hash", "page_index", ["source_hash"])


def downgrade() -> None:
    op.drop_table("page_index")
