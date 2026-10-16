"""row level security for tenant isolation (Postgres/Neon only)

Second layer of defense: even if a query in application code forgot its
tenant_id filter, Postgres will only return rows matching the tenant set on
the connection via:  SET app.current_tenant = '<uuid>'

This migration is a NO-OP on SQLite (dev) — RLS is a Postgres feature.
Apply on Neon with:  alembic upgrade head

Revision ID: c1f5a7e2b9d0
Revises: b4ab982e6957
"""
from alembic import op
import sqlalchemy as sa

revision = "c1f5a7e2b9d0"
down_revision = "b4ab982e6957"
branch_labels = None
depends_on = None

# Every tenant-scoped table (has a tenant_id column)
TENANT_TABLES = [
    "orders", "order_items", "dim_items", "recipes", "recipe_ingredients",
    "overheads", "work_times", "goals", "agent_memory", "chat_messages",
    "company_profile", "app_settings", "sync_state",
    "invoices", "invoice_items", "ingredient_price_history",
    "external_signals", "monitoring_jobs",
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # RLS is Postgres-only; dev SQLite skips this

    for t in TENANT_TABLES:
        op.execute(f'ALTER TABLE {t} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {t} FORCE ROW LEVEL SECURITY')
        # Policy: row visible only when its tenant_id matches the connection's
        # app.current_tenant GUC. If the GUC is unset, current_setting(...,true)
        # returns NULL and no rows match — safe default (deny).
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {t}
            USING (tenant_id = current_setting('app.current_tenant', true))
            WITH CHECK (tenant_id = current_setting('app.current_tenant', true))
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for t in TENANT_TABLES:
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON {t}')
        op.execute(f'ALTER TABLE {t} DISABLE ROW LEVEL SECURITY')
