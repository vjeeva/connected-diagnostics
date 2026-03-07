"""Parts catalog table for OEM part numbers and pricing.

Revision ID: 004_parts_catalog
Revises: 003_contributions
Create Date: 2026-03-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "004_parts_catalog"
down_revision: Union[str, None] = "003_contributions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parts_catalog",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("make", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("year_start", sa.Integer(), nullable=False),
        sa.Column("year_end", sa.Integer(), nullable=False),
        # Part identification
        sa.Column("oem_part_number", sa.String(), nullable=False),
        sa.Column("part_name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # Hierarchy: category > subcategory > diagram
        sa.Column("category", sa.String(), nullable=True),      # e.g. "Transmission and Driveline"
        sa.Column("subcategory", sa.String(), nullable=True),    # e.g. "Valve Body"
        sa.Column("diagram_id", sa.String(), nullable=True),     # SimplePart diagram reference
        sa.Column("diagram_url", sa.String(), nullable=True),    # URL to the parts diagram image
        sa.Column("callout_number", sa.String(), nullable=True), # Position in exploded diagram
        # Pricing
        sa.Column("msrp", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        # Metadata
        sa.Column("superseded_by", sa.String(), nullable=True),  # If part was replaced by newer P/N
        sa.Column("non_reusable", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("quantity_per_assembly", sa.Integer(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )

    # Index for fast lookups by vehicle + part name (what the diagnostic engine needs)
    op.create_index(
        "ix_parts_catalog_vehicle_part",
        "parts_catalog",
        ["make", "model", "year_start", "year_end", "part_name"],
    )
    # Index for direct part number lookups
    op.create_index(
        "ix_parts_catalog_oem_pn",
        "parts_catalog",
        ["oem_part_number"],
    )
    # Index for category browsing
    op.create_index(
        "ix_parts_catalog_category",
        "parts_catalog",
        ["make", "model", "category", "subcategory"],
    )


def downgrade() -> None:
    op.drop_index("ix_parts_catalog_category")
    op.drop_index("ix_parts_catalog_oem_pn")
    op.drop_index("ix_parts_catalog_vehicle_part")
    op.drop_table("parts_catalog")
