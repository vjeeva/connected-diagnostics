"""Shop rules table for tech-contributed work order corrections.

Revision ID: 006_shop_rules
Revises: 005_parts_region_price_type
Create Date: 2026-03-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006_shop_rules"
down_revision: Union[str, None] = "005_parts_region_price_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shop_rules",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("rule_text", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False, server_default="work_order"),
        sa.Column("scope", sa.Text(), nullable=False, server_default="global"),
        sa.Column("scope_value", sa.Text(), nullable=True),
        sa.Column("contributed_by", sa.Text(), nullable=True),
        sa.Column("source_session", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("category IN ('work_order', 'diagnostic', 'parts', 'general')", name="ck_shop_rules_category"),
        sa.CheckConstraint("scope IN ('global', 'vehicle', 'dtc', 'category')", name="ck_shop_rules_scope"),
        sa.CheckConstraint("status IN ('active', 'disabled', 'superseded')", name="ck_shop_rules_status"),
    )
    op.create_index("ix_shop_rules_scope", "shop_rules", ["scope", "scope_value"])
    op.create_index("ix_shop_rules_category", "shop_rules", ["category"])


def downgrade() -> None:
    op.drop_index("ix_shop_rules_category")
    op.drop_index("ix_shop_rules_scope")
    op.drop_table("shop_rules")
