"""
Notifications API — test channels, send briefing on demand.
"""
import logging
from fastapi import APIRouter

from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/status")
async def notifications_status():
    return {
        "telegram": settings.telegram_configured,
        "slack": settings.slack_configured,
        "briefing_hour": settings.briefing_hour,
        "any_configured": settings.notifications_configured,
    }


@router.post("/test")
async def send_test():
    """Send a test message to all configured channels."""
    from app.core.notifications import send_all
    if not settings.notifications_configured:
        return {"status": "error", "message": "Brak konfiguracji — ustaw TELEGRAM_BOT_TOKEN i TELEGRAM_CHAT_ID (lub SLACK_WEBHOOK_URL) w .env"}
    results = send_all("✅ Restaurant Growth OS — test powiadomień działa!")
    return {"status": "sent", **results}


@router.post("/send-briefing")
async def send_briefing_now():
    """Generate and send the morning briefing immediately."""
    from app.core.notifications import send_morning_briefing
    return send_morning_briefing()
