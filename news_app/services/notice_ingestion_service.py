from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dateutil import parser as date_parser
import httpx

from news_app.config import Settings, get_settings
from news_app.db import session_scope
from news_app.db.repository import ArticleRepository, NoticeCreate
from news_app.ingestion.dedupe import hash_text, normalize_url
from news_app.ingestion.notice_sources import NoticeSource, load_notice_sources
from news_app.ingestion.rss_client import RSSClient
from news_app.ingestion.sources import Source

logger = logging.getLogger(__name__)


@dataclass
class NoticeIngestionStats:
    sources_total: int = 0
    sources_enabled: int = 0
    sources_failed: list[str] | None = None
    items_seen: int = 0
    prepared: int = 0
    inserted: int = 0
    updated: int = 0

    def __post_init__(self) -> None:
        if self.sources_failed is None:
            self.sources_failed = []


@dataclass(frozen=True)
class NoticeCandidate:
    source: NoticeSource
    title: str
    description: str | None
    url: str
    published_at: datetime


class NoticeIngestionService:
    def __init__(
        self,
        settings: Settings | None = None,
        repository: ArticleRepository | None = None,
        rss_client: RSSClient | None = None,
    ):
        self.settings = settings or get_settings()
        self.repository = repository or ArticleRepository()
        self.rss_client = rss_client or RSSClient(self.settings)

    def run(self) -> NoticeIngestionStats:
        sources = load_notice_sources(self.settings.notice_sources_file)
        enabled_sources = [source for source in sources if source.enabled]
        stats = NoticeIngestionStats(sources_total=len(sources), sources_enabled=len(enabled_sources))
        prepared: list[NoticeCreate] = []

        with session_scope() as session:
            for source in enabled_sources:
                candidates = self._fetch_source(source)
                stats.items_seen += len(candidates)
                self.repository.record_source_health(
                    session,
                    source.name,
                    source.feed_url,
                    success=bool(candidates),
                    items_seen=len(candidates),
                    failure_reason=None if candidates else "Notice source returned no valid items",
                )
                if not candidates:
                    stats.sources_failed.append(source.name)
                    continue
                for candidate in candidates:
                    normalized_url = normalize_url(candidate.url)
                    prepared.append(
                        NoticeCreate(
                            url_hash=hash_text(normalized_url),
                            document_url=normalized_url,
                            feed_url=source.feed_url,
                            source_url=source.homepage,
                            source_name=source.name,
                            source_country=source.country,
                            source_region=source.region,
                            source_type=source.type,
                            language=source.language,
                            document_type=source.document_type,
                            title=candidate.title,
                            description=candidate.description,
                            content=candidate.description,
                            thumbnail_path="/static/fallbacks/notice.svg",
                            published_at=candidate.published_at,
                        )
                    )
            stats.prepared = len(prepared)
            if prepared:
                stats.inserted, stats.updated = self.repository.upsert_notices(session, prepared)

        logger.info("Notice ingestion finished: %s", stats)
        return stats

    def _fetch_source(self, source: NoticeSource) -> list[NoticeCandidate]:
        if source.mode == "page":
            return self._fetch_page_source(source)
        return self._fetch_rss_source(source)

    def _fetch_rss_source(self, source: NoticeSource) -> list[NoticeCandidate]:
        rss_source = Source(
            name=source.name,
            country=source.country,
            region=source.region,
            language=source.language,
            type=source.type,
            homepage=source.homepage,
            feed_url=source.feed_url,
            enabled=source.enabled,
        )
        items = self.rss_client.fetch(rss_source, require_published_at=False)
        return [
            NoticeCandidate(
                source=source,
                title=item.title,
                description=item.description,
                url=item.link,
                published_at=item.published_at,
            )
            for item in items
        ]

    def _fetch_page_source(self, source: NoticeSource) -> list[NoticeCandidate]:
        try:
            with httpx.Client(
                timeout=self.settings.feed_fetch_timeout_seconds,
                headers={"User-Agent": self.settings.request_user_agent},
                follow_redirects=True,
            ) as client:
                response = client.get(source.feed_url)
                response.raise_for_status()
        except Exception:
            logger.info("Notice page fetch failed for %s", source.name, exc_info=True)
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        base_domain = urlparse(str(response.url)).netloc
        candidates: list[NoticeCandidate] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            title = _clean(anchor.get_text(" ", strip=True))
            href = str(anchor["href"]).strip()
            if not _looks_like_notice_link(title, href):
                continue
            url = normalize_url(urljoin(str(response.url), href))
            if url in seen:
                continue
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or parsed.netloc != base_domain:
                continue
            seen.add(url)
            context = _clean(anchor.parent.get_text(" ", strip=True) if anchor.parent else title)
            candidates.append(
                NoticeCandidate(
                    source=source,
                    title=title,
                    description=context if context and context != title else None,
                    url=url,
                    published_at=_parse_date(context) or datetime.now(timezone.utc),
                )
            )
            if len(candidates) >= 80:
                break
        return candidates


def run_notice_ingestion() -> NoticeIngestionStats:
    return NoticeIngestionService(get_settings()).run()


def _clean(value: str) -> str:
    return " ".join((value or "").split())


def _looks_like_notice_link(title: str, href: str) -> bool:
    if not title or len(title) < 8:
        return False
    lowered = f"{title} {href}".lower()
    if any(skip in lowered for skip in ("javascript:", "mailto:", "facebook", "twitter", "instagram", "youtube")):
        return False
    keywords = (
        "notice",
        "notification",
        "circular",
        "press release",
        "release",
        "report",
        "policy",
        "rule",
        "regulation",
        "gazette",
        "budget",
        "survey",
        "bill",
        "act",
        "order",
        "guideline",
        "scheme",
        ".pdf",
    )
    return any(keyword in lowered for keyword in keywords)


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    patterns = (
        r"\b\d{1,2}\s+[A-Z][a-z]+\s*,?\s+\d{4}\b",
        r"\b[A-Z][a-z]+\s+\d{1,2}\s*,?\s+\d{4}\b",
        r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b",
        r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        try:
            parsed = date_parser.parse(match.group(0))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
    return None
