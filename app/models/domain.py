"""
Domain models — the actual restaurant data, every table scoped by tenant_id
(via TenantMixin). These mirror the DuckDB tables from the single-tenant version,
now multi-tenant on Postgres.
"""

from datetime import datetime, date
from typing import Optional

from sqlalchemy import String, DateTime, Date, Float, Integer, Boolean, Text, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin


class Order(TenantMixin, Base):
    __tablename__ = "orders"
    gopos_id: Mapped[str] = mapped_column(String(64), index=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    status: Mapped[Optional[str]] = mapped_column(String(32))
    order_type: Mapped[Optional[str]] = mapped_column(String(32))
    total_price: Mapped[float] = mapped_column(Float, default=0.0)
    total_price_net: Mapped[float] = mapped_column(Float, default=0.0)
    tip: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (Index("ix_orders_tenant_closed", "tenant_id", "closed_at"),
                      Index("ix_orders_tenant_gopos", "tenant_id", "gopos_id", unique=True))


class OrderItem(TenantMixin, Base):
    __tablename__ = "order_items"
    gopos_id: Mapped[str] = mapped_column(String(64), index=True)
    order_gopos_id: Mapped[str] = mapped_column(String(64), index=True)
    item_id: Mapped[Optional[str]] = mapped_column(String(64))
    item_name: Mapped[str] = mapped_column(String(300), index=True)
    category_name: Mapped[Optional[str]] = mapped_column(String(200))
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    total_price: Mapped[float] = mapped_column(Float, default=0.0)
    is_removed: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    __table_args__ = (Index("ix_oitems_tenant_closed", "tenant_id", "closed_at"),
                      Index("ix_oitems_tenant_name", "tenant_id", "item_name"))


class DimItem(TenantMixin, Base):
    __tablename__ = "dim_items"
    gopos_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    category_name: Mapped[Optional[str]] = mapped_column(String(200))
    item_group_name: Mapped[Optional[str]] = mapped_column(String(200))
    price: Mapped[Optional[float]] = mapped_column(Float)
    tax_id: Mapped[Optional[str]] = mapped_column(String(64))
    tax_rate: Mapped[Optional[float]] = mapped_column(Float)
    item_type: Mapped[Optional[str]] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Recipe(TenantMixin, Base):
    __tablename__ = "recipes"
    dish_name: Mapped[str] = mapped_column(String(300), index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    selling_price: Mapped[Optional[float]] = mapped_column(Float)
    vat_rate: Mapped[Optional[float]] = mapped_column(Float)


class RecipeIngredient(TenantMixin, Base):
    __tablename__ = "recipe_ingredients"
    dish_name: Mapped[str] = mapped_column(String(300), index=True)
    ingredient: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    unit: Mapped[str] = mapped_column(String(20), default="g")
    cost_per_kg: Mapped[float] = mapped_column(Float, default=0.0)


class Overhead(TenantMixin, Base):
    __tablename__ = "overheads"
    category: Mapped[str] = mapped_column(String(120), index=True)
    monthly_amount: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text)


class WorkTime(TenantMixin, Base):
    __tablename__ = "work_times"
    gopos_id: Mapped[str] = mapped_column(String(64), index=True)
    employee_id: Mapped[Optional[str]] = mapped_column(String(64))
    employee_name: Mapped[Optional[str]] = mapped_column(String(200))
    workplace_name: Mapped[Optional[str]] = mapped_column(String(120))
    start_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    duration_minutes: Mapped[Optional[float]] = mapped_column(Float)
    hourly_rate: Mapped[Optional[float]] = mapped_column(Float)
    to_pay: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[Optional[str]] = mapped_column(String(32))


class Goal(TenantMixin, Base):
    __tablename__ = "goals"
    title: Mapped[str] = mapped_column(String(300))
    metric: Mapped[str] = mapped_column(String(40))
    target_value: Mapped[float] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(String(10), default="increase")
    baseline_value: Mapped[Optional[float]] = mapped_column(Float)
    deadline: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)


class AgentMemory(TenantMixin, Base):
    __tablename__ = "agent_memory"
    category: Mapped[str] = mapped_column(String(40), default="general")
    content: Mapped[str] = mapped_column(Text)


class ChatMessage(TenantMixin, Base):
    __tablename__ = "chat_messages"
    session: Mapped[str] = mapped_column(String(20), index=True)  # web | telegram
    role: Mapped[str] = mapped_column(String(16))
    agent: Mapped[str] = mapped_column(String(20), default="ceo")
    content: Mapped[str] = mapped_column(Text)
    # Conversations are grouped into threads; a new thread starts after a 12h gap
    # (or a manual "new chat"). Old threads move to history instead of piling up.
    thread_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)


class CompanyProfile(TenantMixin, Base):
    __tablename__ = "company_profile"
    # one row per profile key per tenant
    key: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    __table_args__ = (Index("ix_profile_tenant_key", "tenant_id", "key", unique=True),)


class AppSetting(TenantMixin, Base):
    __tablename__ = "app_settings"
    # opening_hours, audit_report, etc. — JSON stored as text
    key: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    __table_args__ = (Index("ix_settings_tenant_key", "tenant_id", "key", unique=True),)


class SyncState(TenantMixin, Base):
    __tablename__ = "sync_state"
    key: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    __table_args__ = (Index("ix_sync_tenant_key", "tenant_id", "key", unique=True),)
