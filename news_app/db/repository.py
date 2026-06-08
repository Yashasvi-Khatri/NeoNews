from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
import secrets
from typing import Sequence

from sqlalchemy import desc, distinct, func, or_, select, text
from sqlalchemy.orm import Session

from news_app.db.exceptions import DatabaseOperationError
from news_app.db.models import (
    ArticleEvent,
    ArticleMetric,
    Bookmark,
    GovernmentNotice,
    IngestionCursor,
    NewsArticle,
    ShareLink,
    SourceHealth,
    StoryCluster,
    StoryClusterArticle,
    User,
    UserCategoryScore,
    UserPreference,
)
from news_app.services.article_enrichment import CATEGORIES

EVENT_METRIC_FIELDS = {
    "view": "views",
    "summary_open": "summary_opens",
    "source_open": "source_opens",
    "bookmark": "bookmarks",
    "share": "shares",
}

EVENT_SCORE_WEIGHTS = {
    "view": 0.5,
    "summary_open": 1.25,
    "source_open": 1.0,
    "bookmark": 3.0,
    "unbookmark": -2.0,
    "share": 2.0,
}


@dataclass(frozen=True)
class ArticleCreate:
    url_hash: str
    content_hash: str | None
    article_url: str
    feed_url: str
    source_url: str
    source_name: str
    source_country: str
    source_region: str
    source_type: str
    language: str
    original_title: str
    headline: str
    description: str | None
    content: str | None
    published_at: datetime
    collected_for_date: date
    event_fingerprint: str | None = None
    primary_category: str = "General"
    category_confidence: float = 0.0
    reading_time_minutes: int = 1
    slug: str | None = None
    thumbnail_path: str | None = None
    thumbnail_cached_at: datetime | None = None


@dataclass(frozen=True)
class NoticeCreate:
    url_hash: str
    document_url: str
    feed_url: str
    source_url: str
    source_name: str
    source_country: str
    source_region: str
    source_type: str
    language: str
    document_type: str
    title: str
    description: str | None
    content: str | None
    thumbnail_path: str | None
    published_at: datetime


class ArticleRepository:
    def upsert_many(self, session: Session, articles: Sequence[ArticleCreate]) -> tuple[int, int]:
        try:
            inserted = 0
            updated = 0
            for item in articles:
                existing = session.execute(
                    select(NewsArticle).where(NewsArticle.url_hash == item.url_hash)
                ).scalar_one_or_none()
                if existing is None:
                    session.add(NewsArticle(**item.__dict__))
                    inserted += 1
                    continue

                existing.content_hash = item.content_hash
                existing.article_url = item.article_url
                existing.feed_url = item.feed_url
                existing.source_url = item.source_url
                existing.source_name = item.source_name
                existing.source_country = item.source_country
                existing.source_region = item.source_region
                existing.source_type = item.source_type
                existing.language = item.language
                existing.original_title = item.original_title
                existing.headline = item.headline
                existing.description = item.description
                existing.content = item.content
                existing.event_fingerprint = item.event_fingerprint
                existing.primary_category = item.primary_category
                existing.category_confidence = item.category_confidence
                existing.reading_time_minutes = item.reading_time_minutes
                existing.slug = item.slug
                existing.thumbnail_path = item.thumbnail_path or existing.thumbnail_path
                existing.thumbnail_cached_at = item.thumbnail_cached_at or existing.thumbnail_cached_at
                existing.published_at = item.published_at
                existing.collected_for_date = item.collected_for_date
                existing.fetched_at = datetime.now(timezone.utc)
                updated += 1
            return inserted, updated
        except Exception as exc:
            raise DatabaseOperationError("Failed to upsert articles") from exc

    def get_ingestion_cursor(self, session: Session, key: str) -> IngestionCursor | None:
        try:
            return session.get(IngestionCursor, key)
        except Exception as exc:
            raise DatabaseOperationError("Failed to read ingestion cursor") from exc

    def advance_ingestion_cursor(
        self,
        session: Session,
        key: str,
        window_end: datetime,
        run_id: str | None,
    ) -> IngestionCursor:
        try:
            now = datetime.now(timezone.utc)
            cursor = session.get(IngestionCursor, key)
            if cursor is None:
                cursor = IngestionCursor(
                    key=key,
                    created_at=now,
                    updated_at=now,
                )
                session.add(cursor)
            cursor.last_successful_window_end = _ensure_utc(window_end)
            cursor.last_successful_run_id = run_id
            cursor.last_completed_at = now
            cursor.updated_at = now
            session.flush()
            return cursor
        except Exception as exc:
            raise DatabaseOperationError("Failed to advance ingestion cursor") from exc

    def prune_expired_news(self, session: Session, cutoff: datetime) -> dict[str, int]:
        try:
            cutoff = _ensure_utc(cutoff)
            expired_ids = [
                row[0]
                for row in session.execute(
                    select(NewsArticle.id).where(NewsArticle.published_at < cutoff)
                ).all()
            ]
            if expired_ids:
                session.query(StoryClusterArticle).filter(
                    StoryClusterArticle.article_id.in_(expired_ids)
                ).delete(synchronize_session=False)

            bookmarked_ids = {
                row[0]
                for row in session.execute(
                    select(Bookmark.article_id).where(Bookmark.article_id.in_(expired_ids))
                ).all()
            } if expired_ids else set()
            deletable_ids = [article_id for article_id in expired_ids if article_id not in bookmarked_ids]
            deleted_articles = self._delete_article_ids(session, deletable_ids)
            deleted_clusters = self._delete_non_live_clusters(session, cutoff)
            return {"articles": deleted_articles, "clusters": deleted_clusters}
        except DatabaseOperationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError("Failed to prune expired news") from exc

    def prune_article_if_expired_unbookmarked(
        self,
        session: Session,
        article_id: int,
        cutoff: datetime,
    ) -> bool:
        try:
            article = session.get(NewsArticle, article_id)
            if article is None or _ensure_utc(article.published_at) >= _ensure_utc(cutoff):
                return False
            bookmark_exists = session.execute(
                select(Bookmark.id).where(Bookmark.article_id == article_id).limit(1)
            ).first()
            if bookmark_exists is not None:
                return False
            deleted = self._delete_article_ids(session, [article_id])
            self._delete_non_live_clusters(session, _ensure_utc(cutoff))
            return deleted > 0
        except DatabaseOperationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError("Failed to prune unbookmarked expired article") from exc

    def _delete_article_ids(self, session: Session, article_ids: Sequence[int]) -> int:
        ids = list(dict.fromkeys(article_ids))
        if not ids:
            return 0
        try:
            session.query(StoryClusterArticle).filter(
                StoryClusterArticle.article_id.in_(ids)
            ).delete(synchronize_session=False)
            session.query(ShareLink).filter(ShareLink.article_id.in_(ids)).delete(synchronize_session=False)
            session.query(ArticleMetric).filter(ArticleMetric.article_id.in_(ids)).delete(synchronize_session=False)
            session.query(ArticleEvent).filter(ArticleEvent.article_id.in_(ids)).delete(synchronize_session=False)
            session.query(Bookmark).filter(Bookmark.article_id.in_(ids)).delete(synchronize_session=False)
            return session.query(NewsArticle).filter(NewsArticle.id.in_(ids)).delete(synchronize_session=False)
        except Exception as exc:
            raise DatabaseOperationError("Failed to delete expired articles") from exc

    def _delete_non_live_clusters(self, session: Session, cutoff: datetime) -> int:
        try:
            old_ids = [
                row[0]
                for row in session.execute(
                    select(StoryCluster.id).where(StoryCluster.latest_published_at < cutoff)
                ).all()
            ]
            if old_ids:
                session.query(StoryClusterArticle).filter(
                    StoryClusterArticle.cluster_id.in_(old_ids)
                ).delete(synchronize_session=False)
            old_count = session.query(StoryCluster).filter(StoryCluster.id.in_(old_ids)).delete(
                synchronize_session=False
            ) if old_ids else 0
            linked_cluster_ids = select(StoryClusterArticle.cluster_id)
            orphan_count = session.query(StoryCluster).filter(
                StoryCluster.id.not_in(linked_cluster_ids)
            ).delete(synchronize_session=False)
            return int(old_count or 0) + int(orphan_count or 0)
        except Exception as exc:
            raise DatabaseOperationError("Failed to delete expired story clusters") from exc

    def list_articles(
        self,
        session: Session,
        target_date: date | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
        source: str | None = None,
        region: str | None = None,
        query: str | None = None,
        category: str | None = None,
        sort: str = "date",
        limit: int = 500,
        offset: int = 0,
        user_id: int | None = None,
        view: str | None = None,
        exclude_article_ids: set[int] | None = None,
        exclude_clustered: bool = False,
    ) -> list[NewsArticle]:
        try:
            fts_query = _build_fts_query(query)
            if fts_query:
                return self._list_articles_fts(
                    session=session,
                    target_date=target_date,
                    published_after=published_after,
                    published_before=published_before,
                    source=source,
                    region=region,
                    category=category,
                    sort=sort,
                    fts_query=fts_query,
                    limit=limit,
                    offset=offset,
                    user_id=user_id,
                    view=view,
                    exclude_article_ids=exclude_article_ids,
                    exclude_clustered=exclude_clustered,
                )

            stmt = select(NewsArticle)
            stmt = self._apply_article_filters(
                stmt,
                target_date=target_date,
                published_after=published_after,
                published_before=published_before,
                source=source,
                region=region,
                query=query,
                category=category,
                user_id=user_id,
                view=view,
                exclude_article_ids=exclude_article_ids,
                exclude_clustered=exclude_clustered,
            )
            stmt = self._apply_sort(stmt, sort)
            stmt = stmt.limit(max(1, min(limit, 500))).offset(max(0, offset))
            return list(session.execute(stmt).scalars())
        except DatabaseOperationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError("Failed to list articles") from exc

    def _apply_article_filters(
        self,
        stmt,
        *,
        target_date: date | None,
        published_after: datetime | None,
        published_before: datetime | None,
        source: str | None,
        region: str | None,
        query: str | None,
        category: str | None,
        user_id: int | None,
        view: str | None,
        exclude_article_ids: set[int] | None,
        exclude_clustered: bool,
    ):
        if view == "read_later":
            if user_id is None:
                stmt = stmt.where(False)
            else:
                stmt = stmt.join(Bookmark, Bookmark.article_id == NewsArticle.id).where(Bookmark.user_id == user_id)
        if target_date and view != "read_later":
            stmt = stmt.where(NewsArticle.collected_for_date == target_date)
        if published_after and view != "read_later":
            stmt = stmt.where(NewsArticle.published_at >= published_after)
        if published_before and view != "read_later":
            stmt = stmt.where(NewsArticle.published_at <= published_before)
        if source:
            stmt = stmt.where(NewsArticle.source_name == source)
        if region:
            stmt = stmt.where(NewsArticle.source_region == region)
        if category:
            stmt = stmt.where(NewsArticle.primary_category == category)
        if query:
            needle = f"%{query.strip()}%"
            stmt = stmt.where(
                or_(
                    NewsArticle.headline.ilike(needle),
                    NewsArticle.original_title.ilike(needle),
                    NewsArticle.description.ilike(needle),
                    NewsArticle.content.ilike(needle),
                    NewsArticle.source_name.ilike(needle),
                    NewsArticle.primary_category.ilike(needle),
                )
            )
        if exclude_article_ids:
            stmt = stmt.where(NewsArticle.id.not_in(exclude_article_ids))
        if exclude_clustered and view != "read_later":
            clustered_ids = (
                select(StoryClusterArticle.article_id)
                .join(StoryCluster, StoryCluster.id == StoryClusterArticle.cluster_id)
                .where(StoryCluster.source_count >= 2)
                .where(StoryCluster.confidence_score >= 0.90)
            )
            if target_date:
                clustered_ids = clustered_ids.where(StoryCluster.collected_for_date == target_date)
            if published_after:
                clustered_ids = clustered_ids.where(StoryCluster.latest_published_at >= published_after)
            if published_before:
                clustered_ids = clustered_ids.where(StoryCluster.latest_published_at <= published_before)
            stmt = stmt.where(NewsArticle.id.not_in(clustered_ids))
        return stmt

    def _apply_sort(self, stmt, sort: str):
        return stmt.order_by(desc(NewsArticle.published_at))

    def _list_articles_fts(
        self,
        *,
        session: Session,
        target_date: date | None,
        published_after: datetime | None,
        published_before: datetime | None,
        source: str | None,
        region: str | None,
        category: str | None,
        sort: str,
        fts_query: str,
        limit: int,
        offset: int,
        user_id: int | None,
        view: str | None,
        exclude_article_ids: set[int] | None,
        exclude_clustered: bool,
    ) -> list[NewsArticle]:
        joins = [
            "JOIN news_articles_fts ON news_articles_fts.rowid = a.id",
        ]
        where = ["news_articles_fts MATCH :fts_query"]
        params: dict[str, object] = {
            "fts_query": fts_query,
            "limit": max(1, min(limit, 500)),
            "offset": max(0, offset),
        }
        if view == "read_later":
            if user_id is None:
                return []
            joins.append("JOIN bookmarks b ON b.article_id = a.id AND b.user_id = :user_id")
            params["user_id"] = user_id
        elif target_date:
            where.append("a.collected_for_date = :target_date")
            params["target_date"] = target_date
        if view != "read_later" and published_after:
            where.append("a.published_at >= :published_after")
            params["published_after"] = published_after
        if view != "read_later" and published_before:
            where.append("a.published_at <= :published_before")
            params["published_before"] = published_before
        if source:
            where.append("a.source_name = :source")
            params["source"] = source
        if region:
            where.append("a.source_region = :region")
            params["region"] = region
        if category:
            where.append("a.primary_category = :category")
            params["category"] = category
        if exclude_article_ids:
            placeholders = []
            for index, article_id in enumerate(exclude_article_ids):
                key = f"excluded_{index}"
                placeholders.append(f":{key}")
                params[key] = article_id
            where.append(f"a.id NOT IN ({', '.join(placeholders)})")
        if exclude_clustered and view != "read_later":
            cluster_filters = ["sc.source_count >= 2", "sc.confidence_score >= 0.90"]
            if target_date:
                cluster_filters.append("sc.collected_for_date = :clustered_date")
                params["clustered_date"] = target_date
            if published_after:
                cluster_filters.append("sc.latest_published_at >= :clustered_after")
                params["clustered_after"] = published_after
            if published_before:
                cluster_filters.append("sc.latest_published_at <= :clustered_before")
                params["clustered_before"] = published_before
            where.append(
                f"""
                a.id NOT IN (
                    SELECT sca.article_id
                    FROM story_cluster_articles sca
                    JOIN story_clusters sc ON sc.id = sca.cluster_id
                    WHERE {' AND '.join(cluster_filters)}
                )
                """
            )

        if sort == "date":
            order_by = "a.published_at DESC"
        else:
            order_by = "bm25(news_articles_fts), a.published_at DESC"

        sql = f"""
            SELECT a.id
            FROM news_articles a
            {' '.join(joins)}
            WHERE {' AND '.join(where)}
            ORDER BY {order_by}
            LIMIT :limit OFFSET :offset
        """
        ids = [row[0] for row in session.execute(text(sql), params).all()]
        if not ids:
            return []
        articles = list(session.execute(select(NewsArticle).where(NewsArticle.id.in_(ids))).scalars())
        article_by_id = {article.id: article for article in articles}
        return [article_by_id[article_id] for article_id in ids if article_id in article_by_id]

    def get_article(self, session: Session, article_id: int) -> NewsArticle | None:
        try:
            return session.get(NewsArticle, article_id)
        except Exception as exc:
            raise DatabaseOperationError("Failed to get article") from exc

    def save_summary(self, session: Session, article_id: int, summary: str) -> NewsArticle | None:
        try:
            article = session.get(NewsArticle, article_id)
            if article is None:
                return None
            article.summary = summary
            article.summary_generated_at = datetime.now(timezone.utc)
            return article
        except Exception as exc:
            raise DatabaseOperationError("Failed to save article summary") from exc

    def upsert_notices(self, session: Session, notices: Sequence[NoticeCreate]) -> tuple[int, int]:
        try:
            inserted = 0
            updated = 0
            now = datetime.now(timezone.utc)
            deduped = {item.url_hash: item for item in notices}
            for item in deduped.values():
                existing = session.execute(
                    select(GovernmentNotice).where(GovernmentNotice.url_hash == item.url_hash)
                ).scalar_one_or_none()
                if existing is None:
                    session.add(
                        GovernmentNotice(
                            **item.__dict__,
                            fetched_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    inserted += 1
                    continue

                existing.document_url = item.document_url
                existing.feed_url = item.feed_url
                existing.source_url = item.source_url
                existing.source_name = item.source_name
                existing.source_country = item.source_country
                existing.source_region = item.source_region
                existing.source_type = item.source_type
                existing.language = item.language
                existing.document_type = item.document_type
                existing.title = item.title
                existing.description = item.description
                existing.content = item.content
                existing.thumbnail_path = item.thumbnail_path or existing.thumbnail_path
                existing.published_at = item.published_at
                existing.fetched_at = now
                existing.updated_at = now
                updated += 1
            return inserted, updated
        except Exception as exc:
            raise DatabaseOperationError("Failed to upsert government notices") from exc

    def list_notices(
        self,
        session: Session,
        source: str | None = None,
        document_type: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[GovernmentNotice]:
        try:
            stmt = select(GovernmentNotice)
            if source:
                stmt = stmt.where(GovernmentNotice.source_name == source)
            if document_type:
                stmt = stmt.where(GovernmentNotice.document_type == document_type)
            if query:
                needle = f"%{query.strip()}%"
                stmt = stmt.where(
                    or_(
                        GovernmentNotice.title.ilike(needle),
                        GovernmentNotice.description.ilike(needle),
                        GovernmentNotice.content.ilike(needle),
                        GovernmentNotice.source_name.ilike(needle),
                        GovernmentNotice.document_type.ilike(needle),
                    )
                )
            stmt = stmt.order_by(desc(GovernmentNotice.published_at)).limit(max(1, min(limit, 200))).offset(max(0, offset))
            return list(session.execute(stmt).scalars())
        except Exception as exc:
            raise DatabaseOperationError("Failed to list government notices") from exc

    def notice_sources(self, session: Session) -> list[str]:
        try:
            stmt = select(distinct(GovernmentNotice.source_name)).order_by(GovernmentNotice.source_name.asc())
            return [row[0] for row in session.execute(stmt).all()]
        except Exception as exc:
            raise DatabaseOperationError("Failed to list government notice sources") from exc

    def notice_document_types(self, session: Session) -> list[str]:
        try:
            stmt = select(distinct(GovernmentNotice.document_type)).order_by(GovernmentNotice.document_type.asc())
            return [row[0] for row in session.execute(stmt).all()]
        except Exception as exc:
            raise DatabaseOperationError("Failed to list government notice types") from exc

    def notice_stats(self, session: Session) -> dict[str, int]:
        try:
            total = session.execute(select(func.count()).select_from(GovernmentNotice)).scalar_one()
            source_count = session.execute(select(func.count(distinct(GovernmentNotice.source_name)))).scalar_one()
            return {"total": int(total), "sources": int(source_count)}
        except Exception as exc:
            raise DatabaseOperationError("Failed to read government notice stats") from exc

    def available_dates(self, session: Session) -> list[date]:
        try:
            stmt = select(distinct(NewsArticle.collected_for_date)).order_by(desc(NewsArticle.collected_for_date))
            return [row[0] for row in session.execute(stmt).all()]
        except Exception as exc:
            raise DatabaseOperationError("Failed to list available dates") from exc

    def source_names(self, session: Session) -> list[str]:
        try:
            stmt = select(distinct(NewsArticle.source_name)).order_by(NewsArticle.source_name.asc())
            return [row[0] for row in session.execute(stmt).all()]
        except Exception as exc:
            raise DatabaseOperationError("Failed to list sources") from exc

    def regions(self, session: Session) -> list[str]:
        try:
            stmt = select(distinct(NewsArticle.source_region)).order_by(NewsArticle.source_region.asc())
            return [row[0] for row in session.execute(stmt).all()]
        except Exception as exc:
            raise DatabaseOperationError("Failed to list regions") from exc

    def countries(self, session: Session) -> list[str]:
        try:
            stmt = select(distinct(NewsArticle.source_country)).order_by(NewsArticle.source_country.asc())
            return [row[0] for row in session.execute(stmt).all()]
        except Exception as exc:
            raise DatabaseOperationError("Failed to list countries") from exc

    def languages(self, session: Session) -> list[str]:
        try:
            stmt = select(distinct(NewsArticle.language)).order_by(NewsArticle.language.asc())
            return [row[0] for row in session.execute(stmt).all()]
        except Exception as exc:
            raise DatabaseOperationError("Failed to list languages") from exc

    def categories(
        self,
        session: Session,
        target_date: date | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
    ) -> list[dict[str, int | str]]:
        try:
            stmt = select(NewsArticle.primary_category, func.count()).group_by(NewsArticle.primary_category)
            if target_date:
                stmt = stmt.where(NewsArticle.collected_for_date == target_date)
            if published_after:
                stmt = stmt.where(NewsArticle.published_at >= published_after)
            if published_before:
                stmt = stmt.where(NewsArticle.published_at <= published_before)
            counts = {row[0] or "General": row[1] for row in session.execute(stmt).all()}
            return [{"name": category, "count": int(counts.get(category, 0))} for category in CATEGORIES]
        except Exception as exc:
            raise DatabaseOperationError("Failed to list categories") from exc

    def stats_for_window(
        self,
        session: Session,
        published_after: datetime,
        published_before: datetime | None = None,
    ) -> dict[str, int]:
        try:
            article_filters = [NewsArticle.published_at >= published_after]
            if published_before:
                article_filters.append(NewsArticle.published_at <= published_before)
            total = session.execute(select(func.count()).select_from(NewsArticle).where(*article_filters)).scalar_one()
            summarized = session.execute(
                select(func.count())
                .select_from(NewsArticle)
                .where(*article_filters, NewsArticle.summary.is_not(None))
            ).scalar_one()
            bookmarked = session.execute(select(func.count()).select_from(Bookmark)).scalar_one()
            return {"total": total, "summarized": summarized, "bookmarked": bookmarked}
        except Exception as exc:
            raise DatabaseOperationError("Failed to read rolling article statistics") from exc

    def stats_for_date(self, session: Session, target_date: date) -> dict[str, int]:
        try:
            total = session.execute(
                select(func.count()).select_from(NewsArticle).where(NewsArticle.collected_for_date == target_date)
            ).scalar_one()
            summarized = session.execute(
                select(func.count())
                .select_from(NewsArticle)
                .where(NewsArticle.collected_for_date == target_date, NewsArticle.summary.is_not(None))
            ).scalar_one()
            bookmarked = session.execute(select(func.count()).select_from(Bookmark)).scalar_one()
            return {"total": total, "summarized": summarized, "bookmarked": bookmarked}
        except Exception as exc:
            raise DatabaseOperationError("Failed to read article statistics") from exc

    def get_user_by_public_id(self, session: Session, public_id: str) -> User | None:
        try:
            return session.execute(select(User).where(User.public_id == public_id)).scalar_one_or_none()
        except Exception as exc:
            raise DatabaseOperationError("Failed to get user") from exc

    def create_user(self, session: Session, public_id: str) -> User:
        try:
            user = User(public_id=public_id, is_anonymous=True)
            session.add(user)
            session.flush()
            return user
        except Exception as exc:
            raise DatabaseOperationError("Failed to create user") from exc

    def touch_user(self, user: User) -> None:
        user.last_seen_at = datetime.now(timezone.utc)

    def bookmark_article(self, session: Session, user_id: int, article_id: int) -> tuple[Bookmark, bool]:
        try:
            existing = session.execute(
                select(Bookmark).where(Bookmark.user_id == user_id, Bookmark.article_id == article_id)
            ).scalar_one_or_none()
            if existing is not None:
                return existing, False
            bookmark = Bookmark(user_id=user_id, article_id=article_id)
            session.add(bookmark)
            session.flush()
            return bookmark, True
        except Exception as exc:
            raise DatabaseOperationError("Failed to bookmark article") from exc

    def remove_bookmark(self, session: Session, user_id: int, article_id: int) -> bool:
        try:
            bookmark = session.execute(
                select(Bookmark).where(Bookmark.user_id == user_id, Bookmark.article_id == article_id)
            ).scalar_one_or_none()
            if bookmark is None:
                return False
            session.delete(bookmark)
            return True
        except Exception as exc:
            raise DatabaseOperationError("Failed to remove bookmark") from exc

    def bookmarked_article_ids(self, session: Session, user_id: int, article_ids: Sequence[int]) -> set[int]:
        if not article_ids:
            return set()
        try:
            stmt = select(Bookmark.article_id).where(Bookmark.user_id == user_id, Bookmark.article_id.in_(article_ids))
            return {row[0] for row in session.execute(stmt).all()}
        except Exception as exc:
            raise DatabaseOperationError("Failed to list bookmark state") from exc

    def record_event(
        self,
        session: Session,
        user_id: int | None,
        article_id: int | None,
        event_type: str,
        event_value: str | None = None,
    ) -> ArticleEvent:
        try:
            event = ArticleEvent(
                user_id=user_id,
                article_id=article_id,
                event_type=event_type,
                event_value=event_value,
            )
            session.add(event)
            if article_id is not None:
                self._update_metric(session, article_id, event_type)
                self._update_user_category_score(session, user_id, article_id, event_type)
            return event
        except Exception as exc:
            raise DatabaseOperationError("Failed to record article event") from exc

    def _update_metric(self, session: Session, article_id: int, event_type: str) -> None:
        metric = session.get(ArticleMetric, article_id)
        if metric is None:
            metric = ArticleMetric(article_id=article_id)
            session.add(metric)
            session.flush()

        field = EVENT_METRIC_FIELDS.get(event_type)
        if field:
            setattr(metric, field, getattr(metric, field) + 1)
        elif event_type == "unbookmark":
            metric.bookmarks = max(0, metric.bookmarks - 1)

        now = datetime.now(timezone.utc)
        metric.popularity_score = (
            float(metric.views)
            + (float(metric.summary_opens) * 1.25)
            + (float(metric.source_opens) * 1.0)
            + (float(metric.bookmarks) * 3.0)
            + (float(metric.shares) * 2.0)
        )
        metric.last_event_at = now
        metric.updated_at = now

    def _update_user_category_score(
        self,
        session: Session,
        user_id: int | None,
        article_id: int,
        event_type: str,
    ) -> None:
        if user_id is None:
            return
        weight = EVENT_SCORE_WEIGHTS.get(event_type, 0.0)
        if weight == 0:
            return
        article = session.get(NewsArticle, article_id)
        if article is None:
            return
        category = article.primary_category or "General"
        score = session.execute(
            select(UserCategoryScore).where(
                UserCategoryScore.user_id == user_id,
                UserCategoryScore.category == category,
            )
        ).scalar_one_or_none()
        if score is None:
            score = UserCategoryScore(user_id=user_id, category=category, score=0.0)
            session.add(score)
        score.score = max(0.0, score.score + weight)
        score.updated_at = datetime.now(timezone.utc)

    def user_category_scores(self, session: Session, user_id: int) -> dict[str, float]:
        try:
            rows = session.execute(select(UserCategoryScore).where(UserCategoryScore.user_id == user_id)).scalars()
            return {row.category: row.score for row in rows}
        except Exception as exc:
            raise DatabaseOperationError("Failed to read user category scores") from exc

    def create_share_link(
        self,
        session: Session,
        article_id: int,
        user_id: int | None,
        platform: str | None = None,
    ) -> ShareLink:
        try:
            for _ in range(5):
                token = secrets.token_urlsafe(10).replace("-", "").replace("_", "")
                existing = session.execute(select(ShareLink).where(ShareLink.token == token)).scalar_one_or_none()
                if existing is None:
                    link = ShareLink(article_id=article_id, user_id=user_id, platform=platform, token=token)
                    session.add(link)
                    session.flush()
                    return link
            raise DatabaseOperationError("Failed to generate unique share token")
        except DatabaseOperationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError("Failed to create share link") from exc

    def get_share_link(self, session: Session, token: str) -> ShareLink | None:
        try:
            return session.execute(select(ShareLink).where(ShareLink.token == token)).scalar_one_or_none()
        except Exception as exc:
            raise DatabaseOperationError("Failed to get share link") from exc

    def record_share_click(self, session: Session, token: str) -> ShareLink | None:
        try:
            link = self.get_share_link(session, token)
            if link is None:
                return None
            link.click_count += 1
            link.last_clicked_at = datetime.now(timezone.utc)
            return link
        except Exception as exc:
            raise DatabaseOperationError("Failed to record share click") from exc

    def offline_top_articles(self, session: Session, limit: int) -> list[NewsArticle]:
        try:
            stmt = (
                select(NewsArticle)
                .order_by(desc(NewsArticle.published_at))
                .limit(max(1, min(limit, 100)))
            )
            return list(session.execute(stmt).scalars())
        except Exception as exc:
            raise DatabaseOperationError("Failed to list offline articles") from exc

    def popular_articles(
        self,
        session: Session,
        limit: int = 5,
        metric: str = "overall",
        target_date: date | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
    ) -> list[NewsArticle]:
        try:
            metric_score = {
                "views": func.coalesce(ArticleMetric.views, 0),
                "bookmarks": func.coalesce(ArticleMetric.bookmarks, 0),
                "shares": func.coalesce(ArticleMetric.shares, 0),
            }.get(
                metric,
                func.coalesce(ArticleMetric.views, 0)
                + (func.coalesce(ArticleMetric.bookmarks, 0) * 3)
                + (func.coalesce(ArticleMetric.shares, 0) * 2),
            )
            stmt = (
                select(NewsArticle)
                .join(ArticleMetric, ArticleMetric.article_id == NewsArticle.id)
                .where(metric_score > 0)
            )
            if target_date is not None:
                stmt = stmt.where(NewsArticle.collected_for_date == target_date)
            if published_after is not None:
                stmt = stmt.where(NewsArticle.published_at >= published_after)
            if published_before is not None:
                stmt = stmt.where(NewsArticle.published_at <= published_before)
            stmt = stmt.order_by(desc(metric_score), desc(NewsArticle.published_at)).limit(max(1, min(limit, 20)))
            return list(session.execute(stmt).scalars())
        except Exception as exc:
            raise DatabaseOperationError("Failed to list popular articles") from exc

    def article_ids_by_url_hashes(self, session: Session, url_hashes: Sequence[str]) -> dict[str, int]:
        if not url_hashes:
            return {}
        try:
            rows = session.execute(
                select(NewsArticle.url_hash, NewsArticle.id).where(NewsArticle.url_hash.in_(url_hashes))
            ).all()
            return {row[0]: row[1] for row in rows}
        except Exception as exc:
            raise DatabaseOperationError("Failed to read article IDs by URL hash") from exc

    def popularity_by_url_hashes(self, session: Session, url_hashes: Sequence[str]) -> dict[str, float]:
        if not url_hashes:
            return {}
        try:
            rows = session.execute(
                select(NewsArticle.url_hash, ArticleMetric.popularity_score)
                .join(ArticleMetric, ArticleMetric.article_id == NewsArticle.id)
                .where(NewsArticle.url_hash.in_(url_hashes))
            ).all()
            return {row[0]: float(row[1] or 0.0) for row in rows}
        except Exception as exc:
            raise DatabaseOperationError("Failed to read popularity by URL hash") from exc

    def related_articles(self, session: Session, article: NewsArticle, limit: int = 6) -> list[NewsArticle]:
        try:
            stmt = (
                select(NewsArticle)
                .where(
                    NewsArticle.id != article.id,
                    or_(
                        NewsArticle.primary_category == article.primary_category,
                        NewsArticle.source_name == article.source_name,
                        NewsArticle.source_region == article.source_region,
                    ),
                )
                .order_by(
                    desc(NewsArticle.primary_category == article.primary_category),
                    desc(NewsArticle.source_name == article.source_name),
                    desc(NewsArticle.published_at),
                )
                .limit(max(1, min(limit, 12)))
            )
            return list(session.execute(stmt).scalars())
        except Exception as exc:
            raise DatabaseOperationError("Failed to list related articles") from exc

    def user_preferences(self, session: Session, user_id: int) -> dict[str, list[str]]:
        try:
            rows = session.execute(select(UserPreference).where(UserPreference.user_id == user_id)).scalars()
            preferences = {"categories": [], "sources": [], "regions": []}
            type_to_key = {"category": "categories", "source": "sources", "region": "regions"}
            for row in rows:
                key = type_to_key.get(row.preference_type)
                if key in preferences:
                    preferences[key].append(row.value)
            return {key: sorted(values) for key, values in preferences.items()}
        except Exception as exc:
            raise DatabaseOperationError("Failed to read user preferences") from exc

    def replace_user_preferences(self, session: Session, user_id: int, preferences: dict[str, list[str]]) -> dict[str, list[str]]:
        try:
            session.query(UserPreference).filter(UserPreference.user_id == user_id).delete()
            for preference_type, key in (("category", "categories"), ("source", "sources"), ("region", "regions")):
                seen = set()
                for value in preferences.get(key, []):
                    cleaned = str(value).strip()
                    if not cleaned or cleaned in seen:
                        continue
                    seen.add(cleaned)
                    session.add(UserPreference(user_id=user_id, preference_type=preference_type, value=cleaned))
            session.flush()
            return self.user_preferences(session, user_id)
        except Exception as exc:
            raise DatabaseOperationError("Failed to save user preferences") from exc

    def record_source_health(
        self,
        session: Session,
        source_name: str,
        feed_url: str,
        *,
        success: bool,
        items_seen: int = 0,
        failure_reason: str | None = None,
    ) -> SourceHealth:
        try:
            health = session.execute(
                select(SourceHealth).where(SourceHealth.source_name == source_name)
            ).scalar_one_or_none()
            if health is None:
                health = SourceHealth(source_name=source_name, feed_url=feed_url)
                session.add(health)
                session.flush()
            now = datetime.now(timezone.utc)
            health.feed_url = feed_url
            health.last_checked_at = now
            health.last_status = "success" if success else "failed"
            health.items_seen = items_seen
            health.failure_reason = None if success else failure_reason or "No valid feed items returned"
            if success:
                health.last_success_at = now
            else:
                health.last_failure_at = now
            health.updated_at = now
            return health
        except Exception as exc:
            raise DatabaseOperationError("Failed to record source health") from exc

    def source_health(self, session: Session) -> list[SourceHealth]:
        try:
            stmt = select(SourceHealth).order_by(SourceHealth.source_name.asc())
            return list(session.execute(stmt).scalars())
        except Exception as exc:
            raise DatabaseOperationError("Failed to list source health") from exc

    def articles_for_story_clustering(
        self,
        session: Session,
        target_date: date | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
    ) -> list[NewsArticle]:
        try:
            stmt = select(NewsArticle)
            if target_date:
                stmt = stmt.where(NewsArticle.collected_for_date == target_date)
            if published_after:
                stmt = stmt.where(NewsArticle.published_at >= published_after)
            if published_before:
                stmt = stmt.where(NewsArticle.published_at <= published_before)
            stmt = stmt.order_by(NewsArticle.collected_for_date.asc(), NewsArticle.primary_category.asc(), desc(NewsArticle.published_at))
            return list(session.execute(stmt).scalars())
        except Exception as exc:
            raise DatabaseOperationError("Failed to list articles for story clustering") from exc

    def replace_story_clusters(self, session: Session, target_date: date, clusters: list[dict]) -> int:
        try:
            incoming_article_ids = [
                article.id
                for cluster_data in clusters
                for article in cluster_data["articles"]
                if article.id is not None
            ]
            existing_ids = [
                row[0]
                for row in session.execute(
                    select(StoryCluster.id).where(StoryCluster.collected_for_date == target_date)
                ).all()
            ]
            if existing_ids:
                session.query(StoryClusterArticle).filter(StoryClusterArticle.cluster_id.in_(existing_ids)).delete()
                session.query(StoryCluster).filter(StoryCluster.id.in_(existing_ids)).delete()
            if incoming_article_ids:
                session.query(StoryClusterArticle).filter(
                    StoryClusterArticle.article_id.in_(incoming_article_ids)
                ).delete()

            for cluster_data in clusters:
                articles = cluster_data["articles"]
                if not articles:
                    continue
                cluster = StoryCluster(
                    cluster_key=cluster_data["cluster_key"],
                    title=cluster_data["title"],
                    primary_category=cluster_data["primary_category"],
                    collected_for_date=target_date,
                    seed_article_id=cluster_data.get("seed_article_id"),
                    cluster_fingerprint=cluster_data.get("cluster_fingerprint"),
                    confidence_score=cluster_data.get("confidence_score", 1.0),
                    article_count=len(articles),
                    source_count=len({article.source_name for article in articles}),
                    source_names=", ".join(sorted({article.source_name for article in articles})),
                    thumbnail_path=cluster_data.get("thumbnail_path"),
                    first_published_at=cluster_data.get("first_published_at") or min(article.published_at for article in articles),
                    latest_published_at=max(article.published_at for article in articles),
                )
                session.add(cluster)
                session.flush()
                primary_id = articles[0].id
                for article in articles:
                    session.add(
                        StoryClusterArticle(
                            cluster_id=cluster.id,
                            article_id=article.id,
                            is_primary=article.id == primary_id,
                        )
                    )
            return len(clusters)
        except Exception as exc:
            raise DatabaseOperationError("Failed to replace story clusters") from exc

    def replace_live_story_clusters(self, session: Session, target_date: date, clusters: list[dict]) -> int:
        try:
            session.query(StoryClusterArticle).delete(synchronize_session=False)
            session.query(StoryCluster).delete(synchronize_session=False)

            for cluster_data in clusters:
                articles = cluster_data["articles"]
                if not articles:
                    continue
                cluster = StoryCluster(
                    cluster_key=cluster_data["cluster_key"],
                    title=cluster_data["title"],
                    primary_category=cluster_data["primary_category"],
                    collected_for_date=target_date,
                    seed_article_id=cluster_data.get("seed_article_id"),
                    cluster_fingerprint=cluster_data.get("cluster_fingerprint"),
                    confidence_score=cluster_data.get("confidence_score", 1.0),
                    article_count=len(articles),
                    source_count=len({article.source_name for article in articles}),
                    source_names=", ".join(sorted({article.source_name for article in articles})),
                    thumbnail_path=cluster_data.get("thumbnail_path"),
                    first_published_at=cluster_data.get("first_published_at") or min(article.published_at for article in articles),
                    latest_published_at=max(article.published_at for article in articles),
                )
                session.add(cluster)
                session.flush()
                primary_id = articles[0].id
                for article in articles:
                    session.add(
                        StoryClusterArticle(
                            cluster_id=cluster.id,
                            article_id=article.id,
                            is_primary=article.id == primary_id,
                        )
                    )
            return len(clusters)
        except Exception as exc:
            raise DatabaseOperationError("Failed to replace live story clusters") from exc

    def list_story_clusters(
        self,
        session: Session,
        target_date: date | None = None,
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
    ) -> list[StoryCluster]:
        try:
            stmt = select(StoryCluster)
            if source or region:
                stmt = stmt.join(StoryClusterArticle, StoryClusterArticle.cluster_id == StoryCluster.id).join(
                    NewsArticle, NewsArticle.id == StoryClusterArticle.article_id
                )
            if target_date:
                stmt = stmt.where(StoryCluster.collected_for_date == target_date)
            if published_after:
                stmt = stmt.where(StoryCluster.latest_published_at >= published_after)
            if published_before:
                stmt = stmt.where(StoryCluster.latest_published_at <= published_before)
            if min_source_count:
                stmt = stmt.where(StoryCluster.source_count >= min_source_count)
            if min_confidence is not None:
                stmt = stmt.where(StoryCluster.confidence_score >= min_confidence)
            if category:
                stmt = stmt.where(StoryCluster.primary_category == category)
            if source:
                stmt = stmt.where(NewsArticle.source_name == source)
            if region:
                stmt = stmt.where(NewsArticle.source_region == region)
            if query:
                needle = f"%{query.strip()}%"
                stmt = stmt.where(or_(StoryCluster.title.ilike(needle), StoryCluster.summary.ilike(needle)))
            stmt = stmt.distinct().order_by(desc(StoryCluster.latest_published_at)).limit(max(1, min(limit, 100))).offset(max(0, offset))
            return list(session.execute(stmt).scalars())
        except Exception as exc:
            raise DatabaseOperationError("Failed to list story clusters") from exc

    def get_story_cluster(self, session: Session, cluster_id: int) -> StoryCluster | None:
        try:
            return session.get(StoryCluster, cluster_id)
        except Exception as exc:
            raise DatabaseOperationError("Failed to get story cluster") from exc

    def story_cluster_articles(self, session: Session, cluster_id: int) -> list[NewsArticle]:
        try:
            stmt = (
                select(NewsArticle)
                .join(StoryClusterArticle, StoryClusterArticle.article_id == NewsArticle.id)
                .where(StoryClusterArticle.cluster_id == cluster_id)
                .order_by(desc(StoryClusterArticle.is_primary), desc(NewsArticle.published_at))
            )
            return list(session.execute(stmt).scalars())
        except Exception as exc:
            raise DatabaseOperationError("Failed to list story cluster articles") from exc

    def recently_viewed_articles(self, session: Session, user_id: int, limit: int = 5) -> list[NewsArticle]:
        try:
            last_viewed_at = func.max(ArticleEvent.created_at).label("last_viewed_at")
            rows = (
                session.execute(
                    select(NewsArticle, last_viewed_at)
                    .join(ArticleEvent, ArticleEvent.article_id == NewsArticle.id)
                    .where(ArticleEvent.user_id == user_id, ArticleEvent.event_type == "view")
                    .group_by(NewsArticle.id)
                    .order_by(desc(last_viewed_at))
                    .limit(max(1, min(limit, 20)))
                )
                .all()
            )
            return [row[0] for row in rows]
        except Exception as exc:
            raise DatabaseOperationError("Failed to list recently viewed articles") from exc

    def recently_viewed_cards(self, session: Session, user_id: int, limit: int = 20) -> list[tuple[str, NewsArticle | StoryCluster]]:
        try:
            requested_limit = max(1, min(limit, 20))
            events = (
                session.execute(
                    select(ArticleEvent)
                    .where(ArticleEvent.user_id == user_id, ArticleEvent.event_type == "view")
                    .order_by(desc(ArticleEvent.created_at))
                    .limit(500)
                )
                .scalars()
                .all()
            )

            ordered_keys: list[tuple[str, int]] = []
            seen: set[tuple[str, int]] = set()
            for event in events:
                key: tuple[str, int] | None = None
                if event.event_value and event.event_value.startswith("story:"):
                    try:
                        key = ("story", int(event.event_value.split(":", 1)[1]))
                    except ValueError:
                        key = None
                elif event.article_id is not None:
                    key = ("article", event.article_id)
                if key is None or key in seen:
                    continue
                seen.add(key)
                ordered_keys.append(key)
                if len(ordered_keys) >= requested_limit:
                    break

            article_ids = [item_id for kind, item_id in ordered_keys if kind == "article"]
            story_ids = [item_id for kind, item_id in ordered_keys if kind == "story"]
            articles = {
                article.id: article
                for article in session.execute(select(NewsArticle).where(NewsArticle.id.in_(article_ids))).scalars()
            } if article_ids else {}
            stories = {
                story.id: story
                for story in session.execute(select(StoryCluster).where(StoryCluster.id.in_(story_ids))).scalars()
            } if story_ids else {}

            cards: list[tuple[str, NewsArticle | StoryCluster]] = []
            for kind, item_id in ordered_keys:
                item = stories.get(item_id) if kind == "story" else articles.get(item_id)
                if item is not None:
                    cards.append((kind, item))
            return cards
        except Exception as exc:
            raise DatabaseOperationError("Failed to list recently viewed cards") from exc

    def save_story_cluster_summary(self, session: Session, cluster_id: int, summary: str) -> StoryCluster | None:
        try:
            cluster = session.get(StoryCluster, cluster_id)
            if cluster is None:
                return None
            cluster.summary = summary
            cluster.summary_generated_at = datetime.now(timezone.utc)
            return cluster
        except Exception as exc:
            raise DatabaseOperationError("Failed to save story cluster summary") from exc


def _build_fts_query(query: str | None) -> str | None:
    if not query or not query.strip():
        return None
    tokens = re.findall(r"[A-Za-z0-9]+", query.lower())
    if not tokens:
        return None
    return " ".join(f"{token}*" for token in tokens[:8])


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
