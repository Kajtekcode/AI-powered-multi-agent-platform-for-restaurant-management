"""
Background Scheduler — APScheduler for periodic data sync.
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler = None


def _sync_job():
    """Called every SYNC_INTERVAL_MINUTES — incremental sync for all connected tenants."""
    try:
        from app.data.sync import sync_all_tenants
        result = sync_all_tenants(None)  # auto window per tenant
        logger.info(f"Scheduled sync: {result['tenants']} tenants")
    except Exception as e:
        logger.error(f"Scheduled sync failed: {e}")


def _briefing_job():
    """Daily morning briefing → each tenant's configured channel."""
    try:
        from app.core.notifications import send_morning_briefing_all
        result = send_morning_briefing_all()
        logger.info(f"Morning briefing: {result}")
    except Exception as e:
        logger.error(f"Morning briefing failed: {e}")


def start_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _sync_job,
        trigger=IntervalTrigger(minutes=settings.sync_interval_minutes),
        id="gopos_sync",
        name="GoPOS Incremental Sync",
        replace_existing=True,
    )
    if settings.notifications_configured:
        _scheduler.add_job(
            _briefing_job,
            trigger=CronTrigger(hour=settings.briefing_hour, minute=0),
            id="morning_briefing",
            name="Morning Briefing",
            replace_existing=True,
        )
        logger.info(f"Morning briefing scheduled daily at {settings.briefing_hour}:00")
    _scheduler.start()
    logger.info(f"Scheduler started: sync every {settings.sync_interval_minutes} minutes")


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown()
