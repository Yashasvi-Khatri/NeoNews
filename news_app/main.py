from contextlib import asynccontextmanager
import logging
import os
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from news_app.config import get_settings
from news_app.db import init_db
from news_app.logging_config import configure_logging
from news_app.services.ingestion_service import run_recent_ingestion, run_retention_cleanup
from news_app.services.scheduler import build_scheduler
from news_app.web.routes import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as exc:
        logger.warning(f"Database initialization failed, continuing without database: {exc}")
    
    try:
        run_retention_cleanup(settings=settings)
    except Exception as exc:
        logger.warning(f"Retention cleanup failed: {exc}")

    # Detect serverless environment (Vercel sets AWS_LAMBDA_FUNCTION_NAME or VERCEL)
    _is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

    scheduler = None
    if settings.scheduler_enabled and not _is_serverless:
        scheduler = build_scheduler(settings)
        scheduler.start()
        app.state.scheduler = scheduler
        logger.info(
            "Scheduler started for %02d:%02d %s",
            settings.ingestion_hour,
            settings.ingestion_minute,
            settings.app_timezone,
        )
    elif _is_serverless:
        logger.info("Serverless environment detected — scheduler disabled")

    if settings.auto_ingest_on_startup and not _is_serverless:
        threading.Thread(target=run_recent_ingestion, daemon=True).start()

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],          # Tighten to your Vercel domain in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    static_dir = Path(__file__).resolve().parent / "web" / "static"
    if static_dir.exists():
        try:
            app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
            logger.info(f"Static files mounted from {static_dir}")
        except Exception as exc:
            logger.warning(f"Failed to mount static files: {exc}")
    else:
        logger.warning(f"Static directory not found: {static_dir}")
    app.include_router(router)
    return app


app = create_app()
