from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("url_hash", name="uq_news_articles_url_hash"),
        Index("ix_news_articles_collected_date", "collected_for_date"),
        Index("ix_news_articles_published_at", "published_at"),
        Index("ix_news_articles_source_name", "source_name"),
        Index("ix_news_articles_region", "source_region"),
        Index("ix_news_articles_category", "primary_category"),
        Index("ix_news_articles_slug", "slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    article_url: Mapped[str] = mapped_column(Text, nullable=False)
    feed_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_country: Mapped[str] = mapped_column(String(80), nullable=False)
    source_region: Mapped[str] = mapped_column(String(80), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")

    original_title: Mapped[str] = mapped_column(Text, nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    event_fingerprint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    primary_category: Mapped[str] = mapped_column(String(40), nullable=False, default="General")
    category_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reading_time_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    slug: Mapped[Optional[str]] = mapped_column(String(220), nullable=True)
    thumbnail_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thumbnail_cached_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    offline_cached_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collected_for_date: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    summary_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class StoryCluster(Base):
    __tablename__ = "story_clusters"
    __table_args__ = (
        UniqueConstraint("cluster_key", name="uq_story_clusters_cluster_key"),
        Index("ix_story_clusters_date", "collected_for_date"),
        Index("ix_story_clusters_category", "primary_category"),
        Index("ix_story_clusters_latest", "latest_published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_key: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    primary_category: Mapped[str] = mapped_column(String(40), nullable=False, default="General")
    collected_for_date: Mapped[date] = mapped_column(Date, nullable=False)
    seed_article_id: Mapped[Optional[int]] = mapped_column(ForeignKey("news_articles.id", ondelete="SET NULL"), nullable=True)
    cluster_fingerprint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_names: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thumbnail_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class StoryClusterArticle(Base):
    __tablename__ = "story_cluster_articles"
    __table_args__ = (
        UniqueConstraint("article_id", name="uq_story_cluster_articles_article_id"),
        Index("ix_story_cluster_articles_cluster_id", "cluster_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("story_clusters.id", ondelete="CASCADE"), nullable=False)
    article_id: Mapped[int] = mapped_column(ForeignKey("news_articles.id", ondelete="CASCADE"), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class GovernmentNotice(Base):
    __tablename__ = "government_notices"
    __table_args__ = (
        UniqueConstraint("url_hash", name="uq_government_notices_url_hash"),
        Index("ix_government_notices_published_at", "published_at"),
        Index("ix_government_notices_source_name", "source_name"),
        Index("ix_government_notices_type", "document_type"),
        Index("ix_government_notices_region", "source_region"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document_url: Mapped[str] = mapped_column(Text, nullable=False)
    feed_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_country: Mapped[str] = mapped_column(String(80), nullable=False, default="India")
    source_region: Mapped[str] = mapped_column(String(80), nullable=False, default="Government")
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="government")
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    document_type: Mapped[str] = mapped_column(String(80), nullable=False, default="Notice")

    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thumbnail_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_users_public_id"),
        Index("ix_users_email", "email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class OAuthAccount(Base):
    __tablename__ = "oauth_accounts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_account_id", name="uq_oauth_provider_account"),
        Index("ix_oauth_accounts_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class Bookmark(Base):
    __tablename__ = "bookmarks"
    __table_args__ = (
        UniqueConstraint("user_id", "article_id", name="uq_bookmarks_user_article"),
        Index("ix_bookmarks_user_id", "user_id"),
        Index("ix_bookmarks_article_id", "article_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    article_id: Mapped[int] = mapped_column(ForeignKey("news_articles.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ArticleEvent(Base):
    __tablename__ = "article_events"
    __table_args__ = (
        Index("ix_article_events_user_id", "user_id"),
        Index("ix_article_events_article_id", "article_id"),
        Index("ix_article_events_type_created", "event_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    article_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("news_articles.id", ondelete="CASCADE"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    event_value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ArticleMetric(Base):
    __tablename__ = "article_metrics"

    article_id: Mapped[int] = mapped_column(ForeignKey("news_articles.id", ondelete="CASCADE"), primary_key=True)
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_opens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_opens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bookmarks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    popularity_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_event_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ShareLink(Base):
    __tablename__ = "share_links"
    __table_args__ = (
        UniqueConstraint("token", name="uq_share_links_token"),
        Index("ix_share_links_article_id", "article_id"),
        Index("ix_share_links_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(80), nullable=False)
    article_id: Mapped[int] = mapped_column(ForeignKey("news_articles.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    platform: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    click_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_clicked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class UserCategoryScore(Base):
    __tablename__ = "user_category_scores"
    __table_args__ = (
        UniqueConstraint("user_id", "category", name="uq_user_category_scores_user_category"),
        Index("ix_user_category_scores_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class UserPreference(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "preference_type", "value", name="uq_user_preferences_type_value"),
        Index("ix_user_preferences_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    preference_type: Mapped[str] = mapped_column(String(40), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class SourceHealth(Base):
    __tablename__ = "source_health"
    __table_args__ = (
        UniqueConstraint("source_name", name="uq_source_health_source_name"),
        Index("ix_source_health_status", "last_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    feed_url: Mapped[str] = mapped_column(Text, nullable=False)
    last_status: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    items_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class IngestionCursor(Base):
    __tablename__ = "ingestion_cursors"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    last_successful_window_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_successful_run_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    last_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
