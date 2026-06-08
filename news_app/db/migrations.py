from __future__ import annotations

from datetime import datetime, timezone
import logging

from sqlalchemy import Engine, text

from news_app.services.article_enrichment import classify_article, estimate_reading_time, make_article_slug

logger = logging.getLogger(__name__)

ARTICLE_COLUMNS: dict[str, str] = {
    "event_fingerprint": "TEXT",
    "primary_category": "VARCHAR(40) NOT NULL DEFAULT 'General'",
    "category_confidence": "FLOAT NOT NULL DEFAULT 0.0",
    "reading_time_minutes": "INTEGER NOT NULL DEFAULT 1",
    "slug": "VARCHAR(220)",
    "thumbnail_path": "TEXT",
    "thumbnail_cached_at": "DATETIME",
    "offline_cached_at": "DATETIME",
}

STORY_CLUSTER_COLUMNS: dict[str, str] = {
    "seed_article_id": "INTEGER",
    "cluster_fingerprint": "TEXT",
    "confidence_score": "FLOAT NOT NULL DEFAULT 1.0",
    "first_published_at": "DATETIME",
}


def run_migrations(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        logger.warning("Skipping SQLite-specific migrations for %s", engine.dialect.name)
        return

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS app_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at DATETIME NOT NULL
                )
                """
            )
        )
        _add_article_columns(connection)
        _add_story_cluster_columns(connection)
        _add_metric_columns(connection)
        _create_ingestion_cursor_table(connection)
        _create_government_notice_table(connection)
        _create_indexes(connection)
        _create_fts_table(connection)
        _drop_fts_triggers(connection)
        full_backfill = not _migration_applied(connection, "sqlite_fts_article_enrichment_v1")
        _backfill_article_metadata(connection, full_backfill=full_backfill)
        _rebuild_fts(connection)
        _create_fts_triggers(connection)


def _migration_applied(connection, name: str) -> bool:
    row = connection.execute(
        text("SELECT 1 FROM app_migrations WHERE name = :name"),
        {"name": name},
    ).first()
    return row is not None


def _add_article_columns(connection) -> None:
    existing_columns = {
        row[1] for row in connection.execute(text("PRAGMA table_info(news_articles)")).fetchall()
    }
    for name, definition in ARTICLE_COLUMNS.items():
        if name not in existing_columns:
            connection.execute(text(f"ALTER TABLE news_articles ADD COLUMN {name} {definition}"))


def _add_story_cluster_columns(connection) -> None:
    existing_columns = {
        row[1] for row in connection.execute(text("PRAGMA table_info(story_clusters)")).fetchall()
    }
    for name, definition in STORY_CLUSTER_COLUMNS.items():
        if name not in existing_columns:
            connection.execute(text(f"ALTER TABLE story_clusters ADD COLUMN {name} {definition}"))


def _add_metric_columns(connection) -> None:
    existing_columns = {
        row[1] for row in connection.execute(text("PRAGMA table_info(article_metrics)")).fetchall()
    }
    if "popularity_score" not in existing_columns:
        connection.execute(text("ALTER TABLE article_metrics ADD COLUMN popularity_score FLOAT NOT NULL DEFAULT 0.0"))


def _create_ingestion_cursor_table(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS ingestion_cursors (
                key VARCHAR(80) PRIMARY KEY,
                last_successful_window_end DATETIME,
                last_successful_run_id VARCHAR(80),
                last_completed_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )


def _create_government_notice_table(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS government_notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash VARCHAR(64) NOT NULL,
                document_url TEXT NOT NULL,
                feed_url TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_name VARCHAR(160) NOT NULL,
                source_country VARCHAR(80) NOT NULL DEFAULT 'India',
                source_region VARCHAR(80) NOT NULL DEFAULT 'Government',
                source_type VARCHAR(40) NOT NULL DEFAULT 'government',
                language VARCHAR(16) NOT NULL DEFAULT 'en',
                document_type VARCHAR(80) NOT NULL DEFAULT 'Notice',
                title TEXT NOT NULL,
                description TEXT,
                content TEXT,
                thumbnail_path TEXT,
                published_at DATETIME NOT NULL,
                fetched_at DATETIME NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_government_notices_url_hash UNIQUE (url_hash)
            )
            """
        )
    )


def _create_indexes(connection) -> None:
    statements = [
        "CREATE INDEX IF NOT EXISTS ix_news_articles_category ON news_articles(primary_category)",
        "CREATE INDEX IF NOT EXISTS ix_news_articles_slug ON news_articles(slug)",
        "CREATE INDEX IF NOT EXISTS ix_story_clusters_confidence ON story_clusters(confidence_score)",
        "CREATE INDEX IF NOT EXISTS ix_government_notices_published_at ON government_notices(published_at)",
        "CREATE INDEX IF NOT EXISTS ix_government_notices_source_name ON government_notices(source_name)",
        "CREATE INDEX IF NOT EXISTS ix_government_notices_type ON government_notices(document_type)",
        "CREATE INDEX IF NOT EXISTS ix_government_notices_region ON government_notices(source_region)",
    ]
    for statement in statements:
        connection.execute(text(statement))


def _create_fts_table(connection) -> None:
    connection.execute(
        text(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS news_articles_fts USING fts5(
                headline,
                original_title,
                description,
                content,
                source_name,
                primary_category,
                content='news_articles',
                content_rowid='id'
            )
            """
        )
    )


def _drop_fts_triggers(connection) -> None:
    for trigger_name in ("news_articles_fts_ai", "news_articles_fts_ad", "news_articles_fts_au"):
        connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))


def _create_fts_triggers(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TRIGGER IF NOT EXISTS news_articles_fts_ai AFTER INSERT ON news_articles BEGIN
                INSERT INTO news_articles_fts(
                    rowid, headline, original_title, description, content, source_name, primary_category
                )
                VALUES(
                    new.id, new.headline, new.original_title, new.description, new.content,
                    new.source_name, new.primary_category
                );
            END
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TRIGGER IF NOT EXISTS news_articles_fts_ad AFTER DELETE ON news_articles BEGIN
                INSERT INTO news_articles_fts(
                    news_articles_fts, rowid, headline, original_title, description, content, source_name,
                    primary_category
                )
                VALUES(
                    'delete', old.id, old.headline, old.original_title, old.description, old.content,
                    old.source_name, old.primary_category
                );
            END
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TRIGGER IF NOT EXISTS news_articles_fts_au AFTER UPDATE ON news_articles BEGIN
                INSERT INTO news_articles_fts(
                    news_articles_fts, rowid, headline, original_title, description, content, source_name,
                    primary_category
                )
                VALUES(
                    'delete', old.id, old.headline, old.original_title, old.description, old.content,
                    old.source_name, old.primary_category
                );
                INSERT INTO news_articles_fts(
                    rowid, headline, original_title, description, content, source_name, primary_category
                )
                VALUES(
                    new.id, new.headline, new.original_title, new.description, new.content,
                    new.source_name, new.primary_category
                );
            END
            """
        )
    )


def _backfill_article_metadata(connection, *, full_backfill: bool) -> None:
    where_clause = ""
    if not full_backfill:
        where_clause = """
            WHERE slug IS NULL
               OR slug = ''
               OR primary_category IS NULL
               OR category_confidence IS NULL
               OR reading_time_minutes IS NULL
               OR reading_time_minutes < 1
        """
    rows = connection.execute(
        text(
            f"""
            SELECT id, url_hash, headline, original_title, description, content, source_region,
                   primary_category, category_confidence, reading_time_minutes, slug
            FROM news_articles
            {where_clause}
            """
        )
    ).mappings()
    now = datetime.now(timezone.utc)
    for row in rows:
        title = row["headline"] or row["original_title"] or "Article"
        content = row["content"] or row["description"] or title
        category = classify_article(row["headline"], row["description"], row["content"], row["source_region"])
        connection.execute(
            text(
                """
                UPDATE news_articles
                SET primary_category = :primary_category,
                    category_confidence = :category_confidence,
                    reading_time_minutes = :reading_time_minutes,
                    slug = :slug,
                    updated_at = :updated_at
                WHERE id = :id
                """
            ),
            {
                "id": row["id"],
                "primary_category": category.category,
                "category_confidence": category.confidence,
                "reading_time_minutes": estimate_reading_time(content),
                "slug": make_article_slug(title, row["url_hash"]),
                "updated_at": now,
            },
        )


def _rebuild_fts(connection) -> None:
    connection.execute(text("INSERT INTO news_articles_fts(news_articles_fts) VALUES('rebuild')"))
    connection.execute(
        text(
            """
            INSERT OR REPLACE INTO app_migrations(name, applied_at)
            VALUES('sqlite_fts_article_enrichment_v1', :applied_at)
            """
        ),
        {"applied_at": datetime.now(timezone.utc)},
    )
