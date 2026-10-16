"""
Telegram Bot — two-way chat with the CEO agent.

Long-polling (getUpdates) in a daemon thread — no public URL needed,
works on localhost. Only responds to the configured TELEGRAM_CHAT_ID.

Commands:
  /briefing  → morning briefing on demand
  /reset     → clear Telegram conversation history
  /pamiec    → list saved long-term memories
  anything else → CEO agent (with persistent memory + history)
"""

import logging
import re
import threading
import time

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_thread: threading.Thread = None
_stop = threading.Event()


def _api(method: str, **params):
    r = httpx.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}",
        json=params,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def _strip_markdown(text: str) -> str:
    """Telegram plain-text: remove markdown the agent might emit."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"#{1,4}\s*", "", text)
    text = re.sub(r"`{1,3}", "", text)
    return text


def _send(chat_id, text: str):
    text = _strip_markdown(text)
    for i in range(0, len(text), 4000):
        _api("sendMessage", chat_id=chat_id, text=text[i:i + 4000])


def _resolve_tenant_id():
    """Resolve which tenant this bot serves.

    Transitional: a single bot token maps to one restaurant — use the oldest
    tenant with a connected POS, else the oldest tenant. Proper chat_id->tenant
    routing (multi-restaurant bots) is a Round B concern.
    """
    from app.core.db import db_session
    from app.models.tenant import Tenant, GoposConnection, ConnectionStatus
    with db_session() as db:
        conn = (db.query(GoposConnection)
                .filter(GoposConnection.status == ConnectionStatus.connected)
                .order_by(GoposConnection.created_at.asc()).first())
        if conn:
            return conn.tenant_id
        t = db.query(Tenant).order_by(Tenant.created_at.asc()).first()
        return t.id if t else None


def _repo(tenant_id):
    from app.core.db import SessionLocal
    from app.repositories.agent_repo import AgentRepo
    return AgentRepo(SessionLocal(), tenant_id)


def _handle_message(chat_id, text: str):
    text = text.strip()
    tenant_id = _resolve_tenant_id()
    if not tenant_id:
        _send(chat_id, "⚠️ Brak skonfigurowanej restauracji. Zarejestruj się w aplikacji.")
        return

    if text.startswith("/start"):
        _send(chat_id,
              "👋 Cześć! Jestem CEO Agentem Twojej restauracji.\n\n"
              "Pisz do mnie jak do doradcy — mam dostęp do wszystkich danych "
              "sprzedażowych i pamiętam nasze rozmowy.\n\n"
              "Komendy:\n"
              "/audyt — audyt firmy (wywiad + plan)\n"
              "/cele — postęp celów\n"
              "/briefing — poranny briefing teraz\n"
              "/pamiec — co zapamiętałem\n"
              "/reset — wyczyść historię rozmowy")
        return

    if text.startswith("/briefing"):
        _api("sendChatAction", chat_id=chat_id, action="typing")
        from app.core.notifications import build_morning_briefing
        from app.core.tenant_context import tenant_scope
        with tenant_scope(tenant_id):
            _send(chat_id, build_morning_briefing(_repo(tenant_id), tenant_id))
        return

    if text.startswith("/reset"):
        _repo(tenant_id).clear_chat_history("telegram")
        _send(chat_id, "🧹 Historia rozmowy wyczyszczona (pamięć długoterminowa zostaje).")
        return

    if text.startswith("/pamiec") or text.startswith("/pamięć"):
        mems = _repo(tenant_id).get_memories(limit=20)
        if not mems:
            _send(chat_id, "Pamięć długoterminowa jest pusta — będę zapamiętywał ważne fakty z naszych rozmów.")
        else:
            lines = [f"• [{m['category']}] {m['content']}" for m in mems]
            _send(chat_id, "🧠 Co pamiętam:\n\n" + "\n".join(lines))
        return

    if text.startswith("/audyt") or text.startswith("/audit"):
        _api("sendChatAction", chat_id=chat_id, action="typing")
        from app.agents.orchestra import start_audit
        result = start_audit(tenant_id=tenant_id)
        _repo(tenant_id).set_setting("telegram_mode", "audit")
        _send(chat_id, result["response"])
        return

    if text.startswith("/cele") or text.startswith("/goals"):
        goals = _repo(tenant_id).get_goals(status="active")
        if not goals:
            _send(chat_id, "Nie masz jeszcze celów. Zrób audyt (/audyt) — zaproponuję plan z celami.")
        else:
            lines = []
            for g in goals:
                cur = g.get("current_value")
                cur_txt = f"{cur:.1f}" if isinstance(cur, (int, float)) else "—"
                prog = g.get("progress_pct")
                mark = "✅" if g.get("reached") else "📈"
                lines.append(f"{mark} {g['title']}: {cur_txt} / {g['target_value']}"
                             + (f" ({prog:.0f}%)" if prog is not None else ""))
            _send(chat_id, "🎯 Twoje cele:\n\n" + "\n".join(lines))
        return

    # Regular question → CEO agent with persistent telegram session
    _api("sendChatAction", chat_id=chat_id, action="typing")
    try:
        mode = _repo(tenant_id).get_setting("telegram_mode")
        if mode == "audit":
            from app.agents.orchestra import run_audit_interview
            result = run_audit_interview(text, session="telegram", tenant_id=tenant_id)
            if result["progress"]["complete"]:
                _repo(tenant_id).set_setting("telegram_mode", "normal")
            _send(chat_id, result["response"])
            return
        from app.agents.orchestra import run_ceo_agent
        response = run_ceo_agent(text, session="telegram", tenant_id=tenant_id)
        _send(chat_id, response or "Nie mam odpowiedzi — spróbuj inaczej sformułować pytanie.")
    except Exception as e:
        logger.error(f"Telegram CEO error: {e}")
        _send(chat_id, f"⚠️ Błąd agenta: {str(e)[:150]}")


def _poll_loop():
    logger.info("Telegram bot: polling started")
    offset = 0
    authorized = str(settings.telegram_chat_id)

    while not _stop.is_set():
        try:
            data = _api("getUpdates", offset=offset, timeout=50,
                        allowed_updates=["message"])
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = msg.get("text", "")

                if chat_id != authorized:
                    logger.warning(f"Telegram: ignoring unauthorized chat {chat_id}")
                    continue
                if not text:
                    continue

                logger.info(f"Telegram ← {text[:80]}")
                _handle_message(chat_id, text)

        except Exception as e:
            logger.error(f"Telegram poll error: {e}")
            time.sleep(10)


def start_telegram_bot():
    """Start the bot thread (idempotent). Call from app lifespan."""
    global _thread
    if not settings.telegram_configured:
        return False
    if _thread and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_poll_loop, name="telegram-bot", daemon=True)
    _thread.start()
    logger.info("Telegram bot thread started — chat with your CEO agent!")
    return True


def stop_telegram_bot():
    _stop.set()
