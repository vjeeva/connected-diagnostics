"""Add users, contributions, and contribution_reviews tables.

Revision ID: 003_contributions
Revises: 002_page_index
Create Date: 2026-03-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "003_contributions"
down_revision: Union[str, None] = "002_page_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("user_type", sa.Text(), nullable=False),
        sa.Column("reputation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trust_level", sa.Text(), nullable=False, server_default="standard"),
        sa.Column("trust_source", sa.Text(), nullable=False, server_default="earned"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("user_type IN ('customer', 'technician', 'admin')", name="ck_users_user_type"),
        sa.CheckConstraint("trust_level IN ('standard', 'trusted', 'expert', 'admin')", name="ck_users_trust_level"),
        sa.CheckConstraint("trust_source IN ('invited', 'earned', 'admin_granted')", name="ck_users_trust_source"),
    )

    op.create_table(
        "contributions",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("contribution_type", sa.Text(), nullable=False),
        sa.Column("target_neo4j_node_id", sa.Text(), nullable=True),
        sa.Column("created_neo4j_node_id", sa.Text(), nullable=True),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending_review"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "contribution_type IN ('new_node', 'alternative', 'annotation', 'attachment', 'cost_update')",
            name="ck_contributions_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review', 'published', 'rejected', 'superseded')",
            name="ck_contributions_status",
        ),
    )

    op.create_table(
        "contribution_reviews",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("contribution_id", sa.UUID(), sa.ForeignKey("contributions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reviewer_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("contribution_id", "reviewer_id", name="uq_review_per_user"),
        sa.CheckConstraint("action IN ('approve', 'reject', 'flag')", name="ck_reviews_action"),
    )

    op.create_index("ix_contributions_user_id", "contributions", ["user_id"])
    op.create_index("ix_contributions_target", "contributions", ["target_neo4j_node_id"])
    op.create_index("ix_contributions_status", "contributions", ["status"])


def downgrade() -> None:
    op.drop_table("contribution_reviews")
    op.drop_table("contributions")
    op.drop_table("users")
