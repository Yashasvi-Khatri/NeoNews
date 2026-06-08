from __future__ import annotations

from datetime import datetime, timezone

from news_app.config import Settings
from news_app.ingestion.extractor import ArticleExtractor
from news_app.ingestion.rss_client import FeedItem
from news_app.ingestion.sources import Source


def test_ndtv_fallback_thumbnail_comes_from_matching_feed_item(monkeypatch):
    settings = Settings()
    extractor = ArticleExtractor(settings)
    source = Source(
        name="NDTV",
        country="India",
        region="India",
        language="en",
        type="publisher",
        homepage="https://www.ndtv.com",
        feed_url="https://feeds.feedburner.com/ndtvnews-latest",
    )

    monkeypatch.setattr(
        "news_app.ingestion.extractor.load_sources",
        lambda _path: [source],
    )
    monkeypatch.setattr(
        "news_app.ingestion.extractor.RSSClient.fetch",
        lambda self, source, require_published_at=False: [
            FeedItem(
                source=source,
                title="NDTV headline",
                link="https://www.ndtv.com/india-news/example-story-123",
                published_at=datetime(2026, 6, 5, tzinfo=timezone.utc),
                description=None,
                thumbnail_url="https://c.ndtvimg.com/story.jpg",
            )
        ],
    )

    image = extractor._fallback_feed_thumbnail("https://www.ndtv.com/india-news/example-story-123")

    assert image == "https://c.ndtvimg.com/story.jpg"
