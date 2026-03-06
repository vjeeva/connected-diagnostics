"""Initial schema — all five tables.

Revision ID: 001_initial
Revises:
Create Date: 2026-03-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "manual_chunks",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("vehicle_neo4j_id", sa.String(), nullable=False),
        sa.Column("neo4j_node_id", sa.String(), nullable=True),
        sa.Column("source_file", sa.String(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("chunk_type", sa.String(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "diagnostic_sessions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("vehicle_neo4j_id", sa.String(), nullable=False),
        sa.Column("starting_problem_neo4j_id", sa.String(), nullable=False),
        sa.Column("final_solution_neo4j_id", sa.String(), nullable=True),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("extracted_dtcs", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "session_steps",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("neo4j_node_id", sa.String(), nullable=False),
        sa.Column("node_type", sa.String(), nullable=False),
        sa.Column("user_answer", sa.Text(), nullable=True),
        sa.Column("llm_interpretation", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "session_estimates",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("solution_neo4j_id", sa.String(), nullable=False),
        sa.Column("labor_rate_used", sa.Float(), nullable=False),
        sa.Column("estimate_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("total_parts_low", sa.Float(), nullable=True),
        sa.Column("total_parts_high", sa.Float(), nullable=True),
        sa.Column("total_labor_low", sa.Float(), nullable=True),
        sa.Column("total_labor_high", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "session_messages",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("session_messages")
    op.drop_table("session_estimates")
    op.drop_table("session_steps")
    op.drop_table("diagnostic_sessions")
    op.drop_table("manual_chunks")
