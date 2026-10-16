"""
Repository layer — tenant-scoped data access on PostgreSQL.

Every repository is bound to a SQLAlchemy session AND a tenant_id. The tenant_id
is taken from the request/task context (never from user input), so no query can
accidentally cross tenant boundaries. This is the heart of multi-tenancy.

This module covers the simple CRUD / KV stores. Heavy analytics reads
(orders aggregations, net profit, today, menu engineering) live in
repositories/analytics.py.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, delete, func, text
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant
from app.models import domain as m

logger = logging.getLogger(__name__)


def _apply_rls(db: Session, tenant_id: str):
    """On Postgres, set the per-connection GUC that RLS policies check.
    No-op on SQLite (dev). Safe to call repeatedly."""
    try:
        if db.bind and db.bind.dialect.name == "postgresql":
            db.execute(text("SET app.current_tenant = :tid"), {"tid": tenant_id})
    except Exception as e:
        logger.warning(f"Could not set RLS tenant GUC: {e}")


class TenantRepository:
    """Base — binds a session to the active tenant and auto-scopes queries."""

    def __init__(self, db: Session, tenant_id: Optional[str] = None):
        self.db = db
        self.tenant_id = tenant_id or require_tenant()
        _apply_rls(self.db, self.tenant_id)

    # ---- generic helpers ------------------------------------------------- #

    def _scoped(self, model):
        """SELECT for a model already filtered to this tenant."""
        return select(model).where(model.tenant_id == self.tenant_id)

    def _add(self, obj):
        obj.tenant_id = self.tenant_id  # enforce — never trust caller
        self.db.add(obj)
        return obj

    # ===================================================================== #
    # Key-value style stores (settings, sync_state)                          #
    # ===================================================================== #

    def get_setting(self, key: str, default=None):
        row = self.db.execute(
            self._scoped(m.AppSetting).where(m.AppSetting.key == key)
        ).scalar_one_or_none()
        if row is None or row.value is None:
            return default
        try:
            return json.loads(row.value)
        except (json.JSONDecodeError, TypeError):
            return row.value

    def set_setting(self, key: str, value):
        payload = json.dumps(value, ensure_ascii=False)
        row = self.db.execute(
            self._scoped(m.AppSetting).where(m.AppSetting.key == key)
        ).scalar_one_or_none()
        if row:
            row.value = payload
        else:
            self._add(m.AppSetting(key=key, value=payload))
        self.db.commit()

    def get_sync_state(self, key: str) -> Optional[str]:
        row = self.db.execute(
            self._scoped(m.SyncState).where(m.SyncState.key == key)
        ).scalar_one_or_none()
        return row.value if row else None

    def set_sync_state(self, key: str, value: str):
        row = self.db.execute(
            self._scoped(m.SyncState).where(m.SyncState.key == key)
        ).scalar_one_or_none()
        if row:
            row.value = value
        else:
            self._add(m.SyncState(key=key, value=value))
        self.db.commit()

    # ===================================================================== #
    # Company profile (onboarding audit answers)                             #
    # ===================================================================== #

    def get_profile(self) -> Dict[str, str]:
        rows = self.db.execute(self._scoped(m.CompanyProfile)).scalars().all()
        return {r.key: r.value for r in rows}

    def set_profile_value(self, key: str, value: str):
        row = self.db.execute(
            self._scoped(m.CompanyProfile).where(m.CompanyProfile.key == key)
        ).scalar_one_or_none()
        if row:
            row.value = value
        else:
            self._add(m.CompanyProfile(key=key, value=value))
        self.db.commit()

    # ===================================================================== #
    # Agent memory                                                           #
    # ===================================================================== #

    def save_memory(self, content: str, category: str = "general") -> str:
        obj = self._add(m.AgentMemory(category=category, content=content.strip()))
        self.db.commit()
        return obj.id

    def get_memories(self, limit: int = 50, query: Optional[str] = None) -> List[Dict]:
        stmt = self._scoped(m.AgentMemory)
        if query:
            stmt = stmt.where(m.AgentMemory.content.ilike(f"%{query}%"))
        stmt = stmt.order_by(m.AgentMemory.created_at.desc()).limit(limit)
        rows = self.db.execute(stmt).scalars().all()
        return [{"id": r.id, "category": r.category, "content": r.content,
                 "created_at": r.created_at.isoformat()} for r in rows]

    def delete_memory(self, memory_id: str):
        self.db.execute(delete(m.AgentMemory).where(
            m.AgentMemory.tenant_id == self.tenant_id, m.AgentMemory.id == memory_id))
        self.db.commit()

    # ===================================================================== #
    # Chat history                                                           #
    # ===================================================================== #

    CHAT_THREAD_GAP_HOURS = 12  # idle gap after which a new conversation starts

    def _latest_chat_message(self, session: str):
        return self.db.execute(
            self._scoped(m.ChatMessage).where(m.ChatMessage.session == session)
            .order_by(m.ChatMessage.created_at.desc()).limit(1)
        ).scalars().first()

    def _active_thread_id(self, session: str):
        """The thread the live chat should show — or None if the last convo is stale
        (>12h) or the user asked for a fresh chat. In that case the old thread stays
        in history and a new one starts on the next message."""
        from datetime import datetime, timedelta
        if self.get_setting(f"chat_force_new:{session}") == "1":
            return None
        latest = self._latest_chat_message(session)
        if not latest or not latest.thread_id:
            return None
        if datetime.utcnow() - latest.created_at > timedelta(hours=self.CHAT_THREAD_GAP_HOURS):
            return None
        return latest.thread_id

    def save_chat_message(self, session: str, role: str, content: str, agent: str = "ceo"):
        import uuid
        tid = self._active_thread_id(session)
        if tid is None:
            tid = uuid.uuid4().hex[:32]
            if self.get_setting(f"chat_force_new:{session}") == "1":
                self.set_setting(f"chat_force_new:{session}", "0")
        self._add(m.ChatMessage(session=session, role=role, content=content, agent=agent, thread_id=tid))
        self.db.commit()

    def get_chat_history(self, session: str, limit: int = 20) -> List[Dict]:
        """Only the CURRENT conversation thread (older ones live in history)."""
        tid = self._active_thread_id(session)
        if tid is None:
            return []
        stmt = (self._scoped(m.ChatMessage)
                .where(m.ChatMessage.session == session, m.ChatMessage.thread_id == tid)
                .order_by(m.ChatMessage.created_at.desc()).limit(limit))
        rows = list(self.db.execute(stmt).scalars().all())
        rows.reverse()  # chronological
        return [{"role": r.role, "content": r.content, "agent": r.agent} for r in rows]

    def start_new_chat_thread(self, session: str):
        """Manual 'new chat' — current conversation moves to history, view resets."""
        self.set_setting(f"chat_force_new:{session}", "1")

    def get_chat_threads(self, session: str, limit: int = 40) -> List[Dict]:
        """Past conversations for the history list (newest first), minus the active one."""
        rows = list(self.db.execute(
            self._scoped(m.ChatMessage).where(m.ChatMessage.session == session)
            .order_by(m.ChatMessage.created_at.desc())
        ).scalars().all())
        active = self._active_thread_id(session)
        threads: Dict[str, Dict] = {}
        for r in rows:  # newest → oldest
            if not r.thread_id or r.thread_id == active:
                continue
            t = threads.setdefault(r.thread_id, {
                "thread_id": r.thread_id, "count": 0,
                "started_at": r.created_at, "ended_at": r.created_at, "title": None})
            t["count"] += 1
            t["started_at"] = r.created_at  # rows descend → last write = earliest
            if r.role == "user":
                t["title"] = r.content  # last user seen = earliest user message
        out = []
        for t in threads.values():
            out.append({
                "thread_id": t["thread_id"], "count": t["count"],
                "title": (t["title"] or "Rozmowa").strip()[:80],
                "started_at": t["started_at"].isoformat() if t["started_at"] else None,
                "ended_at": t["ended_at"].isoformat() if t["ended_at"] else None,
            })
        out.sort(key=lambda x: x["ended_at"] or "", reverse=True)
        return out[:limit]

    def get_chat_thread_messages(self, session: str, thread_id: str, limit: int = 200) -> List[Dict]:
        stmt = (self._scoped(m.ChatMessage)
                .where(m.ChatMessage.session == session, m.ChatMessage.thread_id == thread_id)
                .order_by(m.ChatMessage.created_at.asc()).limit(limit))
        rows = self.db.execute(stmt).scalars().all()
        return [{"role": r.role, "content": r.content, "agent": r.agent} for r in rows]

    def clear_chat_history(self, session: str):
        self.db.execute(delete(m.ChatMessage).where(
            m.ChatMessage.tenant_id == self.tenant_id, m.ChatMessage.session == session))
        self.db.commit()

    # ===================================================================== #
    # Overheads                                                              #
    # ===================================================================== #

    def get_overhead_costs(self) -> List[Dict]:
        rows = self.db.execute(
            self._scoped(m.Overhead).order_by(m.Overhead.category)
        ).scalars().all()
        return [{"category": r.category, "monthly_amount": r.monthly_amount,
                 "notes": r.notes or ""} for r in rows]

    def set_overhead_cost(self, category: str, monthly_amount: float, notes: str = ""):
        row = self.db.execute(
            self._scoped(m.Overhead).where(m.Overhead.category == category)
        ).scalar_one_or_none()
        if row:
            row.monthly_amount = monthly_amount
            row.notes = notes
        else:
            self._add(m.Overhead(category=category, monthly_amount=monthly_amount, notes=notes))
        self.db.commit()

    def delete_overhead_cost(self, category: str):
        self.db.execute(delete(m.Overhead).where(
            m.Overhead.tenant_id == self.tenant_id, m.Overhead.category == category))
        self.db.commit()

    # ===================================================================== #
    # Goals                                                                  #
    # ===================================================================== #

    def create_goal(self, title, metric, target_value, direction="increase",
                    deadline=None, notes=None, baseline_value=None) -> str:
        # deadline may arrive as a string (from the LLM or API) — coerce to date.
        if isinstance(deadline, str):
            deadline = deadline.strip()
            if deadline:
                from datetime import datetime as _dt
                parsed = None
                for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
                    try:
                        parsed = _dt.strptime(deadline, fmt).date(); break
                    except ValueError:
                        continue
                deadline = parsed   # None if unparseable — safer than crashing
            else:
                deadline = None
        try:
            tv = float(target_value)
        except (TypeError, ValueError):
            tv = 0.0
        obj = self._add(m.Goal(
            title=title, metric=metric, target_value=tv, direction=direction,
            baseline_value=baseline_value, deadline=deadline, notes=notes, status="active"))
        self.db.commit()
        return obj.id

    def get_goals_raw(self, status: str = "active") -> List[m.Goal]:
        stmt = self._scoped(m.Goal)
        if status:
            stmt = stmt.where(m.Goal.status == status)
        stmt = stmt.order_by(m.Goal.created_at.desc())
        return list(self.db.execute(stmt).scalars().all())

    def update_goal_status(self, goal_id: str, status: str):
        row = self.db.execute(
            self._scoped(m.Goal).where(m.Goal.id == goal_id)
        ).scalar_one_or_none()
        if row:
            row.status = status
            self.db.commit()

    def delete_goal(self, goal_id: str):
        self.db.execute(delete(m.Goal).where(
            m.Goal.tenant_id == self.tenant_id, m.Goal.id == goal_id))
        self.db.commit()

    # ===================================================================== #
    # Invoices (purchase invoices/receipts + price history)                  #
    # ===================================================================== #

    def create_invoice(self, **kw) -> Dict:
        from app.models import future as f
        obj = self._add(f.Invoice(tenant_id=self.tenant_id, **kw))
        self.db.commit()
        return self._invoice_dict(obj, item_count=0)

    def add_invoice_item(self, **kw):
        from app.models import future as f
        self._add(f.InvoiceItem(tenant_id=self.tenant_id, **kw))
        self.db.commit()

    def record_ingredient_price(self, ingredient, price, unit=None, supplier=None,
                                source="invoice", recorded_at=None, category=None):
        from app.models import future as f
        from datetime import datetime as _dt
        self._add(f.IngredientPriceHistory(
            tenant_id=self.tenant_id, ingredient=ingredient, price=price, unit=unit,
            supplier=supplier, category=category, source=source,
            recorded_at=recorded_at or _dt.utcnow()))
        self.db.commit()

    def ingredient_price_signals(self, lookback_days: int = 180,
                                 min_increase_pct: float = 8.0, max_items: int = 8) -> Dict:
        """Read the price-history table to answer two owner questions:
          - 'co podrożało?'  → increases: notable jump between the two most recent buys
          - 'gdzie przepłacam?' → overpaying: same ingredient is cheaper at another supplier
        Comparisons are unit-aware. Returns {'increases':[...], 'overpaying':[...]}."""
        from app.models import future as f
        from datetime import datetime as _dt, timedelta as _td
        cutoff = _dt.utcnow() - _td(days=lookback_days)
        rows = self.db.execute(
            self._scoped(f.IngredientPriceHistory)
            .where(f.IngredientPriceHistory.recorded_at >= cutoff)
            .order_by(f.IngredientPriceHistory.recorded_at.asc())
        ).scalars().all()

        def _norm(s):
            return " ".join((s or "").lower().split())

        by_ing: Dict[str, list] = {}
        for r in rows:
            if r.price:
                by_ing.setdefault(_norm(r.ingredient), []).append(r)

        increases, overpaying = [], []
        for recs in by_ing.values():
            recs.sort(key=lambda x: x.recorded_at)
            latest = recs[-1]
            display, unit = latest.ingredient, latest.unit
            # --- price increase: last two buys of the SAME unit AND SAME supplier,
            # so 'podrożało' means your vendor raised the price (not a supplier switch,
            # which is handled separately as overpaying) ---
            same_unit = [x for x in recs
                         if (x.unit or None) == (unit or None) and x.supplier == latest.supplier]
            if len(same_unit) >= 2:
                prev, last = same_unit[-2], same_unit[-1]
                if prev.price and last.price and prev.price > 0:
                    pct = round((last.price - prev.price) / prev.price * 100, 1)
                    if pct >= min_increase_pct:
                        increases.append({
                            "ingredient": display, "unit": unit,
                            "from": round(prev.price, 2), "to": round(last.price, 2), "pct": pct,
                            "supplier": last.supplier,
                            "prev_date": prev.recorded_at.date().isoformat() if prev.recorded_at else None,
                            "last_date": last.recorded_at.date().isoformat() if last.recorded_at else None,
                        })
            # --- overpaying: cheapest current supplier vs the one you last bought from ---
            by_sup: Dict[str, tuple] = {}
            for x in recs:
                if x.supplier and x.price and (x.unit or None) == (unit or None):
                    if x.supplier not in by_sup or x.recorded_at > by_sup[x.supplier][1]:
                        by_sup[x.supplier] = (x.price, x.recorded_at)
            if len(by_sup) >= 2 and latest.supplier in by_sup:
                cheapest_sup = min(by_sup, key=lambda s: by_sup[s][0])
                cheapest_p = by_sup[cheapest_sup][0]
                if (latest.supplier != cheapest_sup and latest.price
                        and cheapest_p and latest.price > cheapest_p * 1.05):
                    overpaying.append({
                        "ingredient": display, "unit": unit,
                        "you_pay": round(latest.price, 2), "you_supplier": latest.supplier,
                        "cheaper": round(cheapest_p, 2), "cheaper_supplier": cheapest_sup,
                        "save_pct": round((latest.price - cheapest_p) / latest.price * 100, 1),
                    })
        increases.sort(key=lambda x: -x["pct"])
        overpaying.sort(key=lambda x: -x["save_pct"])
        return {"increases": increases[:max_items], "overpaying": overpaying[:max_items]}

    def list_tracked_products(self, lookback_days: int = 365, max_products: int = 300) -> List[Dict]:
        """Every product seen on invoices, aggregated: latest price, suppliers, trend,
        and a short price history (for the Produkty tab)."""
        from app.models import future as f
        from datetime import datetime as _dt, timedelta as _td
        cutoff = _dt.utcnow() - _td(days=lookback_days)
        rows = self.db.execute(
            self._scoped(f.IngredientPriceHistory)
            .where(f.IngredientPriceHistory.recorded_at >= cutoff)
            .order_by(f.IngredientPriceHistory.recorded_at.asc())
        ).scalars().all()

        def _norm(s):
            return " ".join((s or "").lower().split())

        groups: Dict[str, list] = {}
        for r in rows:
            if r.price:
                groups.setdefault(_norm(r.ingredient), []).append(r)

        cat_labels = {"food": "Jedzenie", "drinks": "Napoje", "wine": "Wina",
                      "alcohol": "Napoje alkoholowe", "chemistry": "Chemia",
                      "media": "Media", "other": "Inne"}
        out = []
        for recs in groups.values():
            recs.sort(key=lambda x: x.recorded_at)
            latest, first = recs[-1], recs[0]
            cat = next((x.category for x in reversed(recs) if x.category), None) or "other"
            by_sup: Dict[str, dict] = {}
            for x in recs:
                if x.supplier and (x.supplier not in by_sup or x.recorded_at > by_sup[x.supplier]["date"]):
                    by_sup[x.supplier] = {"price": x.price, "date": x.recorded_at}
            cheapest = min(by_sup.items(), key=lambda kv: kv[1]["price"]) if by_sup else None
            change_pct = (round((latest.price - first.price) / first.price * 100, 1)
                          if first.price and len(recs) >= 2 else None)
            out.append({
                "name": latest.ingredient, "unit": latest.unit,
                "category": cat, "category_label": cat_labels.get(cat, "Inne"),
                "latest_price": round(latest.price, 2), "latest_supplier": latest.supplier,
                "latest_date": latest.recorded_at.date().isoformat() if latest.recorded_at else None,
                "first_price": round(first.price, 2), "change_pct": change_pct,
                "buy_count": len(recs), "supplier_count": len(by_sup),
                "cheapest_supplier": cheapest[0] if cheapest else None,
                "cheapest_price": round(cheapest[1]["price"], 2) if cheapest else None,
                "overpaying": bool(cheapest and latest.supplier and latest.supplier != cheapest[0]
                                   and latest.price > cheapest[1]["price"] * 1.05),
                "suppliers": [{"supplier": s, "price": round(v["price"], 2)}
                              for s, v in sorted(by_sup.items(), key=lambda kv: kv[1]["price"])],
                "history": [{"date": x.recorded_at.date().isoformat(), "price": round(x.price, 2),
                             "supplier": x.supplier} for x in recs[-12:]],
            })
        out.sort(key=lambda p: p["latest_date"] or "", reverse=True)
        return out[:max_products]

    def list_invoices(self, payment_status: Optional[str] = None) -> List[Dict]:
        from app.models import future as f
        q = self._scoped(f.Invoice)
        if payment_status:
            q = q.where(f.Invoice.payment_status == payment_status)
        rows = self.db.execute(q.order_by(f.Invoice.due_date.is_(None),
                                          f.Invoice.due_date.asc(),
                                          f.Invoice.invoice_date.desc())).scalars().all()
        # item counts
        out = []
        for inv in rows:
            cnt = self.db.execute(
                select(func.count()).where(f.InvoiceItem.tenant_id == self.tenant_id,
                                           f.InvoiceItem.invoice_id == inv.id)).scalar() or 0
            out.append(self._invoice_dict(inv, item_count=cnt))
        return out

    def get_invoice(self, invoice_id: str) -> Optional[Dict]:
        from app.models import future as f
        inv = self.db.execute(self._scoped(f.Invoice).where(f.Invoice.id == invoice_id)).scalar_one_or_none()
        if not inv:
            return None
        items = self.db.execute(
            self._scoped(f.InvoiceItem).where(f.InvoiceItem.invoice_id == invoice_id)
        ).scalars().all()
        d = self._invoice_dict(inv, item_count=len(items))
        d["items"] = [{"id": i.id, "name": i.raw_name, "quantity": i.quantity,
                       "unit": i.unit, "unit_price": i.unit_price,
                       "net_price": i.net_price, "vat_percent": i.vat_percent,
                       "total_price": i.total_price, "category": i.category} for i in items]
        d["notes"] = inv.notes
        return d

    def set_invoice_paid(self, invoice_id: str, paid: bool = True) -> bool:
        from app.models import future as f
        from datetime import datetime as _dt
        inv = self.db.execute(self._scoped(f.Invoice).where(f.Invoice.id == invoice_id)).scalar_one_or_none()
        if not inv:
            return False
        inv.payment_status = "paid" if paid else "unpaid"
        inv.paid_at = _dt.utcnow() if paid else None
        self.db.commit()
        return True

    def delete_invoice(self, invoice_id: str):
        from app.models import future as f
        import os as _os
        # remove the stored scan file too (don't leave orphans on disk)
        img = self.get_invoice_image_path(invoice_id)
        # grab supplier + date so we can also purge the price-history this invoice fed
        inv = self.db.execute(self._scoped(f.Invoice).where(f.Invoice.id == invoice_id)).scalar_one_or_none()
        supplier = inv.supplier if inv else None
        inv_date = inv.invoice_date if inv else None
        self.db.execute(delete(f.InvoiceItem).where(
            f.InvoiceItem.tenant_id == self.tenant_id, f.InvoiceItem.invoice_id == invoice_id))
        self.db.execute(delete(f.Invoice).where(
            f.Invoice.tenant_id == self.tenant_id, f.Invoice.id == invoice_id))
        # price-history rows aren't linked by id; match this invoice's supplier + date
        if supplier and inv_date:
            day_lo = inv_date.replace(hour=0, minute=0, second=0, microsecond=0)
            from datetime import timedelta as _td
            self.db.execute(delete(f.IngredientPriceHistory).where(
                f.IngredientPriceHistory.tenant_id == self.tenant_id,
                f.IngredientPriceHistory.supplier == supplier,
                f.IngredientPriceHistory.recorded_at >= day_lo,
                f.IngredientPriceHistory.recorded_at < day_lo + _td(days=1)))
        self.db.commit()
        if img:
            try:
                if _os.path.isfile(img):
                    _os.remove(img)
            except OSError:
                pass

    def invoice_totals(self) -> Dict:
        """Summary for the cost/overview panel: unpaid count + amount + nearest due."""
        from app.models import future as f
        unpaid = self.db.execute(self._scoped(f.Invoice).where(
            f.Invoice.payment_status == "unpaid")).scalars().all()
        unpaid_amount = sum((i.total or 0) for i in unpaid)
        due_dates = [i.due_date for i in unpaid if i.due_date]
        nearest = min(due_dates).isoformat() if due_dates else None
        return {"unpaid_count": len(unpaid), "unpaid_amount": round(unpaid_amount, 2),
                "nearest_due": nearest}

    def invoice_costs_summary(self, days: int = 30) -> Dict:
        """Sum invoiced purchases by category — basis for real cost tracking and
        food-cost verification (food purchases vs revenue). Uses the last `days`;
        if there are no recent invoices but older ones exist, falls back to ALL
        invoices so the breakdown isn't blank. Returns the actual period covered."""
        from app.models import future as f
        from datetime import datetime as _dt, timedelta as _td
        since = _dt.utcnow() - _td(days=days)
        rows = self.db.execute(
            self._scoped(f.Invoice).where(f.Invoice.invoice_date >= since)
        ).scalars().all()
        fallback = False
        if not rows:
            rows = self.db.execute(self._scoped(f.Invoice)).scalars().all()
            fallback = bool(rows)
        by_cat: Dict[str, float] = {}
        total = 0.0
        dates = []
        for inv in rows:
            amt = inv.total or 0.0
            cat = inv.category or "other"
            by_cat[cat] = round(by_cat.get(cat, 0.0) + amt, 2)
            total += amt
            if inv.invoice_date:
                dates.append(inv.invoice_date)
        return {"days": days, "total": round(total, 2), "by_category": by_cat,
                "invoice_count": len(rows), "food_purchases": round(by_cat.get("food", 0.0), 2),
                "period_from": min(dates).date().isoformat() if dates else None,
                "period_to": max(dates).date().isoformat() if dates else None,
                "fallback_all": fallback}

    def get_invoice_image_path(self, invoice_id: str) -> Optional[str]:
        """Raw stored file path for the invoice scan — tenant-scoped, server-side only."""
        from app.models import future as f
        inv = self.db.execute(
            self._scoped(f.Invoice).where(f.Invoice.id == invoice_id)).scalar_one_or_none()
        return inv.image_url if inv else None

    def _invoice_dict(self, inv, item_count=0) -> Dict:
        return {
            "id": inv.id, "supplier": inv.supplier, "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
            "due_date": inv.due_date.isoformat() if inv.due_date else None,
            "total": inv.total, "total_net": inv.total_net, "currency": inv.currency or "PLN",
            "payment_method": inv.payment_method, "payment_status": inv.payment_status,
            "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
            "category": inv.category, "has_image": bool(inv.image_url),
            "is_pdf": bool(inv.image_url and str(inv.image_url).lower().endswith(".pdf")),
            "status": inv.status,
            "item_count": item_count,
        }


    # ===================================================================== #
    # Recipes                                                                #
    # ===================================================================== #

    def get_all_recipes(self) -> List[Dict]:
        recipes = self.db.execute(
            self._scoped(m.Recipe).order_by(m.Recipe.dish_name)
        ).scalars().all()
        out = []
        for r in recipes:
            cnt = self.db.execute(
                self._scoped(m.RecipeIngredient).where(m.RecipeIngredient.dish_name == r.dish_name)
            ).scalars().all()
            out.append({"dish_name": r.dish_name, "notes": r.notes,
                        "selling_price": r.selling_price, "vat_rate": r.vat_rate,
                        "ingredient_count": len(cnt)})
        return out

    def get_recipe(self, dish_name: str) -> Optional[Dict]:
        r = self.db.execute(
            self._scoped(m.Recipe).where(func.lower(m.Recipe.dish_name) == dish_name.lower())
        ).scalar_one_or_none()
        if not r:
            # fuzzy: containment either direction
            for cand in self.db.execute(self._scoped(m.Recipe)).scalars().all():
                dn = cand.dish_name.lower()
                if dn in dish_name.lower() or dish_name.lower() in dn:
                    r = cand
                    break
        if not r:
            return None
        ings = self.db.execute(
            self._scoped(m.RecipeIngredient)
            .where(m.RecipeIngredient.dish_name == r.dish_name)
            .order_by(m.RecipeIngredient.ingredient)
        ).scalars().all()
        return {"dish_name": r.dish_name, "notes": r.notes,
                "selling_price": r.selling_price, "vat_rate": r.vat_rate,
                "ingredients": [{"ingredient": i.ingredient, "quantity": i.quantity,
                                 "unit": i.unit, "cost_per_kg": i.cost_per_kg} for i in ings]}

    def save_recipe(self, dish_name, ingredients, notes="",
                    selling_price=None, vat_rate=None):
        row = self.db.execute(
            self._scoped(m.Recipe).where(m.Recipe.dish_name == dish_name)
        ).scalar_one_or_none()
        if row:
            row.notes = notes
            row.selling_price = selling_price
            row.vat_rate = vat_rate
        else:
            self._add(m.Recipe(dish_name=dish_name, notes=notes,
                               selling_price=selling_price, vat_rate=vat_rate))
        # replace ingredients
        self.db.execute(delete(m.RecipeIngredient).where(
            m.RecipeIngredient.tenant_id == self.tenant_id,
            m.RecipeIngredient.dish_name == dish_name))
        for ing in ingredients:
            self._add(m.RecipeIngredient(
                dish_name=dish_name, ingredient=ing["ingredient"], quantity=ing["quantity"],
                unit=ing["unit"], cost_per_kg=ing["cost_per_kg"]))
        self.db.commit()

    def delete_recipe(self, dish_name: str):
        self.db.execute(delete(m.RecipeIngredient).where(
            m.RecipeIngredient.tenant_id == self.tenant_id,
            m.RecipeIngredient.dish_name == dish_name))
        self.db.execute(delete(m.Recipe).where(
            m.Recipe.tenant_id == self.tenant_id, m.Recipe.dish_name == dish_name))
        self.db.commit()

    # ===================================================================== #
    # INGESTION — persist normalized POS DTOs (orders, products, shifts)     #
    # All upserts are keyed on (tenant_id, external/gopos id).               #
    # ===================================================================== #

    def upsert_order(self, order) -> None:
        """order: app.pos.base.POSOrder"""
        existing = self.db.execute(
            self._scoped(m.Order).where(m.Order.gopos_id == order.external_id)
        ).scalar_one_or_none()
        if existing:
            existing.closed_at = order.closed_at
            existing.status = order.status
            existing.order_type = order.order_type
            existing.total_price = order.total_price
            existing.total_price_net = order.total_price_net
            existing.tip = order.tip
        else:
            self._add(m.Order(
                gopos_id=order.external_id, closed_at=order.closed_at, status=order.status,
                order_type=order.order_type, total_price=order.total_price,
                total_price_net=order.total_price_net, tip=order.tip))

        # Replace this order's items (simplest correct upsert)
        self.db.execute(delete(m.OrderItem).where(
            m.OrderItem.tenant_id == self.tenant_id,
            m.OrderItem.order_gopos_id == order.external_id))
        for it in order.items:
            self._add(m.OrderItem(
                gopos_id=it.external_id, order_gopos_id=order.external_id,
                item_id=it.item_external_id, item_name=it.name,
                category_name=it.category_name, quantity=it.quantity,
                unit_price=it.unit_price, total_price=it.total_price,
                is_removed=it.is_removed, closed_at=order.closed_at))

    def bulk_upsert_orders(self, orders) -> int:
        for o in orders:
            self.upsert_order(o)
        self.db.commit()
        return len(orders)

    def bulk_insert_orders_fresh(self, orders) -> int:
        """
        Fast path for INITIAL backfill into an empty tenant: skips per-row
        SELECT existence checks and uses bulk INSERT mappings (one round-trip
        per batch instead of N). Use only when you know these orders are new
        (e.g. first full backfill). For incremental sync use bulk_upsert_orders.
        """
        order_rows, item_rows = [], []
        for o in orders:
            order_rows.append(dict(
                tenant_id=self.tenant_id, gopos_id=o.external_id, closed_at=o.closed_at,
                status=o.status, order_type=o.order_type, total_price=o.total_price,
                total_price_net=o.total_price_net, tip=o.tip))
            for it in o.items:
                item_rows.append(dict(
                    tenant_id=self.tenant_id, gopos_id=it.external_id,
                    order_gopos_id=o.external_id, item_id=it.item_external_id,
                    item_name=it.name, category_name=it.category_name, quantity=it.quantity,
                    unit_price=it.unit_price, total_price=it.total_price,
                    is_removed=it.is_removed, closed_at=o.closed_at))
        if order_rows:
            self.db.bulk_insert_mappings(m.Order, order_rows)
        if item_rows:
            self.db.bulk_insert_mappings(m.OrderItem, item_rows)
        self.db.commit()
        return len(order_rows)

    def upsert_products(self, products) -> int:
        for p in products:
            existing = self.db.execute(
                self._scoped(m.DimItem).where(m.DimItem.gopos_id == p.external_id)
            ).scalar_one_or_none()
            if existing:
                existing.name = p.name
                existing.category_name = p.category_name
                existing.item_group_name = p.item_group_name
                existing.price = p.price
                existing.tax_id = p.tax_id
                existing.tax_rate = p.tax_rate
                existing.item_type = p.item_type
            else:
                self._add(m.DimItem(
                    gopos_id=p.external_id, name=p.name, category_name=p.category_name,
                    item_group_name=p.item_group_name, price=p.price, tax_id=p.tax_id,
                    tax_rate=p.tax_rate, item_type=p.item_type, is_active=True))
        self.db.commit()
        return len(products)

    def upsert_work_shifts(self, shifts) -> int:
        """shifts: list of app.pos.base.POSWorkShift — RCP labor data."""
        for s in shifts:
            existing = self.db.execute(
                self._scoped(m.WorkTime).where(m.WorkTime.gopos_id == s.external_id)
            ).scalar_one_or_none()
            if existing:
                existing.employee_id = s.employee_external_id
                existing.employee_name = s.employee_name
                existing.workplace_name = s.workplace_name
                existing.start_at = s.start_at
                existing.end_at = s.end_at
                existing.duration_minutes = s.duration_minutes
                existing.hourly_rate = s.hourly_rate
                existing.to_pay = s.to_pay
                existing.status = s.status
            else:
                self._add(m.WorkTime(
                    gopos_id=s.external_id, employee_id=s.employee_external_id,
                    employee_name=s.employee_name, workplace_name=s.workplace_name,
                    start_at=s.start_at, end_at=s.end_at,
                    duration_minutes=s.duration_minutes, hourly_rate=s.hourly_rate,
                    to_pay=s.to_pay, status=s.status))
        self.db.commit()
        return len(shifts)

    def resolve_item_categories(self) -> int:
        """Backfill OrderItem.category_name from the product catalog (dim_items)
        by matching OrderItem.item_id → DimItem.gopos_id. GoPOS order lines don't
        carry a category, so we denormalize it here for category analytics and
        menu engineering. Returns number of rows updated."""
        cat_map = {
            d.gopos_id: d.category_name
            for d in self.db.execute(
                self._scoped(m.DimItem).where(m.DimItem.category_name.isnot(None))
            ).scalars().all()
        }
        if not cat_map:
            return 0
        updated = 0
        rows = self.db.execute(
            self._scoped(m.OrderItem).where(m.OrderItem.category_name.is_(None),
                                            m.OrderItem.item_id.isnot(None))
        ).scalars().all()
        for r in rows:
            cat = cat_map.get(r.item_id)
            if cat:
                r.category_name = cat
                updated += 1
        if updated:
            self.db.commit()
        return updated
        for s in shifts:
            existing = self.db.execute(
                self._scoped(m.WorkTime).where(m.WorkTime.gopos_id == s.external_id)
            ).scalar_one_or_none()
            if existing:
                existing.end_at = s.end_at
                existing.duration_minutes = s.duration_minutes
                existing.hourly_rate = s.hourly_rate
                existing.to_pay = s.to_pay
                existing.status = s.status
            else:
                self._add(m.WorkTime(
                    gopos_id=s.external_id, employee_id=s.employee_external_id,
                    employee_name=s.employee_name, workplace_name=s.workplace_name,
                    start_at=s.start_at, end_at=s.end_at, duration_minutes=s.duration_minutes,
                    hourly_rate=s.hourly_rate, to_pay=s.to_pay, status=s.status))
        self.db.commit()
        return len(shifts)

    def get_order_count(self) -> int:
        return self.db.execute(
            select(func.count()).select_from(m.Order).where(m.Order.tenant_id == self.tenant_id)
        ).scalar() or 0

    # ===================================================================== #
    # Opening hours (rules with effective-from dates)                        #
    # ===================================================================== #

    DEFAULT_OPENING_HOURS = [
        {"from": "1900-01-01", "days": [3, 4, 5, 6], "open": "14:00", "close": "22:00"},
    ]

    def get_opening_rules(self):
        rules = self.get_setting("opening_hours")
        if not rules:
            rules = self.DEFAULT_OPENING_HOURS
            self.set_setting("opening_hours", rules)
        return sorted(rules, key=lambda r: r.get("from", ""))

    def set_opening_rules(self, rules):
        self.set_setting("opening_hours", sorted(rules, key=lambda r: r.get("from", "")))

    # ===================================================================== #
    # Onboarding audit interview state                                       #
    # ===================================================================== #

    AUDIT_QUESTIONS = [
        {"key": "concept", "q": "Jak w kilku zdaniach opisałbyś koncept swojej restauracji? Kuchnia, styl, czym się wyróżniacie?"},
        {"key": "target_audience", "q": "Kto jest Waszym głównym gościem? (np. lunch biznesowy, pary na kolację, rodziny, turyści)"},
        {"key": "location_context", "q": "Gdzie się znajdujecie i jak wygląda okolica? (centrum, dzielnica, co jest obok — biura, atrakcje, konkurencja)"},
        {"key": "seasonality", "q": "Jak wygląda u Was sezonowość? Które miesiące/dni są najmocniejsze, a które najsłabsze?"},
        {"key": "capacity", "q": "Ile macie miejsc siedzących i ilu mniej więcej gości obsługujecie w szczycie?"},
        {"key": "avg_ticket_goal", "q": "Jaki średni rachunek na gościa uznałbyś za dobry cel?"},
        {"key": "rent", "q": "Jaki jest miesięczny czynsz za lokal? (potrzebne do realnego progu rentowności)"},
        {"key": "fixed_costs_other", "q": "Jakie inne stałe koszty miesięczne ponosicie poza czynszem i wynagrodzeniami? (media, księgowość, leasing, ubezpieczenia, marketing)"},
        {"key": "staff_structure", "q": "Jak wygląda Wasz zespół? Ilu kucharzy, kelnerów, czy etaty czy zlecenia?"},
        {"key": "labor_cost_monthly", "q": "Jaki jest Wasz łączny miesięczny koszt wynagrodzeń (brutto z ZUS)? Potrzebny do realnego zysku netto — jeśli nie liczycie godzin w GoPOS, użyję tej kwoty."},
        {"key": "financial_goal", "q": "Jaki jest Twój główny cel finansowy na najbliższe miesiące? (np. marża netto X%, przychód Y zł/mies., drugi lokal)"},
        {"key": "marketing_budget", "q": "Ile miesięcznie przeznaczasz (lub chciałbyś) na marketing i promocję?"},
        {"key": "marketing_channels", "q": "Z jakich kanałów promocji już korzystacie? (Instagram, Google, Facebook, pyszne.pl, współprace, nic)"},
        {"key": "competitors", "q": "Kogo uważasz za główną konkurencję? Podaj 2-3 nazwy — sprawdzę je później w internecie."},
        {"key": "online_presence", "q": "Jaką macie obecność online? (strona www, profile social media — podaj linki/nazwy jeśli możesz)"},
        {"key": "biggest_pain", "q": "Co jest teraz Twoją największą bolączką w prowadzeniu restauracji?"},
        {"key": "biggest_opportunity", "q": "A gdzie widzisz największą niewykorzystaną szansę?"},
        {"key": "menu_strategy", "q": "Jak często zmieniacie menu i kto decyduje o daniach i cenach?"},
        {"key": "success_vision", "q": "Na koniec — jak wygląda Twój obraz sukcesu tej restauracji za rok?"},
    ]

    def get_audit_state(self) -> Dict[str, Any]:
        profile = self.get_profile()
        answered = [q["key"] for q in self.AUDIT_QUESTIONS if profile.get(q["key"])]
        total = len(self.AUDIT_QUESTIONS)
        done = len(answered)
        next_q = next((q for q in self.AUDIT_QUESTIONS if q["key"] not in answered), None)
        return {
            "total_questions": total, "answered": done,
            "progress_pct": round(done / total * 100) if total else 0,
            "complete": done >= total, "next_question": next_q,
            "answered_keys": answered, "has_report": bool(self.get_setting("audit_report")),
        }
