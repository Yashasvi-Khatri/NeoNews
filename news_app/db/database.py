from collections.abc import Iterator
from contextlib import contextmanager
import logging
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from news_app.config import get_settings
from news_app.db.exceptions import DatabaseOperationError
from news_app.db.migrations import run_migrations
from news_app.db.models import Base

logger = logging.getLogger(__name__)
settings = get_settings()


def _ensure_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    db_path = database_url.replace("sqlite:///", "", 1)
    if db_path == ":memory:":
        return
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent(settings.database_url)
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def init_db() -> None:
    try:
        Base.metadata.create_all(bind=engine)
        run_migrations(engine)
    except Exception as exc:
        logger.exception("Database initialization failed")
        raise DatabaseOperationError("Database initialization failed") from exc


@contextmanager
def session_scope() -> Iterator[Session]:
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
