"""invoice items net/vat + price history supplier

Revision ID: f2b3c4d5e6a7
Revises: e1a2c3d4f5b6
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa

revision = "f2b3c4d5e6a7"
down_revision = "e1a2c3d4f5b6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invoice_items") as b:
        b.add_column(sa.Column("net_price", sa.Float(), nullable=True))
        b.add_column(sa.Column("vat_percent", sa.Float(), nullable=True))
    with op.batch_alter_table("ingredient_price_history") as b:
        b.add_column(sa.Column("supplier", sa.String(length=200), nullable=True))


def downgrade():
    with op.batch_alter_table("ingredient_price_history") as b:
        b.drop_column("supplier")
    with op.batch_alter_table("invoice_items") as b:
        b.drop_column("vat_percent")
        b.drop_column("net_price")
