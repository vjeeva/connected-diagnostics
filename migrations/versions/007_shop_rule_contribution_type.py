"""Add shop_rule to contribution_type enum and pending_review to shop_rules status.

Revision ID: 007_shop_rule_contribution_type
Revises: 006_shop_rules
Create Date: 2026-03-06
"""
from typing import Sequence, Union

from alembic import op

revision: str = "007_shop_rule_contribution_type"
down_revision: Union[str, None] = "006_shop_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_contributions_type", "contributions")
    op.create_check_constraint(
        "ck_contributions_type", "contributions",
        "contribution_type IN ('new_node', 'alternative', 'annotation', 'attachment', 'cost_update', 'shop_rule')",
    )
    op.drop_constraint("ck_shop_rules_status", "shop_rules")
    op.create_check_constraint(
        "ck_shop_rules_status", "shop_rules",
        "status IN ('active', 'disabled', 'superseded', 'pending_review')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_shop_rules_status", "shop_rules")
    op.create_check_constraint(
        "ck_shop_rules_status", "shop_rules",
        "status IN ('active', 'disabled', 'superseded')",
    )
    op.drop_constraint("ck_contributions_type", "contributions")
    op.create_check_constraint(
        "ck_contributions_type", "contributions",
        "contribution_type IN ('new_node', 'alternative', 'annotation', 'attachment', 'cost_update')",
    )
