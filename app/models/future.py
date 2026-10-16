"""
Future feature tables (Round B+) — created now as a stable skeleton so that
invoices, ingredient price history, external monitoring and the job queue can
be built later by adding LOGIC, not by migrating the schema.

These are intentionally minimal; columns will be extended in Round B+.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, DateTime, Float, Text, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin


class Invoice(TenantMixin, Base):
    """Uploaded purchase invoice/receipt (image in object storage, OCR result in ocr_raw)."""
    __tablename__ = "invoices"
    supplier: Mapped[Optional[str]] = mapped_column(String(200))
    invoice_number: Mapped[Optional[str]] = mapped_column(String(80))
    invoice_date: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)  # termin płatności
    total: Mapped[Optional[float]] = mapped_column(Float)            # brutto
    total_net: Mapped[Optional[float]] = mapped_column(Float)        # netto
    currency: Mapped[str] = mapped_column(String(8), default="PLN")
    payment_method: Mapped[Optional[str]] = mapped_column(String(16))   # cash|card|transfer
    payment_status: Mapped[str] = mapped_column(String(12), default="unpaid", index=True)  # paid|unpaid
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    category: Mapped[Optional[str]] = mapped_column(String(40))      # food|drinks|overhead|other
    image_url: Mapped[Optional[str]] = mapped_column(String(512))
    ocr_raw: Mapped[Optional[str]] = mapped_column(Text)             # JSON blob from vision model
    notes: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|parsed|confirmed
    __table_args__ = (Index("ix_invoices_tenant_pay", "tenant_id", "payment_status", "due_date"),)


class InvoiceItem(TenantMixin, Base):
    __tablename__ = "invoice_items"
    invoice_id: Mapped[str] = mapped_column(String(36), index=True)
    raw_name: Mapped[str] = mapped_column(String(300))
    matched_ingredient: Mapped[Optional[str]] = mapped_column(String(200))
    quantity: Mapped[Optional[float]] = mapped_column(Float)
    unit: Mapped[Optional[str]] = mapped_column(String(20))
    unit_price: Mapped[Optional[float]] = mapped_column(Float)
    total_price: Mapped[Optional[float]] = mapped_column(Float)
    net_price: Mapped[Optional[float]] = mapped_column(Float)        # cena netto za JM
    vat_percent: Mapped[Optional[float]] = mapped_column(Float)
    category: Mapped[Optional[str]] = mapped_column(String(40))


class IngredientPriceHistory(TenantMixin, Base):
    """The golden table — answers 'butter is up 18%'. Fed by invoices or manual entry."""
    __tablename__ = "ingredient_price_history"
    ingredient: Mapped[str] = mapped_column(String(200), index=True)
    price: Mapped[float] = mapped_column(Float)
    unit: Mapped[Optional[str]] = mapped_column(String(20))
    supplier: Mapped[Optional[str]] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(16), default="manual")  # invoice|manual
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    __table_args__ = (Index("ix_priceh_tenant_ing", "tenant_id", "ingredient", "recorded_at"),)


class ExternalSignal(TenantMixin, Base):
    """Proactive monitoring result: nearby event, roadwork, weather, trend."""
    __tablename__ = "external_signals"
    type: Mapped[str] = mapped_column(String(20), index=True)  # event|roadwork|weather|trend
    title: Mapped[str] = mapped_column(String(300))
    payload: Mapped[Optional[str]] = mapped_column(Text)  # JSON detail
    impact: Mapped[str] = mapped_column(String(10), default="low")  # low|med|high
    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    alerted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class MonitoringJob(TenantMixin, Base):
    """Job queue row — the scheduler enqueues, a ticker consumes. Powers 24/7 monitoring."""
    __tablename__ = "monitoring_jobs"
    job_type: Mapped[str] = mapped_column(String(40), index=True)  # sync|briefing|env_scan|...
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    result: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    __table_args__ = (Index("ix_jobs_tenant_status", "tenant_id", "status", "scheduled_for"),)
