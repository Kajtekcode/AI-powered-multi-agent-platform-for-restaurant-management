"""
Notifications — morning briefing via Telegram / Slack.

Two layers:
1. Rule-based stats block (zero LLM cost) — always included
2. CEO agent strategic note (one LLM call) — appended when LLM configured
"""

import logging
from datetime import timedelta

import httpx

from app.core.config import settings
from app.core.db import db_session
from app.core.tenant_context import tenant_scope
from app.repositories.agent_repo import AgentRepo
from app.models.tenant import Tenant

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096


# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> bool:
    if not settings.telegram_configured:
        return False
    try:
        # Telegram hard limit 4096 chars — split if needed
        chunks = [text[i:i + TELEGRAM_LIMIT - 50] for i in range(0, len(text), TELEGRAM_LIMIT - 50)] or [text]
        for chunk in chunks:
            r = httpx.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": chunk},
                timeout=15,
            )
            r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def send_slack(text: str) -> bool:
    if not settings.slack_configured:
        return False
    try:
        r = httpx.post(settings.slack_webhook_url, json={"text": text}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Slack send failed: {e}")
        return False


def send_all(text: str) -> dict:
    return {
        "telegram": send_telegram(text) if settings.telegram_configured else None,
        "slack": send_slack(text) if settings.slack_configured else None,
    }


# ---------------------------------------------------------------------------
# Morning briefing content
# ---------------------------------------------------------------------------

def _fmt(v, dec=0):
    if v is None:
        return "—"
    return f"{v:,.{dec}f} zł".replace(",", " ")


def build_stats_block(store) -> str:
    """Rule-based daily stats — costs zero LLM tokens. `store` is an AgentRepo."""
    ref = store.analytics.reference_now()
    yesterday = (ref - timedelta(days=1)).date().isoformat()
    week_ago_same_day = (ref - timedelta(days=8)).date().isoformat()

    by_day = {d["date"]: d["revenue"] for d in store.get_sales_by_day(days=14)}
    # order counts per day
    counts = {}
    for d in store.get_sales_by_day(days=14):
        counts[d["date"]] = None  # revenue only; orders fetched from summary below

    def day_revenue(date_str):
        return (by_day.get(date_str, 0.0), 0)

    y_rev, y_orders = day_revenue(yesterday)
    w_rev, _ = day_revenue(week_ago_same_day)

    delta_txt = ""
    if w_rev > 0:
        delta = (y_rev - w_rev) / w_rev * 100
        arrow = "▲" if delta >= 0 else "▼"
        delta_txt = f" ({arrow} {abs(delta):.0f}% vs ten sam dzień tydzień temu)"

    summary7 = store.get_sales_summary(days=7)
    net = None
    try:
        net = store.calculate_real_net_profit(days=30)
    except Exception:
        pass
    labor = store.get_labor_cost(days=7)

    lines = [
        f"☀️ PORANNY BRIEFING — {ref.date().isoformat()}",
        "",
        f"Wczoraj ({yesterday}):",
        f"  • Przychód: {_fmt(y_rev)}{delta_txt}",
        f"  • Zamówienia: {y_orders}",
        "",
        f"Ostatnie 7 dni:",
        f"  • Przychód: {_fmt(summary7.get('total_revenue'))}",
        f"  • Śr. rachunek: {_fmt(summary7.get('avg_ticket'), 2)}",
    ]
    if labor.get("has_data"):
        lines.append(f"  • Koszty pracy: {_fmt(labor['total_cost'])} ({labor.get('labor_pct') or '—'}% przychodu)")
    if net:
        lines += [
            "",
            f"Miesiąc (30 dni):",
            f"  • Zysk netto: {_fmt(net['net_profit'])} (marża {net['net_margin_pct']}%)",
            f"  • Food cost: {net['food_cost_pct']}%",
        ]
        if net.get("double_count_warning"):
            lines.append(f"  ⚠️ {net['double_count_warning']}")

    # Goals progress — makes the plan live
    try:
        goals = store.get_goals(status="active")
    except Exception:
        goals = []
    if goals:
        lines += ["", "🎯 Cele:"]
        for g in goals[:5]:
            cur = g.get("current_value")
            tgt = g["target_value"]
            prog = g.get("progress_pct")
            mark = "✅" if g.get("reached") else ("📈" if (prog or 0) >= 50 else "🔸")
            cur_txt = f"{cur:.1f}" if isinstance(cur, (int, float)) else "—"
            dl = f" (do {g['deadline']})" if g.get("deadline") else ""
            lines.append(f"  {mark} {g['title']}: {cur_txt} / cel {tgt}{dl}"
                         + (f" — {prog:.0f}% drogi" if prog is not None else ""))

    return "\n".join(lines)


def build_morning_briefing(store, tenant_id: str, include_ai: bool = True) -> str:
    """Stats block + one focused CEO-agent note (single LLM call)."""
    text = build_stats_block(store)

    if include_ai and settings.llm_configured:
        try:
            from app.agents.orchestra import run_ceo_agent
            ai_note = run_ceo_agent(
                "Poranny briefing dla właściciela. Na podstawie danych z ostatnich 7-30 dni "
                "podaj: (1) jedną najważniejszą obserwację, (2) trzy konkretne priorytety na dziś/tydzień. "
                "Maksymalnie 120 słów, zwykły tekst bez markdown (idzie na Telegram).",
                tenant_id=tenant_id,
            )
            text += f"\n\n🎯 REKOMENDACJE AI:\n{ai_note}"
        except Exception as e:
            logger.error(f"AI briefing note failed: {e}")

    return text


def send_morning_briefing(tenant_id: str) -> dict:
    """Generate and send one tenant's morning briefing to configured channels."""
    if not settings.notifications_configured:
        return {"status": "skipped", "reason": "no channels configured"}
    with tenant_scope(tenant_id), db_session() as db:
        store = AgentRepo(db, tenant_id)
        if store.store.get_order_count() == 0:
            return {"status": "skipped", "reason": "no data"}
        text = build_morning_briefing(store, tenant_id)
    results = send_all(text)
    logger.info(f"Morning briefing sent ({tenant_id}): {results}")
    return {"status": "sent", **results}


def send_morning_briefing_all() -> dict:
    """Scheduler entry — briefing for every tenant with data."""
    from app.data.sync import active_tenant_ids
    out = {}
    for tid in active_tenant_ids():
        try:
            out[tid] = send_morning_briefing(tid)
        except Exception as e:
            logger.error(f"Briefing failed for {tid}: {e}")
            out[tid] = {"status": "error", "message": str(e)}
    return {"tenants": len(out), "results": out}
