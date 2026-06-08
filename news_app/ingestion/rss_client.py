from __future__ import annotations

import asyncio
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from time import struct_time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dateutil import parser as date_parser
import feedparser
import httpx

from news_app.config import Settings
from news_app.ingestion.sources import Source

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedItem:
    source: Source
    title: str
    link: str
    published_at: datetime
    description: str | None
    thumbnail_url: str | None = None


class RSSClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch(self, source: Source, require_published_at: bool = True) -> list[FeedItem]:
        try:
            with httpx.Client(
                timeout=self.settings.feed_fetch_timeout_seconds,
                headers={"User-Agent": self.settings.request_user_agent},
                follow_redirects=True,
            ) as client:
                response = client.get(source.feed_url)
                response.raise_for_status()
            parsed = feedparser.parse(response.content)
            items: list[FeedItem] = []
            for entry in parsed.entries:
                item = self._entry_to_item(source, entry, require_published_at=require_published_at)
                if item is not None:
                    items.append(item)
            return items
        except Exception:
            logger.exception("Failed to fetch feed: %s", source.name)
            return []

    def fetch_many(self, sources: list[Source]) -> dict[Source, list[FeedItem]]:
        return asyncio.run(self.fetch_many_async(sources))

    async def fetch_many_async(self, sources: list[Source]) -> dict[Source, list[FeedItem]]:
        semaphore = asyncio.Semaphore(max(1, self.settings.feed_fetch_concurrency))
        async with httpx.AsyncClient(
            timeout=self.settings.feed_fetch_timeout_seconds,
            headers={"User-Agent": self.settings.request_user_agent},
            follow_redirects=True,
        ) as client:
            tasks = [self._fetch_async(client, semaphore, source) for source in sources]
            results = await asyncio.gather(*tasks)
        return dict(results)

    async def _fetch_async(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        source: Source,
    ) -> tuple[Source, list[FeedItem]]:
        try:
            async with semaphore:
                response = await client.get(source.feed_url)
                response.raise_for_status()
            parsed = await asyncio.to_thread(feedparser.parse, response.content)
            items: list[FeedItem] = []
            for entry in parsed.entries:
                item = self._entry_to_item(source, entry, require_published_at=True)
                if item is not None:
                    items.append(item)
            return source, items
        except Exception:
            logger.exception("Failed to fetch feed: %s", source.name)
            return source, []

    def _entry_to_item(
        self,
        source: Source,
        entry: feedparser.FeedParserDict,
        require_published_at: bool = True,
    ) -> FeedItem | None:
        title = self._clean_text(entry.get("title", ""))
        link = str(entry.get("link", "")).strip()
        published_at = self._parse_published_at(entry)
        if not title or not link:
            return None
        if published_at is None:
            if require_published_at:
                return None
            published_at = datetime.now(timezone.utc)
        description = self._clean_text(entry.get("summary", "") or entry.get("description", "")) or None
        thumbnail_url = self._extract_thumbnail_url(entry, link)
        return FeedItem(
            source=source,
            title=title,
            link=link,
            published_at=published_at,
            description=description,
            thumbnail_url=thumbnail_url,
        )

    def _parse_published_at(self, entry: feedparser.FeedParserDict) -> datetime | None:
        for field in ("published_parsed", "updated_parsed", "created_parsed"):
            value = entry.get(field)
            if isinstance(value, struct_time):
                return datetime.fromtimestamp(timegm(value), tz=timezone.utc)

        for field in ("published", "updated", "created", "pubDate"):
            value = entry.get(field)
            if not value:
                continue
            try:
                parsed = date_parser.parse(str(value))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except (ValueError, TypeError, OverflowError):
                continue
        return None

    def _clean_text(self, value: str) -> str:
        soup = BeautifulSoup(value or "", "html.parser")
        return " ".join(soup.get_text(" ", strip=True).split())

    def _extract_thumbnail_url(self, entry: feedparser.FeedParserDict, page_url: str) -> str | None:
        for field in ("media_content", "media_thumbnail"):
            values = entry.get(field)
            if not values:
                continue
            for item in values:
                if not isinstance(item, dict):
                    continue
                candidate = item.get("url") or item.get("href")
                if candidate:
                    return urljoin(page_url, str(candidate).strip())

        for link in entry.get("links", []) or []:
            if not isinstance(link, dict):
                continue
            href = link.get("href")
            link_type = str(link.get("type", "")).lower()
            rel = str(link.get("rel", "")).lower()
            if href and (rel == "enclosure" or link_type.startswith("image/")):
                return urljoin(page_url, str(href).strip())

        for field in ("summary_detail",):
            value = entry.get(field)
            if isinstance(value, dict):
                image = self._extract_image_from_html(str(value.get("value", "")), page_url)
                if image:
                    return image

        for field in ("summary", "description"):
            image = self._extract_image_from_html(str(entry.get(field, "")), page_url)
            if image:
                return image

        for block in entry.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            image = self._extract_image_from_html(str(block.get("value", "")), page_url)
            if image:
                return image

        return None

    def _extract_image_from_html(self, value: str, page_url: str) -> str | None:
        if not value:
            return None
        soup = BeautifulSoup(value, "html.parser")
        for selector in (
            ("meta", {"property": "og:image"}, "content"),
            ("meta", {"name": "twitter:image"}, "content"),
            ("meta", {"property": "twitter:image"}, "content"),
            ("img", {}, "src"),
        ):
            tag = soup.find(selector[0], attrs=selector[1])
            if tag and tag.get(selector[2]):
                return urljoin(page_url, str(tag[selector[2]]).strip())
        return None
