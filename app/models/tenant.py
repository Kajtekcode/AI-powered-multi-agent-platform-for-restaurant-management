"""
Identity & account models — the multi-tenant backbone.

Tenant            = one restaurant account
User              = a person who logs in (owner/manager/staff), belongs to a tenant
GoposConnection   = encrypted GoPOS credentials for a tenant (one per tenant)
"""

import enum
from datetime import datetime
from typing import Optional, List

from sqlalchemy import String, DateTime, ForeignKey, Boolean, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UUIDMixin, TimestampMixin, gen_uuid


class OnboardingStep(str, enum.Enum):
    registered = "registered"
    gopos_connected = "gopos_connected"
    audit_done = "audit_done"
    research_done = "research_done"
    recipes_added = "recipes_added"
    briefing_active = "briefing_active"


# Order matters — used to compare progress (index in this list = rank)
ONBOARDING_ORDER = [
    OnboardingStep.registered,
    OnboardingStep.gopos_connected,
    OnboardingStep.audit_done,
    OnboardingStep.research_done,
    OnboardingStep.recipes_added,
    OnboardingStep.briefing_active,
]


def step_rank(step: "OnboardingStep | str") -> int:
    val = step.value if isinstance(step, OnboardingStep) else step
    for i, s in enumerate(ONBOARDING_ORDER):
        if s.value == val:
            return i
    return -1


class SubscriptionStatus(str, enum.Enum):
    trial = "trial"
    active = "active"
    past_due = "past_due"
    canceled = "canceled"


class UserRole(str, enum.Enum):
    owner = "owner"
    manager = "manager"
    staff = "staff"


class ConnectionStatus(str, enum.Enum):
    pending = "pending"
    connected = "connected"
    error = "error"


class Tenant(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[Optional[str]] = mapped_column(String(120))
    country: Mapped[str] = mapped_column(String(2), default="PL")

    onboarding_step: Mapped[OnboardingStep] = mapped_column(
        SAEnum(OnboardingStep, native_enum=False, length=32),
        default=OnboardingStep.registered, nullable=False,
    )
    subscription_status: Mapped[SubscriptionStatus] = mapped_column(
        SAEnum(SubscriptionStatus, native_enum=False, length=16),
        default=SubscriptionStatus.trial, nullable=False,
    )
    # Which POS this tenant uses — drives provider factory. Default gopos.
    pos_type: Mapped[str] = mapped_column(String(20), default="gopos", nullable=False)

    users: Mapped[List["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    gopos: Mapped[Optional["GoposConnection"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan", uselist=False
    )


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, native_enum=False, length=16), default=UserRole.owner, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    tenant: Mapped["Tenant"] = relationship(back_populates="users")


class GoposConnection(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "gopos_connections"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True
    )
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Fernet-encrypted — NEVER plaintext
    client_id_encrypted: Mapped[str] = mapped_column(String(512), nullable=False)
    client_secret_encrypted: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[ConnectionStatus] = mapped_column(
        SAEnum(ConnectionStatus, native_enum=False, length=16),
        default=ConnectionStatus.pending, nullable=False,
    )
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    tenant: Mapped["Tenant"] = relationship(back_populates="gopos")
