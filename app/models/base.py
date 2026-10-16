"""
SQLAlchemy declarative base + shared mixins.

UUIDs are stored as strings (36 chars) so the same models work on both
PostgreSQL and the SQLite dev fallback without dialect-specific types.
"""

import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def gen_uuid() -> str:
    return str(uuid.uuid4())


class UUIDMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TenantMixin(UUIDMixin, TimestampMixin):
    """
    Every domain table mixes this in: UUID PK, created_at, and a tenant_id FK.
    The tenant_id is indexed for fast per-tenant scans and used by the repository
    layer to scope all queries.
    """
    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
