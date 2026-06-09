from collections.abc import Iterator
from contextlib import contextmanager
import logging
import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from news_app.config import get_settings
from news_app.db.exceptions import DatabaseOperationError
from news_app.db.migrations import run_migrations
from news_app.db.models import Base

logger = logging.getLogger(__name__)
settings = get_settings()

# Detect if running on Vercel (serverless environment)
_is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _ensure_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    db_path = database_url.replace("sqlite:///", "", 1)
    if db_path == ":memory:":
        return
    try:
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning(f"Failed to create database directory: {exc}")


_ensure_sqlite_parent(settings.database_url)
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    # Skip WAL mode on Vercel/serverless as it requires additional files
    if _is_serverless:
        logger.info("Serverless environment detected - skipping WAL mode")
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    except Exception as exc:
        logger.warning(f"Failed to configure SQLite PRAGMAs: {exc}")
    finally:
        cursor.close()


def init_db() -> None:
    try:
        Base.metadata.create_all(bind=engine)
        run_migrations(engine)
        logger.info("Database initialized successfully")
    except Exception as exc:
        logger.exception("Database initialization failed")
        # On Vercel, fall back to in-memory database if file-based fails
        if _is_serverless and "sqlite" in str(settings.database_url).lower():
            logger.warning("File-based SQLite failed on serverless, falling back to memory")
            global engine, SessionLocal
            try:
                engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, future=True)
                SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
                Base.metadata.create_all(bind=engine)
                run_migrations(engine)
                logger.info("In-memory database initialized successfully")
            except Exception as fallback_exc:
                logger.exception("In-memory database initialization also failed")
                # On Vercel, completely disable database if both fail
                logger.warning("Database initialization completely failed, running without database")
                # Create a dummy engine that will fail gracefully
                engine = None
                SessionLocal = None
        else:
            raise DatabaseOperationError("Database initialization failed") from exc


@contextmanager
def session_scope() -> Iterator[Session]:
    if SessionLocal is None:
        raise DatabaseOperationError("Database not available in this environment")
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception as exc:
        session.rollback()
        if isinstance(exc, DatabaseOperationError):
            logger.exception("Database operation failed")
            raise
        logger.exception("Database transaction failed")
        raise DatabaseOperationError("Database transaction failed") from exc
    finally:
        session.close()
