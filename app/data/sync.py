"""
Sync Service — pulls data from a tenant's POS into Postgres via the provider
abstraction (build_provider_for_tenant) and tenant-scoped repositories.

Per-tenant and provider-based: works for GoPOS today, any future POS without
changes here. Each tenant's sync is isolated — one failing doesn't affect others.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.core.db import db_session
from app.core.tenant_context import tenant_scope
from app.models.tenant import Tenant, ConnectionStatus
from app.pos.factory import build_provider_for_tenant
from app.repositories.store import TenantRepository

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Pure helper (unit-testable)                                                   #
# --------------------------------------------------------------------------- #

def compute_catchup_from(last_sync_iso, now) -> datetime:
    """Window start for incremental sync (catch-up aware, clamped 1h..7d)."""
    floor = now - timedelta(days=7)
    if not last_sync_iso:
        return now - timedelta(hours=24)
    try:
        last_dt = datetime.fromisoformat(last_sync_iso)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return now - timedelta(hours=1)
    start = min(now - timedelta(hours=1), last_dt - timedelta(minutes=30))
    return max(start, floor)


# --------------------------------------------------------------------------- #
# Per-tenant ingestion                                                          #
# --------------------------------------------------------------------------- #

def _ingest_products_and_shifts(provider, repo, date_from, date_to):
    products = provider.fetch_products()
    if products:
        repo.upsert_products(products)
    shifts = provider.fetch_work_shifts(date_from, date_to)
    if shifts:
        repo.upsert_work_shifts(shifts)
    return len(products), len(shifts)


def _month_bounds_back(now: datetime, months_ago: int):
    """[start, end) of the calendar month `months_ago` before `now` (UTC)."""
    total = (now.year * 12 + (now.month - 1)) - months_ago
    sy, sm = divmod(total, 12)
    start = datetime(sy, sm + 1, 1, tzinfo=timezone.utc)
    ny, nm = divmod(total + 1, 12)
    end = datetime(ny, nm + 1, 1, tzinfo=timezone.utc)
    return start, end


def backfill_tenant(tenant_id: str, months: int = 0, force: bool = False) -> dict:
    """Initial history pull, walking backwards month-by-month.

    months = 0  → full available history (stops after 3 consecutive empty months).
    months > 0  → exactly that many months back.

    Progress is written to sync_state so the onboarding UI can poll it:
      backfill_status  : 'running' | 'done' | 'error'
      backfill_orders  : running total of orders ingested (string int)
      backfill_oldest  : ISO date of the oldest month reached so far
      backfill_label   : month currently being fetched, e.g. '2024-07'
    """
    MAX_MONTHS = 120          # hard ceiling: 10 years
    EMPTY_STOP = 3            # stop after N consecutive empty months (full mode)
    full = (months <= 0)
    limit = MAX_MONTHS if full else min(months, MAX_MONTHS)

    with tenant_scope(tenant_id), db_session() as db:
        tenant = db.query(Tenant).filter_by(id=tenant_id).first()
        if not tenant:
            return {"status": "error", "message": "tenant not found"}
        provider = build_provider_for_tenant(db, tenant)
        if not provider:
            return {"status": "skipped", "message": "POS not connected"}
        repo = TenantRepository(db, tenant_id)
        if not force and repo.get_order_count() > 0:
            return {"status": "skipped",
                    "message": f"DB already has {repo.get_order_count()} orders. Use force=True."}

        now = datetime.now(timezone.utc)
        repo.set_sync_state("backfill_status", "running")
        repo.set_sync_state("backfill_orders", "0")

        total_orders = 0
        consecutive_empty = 0
        oldest_reached = None
        try:
            for m in range(limit):
                start, end = _month_bounds_back(now, m)
                label = start.strftime("%Y-%m")
                repo.set_sync_state("backfill_label", label)
                orders = list(provider.fetch_orders(start.isoformat(), end.isoformat()))
                if orders:
                    n = repo.bulk_upsert_orders(orders)
                    total_orders += n
                    consecutive_empty = 0
                    oldest_reached = start.date().isoformat()
                    repo.set_sync_state("backfill_orders", str(total_orders))
                    repo.set_sync_state("backfill_oldest", oldest_reached)
                    logger.info(f"[{tenant_id}] backfill {label}: +{n} (total {total_orders})")
                else:
                    consecutive_empty += 1
                    if full and consecutive_empty >= EMPTY_STOP:
                        logger.info(f"[{tenant_id}] backfill: {EMPTY_STOP} puste miesiące → koniec historii")
                        break

            # products + shifts over the whole reached window
            repo.set_sync_state("backfill_label", "finalizuję: produkty i kategorie…")
            since_date = oldest_reached or (now - timedelta(days=30)).date().isoformat()
            prod, shifts = _ingest_products_and_shifts(
                provider, repo, since_date, now.date().isoformat())
            # GoPOS order lines lack category — backfill it from the catalog
            resolved = repo.resolve_item_categories()
            repo.set_sync_state("last_sync", now.isoformat())
            repo.set_sync_state("backfill_status", "done")
            logger.info(f"[{tenant_id}] backfill done: {total_orders} orders, {prod} products, "
                        f"{shifts} shifts, {resolved} item categories resolved")
            return {"status": "success", "orders": total_orders, "products": prod,
                    "shifts": shifts, "oldest": oldest_reached}
        except Exception as e:
            repo.set_sync_state("backfill_status", "error")
            logger.error(f"[{tenant_id}] backfill failed: {e}")
            return {"status": "error", "message": str(e), "orders": total_orders}


def incremental_sync_tenant(tenant_id: str, hours=None) -> dict:
    """Pull new/changed orders for one tenant since last sync (idempotent upsert)."""
    with tenant_scope(tenant_id), db_session() as db:
        tenant = db.query(Tenant).filter_by(id=tenant_id).first()
        if not tenant:
            return {"status": "error", "message": "tenant not found"}
        provider = build_provider_for_tenant(db, tenant)
        if not provider:
            return {"status": "skipped", "message": "POS not connected"}

        repo = TenantRepository(db, tenant_id)
        now = datetime.now(timezone.utc)
        if hours is not None:
            from_dt = now - timedelta(hours=hours)
        else:
            from_dt = compute_catchup_from(repo.get_sync_state("last_sync"), now)
        from_iso = from_dt.isoformat()

        orders = list(provider.fetch_orders(from_iso, now.isoformat()))
        count = repo.bulk_upsert_orders(orders) if orders else 0
        # labor over a wider window (overnight edits)
        wt_from = min(from_dt, now - timedelta(hours=48))
        shifts = provider.fetch_work_shifts(wt_from.date().isoformat(), now.date().isoformat())
        if shifts:
            repo.upsert_work_shifts(shifts)
        repo.resolve_item_categories()
        repo.set_sync_state("last_sync", now.isoformat())
        logger.info(f"[{tenant_id}] incremental: {count} orders (from {from_iso[:16]})")
        return {"status": "success", "orders": count, "window_from": from_iso}


def maybe_sync_tenant(tenant_id: str, min_interval_minutes: int = 5) -> dict:
    """Throttled incremental sync for one tenant."""
    with tenant_scope(tenant_id), db_session() as db:
        repo = TenantRepository(db, tenant_id)
        last = repo.get_sync_state("last_sync")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                if age_min < min_interval_minutes:
                    return {"status": "skipped", "reason": "too_recent", "age_minutes": round(age_min, 1)}
            except ValueError:
                pass
    return incremental_sync_tenant(tenant_id)


# --------------------------------------------------------------------------- #
# All-tenant fan-out (scheduler)                                                #
# --------------------------------------------------------------------------- #

def active_tenant_ids() -> list[str]:
    """Tenants with a connected POS — the ones worth syncing."""
    with db_session() as db:
        from app.models.tenant import GoposConnection
        rows = db.query(GoposConnection.tenant_id).filter(
            GoposConnection.status == ConnectionStatus.connected).all()
        return [r[0] for r in rows]


def sync_all_tenants(hours=None) -> dict:
    """Scheduler entry — incremental sync every connected tenant, isolated."""
    results = {}
    for tid in active_tenant_ids():
        try:
            results[tid] = incremental_sync_tenant(tid, hours=hours)
        except Exception as e:
            logger.error(f"[{tid}] sync failed: {e}")
            results[tid] = {"status": "error", "message": str(e)}
    return {"tenants": len(results), "results": results}
