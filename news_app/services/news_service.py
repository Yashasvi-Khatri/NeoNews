from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import hmac
import re
import secrets
from urllib.parse import quote
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import Response

from news_app.config import Settings, get_settings
from news_app.db import session_scope
from news_app.db.models import GovernmentNotice, NewsArticle, StoryCluster
from news_app.db.repository import ArticleRepository
from news_app.llm import LocalLLMClient
from news_app.llm.prompts import SUMMARY_SYSTEM_PROMPT, story_summary_prompt, summary_prompt
from news_app.services.story_clustering import StoryClusteringService
from news_app.services.story_clustering import MULTI_SOURCE_CONFIDENCE_THRESHOLD
from news_app.services.text_limits import enforce_summary_limit
from news_app.time_window import rolling_utc_window


class ArticleNotFoundError(RuntimeError):
    pass


class ShareLinkNotFoundError(RuntimeError):
    pass


class StoryClusterNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class CurrentUser:
    id: int
    public_id: str
    is_anonymous: bool
    cookie_value: str
    email: str | None = None
    display_name: str | None = None


class NewsService:
    def __init__(
        self,
        settings: Settings | None = None,
        repository: ArticleRepository | None = None,
        llm_client: LocalLLMClient | None = None,
    ):
        self.settings = settings or get_settings()
        self.repository = repository or ArticleRepository()
        self.llm_client = llm_client or LocalLLMClient(self.settings)

    def ensure_user(self, request: Request) -> CurrentUser:
        cookie_value = request.cookies.get(self.settings.session_cookie_name)
        public_id = self._decode_user_cookie(cookie_value) if cookie_value else None
        if not public_id:
            public_id = secrets.token_urlsafe(18)

        with session_scope() as session:
            user = self.repository.get_user_by_public_id(session, public_id)
            if user is None:
                user = self.repository.create_user(session, public_id)
            self.repository.touch_user(user)
            return CurrentUser(
                id=user.id,
                public_id=user.public_id,
                is_anonymous=user.is_anonymous,
                email=user.email,
                display_name=user.display_name,
                cookie_value=self._encode_user_cookie(user.public_id),
            )

    def attach_user_cookie(self, response: Response, current_user: CurrentUser) -> None:
        response.set_cookie(
            self.settings.session_cookie_name,
            current_user.cookie_value,
            max_age=self.settings.session_max_age_days * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
        )

    def clear_user_cookie(self, response: Response) -> None:
        response.delete_cookie(self.settings.session_cookie_name)

    def list_articles(
        self,
        target_date: date | None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
        source: str | None = None,
        region: str | None = None,
        query: str | None = None,
        category: str | None = None,
        sort: str = "date",
        limit: int = 500,
        offset: int = 0,
        current_user: CurrentUser | None = None,
        view: str | None = None,
        exclude_clustered: bool = False,
    ) -> list[dict]:
        with session_scope() as session:
            articles = self.repository.list_articles(
                session,
                target_date=target_date,
                published_after=published_after,
                published_before=published_before,
                source=source,
                region=region,
                query=query,
                category=category,
                sort=sort,
                limit=limit,
                offset=offset,
                user_id=current_user.id if current_user else None,
                view=view,
                exclude_clustered=exclude_clustered,
            )
            return self._serialize_articles(session, articles, current_user, include_content=False)

    def get_article(
        self,
        article_id: int,
        current_user: CurrentUser | None = None,
        include_content: bool = True,
    ) -> dict:
        with session_scope() as session:
            article = self.repository.get_article(session, article_id)
            if article is None:
                raise ArticleNotFoundError(f"Article {article_id} not found")
            return self._serialize_articles(session, [article], current_user, include_content=include_content)[0]

    def summarize_article(self, article_id: int, current_user: CurrentUser | None = None) -> dict:
        with session_scope() as session:
            article = self.repository.get_article(session, article_id)
            if article is None:
                raise ArticleNotFoundError(f"Article {article_id} not found")
            if article.summary:
                self.repository.record_event(
                    session,
                    current_user.id if current_user else None,
                    article_id,
                    "summary_open",
                )
                return self._serialize_articles(session, [article], current_user, include_content=False)[0]

            prompt = summary_prompt(article.headline, article.source_name, article.content, article.description)
            generated = self.llm_client.generate(SUMMARY_SYSTEM_PROMPT, prompt, max_tokens=320)
            summary = enforce_summary_limit(generated, 200)
            article = self.repository.save_summary(session, article_id, summary)
            if article is None:
                raise ArticleNotFoundError(f"Article {article_id} not found")
            self.repository.record_event(session, current_user.id if current_user else None, article_id, "summary_open")
            return self._serialize_articles(session, [article], current_user, include_content=False)[0]

    def list_story_clusters(
        self,
        target_date: date | None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
        min_source_count: int | None = None,
        min_confidence: float | None = None,
        source: str | None = None,
        region: str | None = None,
        query: str | None = None,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
        current_user: CurrentUser | None = None,
    ) -> list[dict]:
        with session_scope() as session:
            clusters = self.repository.list_story_clusters(
                session,
                target_date=target_date,
                published_after=published_after,
                published_before=published_before,
                min_source_count=min_source_count,
                min_confidence=min_confidence,
                source=source,
                region=region,
                query=query,
                category=category,
                limit=limit,
                offset=offset,
            )
            return [self._serialize_story_cluster(session, cluster, current_user) for cluster in clusters]

    def get_story_cluster(
        self,
        cluster_id: int,
        current_user: CurrentUser | None = None,
        record_view: bool = False,
    ) -> dict:
        with session_scope() as session:
            cluster = self.repository.get_story_cluster(session, cluster_id)
            if cluster is None:
                raise StoryClusterNotFoundError(f"Story cluster {cluster_id} not found")
            if record_view and current_user is not None:
                self.repository.record_event(session, current_user.id, None, "view", f"story:{cluster.id}")
            return self._serialize_story_cluster(session, cluster, current_user)

    def summarize_story_cluster(self, cluster_id: int, current_user: CurrentUser | None = None) -> dict:
        with session_scope() as session:
            cluster = self.repository.get_story_cluster(session, cluster_id)
            if cluster is None:
                raise StoryClusterNotFoundError(f"Story cluster {cluster_id} not found")
            if cluster.summary:
                return self._serialize_story_cluster(session, cluster, current_user)
            articles = self.repository.story_cluster_articles(session, cluster_id)
            prompt_articles = [
                {
                    "source_name": article.source_name,
                    "headline": article.headline,
                    "description": article.description,
                    "content": article.content,
                }
                for article in articles
            ]
            generated = self.llm_client.generate(
                SUMMARY_SYSTEM_PROMPT,
                story_summary_prompt(cluster.title, prompt_articles),
                max_tokens=300,
            )
            summary = enforce_summary_limit(generated, 180)
            cluster = self.repository.save_story_cluster_summary(session, cluster_id, summary)
            if cluster is None:
                raise StoryClusterNotFoundError(f"Story cluster {cluster_id} not found")
            return self._serialize_story_cluster(session, cluster, current_user)

    def rebuild_story_clusters(self, target_date: date) -> dict:
        count = StoryClusteringService(self.repository).rebuild_for_date(target_date)
        return {"date": target_date.isoformat(), "clusters": count}

    def rebuild_story_clusters_for_window(
        self,
        target_date: date,
        published_after: datetime,
        published_before: datetime,
    ) -> dict:
        count = StoryClusteringService(self.repository).rebuild_for_window(target_date, published_after, published_before)
        return {
            "date": target_date.isoformat(),
            "window_start": published_after.isoformat(),
            "window_end": published_before.isoformat(),
            "clusters": count,
        }

    def available_dates(self) -> list[str]:
        with session_scope() as session:
            return [value.isoformat() for value in self.repository.available_dates(session)]

    def facets(self) -> dict[str, list[str]]:
        with session_scope() as session:
            return {
                "sources": self.repository.source_names(session),
                "regions": self.repository.regions(session),
                "countries": self.repository.countries(session),
                "languages": self.repository.languages(session),
            }

    def categories(
        self,
        target_date: date | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
    ) -> list[dict[str, int | str]]:
        with session_scope() as session:
            return self.repository.categories(session, target_date, published_after, published_before)

    def stats_for_date(self, target_date: date) -> dict[str, int]:
        with session_scope() as session:
            return self.repository.stats_for_date(session, target_date)

    def stats_for_window(self, published_after: datetime, published_before: datetime) -> dict[str, int]:
        with session_scope() as session:
            return self.repository.stats_for_window(session, published_after, published_before)

    def bookmark_article(self, article_id: int, current_user: CurrentUser) -> dict:
        with session_scope() as session:
            article = self.repository.get_article(session, article_id)
            if article is None:
                raise ArticleNotFoundError(f"Article {article_id} not found")
            _, created = self.repository.bookmark_article(session, current_user.id, article_id)
            if created:
                self.repository.record_event(session, current_user.id, article_id, "bookmark")
            serialized = self._serialize_articles(session, [article], current_user, include_content=False)[0]
            serialized["created"] = created
            return serialized

    def remove_bookmark(self, article_id: int, current_user: CurrentUser) -> dict:
        with session_scope() as session:
            article = self.repository.get_article(session, article_id)
            if article is None:
                raise ArticleNotFoundError(f"Article {article_id} not found")
            removed = self.repository.remove_bookmark(session, current_user.id, article_id)
            if removed:
                self.repository.record_event(session, current_user.id, article_id, "unbookmark")
            serialized = self._serialize_articles(session, [article], current_user, include_content=False)[0]
            serialized["removed"] = removed
            serialized["is_bookmarked"] = False
            if removed:
                session.flush()
                window = rolling_utc_window(self.settings.app_timezone)
                serialized["deleted_after_unbookmark"] = self.repository.prune_article_if_expired_unbookmarked(
                    session,
                    article_id,
                    window.start_utc,
                )
            return serialized

    def record_article_event(
        self,
        event_type: str,
        article_id: int | None,
        current_user: CurrentUser | None,
        event_value: str | None = None,
    ) -> dict:
        allowed = {"view", "summary_open", "source_open", "bookmark", "unbookmark", "share"}
        if event_type not in allowed:
            raise ValueError(f"Unsupported event type: {event_type}")
        with session_scope() as session:
            if article_id is not None and self.repository.get_article(session, article_id) is None:
                raise ArticleNotFoundError(f"Article {article_id} not found")
            self.repository.record_event(
                session,
                current_user.id if current_user else None,
                article_id,
                event_type,
                event_value,
            )
            return {"status": "recorded"}

    def create_share(
        self,
        article_id: int,
        current_user: CurrentUser,
        base_url: str,
        platform: str | None = None,
    ) -> dict:
        with session_scope() as session:
            article = self.repository.get_article(session, article_id)
            if article is None:
                raise ArticleNotFoundError(f"Article {article_id} not found")
            share_link = self.repository.create_share_link(session, article_id, current_user.id, platform)
            self.repository.record_event(session, current_user.id, article_id, "share", platform)
            share_url = f"{base_url.rstrip('/')}/s/{share_link.token}"
            message = f"{article.headline} - {article.source_name}"
            encoded_text = quote(f"{message} {share_url}")
            return {
                "token": share_link.token,
                "share_url": share_url,
                "message": message,
                "whatsapp_url": f"https://wa.me/?text={encoded_text}",
                "telegram_url": f"https://t.me/share/url?url={quote(share_url)}&text={quote(message)}",
            }

    def resolve_share(self, token: str) -> str:
        with session_scope() as session:
            share_link = self.repository.record_share_click(session, token)
            if share_link is None:
                raise ShareLinkNotFoundError(f"Share link {token} not found")
            article = self.repository.get_article(session, share_link.article_id)
            if article is None:
                raise ArticleNotFoundError(f"Article {share_link.article_id} not found")
            self.repository.record_event(session, share_link.user_id, article.id, "share", "click")
            return article_app_path(article)

    def offline_top(self, limit: int, current_user: CurrentUser | None = None) -> list[dict]:
        with session_scope() as session:
            articles = self.repository.offline_top_articles(session, min(limit, self.settings.offline_article_limit))
            now = datetime.now(timezone.utc)
            for article in articles:
                article.offline_cached_at = now
            return self._serialize_articles(session, articles, current_user, include_content=False)

    def popular_articles(
        self,
        limit: int = 5,
        metric: str = "overall",
        target_date: date | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
        current_user: CurrentUser | None = None,
    ) -> list[dict]:
        with session_scope() as session:
            articles = self.repository.popular_articles(
                session,
                limit=limit,
                metric=metric,
                target_date=target_date,
                published_after=published_after,
                published_before=published_before,
            )
            return self._serialize_articles(session, articles, current_user, include_content=False)

    def trending_cards(
        self,
        target_date: date | None,
        published_after: datetime,
        published_before: datetime,
        limit: int = 5,
        current_user: CurrentUser | None = None,
    ) -> list[dict]:
        requested_limit = max(1, min(limit, 20))
        with session_scope() as session:
            clusters = self.repository.list_story_clusters(
                session,
                target_date=target_date,
                published_after=published_after,
                published_before=published_before,
                min_source_count=2,
                min_confidence=MULTI_SOURCE_CONFIDENCE_THRESHOLD,
                limit=100,
            )
            cluster_cards = []
            clustered_article_ids: set[int] = set()
            for cluster in clusters:
                articles = self.repository.story_cluster_articles(session, cluster.id)
                clustered_article_ids.update(article.id for article in articles)
                cluster_cards.append(self._trending_cluster_card(session, cluster, articles, current_user))

            cluster_cards = sorted(cluster_cards, key=lambda item: item["trending_score"], reverse=True)
            if len(cluster_cards) >= requested_limit:
                return cluster_cards[:requested_limit]

            single_articles = self.repository.list_articles(
                session,
                target_date=target_date,
                published_after=published_after,
                published_before=published_before,
                sort="date",
                limit=150,
                exclude_clustered=True,
            )
            single_cards = [
                self._trending_article_card(session, article, current_user)
                for article in single_articles
                if article.id not in clustered_article_ids
            ]
            single_cards = [
                card
                for card in sorted(single_cards, key=lambda item: item["trending_score"], reverse=True)
                if card["trending_score"] >= 4.0
            ]
            return [*cluster_cards, *single_cards[: requested_limit - len(cluster_cards)]]

    def recently_viewed_articles(
        self,
        current_user: CurrentUser,
        limit: int = 5,
    ) -> list[dict]:
        with session_scope() as session:
            entries = self.repository.recently_viewed_cards(session, current_user.id, limit=limit)
            cards: list[dict] = []
            article_entries = [item for kind, item in entries if kind == "article"]
            serialized_articles = {
                item["id"]: item
                for item in self._serialize_articles(session, article_entries, current_user, include_content=False)
            }
            for kind, item in entries:
                if kind == "story":
                    payload = self._serialize_story_cluster(session, item, current_user)
                    payload["kind"] = "story"
                    payload["article_path"] = payload["story_path"]
                    cards.append(payload)
                else:
                    payload = serialized_articles[item.id]
                    payload["kind"] = "article"
                    cards.append(payload)
            return cards

    def related_articles(
        self,
        article_id: int,
        limit: int = 6,
        current_user: CurrentUser | None = None,
    ) -> list[dict]:
        with session_scope() as session:
            article = self.repository.get_article(session, article_id)
            if article is None:
                raise ArticleNotFoundError(f"Article {article_id} not found")
            related = self.repository.related_articles(session, article, limit=limit)
            return self._serialize_articles(session, related, current_user, include_content=False)

    def preferences(self, current_user: CurrentUser) -> dict[str, list[str]]:
        with session_scope() as session:
            return self.repository.user_preferences(session, current_user.id)

    def save_preferences(self, current_user: CurrentUser, preferences: dict[str, list[str]]) -> dict[str, list[str]]:
        with session_scope() as session:
            return self.repository.replace_user_preferences(session, current_user.id, preferences)

    def list_notices(
        self,
        source: str | None = None,
        document_type: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        with session_scope() as session:
            notices = self.repository.list_notices(
                session,
                source=source,
                document_type=document_type,
                query=query,
                limit=limit,
                offset=offset,
            )
            return [serialize_notice(notice) for notice in notices]

    def notice_meta(self) -> dict:
        with session_scope() as session:
            return {
                "sources": self.repository.notice_sources(session),
                "document_types": self.repository.notice_document_types(session),
                "stats": self.repository.notice_stats(session),
            }

    def _serialize_articles(
        self,
        session,
        articles: list[NewsArticle],
        current_user: CurrentUser | None,
        include_content: bool,
    ) -> list[dict]:
        article_ids = [article.id for article in articles]
        bookmarked_ids = (
            self.repository.bookmarked_article_ids(session, current_user.id, article_ids) if current_user else set()
        )
        return [
            serialize_article(
                article,
                include_content=include_content,
                is_bookmarked=article.id in bookmarked_ids,
            )
            for article in articles
        ]

    def _serialize_story_cluster(
        self,
        session,
        cluster: StoryCluster,
        current_user: CurrentUser | None,
    ) -> dict:
        articles = self.repository.story_cluster_articles(session, cluster.id)
        return {
            "id": cluster.id,
            "title": cluster.title,
            "headline": cluster.title,
            "story_path": story_app_path(cluster),
            "summary": cluster.summary,
            "summary_generated_at": utc_isoformat(cluster.summary_generated_at) if cluster.summary_generated_at else None,
            "primary_category": cluster.primary_category,
            "collected_for_date": cluster.collected_for_date.isoformat(),
            "seed_article_id": cluster.seed_article_id,
            "cluster_fingerprint": cluster.cluster_fingerprint,
            "confidence": round(cluster.confidence_score or 0.0, 4),
            "confidence_score": round(cluster.confidence_score or 0.0, 4),
            "article_count": cluster.article_count,
            "source_count": cluster.source_count,
            "source_names": [name.strip() for name in cluster.source_names.split(",") if name.strip()],
            "thumbnail_path": cluster.thumbnail_path,
            "first_published_at": utc_isoformat(cluster.first_published_at) if cluster.first_published_at else None,
            "latest_published_at": utc_isoformat(cluster.latest_published_at),
            "articles": self._serialize_articles(session, articles, current_user, include_content=False),
        }

    def _trending_cluster_card(
        self,
        session,
        cluster: StoryCluster,
        articles: list[NewsArticle],
        current_user: CurrentUser | None,
    ) -> dict:
        primary = articles[0] if articles else None
        payload = self._serialize_story_cluster(session, cluster, current_user)
        if primary is not None:
            payload["article_path"] = story_app_path(cluster)
            payload["primary_article_path"] = article_app_path(primary)
            payload["article_url"] = primary.article_url
            payload["headline"] = cluster.title
        payload["kind"] = "story"
        payload["trending_score"] = _cluster_trending_score(cluster, articles)
        return payload

    def _trending_article_card(
        self,
        session,
        article: NewsArticle,
        current_user: CurrentUser | None,
    ) -> dict:
        payload = self._serialize_articles(session, [article], current_user, include_content=False)[0]
        payload["kind"] = "article"
        payload["source_count"] = 1
        payload["article_count"] = 1
        payload["trending_score"] = _article_trending_score(article)
        return payload

    def _encode_user_cookie(self, public_id: str) -> str:
        signature = hmac.new(
            self.settings.session_secret.encode("utf-8"),
            public_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{public_id}.{signature}"

    def _decode_user_cookie(self, cookie_value: str | None) -> str | None:
        if not cookie_value or "." not in cookie_value:
            return None
        public_id, signature = cookie_value.rsplit(".", 1)
        expected = self._encode_user_cookie(public_id).rsplit(".", 1)[1]
        if not hmac.compare_digest(signature, expected):
            return None
        return public_id


def serialize_article(
    article: NewsArticle,
    include_content: bool,
    is_bookmarked: bool = False,
) -> dict:
    payload = {
        "id": article.id,
        "headline": article.headline,
        "original_title": article.original_title,
        "description": article.description,
        "source_name": article.source_name,
        "source_country": article.source_country,
        "source_region": article.source_region,
        "source_type": article.source_type,
        "primary_category": article.primary_category or "General",
        "category_confidence": article.category_confidence or 0.0,
        "reading_time_minutes": article.reading_time_minutes or 1,
        "slug": article.slug,
        "thumbnail_path": article.thumbnail_path,
        "article_url": article.article_url,
        "article_path": article_app_path(article),
        "source_url": article.source_url,
        "published_at": utc_isoformat(article.published_at),
        "published_at_display": format_datetime(article.published_at),
        "collected_for_date": article.collected_for_date.isoformat(),
        "summary": article.summary,
        "summary_generated_at": utc_isoformat(article.summary_generated_at) if article.summary_generated_at else None,
        "is_bookmarked": is_bookmarked,
    }
    if include_content:
        payload["content"] = article.content
    return payload


def serialize_notice(notice: GovernmentNotice) -> dict:
    return {
        "id": notice.id,
        "title": notice.title,
        "description": notice.description,
        "content": notice.content,
        "source_name": notice.source_name,
        "source_country": notice.source_country,
        "source_region": notice.source_region,
        "source_type": notice.source_type,
        "document_type": notice.document_type,
        "language": notice.language,
        "thumbnail_path": notice.thumbnail_path or "/static/fallbacks/notice.svg",
        "document_url": notice.document_url,
        "source_url": notice.source_url,
        "published_at": utc_isoformat(notice.published_at),
        "published_at_display": format_datetime(notice.published_at),
        "fetched_at": utc_isoformat(notice.fetched_at),
    }


def article_app_path(article: NewsArticle) -> str:
    slug = article.slug or str(article.id)
    return f"/articles/{article.id}/{slug}"


def story_app_path(cluster: StoryCluster) -> str:
    return f"/stories/{cluster.id}"


SOURCE_TYPE_WEIGHTS = {
    "newspaper": 1.0,
    "business": 0.94,
    "publisher": 0.78,
    "government": 0.72,
}

CATEGORY_IMPORTANCE = {
    "India": 1.0,
    "Politics": 0.98,
    "World": 0.92,
    "Business": 0.88,
    "Health": 0.86,
    "Science": 0.82,
    "Tech": 0.78,
    "Sports": 0.52,
    "General": 0.5,
    "Entertainment": 0.32,
}

LOW_SIGNAL_PATTERNS = (
    r"\blive updates?\b",
    r"\blatest updates?\b",
    r"\blive\b",
    r"\bviral\b",
    r"\bwatch\b",
    r"\bhoroscope\b",
)


def _cluster_trending_score(cluster: StoryCluster, articles: list[NewsArticle]) -> float:
    source_count = max(cluster.source_count, len({article.source_name for article in articles}))
    quality_penalty = _average_quality_penalty(articles, fallback_title=cluster.title)
    score = (
        min(1.0, max(0, source_count - 1) / 3) * 6.0
        + _freshness_score(cluster.latest_published_at) * 3.0
        + _source_credibility(articles) * 2.0
        + CATEGORY_IMPORTANCE.get(cluster.primary_category or "General", 0.5) * 2.0
        + _location_relevance(articles) * 1.0
        - quality_penalty
    )
    return round(score, 4)


def _article_trending_score(article: NewsArticle) -> float:
    score = (
        _freshness_score(article.published_at) * 3.0
        + _source_credibility([article]) * 2.0
        + CATEGORY_IMPORTANCE.get(article.primary_category or "General", 0.5) * 2.0
        + _location_relevance([article]) * 1.0
        - _quality_penalty(article)
    )
    return round(score, 4)


def _freshness_score(published_at: datetime) -> float:
    value = _ensure_aware(published_at)
    age_hours = max(0.0, (datetime.now(timezone.utc) - value).total_seconds() / 3600)
    return max(0.0, 1.0 - (age_hours / 24))


def _source_credibility(articles: list[NewsArticle]) -> float:
    if not articles:
        return 0.0
    return max(SOURCE_TYPE_WEIGHTS.get((article.source_type or "").lower(), 0.65) for article in articles)


def _location_relevance(articles: list[NewsArticle]) -> float:
    if not articles:
        return 0.0
    scores = []
    for article in articles:
        country = (article.source_country or "").lower()
        region = (article.source_region or "").lower()
        if country == "india" or region == "india":
            scores.append(1.0)
        elif region in {"world", "global", "international", "north america", "europe", "middle east"}:
            scores.append(0.82)
        else:
            scores.append(0.6)
    return max(scores)


def _average_quality_penalty(articles: list[NewsArticle], fallback_title: str = "") -> float:
    if not articles:
        return _text_quality_penalty(fallback_title, None, "General")
    return sum(_quality_penalty(article) for article in articles) / len(articles)


def _quality_penalty(article: NewsArticle) -> float:
    return _text_quality_penalty(article.headline or article.original_title, article.description, article.primary_category)


def _text_quality_penalty(title: str | None, description: str | None, category: str | None) -> float:
    penalty = 0.0
    title = title or ""
    description = description or ""
    combined = f"{title} {description}".lower()
    if len(title.split()) < 4:
        penalty += 0.7
    if len(description.split()) < 6:
        penalty += 0.45
    if any(re.search(pattern, combined) for pattern in LOW_SIGNAL_PATTERNS):
        penalty += 0.8
    if (category or "") == "Entertainment":
        penalty += 0.55
    return penalty


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_isoformat(value: datetime) -> str:
    return _ensure_aware(value).isoformat()


def format_datetime(value: datetime) -> str:
    value = _ensure_aware(value).astimezone(ZoneInfo(get_settings().app_timezone))
    months = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    hour = value.hour % 12 or 12
    meridiem = "pm" if value.hour >= 12 else "am"
    return f"{value.day} {months[value.month - 1]} {value.year}, {hour}:{value.minute:02d} {meridiem}"
