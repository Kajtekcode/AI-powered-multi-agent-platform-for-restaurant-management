"""
Read-only, tenant-scoped SQL surface for the AI advisor.

The advisor can write its OWN SELECT queries to answer questions we never
predicted ("czy w deszczowe środy sprzedaję mniej ostryg?"). To make that safe:

  1. Only a single SELECT/WITH statement is allowed (no writes, no PRAGMA/ATTACH,
     no stacked statements).
  2. The AI queries tenant-scoped VIEWS named `q_*` — never the base tables.
     Each view is created per-call as `SELECT <cols> FROM <table> WHERE tenant_id = :tid`,
     so it is physically impossible to read another restaurant's rows or any
     secret column (passwords, encrypted POS keys are simply not exposed).
  3. Results are capped (row limit) and errors are returned as text, not raised.

This is defense-in-depth: SELECT-only validation + base-table ban + scoped views.
"""
from __future__ import annotations

import re
from typing import Dict

from sqlalchemy import text

# view_name -> (base_table, exposed_columns).  tenant_id and secrets are omitted.
VIEWS = {
    "q_orders": ("orders", "closed_at, status, order_type, total_price, total_price_net, tip"),
    "q_order_items": ("order_items",
                      "item_name, category_name, quantity, unit_price, total_price, is_removed, closed_at"),
    "q_menu": ("dim_items", "name, category_name, item_group_name, price, item_type, is_active"),
    "q_recipes": ("recipes", "dish_name, selling_price, vat_rate, notes"),
    "q_recipe_ingredients": ("recipe_ingredients", "dish_name, ingredient, quantity, unit, cost_per_kg"),
    "q_overheads": ("overheads", "category, monthly_amount, notes"),
    "q_work_times": ("work_times",
                     "employee_name, workplace_name, start_at, end_at, duration_minutes, hourly_rate, to_pay"),
    "q_goals": ("goals", "title, metric, target_value, direction, baseline_value, deadline, status"),
    "q_invoices": ("invoices",
                   "supplier, invoice_number, invoice_date, due_date, total, total_net, category, payment_status"),
    "q_invoice_items": ("invoice_items",
                        "invoice_id, raw_name, matched_ingredient, quantity, unit, unit_price, net_price, vat_percent, category"),
    "q_prices": ("ingredient_price_history", "ingredient, price, unit, supplier, category, recorded_at"),
}

# Human-readable schema handed to the model so it can write good SQL.
def schema_doc() -> str:
    lines = []
    for v, (_tbl, cols) in VIEWS.items():
        lines.append(f"  {v}({cols})")
    return "\n".join(lines)


_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|"
    r"vacuum|reindex|truncate|grant|revoke|begin|commit|rollback|savepoint)\b", re.I)
# base tables the AI must NOT touch directly (word-boundary; q_ views won't match)
_BASE = re.compile(
    r"\b(tenants|users|gopos_connections|orders|order_items|dim_items|recipes|"
    r"recipe_ingredients|overheads|work_times|goals|agent_memory|chat_messages|"
    r"company_profile|app_settings|sync_state|invoices|invoice_items|"
    r"ingredient_price_history|external_signals|monitoring_jobs)\b", re.I)


def run_readonly_query(db, tenant_id: str, sql: str, max_rows: int = 200) -> Dict:
    """Validate + run a single read-only SELECT against tenant-scoped q_* views."""
    q = (sql or "").strip().rstrip(";").strip()
    if not q:
        return {"error": "Puste zapytanie."}
    low = q.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return {"error": "Dozwolone tylko zapytania SELECT (lub WITH ... SELECT)."}
    if ";" in q:
        return {"error": "Tylko JEDNO zapytanie — usuń średniki."}
    if _FORBIDDEN.search(q):
        return {"error": "Wykryto niedozwoloną operację. To dostęp tylko do odczytu (SELECT)."}
    if _BASE.search(q):
        return {"error": "Odpytuj widoki q_* (np. q_orders, q_invoices, q_prices), nie tabele bazowe."}

    # tenant_id is server-controlled (from the auth session), but validate its shape
    # before inlining — SQLite forbids bind params inside a VIEW definition.
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(tenant_id or "")):
        return {"error": "Nieprawidłowy identyfikator lokalu."}
    tid = str(tenant_id)

    if " limit " not in f" {low} ":
        q = f"{q} LIMIT {max_rows}"

    try:
        for vname, (tbl, cols) in VIEWS.items():
            db.execute(text(f"DROP VIEW IF EXISTS {vname}"))
            db.execute(text(
                f"CREATE TEMP VIEW {vname} AS SELECT {cols} FROM {tbl} WHERE tenant_id = '{tid}'"
            ))
        rows = db.execute(text(q)).mappings().all()
        out = [dict(r) for r in rows[:max_rows]]
        # stringify non-JSON-native values (dates, Decimal)
        for r in out:
            for k, v in r.items():
                if hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
        return {"columns": list(out[0].keys()) if out else [],
                "rows": out, "row_count": len(out),
                "truncated": len(rows) >= max_rows}
    except Exception as e:
        return {"error": f"Błąd zapytania: {str(e)[:240]}. Sprawdź nazwy widoków/kolumn."}
    finally:
        for vname in VIEWS:
            try:
                db.execute(text(f"DROP VIEW IF EXISTS {vname}"))
            except Exception:
                pass
