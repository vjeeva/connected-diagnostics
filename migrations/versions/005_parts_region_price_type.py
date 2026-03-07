"""Add region and price_type to parts_catalog.

Revision ID: 005_parts_region_price_type
Revises: 004_parts_catalog
Create Date: 2026-03-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005_parts_region_price_type"
down_revision: Union[str, None] = "004_parts_catalog"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parts_catalog", sa.Column("region", sa.String(10), nullable=False, server_default="US"))
    op.add_column("parts_catalog", sa.Column("price_type", sa.String(20), nullable=False, server_default="MSRP"))


def downgrade() -> None:
    op.drop_column("parts_catalog", "price_type")
    op.drop_column("parts_catalog", "region")
