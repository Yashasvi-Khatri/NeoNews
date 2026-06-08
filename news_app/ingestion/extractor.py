from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from functools import lru_cache
from urllib.parse import urljoin
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import httpx
import trafilatura

from news_app.config import Settings
from news_app.ingestion.dedupe import normalize_url
from news_app.ingestion.rss_client import RSSClient
from news_app.ingestion.sources import load_sources

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractedArticle:
    content: str | None
    thumbnail_url: str | None


class ArticleExtractor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def extract_text(self, url: str) -> str | None:
        return self.extract_article(url).content

    def extract_article(self, url: str) -> ExtractedArticle:
        try:
            with httpx.Client(
                timeout=self.settings.article_fetch_timeout_seconds,
                headers={"User-Agent": self.settings.request_user_agent},
                follow_redirects=True,
            ) as client:
                response = client.get(url)
                response.raise_for_status()
            thumbnail_url = self._extract_thumbnail_url(response.text, url) or self._fallback_feed_thumbnail(url)
            extracted = trafilatura.extract(
                response.text,
                url=url,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
            if not extracted:
                return ExtractedArticle(content=None, thumbnail_url=thumbnail_url)
            return ExtractedArticle(content=self._clean(extracted), thumbnail_url=thumbnail_url)
        except Exception:
            logger.info("Article extraction failed for %s", url, exc_info=True)
            return ExtractedArticle(content=None, thumbnail_url=self._fallback_feed_thumbnail(url))

    def extract_many(self, urls: list[str]) -> dict[str, ExtractedArticle]:
        return asyncio.run(self.extract_many_async(urls))

    async def extract_many_async(self, urls: list[str]) -> dict[str, ExtractedArticle]:
        semaphore = asyncio.Semaphore(max(1, self.settings.article_fetch_concurrency))
        async with httpx.AsyncClient(
            timeout=self.settings.article_fetch_timeout_seconds,
            headers={"User-Agent": self.settings.request_user_agent},
            follow_redirects=True,
        ) as client:
            tasks = [self._extract_async(client, semaphore, url) for url in urls]
            results = await asyncio.gather(*tasks)
        return dict(results)

    async def _extract_async(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        url: str,
    ) -> tuple[str, ExtractedArticle]:
        try:
            async with semaphore:
                response = await client.get(url)
                response.raise_for_status()
            html = response.text
            thumbnail_url = await asyncio.to_thread(self._extract_thumbnail_url, html, url)
            if not thumbnail_url:
                thumbnail_url = await asyncio.to_thread(self._fallback_feed_thumbnail, url)
            extracted = await asyncio.to_thread(
                trafilatura.extract,
                html,
                url=url,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
            if not extracted:
                return url, ExtractedArticle(content=None, thumbnail_url=thumbnail_url)
            return url, ExtractedArticle(content=self._clean(extracted), thumbnail_url=thumbnail_url)
        except Exception:
            logger.info("Article extraction failed for %s", url, exc_info=True)
            return url, ExtractedArticle(content=None, thumbnail_url=await asyncio.to_thread(self._fallback_feed_thumbnail, url))

    def _clean(self, value: str) -> str:
        return " ".join(value.split())

    def _extract_thumbnail_url(self, html: str, page_url: str) -> str | None:
        soup = BeautifulSoup(html or "", "html.parser")
        selectors = (
            ("meta", {"property": "og:image"}, "content"),
            ("meta", {"name": "twitter:image"}, "content"),
            ("meta", {"property": "twitter:image"}, "content"),
            ("link", {"rel": "image_src"}, "href"),
            ("img", {}, "src"),
        )
        for tag_name, attrs, value_attr in selectors:
            tag = soup.find(tag_name, attrs=attrs)
            if tag and tag.get(value_attr):
                return urljoin(page_url, str(tag[value_attr]).strip())
        return None

    @lru_cache(maxsize=64)
    def _fallback_feed_thumbnail(self, page_url: str) -> str | None:
        hostname = urlparse(page_url).hostname or ""
        if not hostname.endswith("ndtv.com"):
            return None

        normalized_page_url = normalize_url(page_url)
        for source in load_sources(self.settings.sources_file):
            source_host = urlparse(source.feed_url).hostname or ""
            if "ndtv" not in source.name.lower() and "ndtv" not in source_host.lower():
                continue
            items = RSSClient(self.settings).fetch(source, require_published_at=False)
            for item in items:
                if normalize_url(item.link) == normalized_page_url:
                    return item.thumbnail_url
        return None
