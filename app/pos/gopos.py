"""
GoPOS implementation of POSProvider.

Maps GoPOS's raw JSON into the normalized DTOs. The vendor-specific quirks
(money as {amount,currency}, list under "data", include= strings) are contained
HERE and nowhere else.

Pure mapping functions (parse_*) are separated from the network client so they
can be unit-tested without GoPOS access.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterator, List, Optional

from app.pos.base import (POSProvider, POSOrder, POSOrderItem, POSProduct,
                          POSWorkShift, register_provider)

logger = logging.getLogger(__name__)


def _money(v):
    if isinstance(v, dict):
        return v.get("amount")
    return v


def _dt(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


# --------------------------- pure mappers (testable) ------------------------ #

def parse_order(raw: dict) -> POSOrder:
    items = []
    for it in (raw.get("items") or []):
        # GoPOS item shape: {id(uuid), name, item_id(int), quantity,
        #   unit_price{amount}, total_price{amount}, status}
        # Category isn't on the line — it's resolved later from the product
        # catalog by item_id (see ingestion). 'removed' = status != ACTIVE.
        status = (it.get("status") or "ACTIVE").upper()
        items.append(POSOrderItem(
            external_id=str(it.get("id", "")),
            item_external_id=(str(it.get("item_id")) if it.get("item_id") is not None
                              else (str((it.get("product") or {}).get("id"))
                                    if (it.get("product") or {}).get("id") is not None else None)),
            name=it.get("name") or (it.get("product") or {}).get("name") or "?",
            category_name=((it.get("product") or {}).get("category") or {}).get("name"),
            quantity=float(it.get("quantity") or 0),
            unit_price=float(_money(it.get("unit_price")) or _money(it.get("price")) or 0),
            total_price=float(_money(it.get("total_price"))
                              or _money(it.get("unit_price")) or 0),
            is_removed=(status not in ("ACTIVE", "CLOSED", "PAID")),
        ))
    return POSOrder(
        external_id=str(raw.get("id", "")),
        closed_at=_dt(raw.get("closed_at") or raw.get("created_at")),
        status=raw.get("status") or "CLOSED",
        order_type=raw.get("type"),
        total_price=float(_money(raw.get("total_price")) or 0),
        total_price_net=float(_money(raw.get("total_price_net")) or 0),
        tip=float(_money(raw.get("tip")) or 0),
        items=items,
    )


def parse_product(raw: dict, tax_map: dict) -> POSProduct:
    cat = raw.get("category") or {}
    grp = raw.get("item_group") or {}
    tax_id = raw.get("tax_id")
    return POSProduct(
        external_id=str(raw.get("id", "")),
        name=raw.get("name", ""),
        category_name=cat.get("name") if cat else None,
        item_group_name=grp.get("name") if grp else None,
        price=_money(raw.get("price")),
        tax_id=str(tax_id) if tax_id is not None else None,
        tax_rate=tax_map.get(str(tax_id)) if tax_id is not None else None,
        item_type=raw.get("type"),
    )


def parse_work_shift(raw: dict) -> POSWorkShift:
    emp = raw.get("employee") or {}
    wp = raw.get("workplace") or {}
    return POSWorkShift(
        external_id=str(raw.get("id", "")),
        employee_external_id=(str(raw.get("employee_id")) if raw.get("employee_id")
                              else (str(emp.get("id")) if emp.get("id") else None)),
        employee_name=emp.get("name") if emp else None,
        workplace_name=wp.get("name") if wp else None,
        start_at=_dt(raw.get("start_at")),
        end_at=_dt(raw.get("end_at")),
        duration_minutes=raw.get("duration_in_minutes"),
        hourly_rate=_money(raw.get("hourly_rate")),
        to_pay=_money(raw.get("to_pay")),
        status=raw.get("status"),
    )


# --------------------------- network provider ------------------------------- #

@register_provider
class GoPosProvider(POSProvider):
    name = "gopos"

    def __init__(self, client_id: str, client_secret: str, organization_id: str):
        # Lazy import keeps the heavy client out of pure-mapper tests
        from app.core.gopos_client import GoPosClient
        self._client = GoPosClient(
            client_id=client_id, client_secret=client_secret,
            organization_id=organization_id,
        )

    def ping(self) -> bool:
        try:
            self._client.list_taxes()
            return True
        except Exception as e:
            logger.warning(f"GoPOS ping failed: {e}")
            return False

    def _tax_map(self) -> dict:
        try:
            return {str(t["id"]): t.get("rate") for t in self._client.list_taxes()}
        except Exception:
            return {}

    def fetch_orders(self, since_iso: str, until_iso: Optional[str] = None) -> Iterator[POSOrder]:
        for raw in self._client.list_orders(closed_at_from=since_iso,
                                            closed_at_to=until_iso, status="CLOSED"):
            yield parse_order(raw)

    def fetch_products(self) -> List[POSProduct]:
        tax_map = self._tax_map()
        return [parse_product(p, tax_map) for p in self._client.list_items()]

    def fetch_work_shifts(self, date_from: str, date_to: str) -> List[POSWorkShift]:
        try:
            raws = self._client.list_work_times(date_from=date_from, date_to=date_to)
        except Exception as e:
            logger.warning(f"work_times unavailable: {e}")
            return []
        return [parse_work_shift(w) for w in raws]
