"""
POS provider abstraction.

The rest of the system (ingestion, repositories, analytics, agents) only ever
sees these NORMALIZED dataclasses — never a vendor's raw JSON. Adding a new POS
(iiko, Toast, Symfonia…) means writing ONE new POSProvider subclass that maps
that vendor's API into these DTOs. Nothing else changes.

Design rule: providers are read-only and stateless w.r.t. our DB. They fetch and
normalize; the ingestion layer persists.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, List, Optional


# --------------------------- normalized DTOs -------------------------------- #

@dataclass
class POSOrderItem:
    external_id: str
    item_external_id: Optional[str]
    name: str
    category_name: Optional[str]
    quantity: float
    unit_price: float
    total_price: float
    is_removed: bool = False


@dataclass
class POSOrder:
    external_id: str
    closed_at: Optional[datetime]
    status: str
    order_type: Optional[str]
    total_price: float
    total_price_net: float
    tip: float = 0.0
    items: List[POSOrderItem] = field(default_factory=list)


@dataclass
class POSProduct:
    external_id: str
    name: str
    category_name: Optional[str]
    item_group_name: Optional[str]
    price: Optional[float]
    tax_id: Optional[str]
    tax_rate: Optional[float]
    item_type: Optional[str]  # PRODUCT | MODIFIER


@dataclass
class POSWorkShift:
    external_id: str
    employee_external_id: Optional[str]
    employee_name: Optional[str]
    workplace_name: Optional[str]
    start_at: Optional[datetime]
    end_at: Optional[datetime]
    duration_minutes: Optional[float]
    hourly_rate: Optional[float]
    to_pay: Optional[float]
    status: Optional[str]


# --------------------------- provider interface ----------------------------- #

class POSProvider(ABC):
    """One implementation per POS vendor. Read-only."""

    #: short stable identifier, e.g. "gopos", "iiko"
    name: str = "abstract"

    @abstractmethod
    def ping(self) -> bool:
        """Verify credentials / connectivity."""

    @abstractmethod
    def fetch_orders(self, since_iso: str, until_iso: Optional[str] = None) -> Iterator[POSOrder]:
        """Yield CLOSED orders (with items) in the time window."""

    @abstractmethod
    def fetch_products(self) -> List[POSProduct]:
        """Full product catalog with prices and VAT rates."""

    @abstractmethod
    def fetch_work_shifts(self, date_from: str, date_to: str) -> List[POSWorkShift]:
        """Labor shifts in the date window."""


# Registry so ingestion can resolve a provider by name (future POS plug-in point)
_PROVIDERS: dict[str, type[POSProvider]] = {}


def register_provider(cls: type[POSProvider]) -> type[POSProvider]:
    _PROVIDERS[cls.name] = cls
    return cls


def get_provider_class(name: str) -> type[POSProvider]:
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown POS provider '{name}'. Registered: {list(_PROVIDERS)}")
    return _PROVIDERS[name]
