import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from news_app.config import Settings
from news_app.services.ingestion_service import run_recent_ingestion, run_retention_cleanup

logger = logging.getLogger(__name__)


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=ZoneInfo(settings.app_timezone))
    trigger = CronTrigger(
        hour=settings.ingestion_hour,
        minute=settings.ingestion_minute,
        timezone=ZoneInfo(settings.app_timezone),
    )
    scheduler.add_job(
        _scheduled_ingestion_job,
        trigger=trigger,
        id="recent_news_ingestion",
        name="Collect latest 24-hour news",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _scheduled_retention_job,
        trigger="interval",
        hours=1,
        id="recent_news_retention",
        name="Prune expired live news",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def _scheduled_ingestion_job() -> None:
    try:
        run_recent_ingestion()
    except Exception:
        logger.exception("Scheduled ingestion failed")


def _scheduled_retention_job() -> None:
    try:
        run_retention_cleanup()
    except Exception:
        logger.exception("Scheduled retention cleanup failed")
