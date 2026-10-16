"""extend invoices for OCR + payment tracking

Revision ID: e1a2c3d4f5b6
Revises: c1f5a7e2b9d0
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa

revision = "e1a2c3d4f5b6"
down_revision = "c1f5a7e2b9d0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invoices") as b:
        b.add_column(sa.Column("invoice_number", sa.String(length=80), nullable=True))
        b.add_column(sa.Column("due_date", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("total_net", sa.Float(), nullable=True))
        b.add_column(sa.Column("currency", sa.String(length=8), nullable=True))
        b.add_column(sa.Column("payment_method", sa.String(length=16), nullable=True))
        b.add_column(sa.Column("payment_status", sa.String(length=12), nullable=True))
        b.add_column(sa.Column("paid_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("category", sa.String(length=40), nullable=True))
        b.add_column(sa.Column("notes", sa.Text(), nullable=True))
    op.execute("UPDATE invoices SET payment_status = 'unpaid' WHERE payment_status IS NULL")
    op.execute("UPDATE invoices SET currency = 'PLN' WHERE currency IS NULL")
    with op.batch_alter_table("invoices") as b:
        b.create_index("ix_invoices_tenant_pay", ["tenant_id", "payment_status", "due_date"])

    with op.batch_alter_table("invoice_items") as b:
        b.add_column(sa.Column("total_price", sa.Float(), nullable=True))
        b.add_column(sa.Column("category", sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table("invoice_items") as b:
        b.drop_column("category")
        b.drop_column("total_price")
    with op.batch_alter_table("invoices") as b:
        b.drop_index("ix_invoices_tenant_pay")
        for c in ["notes", "category", "paid_at", "payment_status", "payment_method",
                  "currency", "total_net", "due_date", "invoice_number"]:
            b.drop_column(c)
