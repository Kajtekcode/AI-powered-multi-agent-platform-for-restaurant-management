"""
Analytics repository — tenant-scoped reads on PostgreSQL.

Written portably: aggregate queries use SQLAlchemy functions and date-range
BETWEEN comparisons (work identically on Postgres and the SQLite dev fallback).
Anything that needs hour/weekday shaping is computed in Python from fetched rows
to avoid dialect-specific date functions.

Covers the financial core: sales summaries (rolling + exact period), food cost,
labor cost, real net profit (rolling + exact calendar period), and TODAY /
breakeven. Menu engineering & projections come in the next batch.
"""

import logging
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func, and_, cast, Date
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant
from app.models import domain as m

logger = logging.getLogger(__name__)


class AnalyticsRepository:
    def __init__(self, db: Session, tenant_id: Optional[str] = None):
        self.db = db
        self.tenant_id = tenant_id or require_tenant()
        from app.repositories.store import _apply_rls
        _apply_rls(self.db, self.tenant_id)

    # ---- internal helpers ------------------------------------------------ #

    def _t(self, model):
        return model.tenant_id == self.tenant_id

    def reference_now(self) -> datetime:
        """Latest closed order timestamp, or now() if empty. Keeps demo/historic data sensible."""
        latest = self.db.execute(
            select(func.max(m.Order.closed_at)).where(
                self._t(m.Order), m.Order.status == "CLOSED")
        ).scalar()
        return latest or datetime.utcnow()

    def reference_range(self, days: int):
        end = self.reference_now()
        return end - timedelta(days=days), end

    # ===================================================================== #
    # Sales summaries                                                        #
    # ===================================================================== #

    def _bounds(self, dt_from, dt_to, by_date: bool):
        """Inclusive comparison bounds for closed_at. When by_date, expand a date
        to full-day datetime bounds (00:00:00 → 23:59:59) and compare the RAW
        datetime column. This avoids CAST(x AS DATE), which silently returns no
        rows on SQLite — so the same query works on both SQLite and Postgres."""
        if not by_date:
            return dt_from, dt_to
        from datetime import date as _date, time as _time

        def _to_date(x):
            if isinstance(x, datetime):
                return x.date()
            if isinstance(x, _date):
                return x
            return datetime.strptime(str(x)[:10], "%Y-%m-%d").date()

        lo = datetime.combine(_to_date(dt_from), _time.min)
        hi = datetime.combine(_to_date(dt_to), _time.max)
        return lo, hi

    def _sales_between(self, dt_from, dt_to, by_date: bool) -> Dict[str, Any]:
        col = m.Order.closed_at
        lo, hi = self._bounds(dt_from, dt_to, by_date)
        row = self.db.execute(
            select(func.count(), func.coalesce(func.sum(m.Order.total_price), 0.0),
                   func.coalesce(func.avg(m.Order.total_price), 0.0),
                   func.coalesce(func.sum(m.Order.tip), 0.0),
                   func.min(m.Order.closed_at), func.max(m.Order.closed_at))
            .where(self._t(m.Order), m.Order.status == "CLOSED", and_(col >= lo, col <= hi))
        ).one()
        return {
            "order_count": row[0] or 0,
            "total_revenue": round(float(row[1]), 2),
            "avg_ticket": round(float(row[2]), 2),
            "total_tips": round(float(row[3]), 2),
            "actual_data_from": (row[4].date().isoformat() if isinstance(row[4], datetime)
                                 else (str(row[4])[:10] if row[4] else None)),
            "actual_data_to": (row[5].date().isoformat() if isinstance(row[5], datetime)
                               else (str(row[5])[:10] if row[5] else None)),
        }

    def get_sales_summary(self, days: int = 30) -> Dict[str, Any]:
        lo, hi = self.reference_range(days)
        d = self._sales_between(lo, hi, by_date=False)
        d["period_days"] = days
        return d

    def get_sales_summary_prev(self, days: int = 30) -> Dict[str, Any]:
        """Summary for the period BEFORE the current one (for % comparison)."""
        end = self.reference_now() - timedelta(days=days)
        start = end - timedelta(days=days)
        return self._sales_between(start, end, by_date=False)

    def get_sales_summary_range(self, date_from: str, date_to: str) -> Dict[str, Any]:
        d = self._sales_between(date_from, date_to, by_date=True)
        complete = bool(d["order_count"] > 0 and d["actual_data_from"]
                        and d["actual_data_from"] <= date_from and d["actual_data_to"] >= date_to)
        d.update({"requested_from": date_from, "requested_to": date_to,
                  "data_complete": complete,
                  "note": ("Brak zamówień w tym okresie — brak danych lub niezsynchronizowane."
                           if d["order_count"] == 0 else None)})
        return d

    def get_sales_by_month(self) -> List[Dict]:
        rows = self.db.execute(
            select(m.Order.closed_at, m.Order.total_price)
            .where(self._t(m.Order), m.Order.status == "CLOSED", m.Order.closed_at.isnot(None))
        ).all()
        agg: Dict[str, Dict[str, float]] = {}
        for closed_at, total in rows:
            key = closed_at.strftime("%Y-%m")
            a = agg.setdefault(key, {"orders": 0, "revenue": 0.0})
            a["orders"] += 1
            a["revenue"] += float(total or 0)
        return [{"month": k, "orders": v["orders"], "revenue": round(v["revenue"], 2),
                 "avg_ticket": round(v["revenue"] / v["orders"], 2) if v["orders"] else 0}
                for k, v in sorted(agg.items())]

    # ===================================================================== #
    # Food cost (uses recipes from the store repo)                           #
    # ===================================================================== #

    def _dish_food_cost(self, store, dish_name: str) -> Optional[Dict]:
        recipe = store.get_recipe(dish_name)
        if not recipe or not recipe["ingredients"]:
            return None
        total = 0.0
        for ing in recipe["ingredients"]:
            unit = (ing["unit"] or "g").lower()
            factor = ing["quantity"] / 1000 if unit in ("g", "ml") else ing["quantity"]
            total += factor * ing["cost_per_kg"]
        res = {"dish_name": recipe["dish_name"], "total_cost": round(total, 4)}
        sp = recipe.get("selling_price")
        if sp:
            vat = recipe.get("vat_rate") or 0.0
            net = sp / (1 + vat / 100)
            res.update({"selling_price_net": round(net, 2),
                        "margin": round(net - total, 2),
                        "margin_pct": round((net - total) / net * 100, 1) if net > 0 else None,
                        "food_cost_pct_of_net": round(total / net * 100, 1) if net > 0 else None})
        return res

    def _food_cost_between(self, store, lo, hi, by_date: bool) -> Dict[str, Any]:
        summ = self._sales_between(lo, hi, by_date)
        revenue = summ["total_revenue"]
        col = m.OrderItem.closed_at
        clo, chi = self._bounds(lo, hi, by_date)

        total_fc = 0.0
        breakdown = []
        for r in store.get_all_recipes():
            cost_data = self._dish_food_cost(store, r["dish_name"])
            if not cost_data:
                continue
            units = self.db.execute(
                select(func.coalesce(func.sum(m.OrderItem.quantity), 0.0)).where(
                    self._t(m.OrderItem), m.OrderItem.is_removed.is_(False),
                    func.lower(m.OrderItem.item_name).like(f"%{r['dish_name'].lower()}%"),
                    and_(col >= clo, col <= chi))
            ).scalar() or 0.0
            units = float(units)
            if units > 0:
                dish_cost = cost_data["total_cost"] * units
                total_fc += dish_cost
                breakdown.append({"dish_name": r["dish_name"], "units_sold": units,
                                  "cost_per_dish": cost_data["total_cost"],
                                  "total_cost": round(dish_cost, 2)})
        return {
            "full_period_revenue": revenue,
            "total_food_cost": round(total_fc, 2),
            "food_cost_pct": round(total_fc / revenue * 100, 2) if revenue > 0 else 0.0,
            "dishes_with_recipes": len(breakdown),
            "dish_breakdown": sorted(breakdown, key=lambda x: -x["total_cost"]),
        }

    def calculate_food_cost_pct(self, store, days: int = 30) -> Dict[str, Any]:
        lo, hi = self.reference_range(days)
        d = self._food_cost_between(store, lo, hi, by_date=False)
        d["period_days"] = days
        return d

    def calculate_dish_food_cost(self, store, dish_name: str) -> Optional[Dict]:
        """Public single-dish food cost (food cost %, margin) for the recipes UI."""
        return self._dish_food_cost(store, dish_name)

    def get_all_recipes_with_costs(self, store) -> List[Dict]:
        """Recipes list enriched with computed food cost + margin per dish."""
        out = []
        for r in store.get_all_recipes():
            cd = self._dish_food_cost(store, r["dish_name"])
            out.append({**r,
                        "food_cost": cd["total_cost"] if cd else None,
                        "margin": cd.get("margin") if cd else None,
                        "margin_pct": cd.get("margin_pct") if cd else None,
                        "food_cost_pct_of_net": cd.get("food_cost_pct_of_net") if cd else None})
        return out

    # ===================================================================== #
    # Labor                                                                  #
    # ===================================================================== #

    def _labor_between(self, lo, hi, by_date: bool) -> Dict[str, Any]:
        col = m.WorkTime.start_at
        clo, chi = self._bounds(lo, hi, by_date)
        rows = self.db.execute(
            select(m.WorkTime.duration_minutes, m.WorkTime.hourly_rate,
                   m.WorkTime.to_pay, m.WorkTime.employee_name)
            .where(self._t(m.WorkTime), and_(col >= clo, col <= chi))
        ).all()
        shifts = len(rows)
        total_hours = sum((r[0] or 0) for r in rows) / 60.0
        total_cost = 0.0
        per_emp: Dict[str, Dict[str, float]] = {}
        for dur, rate, to_pay, name in rows:
            cost = to_pay if to_pay is not None else (dur or 0) / 60.0 * (rate or 0)
            total_cost += cost
            e = per_emp.setdefault(name or "Nieznany", {"shifts": 0, "hours": 0.0, "cost": 0.0, "rates": []})
            e["shifts"] += 1
            e["hours"] += (dur or 0) / 60.0
            e["cost"] += cost
            if rate:
                e["rates"].append(rate)

        summ = self._sales_between(lo, hi, by_date)
        revenue = summ["total_revenue"]
        return {
            "shifts": shifts, "total_hours": round(total_hours, 1),
            "total_cost": round(total_cost, 2), "revenue": revenue,
            "labor_pct": round(total_cost / revenue * 100, 1) if revenue > 0 else None,
            "has_data": shifts > 0,
            "by_employee": [
                {"employee": k, "shifts": v["shifts"], "hours": round(v["hours"], 1),
                 "avg_rate": round(sum(v["rates"]) / len(v["rates"]), 2) if v["rates"] else None,
                 "cost": round(v["cost"], 2)}
                for k, v in sorted(per_emp.items(), key=lambda kv: -kv[1]["cost"])
            ],
            "benchmark_note": "Branżowy cel labor cost: 25-35% przychodu",
        }

    def get_labor_cost(self, days: int = 30) -> Dict[str, Any]:
        lo, hi = self.reference_range(days)
        d = self._labor_between(lo, hi, by_date=False)
        d["period_days"] = days
        return d

    def get_labor_cost_range(self, date_from: str, date_to: str) -> Dict[str, Any]:
        d = self._labor_between(date_from, date_to, by_date=True)
        d.update({"date_from": date_from, "date_to": date_to})
        return d

    # ===================================================================== #
    # Real net profit                                                        #
    # ===================================================================== #

    @staticmethod
    def _parse_money(text) -> Optional[float]:
        """Best-effort parse of a free-text PLN amount: '16 000', '16000 zł', '16 tys', '20k'."""
        if text is None:
            return None
        import re as _re
        s = str(text).lower().replace(" ", " ")
        mult = 1000 if ("tys" in s or _re.search(r"\d\s*k(\b|\s|$)", s)) else 1
        m = _re.search(r"\d[\d \.]*(,\d+)?", s)
        if not m:
            return None
        num = m.group(0).replace(" ", "").replace(".", "").replace(",", ".")
        try:
            v = float(num) * mult
            return v if v > 0 else None
        except ValueError:
            return None

    @staticmethod
    def _sum_money(text) -> float:
        """Sum every PLN amount mentioned in free text. '4000 zł, 800 zł, 1,5 tys' → 6300."""
        if text is None:
            return 0.0
        import re as _re
        total = 0.0
        for m in _re.finditer(r"\d[\d \.]*(?:,\d+)?\s*(tys|k\b)?", str(text).lower()):
            raw = m.group(0)
            mult = 1000 if (m.group(1)) else 1
            num = _re.sub(r"[^\d,\.]", "", raw).replace(".", "").replace(",", ".")
            try:
                v = float(num) * mult
                if v > 0:
                    total += v
            except ValueError:
                continue
        return total

    def _overhead_from_profile(self, store) -> list:
        """When no overhead is explicitly tracked, derive it from the audit profile:
        declared rent + the sum of other fixed costs. Lets net profit be honest the
        moment the audit is done, before the user fills the cost tracker by hand."""
        prof = store.get_profile() or {}
        out = []
        rent = self._parse_money(prof.get("rent"))
        if rent:
            out.append({"category": "Czynsz", "monthly_amount": round(rent, 2), "source": "audyt"})
        other = self._sum_money(prof.get("fixed_costs_other"))
        if other:
            out.append({"category": "Pozostałe koszty stałe", "monthly_amount": round(other, 2), "source": "audyt"})
        return out

    def _resolve_overheads(self, store):
        """Tracked overhead if present, else declared-from-audit. Returns (list, source)."""
        oh = store.get_overhead_costs()
        if oh:
            return oh, "tracked"
        derived = self._overhead_from_profile(store)
        return derived, ("audyt" if derived else "none")

    def _labor_cost_for_period(self, store, labor, period_days):
        """Returns (labor_cost, source). GoPOS RCP if available, else the audit's declared
        monthly payroll prorated, else 0. 'source' ∈ {'rcp','declared','none'}.

        Guard: freshly-adopted RCP covers only a slice of the staff, so its total can
        be far below reality. If RCP reports well under the declared payroll for the
        same period, the declared figure is the honest one — otherwise net profit
        would be silently overstated the day RCP goes live."""
        rcp = float(labor.get("total_cost") or 0.0) if labor.get("has_data") else None
        declared_pd = None
        try:
            declared = self._parse_money((store.get_profile() or {}).get("labor_cost_monthly"))
            if declared:
                declared_pd = round(declared / 30.0 * period_days, 2)
        except Exception:
            pass
        if rcp is not None:
            if declared_pd and rcp < 0.6 * declared_pd:
                return declared_pd, "declared"  # RCP too incomplete to trust yet
            return rcp, "rcp"
        if declared_pd:
            return declared_pd, "declared"
        return 0.0, "none"

    def calculate_real_net_profit(self, store, days: int = 30) -> Dict[str, Any]:
        fc = self.calculate_food_cost_pct(store, days)
        overheads, oh_source = self._resolve_overheads(store)
        labor = self.get_labor_cost(days)
        monthly_oh = sum(o["monthly_amount"] for o in overheads)
        prorated_oh = monthly_oh / 30 * days
        revenue = fc["full_period_revenue"]
        food = fc["total_food_cost"]
        labor_cost, labor_src = self._labor_cost_for_period(store, labor, days)
        has_labor = labor_src != "none"
        net = revenue - food - labor_cost - prorated_oh
        # Honesty about what the figure includes
        missing = []
        if not has_labor:
            missing.append("koszty pracy")
        if not overheads:
            missing.append("koszty stałe")
        if fc["dishes_with_recipes"] == 0:
            missing.append("food cost (brak receptur)")
        complete = len(missing) == 0
        basis_note = ("Pełny zysk netto (przychód − food cost − praca − koszty stałe)."
                      if complete else
                      "Zysk NIEPEŁNY — brak: " + ", ".join(missing) +
                      ". To zysk brutto pomniejszony o dostępne koszty.")
        return {
            "period_days": days, "revenue": revenue, "food_cost": food,
            "food_cost_pct": fc["food_cost_pct"], "labor_cost": round(labor_cost, 2),
            "labor_pct": round(labor_cost / revenue * 100, 1) if revenue > 0 else None,
            "labor_has_data": has_labor, "labor_source": labor_src,
            "overhead": round(prorated_oh, 2), "overhead_monthly_total": round(monthly_oh, 2),
            "overhead_source": oh_source,
            "net_profit": round(net, 2),
            "net_margin_pct": round(net / revenue * 100, 2) if revenue > 0 else 0.0,
            "overhead_breakdown": overheads,
            "complete": complete, "missing_components": missing, "basis_note": basis_note,
            "double_count_warning": self._payroll_warning(overheads, has_labor),
        }

    def calculate_real_net_profit_range(self, store, date_from: str, date_to: str) -> Dict[str, Any]:
        d_from = date.fromisoformat(date_from[:10])
        d_to = date.fromisoformat(date_to[:10])
        period_days = (d_to - d_from).days + 1
        if period_days <= 0:
            return {"error": "date_to must be >= date_from"}

        coverage = self.get_sales_summary_range(date_from, date_to)
        fc = self._food_cost_between(store, date_from, date_to, by_date=True)
        labor = self.get_labor_cost_range(date_from, date_to)
        overheads, oh_source = self._resolve_overheads(store)
        monthly_oh = sum(o["monthly_amount"] for o in overheads)
        prorated_oh = monthly_oh / 30 * period_days
        revenue = fc["full_period_revenue"]
        food = fc["total_food_cost"]
        labor_cost, labor_src = self._labor_cost_for_period(store, labor, period_days)
        has_labor = labor_src != "none"
        net = revenue - food - labor_cost - prorated_oh

        basis = [
            "Przychód i sprzedane sztuki: rzeczywiste dane z tego okresu.",
            ("Koszty pracy: rzeczywiste grafiki z tego okresu." if labor_src == "rcp"
             else ("Koszty pracy: szacunek z deklarowanego miesięcznego kosztu wynagrodzeń." if labor_src == "declared"
                   else "Koszty pracy: brak danych (przyjęto 0).")),
            "Food cost: sztuki z okresu × AKTUALNE receptury i ceny.",
            (f"Koszty stałe: AKTUALNE stawki proporcjonalnie ({period_days} dni)." if oh_source == "tracked"
             else (f"Koszty stałe: z audytu (czynsz + pozostałe), proporcjonalnie ({period_days} dni)." if oh_source == "audyt"
                   else "Koszty stałe: brak danych (przyjęto 0).")),
        ]
        return {
            "date_from": date_from, "date_to": date_to, "period_days": period_days,
            "order_count": coverage["order_count"],
            "data_coverage": {"actual_from": coverage.get("actual_data_from"),
                              "actual_to": coverage.get("actual_data_to"),
                              "complete": coverage.get("data_complete", False)},
            "revenue": revenue, "food_cost": food, "food_cost_pct": fc["food_cost_pct"],
            "dishes_with_recipes": fc["dishes_with_recipes"],
            "labor_cost": round(labor_cost, 2),
            "labor_pct": round(labor_cost / revenue * 100, 1) if revenue > 0 else None,
            "labor_has_data": has_labor, "labor_source": labor_src, "overhead": round(prorated_oh, 2),
            "overhead_monthly_total": round(monthly_oh, 2), "overhead_source": oh_source,
            "net_profit": round(net, 2),
            "net_margin_pct": round(net / revenue * 100, 2) if revenue > 0 else 0.0,
            "basis_note": " ".join(basis),
            "double_count_warning": self._payroll_warning(overheads, labor["has_data"]),
        }

    @staticmethod
    def _payroll_warning(overheads, labor_has_data) -> Optional[str]:
        kw = ("pracownic", "płac", "place", "payroll", "wynagrodz", "pensj", "kadr")
        hits = [o["category"] for o in overheads if any(k in o["category"].lower() for k in kw)]
        if labor_has_data and hits:
            return (f"UWAGA: kategoria kosztów stałych ({', '.join(hits)}) + realne grafiki "
                    f"= możliwe podwójne liczenie płac.")
        return None

    # ===================================================================== #
    # TODAY + breakeven (revenue today, breakeven point)                     #
    # ===================================================================== #

    def get_today_basics(self, store) -> Dict[str, Any]:
        now = datetime.now()  # "today" = the real calendar day; live data arrives via GoPOS sync
        today = now.date()
        day_lo = datetime.combine(today, datetime.min.time())
        day_hi = day_lo + timedelta(days=1)
        row = self.db.execute(
            select(func.count(), func.coalesce(func.sum(m.Order.total_price), 0.0),
                   func.coalesce(func.avg(m.Order.total_price), 0.0), func.max(m.Order.closed_at))
            .where(self._t(m.Order), m.Order.status == "CLOSED",
                   and_(m.Order.closed_at >= day_lo, m.Order.closed_at < day_hi))
        ).one()
        orders_today, revenue_today = row[0] or 0, float(row[1])
        avg_ticket, last_order = float(row[2]), (row[3].strftime("%H:%M") if isinstance(row[3], datetime) else None)

        lab = self.db.execute(
            select(func.count(),
                   func.coalesce(func.sum(
                       func.coalesce(m.WorkTime.to_pay,
                                     m.WorkTime.duration_minutes / 60.0 * func.coalesce(m.WorkTime.hourly_rate, 0))), 0.0))
            .where(self._t(m.WorkTime),
                   and_(m.WorkTime.start_at >= day_lo, m.WorkTime.start_at < day_hi))
        ).one()
        labor_shifts, labor_cost_today = lab[0] or 0, float(lab[1])

        # Daily fixed costs = overhead (tracked, else declared from audit) + labor.
        overheads, _oh_src = self._resolve_overheads(store)
        daily_oh = sum(o["monthly_amount"] for o in overheads) / 30.0
        fc = self.calculate_food_cost_pct(store, days=30)
        fc_pct = fc["food_cost_pct"] if fc["dishes_with_recipes"] > 0 else 0.0

        # Labor for the breakeven: real shifts today if logged, otherwise the declared
        # monthly payroll prorated to one day (so the threshold isn't falsely 0).
        labor_be, labor_be_est = labor_cost_today, False
        if labor_be <= 0:
            declared = self._parse_money((store.get_profile() or {}).get("labor_cost_monthly"))
            if declared:
                labor_be, labor_be_est = round(declared / 30.0, 2), True

        fixed_today = daily_oh + labor_be
        breakeven = fixed_today / (1 - fc_pct / 100) if 0 < fc_pct < 95 else fixed_today
        vs = revenue_today - breakeven
        return {
            "date": today.isoformat(), "now": now.strftime("%H:%M"),
            "orders": orders_today, "revenue": round(revenue_today, 2),
            "avg_ticket": round(avg_ticket, 2), "last_order": last_order,
            "labor": {"shifts": labor_shifts, "cost": round(labor_cost_today, 2)},
            "breakeven": {
                "revenue_needed": round(breakeven, 2), "daily_overhead": round(daily_oh, 2),
                "labor_today": round(labor_be, 2), "labor_estimated": labor_be_est,
                "food_cost_pct_used": fc_pct,
                "vs_breakeven": round(vs, 2), "above": vs >= 0,
                "estimated": (daily_oh > 0 and _oh_src == "audyt") or labor_be_est,
                "progress_pct": round(min(revenue_today / breakeven * 100, 999), 1) if breakeven > 0 else None,
            },
        }

    # ===================================================================== #
    # TODAY — full (projection, pace, hourly curve, top items)               #
    # ===================================================================== #

    def get_today_live(self, store) -> Dict[str, Any]:
        basics = self.get_today_basics(store)
        now = datetime.now()  # "today" = the real calendar day; live data arrives via GoPOS sync
        today = now.date()
        day_lo = datetime.combine(today, datetime.min.time())
        day_hi = day_lo + timedelta(days=1)

        # opening hours for today + where we are in the day
        hours = self._opening_hours_for(store, today)
        day_phase = "open"
        if hours is None:
            day_phase = "closed_today"          # not an open weekday this season
        else:
            try:
                oh, om = (int(x) for x in str(hours["open"]).split(":")[:2])
                ch, cm = (int(x) for x in str(hours["close"]).split(":")[:2])
                minute_now = now.hour * 60 + now.minute
                if minute_now < oh * 60 + om:
                    day_phase = "before_open"
                elif minute_now >= ch * 60 + cm:
                    day_phase = "after_close"
            except (ValueError, KeyError, AttributeError):
                pass

        # top items today
        rows = self.db.execute(
            select(m.OrderItem.item_name, func.sum(m.OrderItem.quantity),
                   func.sum(m.OrderItem.total_price))
            .where(self._t(m.OrderItem), m.OrderItem.is_removed.is_(False),
                   and_(m.OrderItem.closed_at >= day_lo, m.OrderItem.closed_at < day_hi))
            .group_by(m.OrderItem.item_name)
            .order_by(func.sum(m.OrderItem.total_price).desc()).limit(5)
        ).all()
        top_items = [{"item_name": r[0], "units": float(r[1] or 0), "revenue": round(float(r[2] or 0), 2)} for r in rows]

        # today's hourly + projection + pace (computed in Python — portable)
        hourly_today = self._hourly_for_day(today)
        weekday = today.weekday()
        typical_hourly, typical_days = self._typical_hourly_for_weekday(weekday, today)
        projection = self._project_eod(now, basics["revenue"], typical_hourly, typical_days)
        pace = self._pace_vs_last_week(now, basics["revenue"])

        # When today is empty/closed, surface the last day that actually sold —
        # far more useful than a wall of zeros.
        recent_day = self._last_sales_day(today) if (basics.get("orders") or 0) == 0 else None

        basics.update({
            "weekday": weekday, "is_open_day": hours is not None, "hours": hours,
            "day_phase": day_phase,
            "top_items": top_items, "hourly_today": hourly_today,
            "hourly_typical": [{"hour": h, "revenue": round(v, 2)} for h, v in sorted(typical_hourly.items())],
            "projection": projection, "pace": pace, "recent_day": recent_day,
        })
        return basics

    def _last_sales_day(self, before_day) -> Optional[Dict[str, Any]]:
        """Most recent day (before today) that had sales — for the 'today is empty' view."""
        lo = datetime.combine(before_day, datetime.min.time())
        row = self.db.execute(
            select(func.date(m.Order.closed_at), func.count(),
                   func.coalesce(func.sum(m.Order.total_price), 0.0))
            .where(self._t(m.Order), m.Order.status == "CLOSED", m.Order.closed_at < lo)
            .group_by(func.date(m.Order.closed_at))
            .order_by(func.date(m.Order.closed_at).desc()).limit(1)
        ).first()
        if not row or not row[0]:
            return None
        d = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0])[:10])
        PL = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
        return {"date": d.isoformat(), "weekday_label": PL[d.weekday()],
                "revenue": round(float(row[2]), 2), "orders": int(row[1] or 0)}

    def _hourly_for_day(self, day) -> List[Dict]:
        lo = datetime.combine(day, datetime.min.time())
        hi = lo + timedelta(days=1)
        rows = self.db.execute(
            select(m.Order.closed_at, m.Order.total_price)
            .where(self._t(m.Order), m.Order.status == "CLOSED",
                   and_(m.Order.closed_at >= lo, m.Order.closed_at < hi))
        ).all()
        by_hour: Dict[int, float] = {}
        for closed_at, total in rows:
            by_hour[closed_at.hour] = by_hour.get(closed_at.hour, 0.0) + float(total or 0)
        return [{"hour": h, "revenue": round(v, 2)} for h, v in sorted(by_hour.items())]

    def _typical_hourly_for_weekday(self, weekday: int, today, lookback_days: int = 70):
        """Avg revenue per hour for this weekday over recent occurrences. Python-side."""
        start = datetime.combine(today, datetime.min.time()) - timedelta(days=lookback_days)
        rows = self.db.execute(
            select(m.Order.closed_at, m.Order.total_price)
            .where(self._t(m.Order), m.Order.status == "CLOSED",
                   m.Order.closed_at >= start, m.Order.closed_at < datetime.combine(today, datetime.min.time()))
        ).all()
        # group by day, keep only same weekday
        per_day: Dict[Any, Dict[int, float]] = {}
        for closed_at, total in rows:
            if closed_at.weekday() != weekday:
                continue
            d = closed_at.date()
            per_day.setdefault(d, {})
            per_day[d][closed_at.hour] = per_day[d].get(closed_at.hour, 0.0) + float(total or 0)
        days = sorted(per_day.keys(), reverse=True)[:8]
        if not days:
            return {}, 0
        hour_sums: Dict[int, float] = {}
        for d in days:
            for h, v in per_day[d].items():
                hour_sums[h] = hour_sums.get(h, 0.0) + v
        typical = {h: v / len(days) for h, v in hour_sums.items()}
        return typical, len(days)

    def _project_eod(self, now, revenue_so_far, typical_hourly, sample_days) -> Dict[str, Any]:
        if not typical_hourly or sample_days == 0:
            return {"available": False, "note": "Brak historii dla tego dnia tygodnia."}
        typical_total = sum(typical_hourly.values())
        cur_h, frac = now.hour, now.minute / 60.0
        cum_by_now = sum(v for h, v in typical_hourly.items() if h < cur_h)
        cum_by_now += typical_hourly.get(cur_h, 0.0) * frac
        share = cum_by_now / typical_total if typical_total > 0 else 0.0
        if share < 0.05:
            return {"available": True, "reliable": False, "basis": "typical",
                    "projected_total": round(typical_total, 2), "typical_total": round(typical_total, 2),
                    "sample_days": sample_days,
                    "note": "Za wcześnie na projekcję z tempa — pokazuję typowy wynik tego dnia."}
        projected = revenue_so_far / share if share > 0 else None
        vs = round((projected - typical_total) / typical_total * 100, 1) if (projected and typical_total > 0) else None
        return {"available": True, "reliable": True, "basis": "pace",
                "projected_total": round(projected, 2) if projected else None,
                "share_of_day_elapsed": round(share * 100, 1),
                "typical_total": round(typical_total, 2), "vs_typical_pct": vs, "sample_days": sample_days}

    def _pace_vs_last_week(self, now, revenue_today) -> Dict[str, Any]:
        lw = (now - timedelta(days=7)).date()
        lo = datetime.combine(lw, datetime.min.time())
        hi = lo + timedelta(days=1)
        minute_of_day = now.hour * 60 + now.minute
        rows = self.db.execute(
            select(m.Order.closed_at, m.Order.total_price)
            .where(self._t(m.Order), m.Order.status == "CLOSED",
                   and_(m.Order.closed_at >= lo, m.Order.closed_at < hi))
        ).all()
        lw_by_now = sum(float(t or 0) for c, t in rows if (c.hour * 60 + c.minute) <= minute_of_day)
        lw_full = sum(float(t or 0) for c, t in rows)
        pace = round((revenue_today - lw_by_now) / lw_by_now * 100, 1) if lw_by_now > 0 else None
        return {"last_week_by_now": round(lw_by_now, 2), "last_week_total": round(lw_full, 2),
                "pace_vs_last_week_pct": pace}

    def _opening_hours_for(self, store, d) -> Optional[Dict]:
        rules = store.get_setting("opening_hours")
        if not rules:
            return {"open": "00:00", "close": "23:59"}  # unknown → treat as open
        date_str = d.isoformat()
        applicable = None
        for rule in sorted(rules, key=lambda r: r.get("from", "")):
            if rule.get("from", "1900-01-01") <= date_str:
                applicable = rule
        if not applicable or d.weekday() not in applicable.get("days", []):
            return None
        return {"open": applicable["open"], "close": applicable["close"]}

    # ===================================================================== #
    # Top items, categories, by hour/day/direction                           #
    # ===================================================================== #

    def get_top_items(self, metric: str = "revenue", days: int = 30, limit: int = 20) -> List[Dict]:
        lo, hi = self.reference_range(days)
        order_col = (func.sum(m.OrderItem.total_price) if metric == "revenue"
                     else func.sum(m.OrderItem.quantity))
        rows = self.db.execute(
            select(m.OrderItem.item_name, m.OrderItem.category_name,
                   func.sum(m.OrderItem.total_price), func.sum(m.OrderItem.quantity),
                   func.avg(m.OrderItem.unit_price))
            .where(self._t(m.OrderItem), m.OrderItem.is_removed.is_(False),
                   and_(m.OrderItem.closed_at >= lo, m.OrderItem.closed_at <= hi))
            .group_by(m.OrderItem.item_name, m.OrderItem.category_name)
            .order_by(order_col.desc()).limit(limit)
        ).all()
        return [{"item_name": r[0], "category": r[1], "total_revenue": round(float(r[2] or 0), 2),
                 "total_units": float(r[3] or 0), "avg_price": round(float(r[4] or 0), 2)} for r in rows]

    def get_top_items_range(self, date_from: str, date_to: str, limit: int = 20) -> List[Dict]:
        d_from = date.fromisoformat(date_from[:10])
        d_to = date.fromisoformat(date_to[:10]) + timedelta(days=1)
        lo = datetime.combine(d_from, datetime.min.time())
        hi = datetime.combine(d_to, datetime.min.time())
        rows = self.db.execute(
            select(m.OrderItem.item_name, m.OrderItem.category_name,
                   func.sum(m.OrderItem.total_price), func.sum(m.OrderItem.quantity),
                   func.avg(m.OrderItem.unit_price))
            .where(self._t(m.OrderItem), m.OrderItem.is_removed.is_(False),
                   and_(m.OrderItem.closed_at >= lo, m.OrderItem.closed_at < hi))
            .group_by(m.OrderItem.item_name, m.OrderItem.category_name)
            .order_by(func.sum(m.OrderItem.total_price).desc()).limit(limit)
        ).all()
        return [{"item_name": r[0], "category": r[1], "total_revenue": round(float(r[2] or 0), 2),
                 "total_units": float(r[3] or 0), "avg_price": round(float(r[4] or 0), 2)} for r in rows]

    def get_sales_by_category(self, days: int = 30) -> List[Dict]:
        lo, hi = self.reference_range(days)
        rows = self.db.execute(
            select(m.OrderItem.category_name, func.sum(m.OrderItem.total_price),
                   func.sum(m.OrderItem.quantity), func.count(func.distinct(m.OrderItem.item_name)))
            .where(self._t(m.OrderItem), m.OrderItem.is_removed.is_(False),
                   and_(m.OrderItem.closed_at >= lo, m.OrderItem.closed_at <= hi))
            .group_by(m.OrderItem.category_name)
            .order_by(func.sum(m.OrderItem.total_price).desc())
        ).all()
        return [{"category": r[0] or "Bez kategorii", "revenue": round(float(r[1] or 0), 2),
                 "units": float(r[2] or 0), "dishes": r[3]} for r in rows]

    def get_sales_by_hour(self, days: int = 14) -> List[Dict]:
        lo, hi = self.reference_range(days)
        rows = self.db.execute(
            select(m.Order.closed_at, m.Order.total_price)
            .where(self._t(m.Order), m.Order.status == "CLOSED",
                   m.Order.closed_at >= lo, m.Order.closed_at <= hi)
        ).all()
        by_hour: Dict[int, Dict[str, float]] = {}
        for c, t in rows:
            a = by_hour.setdefault(c.hour, {"revenue": 0.0, "orders": 0})
            a["revenue"] += float(t or 0)
            a["orders"] += 1
        return [{"hour": h, "revenue": round(v["revenue"], 2), "orders": v["orders"]}
                for h, v in sorted(by_hour.items())]

    def get_sales_by_day(self, days: int = 90) -> List[Dict]:
        lo, hi = self.reference_range(days)
        rows = self.db.execute(
            select(m.Order.closed_at, m.Order.total_price)
            .where(self._t(m.Order), m.Order.status == "CLOSED",
                   m.Order.closed_at >= lo, m.Order.closed_at <= hi)
        ).all()
        by_day: Dict[str, float] = {}
        for c, t in rows:
            k = c.date().isoformat()
            by_day[k] = by_day.get(k, 0.0) + float(t or 0)
        return [{"date": k, "revenue": round(v, 2)} for k, v in sorted(by_day.items())]

    def get_day_of_week_performance(self, days: int = 90) -> List[Dict]:
        lo, hi = self.reference_range(days)
        rows = self.db.execute(
            select(m.Order.closed_at, m.Order.total_price)
            .where(self._t(m.Order), m.Order.status == "CLOSED",
                   m.Order.closed_at >= lo, m.Order.closed_at <= hi)
        ).all()
        by_dow: Dict[int, Dict[str, Any]] = {}
        for c, t in rows:
            dow = c.weekday()
            a = by_dow.setdefault(dow, {"revenue": 0.0, "days": set()})
            a["revenue"] += float(t or 0)
            a["days"].add(c.date())
        out = []
        for dow in range(7):
            a = by_dow.get(dow)
            if a and a["days"]:
                out.append({"dow": dow, "avg_day_revenue": round(a["revenue"] / len(a["days"]), 2)})
            else:
                out.append({"dow": dow, "avg_day_revenue": 0.0})
        return out

    # ===================================================================== #
    # Menu engineering + overview                                            #
    # ===================================================================== #

    def get_menu_engineering(self, store, days: int = 30) -> Dict[str, Any]:
        items = self.get_top_items("revenue", days, limit=100)
        recipes = {r["dish_name"].lower(): r for r in store.get_all_recipes()}

        def find_recipe(name):
            n = name.lower()
            if n in recipes:
                return recipes[n]
            for rn, r in recipes.items():
                if rn in n or n in rn:
                    return r
            return None

        analyzed, missing = [], []
        for it in items:
            rec = find_recipe(it["item_name"])
            cd = self._dish_food_cost(store, rec["dish_name"]) if rec else None
            if not cd:
                missing.append({"item_name": it["item_name"], "units": it["total_units"],
                                "revenue": it["total_revenue"]})
                continue
            cost = cd["total_cost"]
            net = cd.get("selling_price_net") or (it["avg_price"] / 1.08 if it.get("avg_price") else None)
            margin = (net - cost) if net else None
            margin_pct = (margin / net * 100) if (margin is not None and net) else None
            analyzed.append({
                "item_name": it["item_name"], "units": it["total_units"], "revenue": it["total_revenue"],
                "food_cost": round(cost, 2), "net_price": round(net, 2) if net else None,
                "margin": round(margin, 2) if margin is not None else None,
                "margin_pct": round(margin_pct, 1) if margin_pct is not None else None,
                "margin_total": round(margin * it["total_units"], 2) if margin is not None else None})

        with_m = [a for a in analyzed if a["margin_pct"] is not None]
        thresholds = None
        if with_m:
            us = sorted(a["units"] for a in with_m)
            ms = sorted(a["margin_pct"] for a in with_m)
            med_u, med_m = us[len(us) // 2], ms[len(ms) // 2]
            thresholds = {"median_units": med_u, "median_margin_pct": med_m}
            RECS = {"star": "Gwiazda — eksponuj, chroń marżę",
                    "plowhorse": "Koń roboczy — popularny, niska marża: podnieś cenę/obniż koszt",
                    "puzzle": "Zagadka — rentowny, mało zamawiany: promuj",
                    "dog": "Pies — rozważ usunięcie z karty"}
            for a in with_m:
                hu, hm = a["units"] >= med_u, a["margin_pct"] >= med_m
                q = "star" if (hu and hm) else "plowhorse" if hu else "puzzle" if hm else "dog"
                a["quadrant"] = q
                a["recommendation"] = RECS[q]

        return {"period_days": days,
                "analyzed": sorted(analyzed, key=lambda x: -(x["margin_total"] or 0)),
                "missing_recipes": missing[:15], "thresholds": thresholds}

    def get_menu_overview(self, store, days: int = 30) -> Dict[str, Any]:
        eng = self.get_menu_engineering(store, days)
        analyzed, missing = eng["analyzed"], eng["missing_recipes"]
        covered = sum(a["revenue"] or 0 for a in analyzed)
        uncovered = sum(m_["revenue"] or 0 for m_ in missing)
        total = covered + uncovered
        coverage_pct = round(covered / total * 100, 1) if total > 0 else 0.0
        priced = [a for a in analyzed if a.get("margin_pct") is not None]
        avg_fc = best = worst = None
        if priced:
            avg_fc = round(sum(100 - a["margin_pct"] for a in priced) / len(priced), 1)
            best = max(priced, key=lambda a: a["margin_pct"])
            worst = min(priced, key=lambda a: a["margin_pct"])
        high_fc = sorted(
            [{"item_name": a["item_name"], "food_cost_pct": round(100 - a["margin_pct"], 1),
              "margin_pct": a["margin_pct"], "units": a["units"]}
             for a in priced if (100 - a["margin_pct"]) > 40],
            key=lambda x: -x["food_cost_pct"])[:8]
        # dead products: active, never sold in window
        lo, hi = self.reference_range(days)
        sold_names = {r[0].lower() for r in self.db.execute(
            select(func.distinct(m.OrderItem.item_name)).where(
                self._t(m.OrderItem), m.OrderItem.is_removed.is_(False),
                m.OrderItem.closed_at >= lo, m.OrderItem.closed_at <= hi)).all()}
        dead = []
        for d in self.db.execute(self._scoped_dim()).scalars().all():
            if d.is_active and (d.item_type or "PRODUCT") == "PRODUCT" and d.name.lower() not in sold_names:
                dead.append({"name": d.name, "category": d.category_name, "price": d.price})
        dead = sorted(dead, key=lambda x: -(x["price"] or 0))[:12]
        return {"period_days": days, "coverage_pct": coverage_pct,
                "covered_revenue": round(covered, 2), "uncovered_revenue": round(uncovered, 2),
                "analyzed_count": len(analyzed), "avg_food_cost_pct": avg_fc,
                "best_margin": {"item_name": best["item_name"], "margin_pct": best["margin_pct"]} if best else None,
                "worst_margin": {"item_name": worst["item_name"], "margin_pct": worst["margin_pct"]} if worst else None,
                "high_food_cost": high_fc, "missing_top": missing[:8], "dead_products": dead}

    def _scoped_dim(self):
        return select(m.DimItem).where(m.DimItem.tenant_id == self.tenant_id)

    # ===================================================================== #
    # Product catalog + fuzzy search                                         #
    # ===================================================================== #

    def get_product_catalog(self, search: Optional[str] = None, limit: int = 200) -> List[Dict]:
        stmt = self._scoped_dim().where(m.DimItem.is_active.is_(True))
        if search:
            stmt = stmt.where(func.lower(m.DimItem.name).like(f"%{search.lower()}%"))
        stmt = stmt.order_by(m.DimItem.name).limit(limit)
        rows = self.db.execute(stmt).scalars().all()
        return [{"id": r.gopos_id, "name": r.name, "category": r.category_name,
                 "price": r.price, "vat_rate": r.tax_rate, "type": r.item_type} for r in rows]

    def search_menu(self, query: str, limit: int = 10) -> List[Dict]:
        words = [w for w in query.strip().split() if len(w) >= 3] or [query.strip()]
        # gather sales stats per item
        rows = self.db.execute(
            select(m.OrderItem.item_name, func.sum(m.OrderItem.quantity),
                   func.sum(m.OrderItem.total_price), func.avg(m.OrderItem.unit_price))
            .where(self._t(m.OrderItem), m.OrderItem.is_removed.is_(False))
            .group_by(m.OrderItem.item_name)
        ).all()
        catalog = {d.name.lower(): d for d in self.db.execute(self._scoped_dim()).scalars().all()}

        def matches(name):
            nl = name.lower()
            if any(w.lower() in nl for w in words):
                return True
            if nl in query.lower():
                return True
            return False

        results = []
        for name, units, rev, avgp in rows:
            if not matches(name):
                continue
            d = catalog.get(name.lower())
            results.append({"item_name": name, "units_sold": float(units or 0),
                            "revenue": round(float(rev or 0), 2),
                            "avg_sold_price": round(float(avgp or 0), 2),
                            "menu_price": d.price if d else None,
                            "vat_rate": d.tax_rate if d else None,
                            "category": d.category_name if d else None})
        results.sort(key=lambda x: -x["revenue"])
        # add catalog-only matches (never sold)
        if len(results) < limit:
            found = {r["item_name"].lower() for r in results}
            for nl, d in catalog.items():
                if len(results) >= limit:
                    break
                if nl in found:
                    continue
                if any(w.lower() in nl for w in words):
                    results.append({"item_name": d.name, "units_sold": 0, "revenue": 0,
                                    "avg_sold_price": None, "menu_price": d.price,
                                    "vat_rate": d.tax_rate, "category": d.category_name})
        return results[:limit]

    def get_data_status(self, store) -> Dict[str, Any]:
        cnt = self.db.execute(
            select(func.count()).select_from(m.Order).where(self._t(m.Order))).scalar() or 0
        rng = self.db.execute(
            select(func.min(m.Order.closed_at), func.max(m.Order.closed_at))
            .where(self._t(m.Order), m.Order.status == "CLOSED")).one()
        rec_cnt = self.db.execute(
            select(func.count()).select_from(m.Recipe).where(self._t(m.Recipe))).scalar() or 0
        oh_cnt = self.db.execute(
            select(func.count()).select_from(m.Overhead).where(self._t(m.Overhead))).scalar() or 0
        return {
            "order_count": cnt,
            "data_from": rng[0].date().isoformat() if isinstance(rng[0], datetime) else None,
            "data_to": rng[1].date().isoformat() if isinstance(rng[1], datetime) else None,
            "recipe_count": rec_cnt, "overhead_count": oh_cnt,
            "reference_now": self.reference_now().isoformat(),
        }

    # ===================================================================== #
    # Goals with auto-measured progress                                      #
    # ===================================================================== #

    def _current_metric_value(self, store, metric: str) -> Optional[float]:
        try:
            if metric == "net_margin_pct":
                return self.calculate_real_net_profit(store, 30)["net_margin_pct"]
            if metric == "net_profit":
                return self.calculate_real_net_profit(store, 30)["net_profit"]
            if metric == "revenue":
                return self.get_sales_summary(30)["total_revenue"]
            if metric == "avg_ticket":
                return self.get_sales_summary(30)["avg_ticket"]
            if metric == "food_cost_pct":
                fc = self.calculate_food_cost_pct(store, 30)
                # 0% is only real if we actually have recipes; otherwise it's missing data,
                # not a perfect score — return None so the goal shows "brak danych".
                return fc["food_cost_pct"] if fc.get("dishes_with_recipes") else None
            if metric == "labor_pct":
                np = self.calculate_real_net_profit(store, 30)
                return np["labor_pct"] if np.get("labor_has_data") else None
        except Exception as e:
            logger.warning(f"metric {metric} unavailable: {e}")
        return None

    def get_goals(self, store, status: str = "active") -> List[Dict]:
        import json as _json
        goals = store.get_goals_raw(status=status)
        out = []
        for g in goals:
            # Spec goals (flexible measurement: YoY, quarter, custom range) use the engine.
            spec_raw = None
            try:
                spec_raw = store.get_setting(f"goal_spec:{g.id}")
            except Exception:
                pass
            if spec_raw:
                try:
                    spec = _json.loads(spec_raw)
                    ms = self.measure_spec(store, spec)
                    pace_info = self._goal_pace(g, ms.get("current_value"), ms.get("progress_pct"), ms.get("reached"))
                    if ms.get("not_started"):
                        pace_info = {**pace_info, "pace": "pending", "days_left": pace_info.get("days_left")}
                    elif ms.get("in_progress") and ms.get("period_pace"):
                        # Period-based goal: pace comes from the goal's own window
                        # (elapsed vs collected), not created_at→deadline. Fixes '-311%'.
                        pace_info = {**pace_info,
                                     "pace": ms.get("period_pace"),
                                     "expected_pct": ms.get("elapsed_pct"),
                                     "days_left": ms.get("days_left")}
                    out.append({
                        "id": g.id, "title": g.title, "metric": ms.get("metric") or g.metric,
                        "target_value": ms.get("target_value"), "direction": g.direction,
                        "baseline_value": ms.get("baseline_value"),
                        "current_value": ms.get("current_value"),
                        "deadline": ms.get("deadline") or (g.deadline.isoformat() if g.deadline else None),
                        "status": g.status, "notes": g.notes,
                        "progress_pct": ms.get("progress_pct"), "reached": ms.get("reached"),
                        "spec": spec, "comparison": ms.get("comparison"),
                        "period_from": ms.get("period_from"), "period_to": ms.get("period_to"),
                        "measurable": ms.get("measurable"), "missing": ms.get("missing"),
                        "not_started": ms.get("not_started"), "starts_in_days": ms.get("starts_in_days"),
                        "in_progress": ms.get("in_progress"), "elapsed_pct": ms.get("elapsed_pct"),
                        **pace_info,
                    })
                    continue
                except Exception as e:
                    logger.warning(f"spec goal {g.id} measure failed, legacy fallback: {e}")

            current = (self._current_metric_value(store, g.metric)
                       if g.metric != "manual" else g.baseline_value)
            progress = None
            reached = None
            if current is not None and g.baseline_value is not None and g.target_value is not None:
                denom = g.target_value - g.baseline_value
                if abs(denom) > 1e-9:
                    progress = max(min(round((current - g.baseline_value) / denom * 100, 1), 999), -999)
                else:
                    progress = 100.0
                reached = (current >= g.target_value) if g.direction == "increase" else (current <= g.target_value)
            out.append({
                "id": g.id, "title": g.title, "metric": g.metric, "target_value": g.target_value,
                "direction": g.direction, "baseline_value": g.baseline_value,
                "current_value": round(current, 2) if isinstance(current, (int, float)) else None,
                "deadline": g.deadline.isoformat() if g.deadline else None,
                "status": g.status, "notes": g.notes, "progress_pct": progress, "reached": reached,
                **self._goal_pace(g, current, progress, reached),
            })
        return out

    def _goal_pace(self, g, current, progress, reached) -> Dict:
        """Time-based 'results' read: are we on track to hit this by the deadline?
        Returns days_left, expected_pct (where we 'should' be by now), and a pace
        verdict the UI turns into a clear status."""
        from datetime import date as _date
        info = {"days_left": None, "expected_pct": None, "pace": "no_deadline"}
        if reached:
            info["pace"] = "reached"
            return info
        if not g.deadline:
            return info
        today = _date.today()
        created = (g.created_at.date() if getattr(g, "created_at", None) else today)
        total_days = max((g.deadline - created).days, 1)
        elapsed = max((today - created).days, 0)
        days_left = (g.deadline - today).days
        info["days_left"] = days_left
        expected = min(round(elapsed / total_days * 100, 1), 100.0)
        info["expected_pct"] = expected
        if days_left < 0:
            info["pace"] = "overdue"
        elif progress is None:
            info["pace"] = "no_data"
        elif progress >= expected + 8:
            info["pace"] = "ahead"
        elif progress >= expected - 8:
            info["pace"] = "on_track"
        else:
            info["pace"] = "behind"
        return info

    def create_goal_tracked(self, store, title, metric, target_value, direction="increase",
                            deadline=None, notes=None) -> str:
        """Create a goal, snapshotting the current metric value as baseline."""
        baseline = (self._current_metric_value(store, metric) if metric != "manual" else None)
        return store.create_goal(title, metric, target_value, direction, deadline, notes, baseline)

    # ===================================================================== #
    # Flexible goal measurement engine                                       #
    # A goal carries a "spec" (metric × window × comparison × deadline).     #
    # This computes current/baseline/target/progress/pace for ANY spec —     #
    # year-over-year, quarter, custom range — not just a fixed 30-day window. #
    # ===================================================================== #

    @staticmethod
    def _month_end(d: "date") -> "date":
        nm = date(d.year + (d.month // 12), (d.month % 12) + 1, 1)
        return nm - timedelta(days=1)

    def _resolve_window(self, win: Dict) -> tuple:
        """Resolve a window spec to (date_from, date_to) ISO strings."""
        today = self.reference_now().date()
        win = win or {}
        typ = win.get("type", "rolling")
        unit = win.get("unit", "days")
        if typ == "custom" and win.get("from") and win.get("to"):
            return win["from"][:10], win["to"][:10]
        if typ == "rolling":
            n = int(win.get("n", 30))
            return (today - timedelta(days=max(n, 1) - 1)).isoformat(), today.isoformat()
        # calendar (full period) or to_date (period start → today)
        to_date = (typ == "to_date")
        if unit == "year":
            start = date(today.year, 1, 1)
            end = today if to_date else date(today.year, 12, 31)
        elif unit == "quarter":
            q0 = ((today.month - 1) // 3) * 3 + 1
            start = date(today.year, q0, 1)
            end = today if to_date else self._month_end(date(today.year, q0 + 2, 1))
        else:  # month
            start = date(today.year, today.month, 1)
            end = today if to_date else self._month_end(start)
        return start.isoformat(), end.isoformat()

    @staticmethod
    def _shift_years(dfrom: str, dto: str, years: int = 1) -> tuple:
        def back(s):
            d = date.fromisoformat(s[:10])
            try:
                return d.replace(year=d.year - years)
            except ValueError:  # Feb 29
                return d.replace(year=d.year - years, day=28)
        return back(dfrom).isoformat(), back(dto).isoformat()

    @staticmethod
    def _shift_back_same_length(dfrom: str, dto: str) -> tuple:
        a = date.fromisoformat(dfrom[:10]); b = date.fromisoformat(dto[:10])
        length = (b - a).days + 1
        new_to = a - timedelta(days=1)
        new_from = new_to - timedelta(days=length - 1)
        return new_from.isoformat(), new_to.isoformat()

    def _value_over(self, store, metric: str, dfrom: str, dto: str) -> tuple:
        """Returns (value, data_complete) for a metric over an arbitrary range."""
        try:
            if metric in ("revenue", "orders", "avg_ticket"):
                s = self.get_sales_summary_range(dfrom, dto) or {}
                v = {"revenue": s.get("total_revenue"), "orders": s.get("order_count"),
                     "avg_ticket": s.get("avg_ticket")}[metric]
                return v, bool(s.get("data_complete"))
            n = self.calculate_real_net_profit_range(store, dfrom, dto) or {}
            v = {"net_profit": n.get("net_profit"), "net_margin_pct": n.get("net_margin_pct"),
                 "food_cost_pct": n.get("food_cost_pct"), "labor_pct": n.get("labor_pct")}.get(metric)
            return v, bool((n.get("data_coverage") or {}).get("complete"))
        except Exception as e:
            logger.warning(f"_value_over failed for {metric} {dfrom}..{dto}: {e}")
            return None, False

    def measure_spec(self, store, spec: Dict) -> Dict:
        """The heart of flexible goals. Computes everything the UI/AI need for ANY spec."""
        metric = spec.get("metric")
        win = spec.get("window") or {"type": "rolling", "unit": "days", "n": 30}
        dfrom, dto = self._resolve_window(win)
        today = self.reference_now().date()
        not_started = date.fromisoformat(dfrom[:10]) > today
        starts_in_days = (date.fromisoformat(dfrom[:10]) - today).days if not_started else None
        current, cur_ok = self._value_over(store, metric, dfrom, dto)
        if not_started:
            current = None  # a future window has no data yet — that's expected, not a gap

        comp = spec.get("comparison") or {}
        ctype = comp.get("type", "absolute")
        operator = comp.get("operator", ">=")
        baseline, base_ok = None, True
        if ctype == "yoy":
            bf, bt = self._shift_years(dfrom, dto, 1)
            baseline, base_ok = self._value_over(store, metric, bf, bt)
        elif ctype == "prev_period":
            bf, bt = self._shift_back_same_length(dfrom, dto)
            baseline, base_ok = self._value_over(store, metric, bf, bt)
        elif ctype == "baseline_snapshot":
            baseline = spec.get("baseline_snapshot")

        # resolve target: either an absolute value, or % growth over the baseline
        target = comp.get("target")
        tkind = comp.get("target_kind", "value")
        target_resolved = target
        if tkind == "pct_growth" and baseline is not None and target is not None:
            target_resolved = round(baseline * (1 + target / 100.0), 2)

        # Is the goal's period currently running (started but not finished)?
        period_start = date.fromisoformat(dfrom[:10])
        period_end = date.fromisoformat(dto[:10])
        in_progress = (not not_started) and period_start <= today <= period_end
        # Additive metrics accumulate over the period → mid-period the "current" is a
        # partial sum, so comparing it to a full-period baseline is meaningless (that
        # was the '-311%' bug). Show % of the target reached so far + how much of the
        # period has elapsed instead.
        ADDITIVE = {"revenue", "orders", "net_profit"}
        elapsed_pct, period_pace, days_left = None, None, None
        progress, reached = None, None
        if current is not None and target_resolved is not None:
            if in_progress and metric in ADDITIVE and target_resolved > 0:
                progress = max(min(round(current / target_resolved * 100, 1), 999), 0)
                total_days = max((period_end - period_start).days + 1, 1)
                elapsed_days = min(max((today - period_start).days + 1, 0), total_days)
                elapsed_pct = round(elapsed_days / total_days * 100, 1)
                days_left = (period_end - today).days
                reached = current >= target_resolved
                if not reached:
                    period_pace = ("ahead" if progress >= elapsed_pct + 8
                                   else ("on_track" if progress >= elapsed_pct - 8 else "behind"))
            else:
                base_for_progress = baseline if baseline is not None else 0
                denom = target_resolved - base_for_progress
                if abs(denom) > 1e-9:
                    progress = max(min(round((current - base_for_progress) / denom * 100, 1), 999), -999)
                else:
                    progress = 100.0
                reached = (current >= target_resolved) if operator == ">=" else (current <= target_resolved)

        missing = []
        if not cur_ok and not not_started:
            missing.append(f"niekompletne dane sprzedaży za okres {dfrom} → {dto}")
        if ctype == "yoy" and not base_ok:
            missing.append("brak/niepełne dane za ten sam okres rok temu (potrzebny backfill historii)")
        if ctype == "prev_period" and not base_ok:
            missing.append("niepełne dane za poprzedni okres porównawczy")
        for req in (spec.get("requires") or []):
            missing.append(req)

        return {
            "metric": metric, "period_from": dfrom, "period_to": dto,
            "current_value": round(current, 2) if isinstance(current, (int, float)) else None,
            "baseline_value": round(baseline, 2) if isinstance(baseline, (int, float)) else None,
            "target_value": round(target_resolved, 2) if isinstance(target_resolved, (int, float)) else target_resolved,
            "comparison": ctype, "operator": operator,
            "progress_pct": progress, "reached": reached,
            "measurable": len(missing) == 0,
            "missing": list(dict.fromkeys(missing)),
            "deadline": spec.get("deadline"),
            "not_started": not_started, "starts_in_days": starts_in_days,
            "in_progress": in_progress, "elapsed_pct": elapsed_pct,
            "period_pace": period_pace, "days_left": days_left,
        }

    def create_spec_goal(self, store, title: str, spec: Dict, notes: str = None) -> str:
        """Create a goal whose measurement is driven by a flexible spec (stored as JSON)."""
        import json as _json
        metric = spec.get("metric", "manual")
        comp = spec.get("comparison") or {}
        target = comp.get("target")
        direction = "increase" if comp.get("operator", ">=") == ">=" else "decrease"
        gid = store.create_goal(
            title, metric, target if isinstance(target, (int, float)) else 0.0,
            direction, spec.get("deadline"), notes, None)
        store.set_setting(f"goal_spec:{gid}", _json.dumps(spec, ensure_ascii=False))
        return gid

    # ===================================================================== #
    # Labor by day, sales by direction, employee performance                 #
    # ===================================================================== #

    def get_labor_by_day(self, days: int = 30) -> List[Dict]:
        lo, hi = self.reference_range(days)
        rows = self.db.execute(
            select(m.WorkTime.start_at, m.WorkTime.duration_minutes,
                   m.WorkTime.hourly_rate, m.WorkTime.to_pay)
            .where(self._t(m.WorkTime), m.WorkTime.start_at >= lo, m.WorkTime.start_at <= hi)
        ).all()
        by_day: Dict[str, float] = {}
        for start, dur, rate, to_pay in rows:
            if not start:
                continue
            cost = to_pay if to_pay is not None else (dur or 0) / 60.0 * (rate or 0)
            k = start.date().isoformat()
            by_day[k] = by_day.get(k, 0.0) + cost
        return [{"date": k, "labor_cost": round(v, 2)} for k, v in sorted(by_day.items())]

    def get_sales_by_direction(self, days: int = 30) -> List[Dict]:
        lo, hi = self.reference_range(days)
        rows = self.db.execute(
            select(m.Order.order_type, func.count(), func.sum(m.Order.total_price))
            .where(self._t(m.Order), m.Order.status == "CLOSED",
                   m.Order.closed_at >= lo, m.Order.closed_at <= hi)
            .group_by(m.Order.order_type)
            .order_by(func.sum(m.Order.total_price).desc())
        ).all()
        return [{"direction": r[0] or "Nieznany", "orders": r[1],
                 "revenue": round(float(r[2] or 0), 2)} for r in rows]

    def get_employee_performance(self, days: int = 30) -> List[Dict]:
        return self.get_labor_cost(days).get("by_employee", [])

