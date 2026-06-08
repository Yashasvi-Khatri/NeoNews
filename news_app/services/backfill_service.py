from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import desc, select

from news_app.config import Settings, get_settings
from news_app.db import session_scope
from news_app.db.models import NewsArticle
from news_app.ingestion.dedupe import normalize_url
from news_app.ingestion.extractor import ArticleExtractor
from news_app.ingestion.rss_client import RSSClient
from news_app.ingestion.sources import load_sources
from news_app.services.article_enrichment import classify_article, estimate_reading_time, make_article_slug
from news_app.services.thumbnails import cache_thumbnail


@dataclass
class BackfillStats:
    inspected: int = 0
    updated: int = 0
    thumbnails_cached: int = 0


def backfill_article_enrichment(
    settings: Settings | None = None,
    *,
    fetch_thumbnails: bool = False,
    fetch_feed_thumbnails: bool = False,
    limit: int | None = None,
) -> BackfillStats:
    settings = settings or get_settings()
    extractor = ArticleExtractor(settings)
    feed_thumbnail_by_url = _feed_thumbnail_map(settings) if fetch_feed_thumbnails else {}
    stats = BackfillStats()

    with session_scope() as session:
        stmt = select(NewsArticle).order_by(desc(NewsArticle.published_at))
        if limit:
            stmt = stmt.limit(limit)
        articles = list(session.execute(stmt).scalars())

        for article in articles:
            stats.inspected += 1
            changed = False
            content_for_reading = article.content or article.description or article.headline
            category = classify_article(
                article.headline,
                article.description,
                article.content,
                article.source_region,
            )
            reading_time = estimate_reading_time(content_for_reading, settings.reading_words_per_minute)
            slug = make_article_slug(article.headline or article.original_title, article.url_hash)

            if article.primary_category != category.category or article.category_confidence != category.confidence:
                article.primary_category = category.category
                article.category_confidence = category.confidence
                changed = True
            if article.reading_time_minutes != reading_time:
                article.reading_time_minutes = reading_time
                changed = True
            if not article.slug or article.slug != slug:
                article.slug = slug
                changed = True

            if fetch_thumbnails and not article.thumbnail_path:
                extracted = extractor.extract_article(article.article_url)
                if extracted.content and not article.content:
                    article.content = extracted.content
                    changed = True
                thumbnail = cache_thumbnail(settings, extracted.thumbnail_url, article.url_hash)
                if thumbnail:
                    article.thumbnail_path, article.thumbnail_cached_at = thumbnail
                    stats.thumbnails_cached += 1
                    changed = True

            if fetch_feed_thumbnails and not article.thumbnail_path:
                thumbnail_url = feed_thumbnail_by_url.get(normalize_url(article.article_url))
                thumbnail = cache_thumbnail(settings, thumbnail_url, article.url_hash)
                if thumbnail:
                    article.thumbnail_path, article.thumbnail_cached_at = thumbnail
                    stats.thumbnails_cached += 1
                    changed = True

            if changed:
                article.updated_at = datetime.now(timezone.utc)
                stats.updated += 1

    return stats


def _feed_thumbnail_map(settings: Settings) -> dict[str, str]:
    rss_client = RSSClient(settings)
    thumbnail_by_url: dict[str, str] = {}
    for source in load_sources(settings.sources_file):
        if not source.enabled:
            continue
        for item in rss_client.fetch(source):
            if item.thumbnail_url:
                thumbnail_by_url[normalize_url(item.link)] = item.thumbnail_url
    return thumbnail_by_url
