import feedparser

from news_app.config import Settings
from news_app.ingestion.rss_client import RSSClient
from news_app.ingestion.sources import Source


def _client() -> RSSClient:
    return RSSClient(Settings())


def _source() -> Source:
    return Source(
        name="Example",
        country="India",
        region="Asia",
        language="en",
        type="publisher",
        homepage="https://example.com",
        feed_url="https://example.com/feed.xml",
    )


def test_feed_item_uses_media_thumbnail_when_present():
    entry = feedparser.FeedParserDict(
        {
            "title": "Example headline",
            "link": "https://example.com/story",
            "published": "Wed, 03 Jun 2026 10:00:00 GMT",
            "summary": "Story summary",
            "media_thumbnail": [{"url": "https://cdn.example.com/thumb.jpg"}],
        }
    )

    item = _client()._entry_to_item(_source(), entry)

    assert item is not None
    assert item.thumbnail_url == "https://cdn.example.com/thumb.jpg"


def test_feed_item_falls_back_to_image_in_summary_html():
    entry = feedparser.FeedParserDict(
        {
            "title": "Example headline",
            "link": "https://example.com/story",
            "published": "Wed, 03 Jun 2026 10:00:00 GMT",
            "summary": '<p><img src="/images/story.jpg" />Story summary</p>',
        }
    )

    item = _client()._entry_to_item(_source(), entry)

    assert item is not None
    assert item.thumbnail_url == "https://example.com/images/story.jpg"
