from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import logging
import re
import threading
import uuid

from news_app.config import Settings, get_settings
from news_app.db import session_scope
from news_app.db.repository import ArticleCreate, ArticleRepository
from news_app.ingestion.dedupe import hash_text, normalize_url
from news_app.ingestion.extractor import ArticleExtractor
from news_app.ingestion.rss_client import RSSClient
from news_app.ingestion.sources import Source, load_sources
from news_app.llm import LLMGenerationError, LocalLLMClient
from news_app.llm.prompts import HEADLINE_SYSTEM_PROMPT, headline_prompt
from news_app.services.article_enrichment import ArticleClassifier, estimate_reading_time, make_article_slug
from news_app.services.story_clustering import (
    ArticleSignal,
    TokenProfile,
    StoryClusteringService,
    build_signal_from_parts,
    cannot_link_reason,
    event_fingerprint_json_for_parts,
    log_merge_decision,
    same_event_decision,
    story_token_profile,
)
from news_app.services.text_limits import enforce_word_limit
from news_app.services.thumbnails import cache_thumbnail
from news_app.time_window import (
    IncrementalRollingWindow,
    RollingWindow,
    day_window_for_date,
    incremental_utc_window,
    is_inside_incremental_window,
    is_inside_rolling_window,
    is_inside_window,
    previous_day_window,
    rolling_utc_window,
)

logger = logging.getLogger(__name__)
RECENT_NEWS_CURSOR_KEY = "recent_news"


@dataclass
class IngestionStats:
    collected_date: str
    run_id: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    previous_ingested_until: str | None = None
    effective_window_start: str | None = None
    sources_total: int = 0
    sources_enabled: int = 0
    sources_failed: list[str] = field(default_factory=list)
    feed_items_seen: int = 0
    articles_in_window: int = 0
    discovered_candidates: int = 0
    discovered_cards: int = 0
    important_cards_selected: int = 0
    articles_selected_for_processing: int = 0
    articles_prepared: int = 0
    processed_cards: int = 0
    remaining_cards: int = 0
    batch_size: int = 0
    batch_status: str = "completed"
    headline_generation_failed: int = 0
    inserted: int = 0
    updated: int = 0
    cursor_advanced: bool = False
    retention_cutoff: str | None = None
    pruned_articles: int = 0
    pruned_clusters: int = 0


@dataclass
class RetentionStats:
    retention_cutoff: str | None = None
    pruned_articles: int = 0
    pruned_clusters: int = 0
    rebuilt_clusters: int = 0


@dataclass(frozen=True)
class ArticleCandidate:
    source: Source
    item_title: str
    item_description: str | None
    item_thumbnail_url: str | None
    item_published_at: datetime
    normalized_url: str
    url_hash: str


@dataclass
class RankedStory:
    story_key: str
    rank: int
    score: float
    score_components: dict[str, float]
    candidates: list[ArticleCandidate]
    tokens: set[str] = field(default_factory=set)
    token_profiles: list[TokenProfile] = field(default_factory=list)
    signals: list[ArticleSignal] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    processed: bool = False
    article_ids: list[int] = field(default_factory=list)


@dataclass
class RecentIngestionRun:
    run_id: str
    collected_date: date
    window_start: datetime
    window_end: datetime
    stories: list[RankedStory]
    sources_total: int
    sources_enabled: int
    sources_failed: list[str]
    feed_items_seen: int
    articles_in_window: int
    discovered_candidate_count: int | None = None
    discovered_story_count: int | None = None
    articles_selected_for_processing: int = 0
    previous_ingested_until: datetime | None = None
    effective_window_start: datetime | None = None
    start_exclusive: bool = False
    cursor_advanced: bool = False

    @property
    def discovered_candidates(self) -> int:
        if self.discovered_candidate_count is not None:
            return self.discovered_candidate_count
        return sum(len(story.candidates) for story in self.stories)

    @property
    def selected_cards(self) -> int:
        return len(self.stories)

    @property
    def discovered_cards(self) -> int:
        if self.discovered_story_count is not None:
            return self.discovered_story_count
        return len(self.stories)

    @property
    def processed_cards(self) -> int:
        return sum(1 for story in self.stories if story.processed)

    @property
    def remaining_cards(self) -> int:
        return sum(1 for story in self.stories if not story.processed)


class RecentIngestionStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._active_run: RecentIngestionRun | None = None

    def set_active(self, run: RecentIngestionRun) -> None:
        with self._lock:
            self._active_run = run

    def active(self) -> RecentIngestionRun | None:
        with self._lock:
            return self._active_run

    def snapshot(self) -> dict:
        with self._lock:
            run = self._active_run
            if run is None:
                return {
                    "run_id": None,
                    "window_start": None,
                    "window_end": None,
                    "previous_ingested_until": None,
                    "effective_window_start": None,
                    "discovered_candidates": 0,
                    "discovered_cards": 0,
                    "important_cards_selected": 0,
                    "articles_selected_for_processing": 0,
                    "processed_cards": 0,
                    "remaining_cards": 0,
                    "cursor_advanced": False,
                }
            return {
                "run_id": run.run_id,
                "window_start": run.window_start.isoformat(),
                "window_end": run.window_end.isoformat(),
                "previous_ingested_until": (
                    run.previous_ingested_until.isoformat() if run.previous_ingested_until else None
                ),
                "effective_window_start": (
                    run.effective_window_start.isoformat() if run.effective_window_start else run.window_start.isoformat()
                ),
                "discovered_candidates": run.discovered_candidates,
                "discovered_cards": run.discovered_cards,
                "important_cards_selected": run.selected_cards,
                "articles_selected_for_processing": run.articles_selected_for_processing,
                "processed_cards": run.processed_cards,
                "remaining_cards": run.remaining_cards,
                "cursor_advanced": run.cursor_advanced,
            }


recent_ingestion_store = RecentIngestionStore()


class IngestionPipeline:
    def __init__(
        self,
        settings: Settings,
        rss_client: RSSClient | None = None,
        extractor: ArticleExtractor | None = None,
        llm_client: LocalLLMClient | None = None,
        repository: ArticleRepository | None = None,
    ):
        self.settings = settings
        self.rss_client = rss_client or RSSClient(settings)
        self.extractor = extractor or ArticleExtractor(settings)
        self.llm_client = llm_client or LocalLLMClient(settings)
        self.repository = repository or ArticleRepository()
        self.classifier = ArticleClassifier(settings, self.llm_client)

    def run_previous_day(self, now: datetime | None = None) -> IngestionStats:
        window = previous_day_window(self.settings.app_timezone, now=now)
        return self.run_window(window)

    def run_for_date(self, target_date: date) -> IngestionStats:
        return self.run_window(day_window_for_date(self.settings.app_timezone, target_date))

    def run_recent(self, run_id: str | None = None, now: datetime | None = None) -> IngestionStats:
        retention = self.prune_retention(now=now, rebuild=True)
        previous_ingested_until = self._recent_cursor_window_end()
        window = incremental_utc_window(self.settings.app_timezone, previous_ingested_until, now=now)
        run = self._stage_recent_run(run_id or uuid.uuid4().hex, window)
        recent_ingestion_store.set_active(run)
        if not run.stories:
            stats = self._stats_for_run(run, batch_status="empty")
            stats.cursor_advanced = self._advance_recent_cursor(run)
            self._apply_retention_stats(stats, retention)
            return stats
        return self.process_next_recent_batch(run.run_id, now=now)

    def process_next_recent_batch(self, run_id: str | None = None, now: datetime | None = None) -> IngestionStats:
        run = recent_ingestion_store.active()
        if run is None:
            window = rolling_utc_window(self.settings.app_timezone)
            return IngestionStats(
                collected_date=window.collected_date.isoformat(),
                run_id=run_id,
                window_start=window.start_utc.isoformat(),
                window_end=window.end_utc.isoformat(),
                effective_window_start=window.start_utc.isoformat(),
                batch_size=self.settings.ingestion_batch_size,
                batch_status="no_staged_run",
            )
        if run_id and run.run_id != run_id:
            return self._stats_for_run(run, batch_status="stale_run")

        retention = self.prune_retention(now=now, rebuild=False)
        selected_stories = [story for story in run.stories if not story.processed]
        batch_size = len(selected_stories)
        if not selected_stories:
            stats = self._stats_for_run(run, batch_status="empty")
            stats.cursor_advanced = self._advance_recent_cursor(run)
            self._apply_retention_stats(stats, retention)
            self._rebuild_live_story_clusters(now=now)
            return stats

        candidates = self._candidates_for_stories(
            selected_stories,
            per_story_limit=None if self.settings.process_all_articles_per_important_story else self.settings.story_supporting_articles_per_card,
            max_candidates=self.settings.max_articles_per_run,
        )
        extracted_by_url = self.extractor.extract_many([candidate.normalized_url for candidate in candidates])
        stats = self._stats_for_run(run, batch_status="completed")
        stats.batch_size = batch_size
        stats.articles_selected_for_processing = len(candidates)
        run.articles_selected_for_processing = len(candidates)
        headlines_by_url = self._generate_headlines(candidates, extracted_by_url, stats)
        prepared = self._prepare_articles(candidates, extracted_by_url, headlines_by_url, run.collected_date)
        stats.articles_prepared = len(prepared)
        if prepared:
            with session_scope() as session:
                stats.inserted, stats.updated = self.repository.upsert_many(session, prepared)
                article_ids = self.repository.article_ids_by_url_hashes(session, [article.url_hash for article in prepared])
            prepared_hashes = {article.url_hash for article in prepared}
            for story in selected_stories:
                story.processed = True
                story.article_ids = [
                    article_ids[candidate.url_hash]
                    for candidate in story.candidates
                    if candidate.url_hash in prepared_hashes and candidate.url_hash in article_ids
                ]
        else:
            for story in selected_stories:
                story.processed = True

        post_retention = self.prune_retention(now=now, rebuild=False)
        rebuilt_clusters = self._rebuild_live_story_clusters(now=now)
        stats.remaining_cards = run.remaining_cards
        stats.processed_cards = run.processed_cards
        stats.discovered_candidates = run.discovered_candidates
        stats.discovered_cards = run.discovered_cards
        stats.important_cards_selected = run.selected_cards
        stats.articles_selected_for_processing = run.articles_selected_for_processing
        if run.remaining_cards <= 0:
            stats.cursor_advanced = self._advance_recent_cursor(run)
        retention.pruned_articles += post_retention.pruned_articles
        retention.pruned_clusters += post_retention.pruned_clusters
        retention.rebuilt_clusters = rebuilt_clusters
        self._apply_retention_stats(stats, retention)
        return stats

    def _stage_recent_run(self, run_id: str, window: IncrementalRollingWindow) -> RecentIngestionRun:
        all_sources = load_sources(self.settings.sources_file)
        enabled_sources = [source for source in all_sources if source.enabled]
        stats = IngestionStats(
            collected_date=window.collected_date.isoformat(),
            run_id=run_id,
            window_start=window.start_utc.isoformat(),
            window_end=window.end_utc.isoformat(),
            previous_ingested_until=(
                window.previous_ingested_until.isoformat() if window.previous_ingested_until else None
            ),
            effective_window_start=window.start_utc.isoformat(),
            sources_total=len(all_sources),
            sources_enabled=len(enabled_sources),
        )
        feed_results = self.rss_client.fetch_many(enabled_sources)
        self._record_feed_health(enabled_sources, feed_results)
        candidates = self._collect_candidates(enabled_sources, feed_results, window, stats, limit=None)
        with session_scope() as session:
            popularity_by_hash = self.repository.popularity_by_url_hashes(
                session,
                [candidate.url_hash for candidate in candidates],
            )
        ranked_stories = rank_candidate_stories(candidates, window, popularity_by_hash)
        stories = select_important_stories(ranked_stories, self.settings)
        selected_candidate_count = count_story_candidates(
            stories,
            per_story_limit=None if self.settings.process_all_articles_per_important_story else self.settings.story_supporting_articles_per_card,
            max_candidates=self.settings.max_articles_per_run,
        )
        return RecentIngestionRun(
            run_id=run_id,
            collected_date=window.collected_date,
            window_start=window.start_utc,
            window_end=window.end_utc,
            stories=stories,
            sources_total=len(all_sources),
            sources_enabled=len(enabled_sources),
            sources_failed=stats.sources_failed,
            feed_items_seen=stats.feed_items_seen,
            articles_in_window=stats.articles_in_window,
            discovered_candidate_count=len(candidates),
            discovered_story_count=len(ranked_stories),
            articles_selected_for_processing=selected_candidate_count,
            previous_ingested_until=window.previous_ingested_until,
            effective_window_start=window.start_utc,
            start_exclusive=window.start_exclusive,
        )

    def run_window(self, window) -> IngestionStats:
        all_sources = load_sources(self.settings.sources_file)
        enabled_sources = [source for source in all_sources if source.enabled]
        stats = IngestionStats(
            collected_date=window.collected_date.isoformat(),
            sources_total=len(all_sources),
            sources_enabled=len(enabled_sources),
        )

        feed_results = self.rss_client.fetch_many(enabled_sources)
        self._record_feed_health(enabled_sources, feed_results)
        candidates = self._collect_candidates(enabled_sources, feed_results, window, stats, limit=self.settings.max_articles_per_run)
        extracted_by_url = self.extractor.extract_many([candidate.normalized_url for candidate in candidates])
        headlines_by_url = self._generate_headlines(candidates, extracted_by_url, stats)

        prepared = self._prepare_articles(candidates, extracted_by_url, headlines_by_url, window.collected_date)

        stats.articles_prepared = len(prepared)
        if prepared:
            with session_scope() as session:
                stats.inserted, stats.updated = self.repository.upsert_many(session, prepared)
            StoryClusteringService(self.repository).rebuild_for_date(window.collected_date)

        logger.info("Ingestion finished: %s", stats)
        return stats

    def prune_retention(self, now: datetime | None = None, rebuild: bool = True) -> RetentionStats:
        return run_retention_cleanup(
            settings=self.settings,
            repository=self.repository,
            now=now,
            rebuild=rebuild,
        )

    def _recent_cursor_window_end(self) -> datetime | None:
        with session_scope() as session:
            cursor = self.repository.get_ingestion_cursor(session, RECENT_NEWS_CURSOR_KEY)
            return cursor.last_successful_window_end if cursor else None

    def _advance_recent_cursor(self, run: RecentIngestionRun) -> bool:
        if run.cursor_advanced:
            return False
        with session_scope() as session:
            self.repository.advance_ingestion_cursor(
                session,
                RECENT_NEWS_CURSOR_KEY,
                run.window_end,
                run.run_id,
            )
        run.cursor_advanced = True
        return True

    def _rebuild_live_story_clusters(self, now: datetime | None = None) -> int:
        window = rolling_utc_window(self.settings.app_timezone, now=now)
        return StoryClusteringService(self.repository).rebuild_for_window(
            window.collected_date,
            window.start_utc,
            window.end_utc,
        )

    def _apply_retention_stats(self, stats: IngestionStats, retention: RetentionStats) -> None:
        stats.retention_cutoff = retention.retention_cutoff
        stats.pruned_articles += retention.pruned_articles
        stats.pruned_clusters += retention.pruned_clusters

    def _record_feed_health(self, enabled_sources, feed_results) -> None:
        with session_scope() as session:
            for source in enabled_sources:
                items = feed_results.get(source, [])
                self.repository.record_source_health(
                    session,
                    source.name,
                    source.feed_url,
                    success=bool(items),
                    items_seen=len(items),
                    failure_reason=None if items else "Feed fetch failed or returned no valid items",
                )

    def _collect_candidates(
        self,
        enabled_sources,
        feed_results,
        window,
        stats: IngestionStats,
        limit: int | None = None,
    ) -> list[ArticleCandidate]:
        candidates: list[ArticleCandidate] = []
        seen_urls: set[str] = set()
        for source in enabled_sources:
            if limit is not None and len(candidates) >= limit:
                break

            items = feed_results.get(source, [])
            if not items:
                stats.sources_failed.append(source.name)
                continue
            stats.feed_items_seen += len(items)

            for item in items:
                if limit is not None and len(candidates) >= limit:
                    break
                if isinstance(window, IncrementalRollingWindow):
                    is_inside = is_inside_incremental_window(item.published_at, window)
                elif isinstance(window, RollingWindow):
                    is_inside = is_inside_rolling_window(item.published_at, window)
                else:
                    is_inside = is_inside_window(item.published_at, window)
                if not is_inside:
                    continue
                stats.articles_in_window += 1

                normalized_url = normalize_url(item.link)
                if normalized_url in seen_urls:
                    continue
                seen_urls.add(normalized_url)
                candidates.append(
                    ArticleCandidate(
                        source=source,
                        item_title=item.title,
                        item_description=item.description,
                        item_thumbnail_url=item.thumbnail_url,
                        item_published_at=item.published_at,
                        normalized_url=normalized_url,
                        url_hash=hash_text(normalized_url),
                    )
                )
        return candidates

    def _candidates_for_stories(
        self,
        stories: list[RankedStory],
        *,
        per_story_limit: int | None = None,
        max_candidates: int | None = None,
    ) -> list[ArticleCandidate]:
        return candidates_for_stories(
            stories,
            per_story_limit=per_story_limit,
            max_candidates=max_candidates,
        )

    def _prepare_articles(
        self,
        candidates: list[ArticleCandidate],
        extracted_by_url,
        headlines_by_url: dict[str, str],
        collected_date: date,
    ) -> list[ArticleCreate]:
        prepared: list[ArticleCreate] = []
        for candidate in candidates:
            headline = headlines_by_url.get(candidate.normalized_url)
            if headline is None:
                continue
            extracted = extracted_by_url.get(candidate.normalized_url)
            content = extracted.content if extracted else None
            text_for_hash = content or candidate.item_description or candidate.item_title
            category = self.classifier.classify(
                headline,
                candidate.item_description,
                content,
                candidate.source.region,
            )
            thumbnail_path = None
            thumbnail_cached_at = None
            if self.settings.cache_thumbnails_during_ingestion:
                thumbnail_url = (extracted.thumbnail_url if extracted else None) or candidate.item_thumbnail_url
                thumbnail = cache_thumbnail(self.settings, thumbnail_url, candidate.url_hash)
                thumbnail_path = thumbnail[0] if thumbnail else None
                thumbnail_cached_at = thumbnail[1] if thumbnail else None

            prepared.append(
                ArticleCreate(
                    url_hash=candidate.url_hash,
                    content_hash=hash_text(text_for_hash) if text_for_hash else None,
                    article_url=candidate.normalized_url,
                    feed_url=candidate.source.feed_url,
                    source_url=candidate.source.homepage,
                    source_name=candidate.source.name,
                    source_country=candidate.source.country,
                    source_region=candidate.source.region,
                    source_type=candidate.source.type,
                    language=candidate.source.language,
                    original_title=candidate.item_title,
                    headline=headline,
                    description=candidate.item_description,
                    content=content,
                    event_fingerprint=event_fingerprint_json_for_parts(
                        source_name=candidate.source.name,
                        language=candidate.source.language,
                        category=category.category,
                        published_at=candidate.item_published_at,
                        title=headline,
                        original_title=candidate.item_title,
                        summary=candidate.item_description,
                        content=content,
                    ),
                    primary_category=category.category,
                    category_confidence=category.confidence,
                    reading_time_minutes=estimate_reading_time(text_for_hash, self.settings.reading_words_per_minute),
                    slug=make_article_slug(headline, candidate.url_hash),
                    thumbnail_path=thumbnail_path,
                    thumbnail_cached_at=thumbnail_cached_at,
                    published_at=candidate.item_published_at,
                    collected_for_date=collected_date,
                )
            )
        return prepared

    def _stats_for_run(self, run: RecentIngestionRun, batch_status: str = "completed") -> IngestionStats:
        return IngestionStats(
            collected_date=run.collected_date.isoformat(),
            run_id=run.run_id,
            window_start=run.window_start.isoformat(),
            window_end=run.window_end.isoformat(),
            previous_ingested_until=(
                run.previous_ingested_until.isoformat() if run.previous_ingested_until else None
            ),
            effective_window_start=(
                run.effective_window_start.isoformat() if run.effective_window_start else run.window_start.isoformat()
            ),
            sources_total=run.sources_total,
            sources_enabled=run.sources_enabled,
            sources_failed=list(run.sources_failed),
            feed_items_seen=run.feed_items_seen,
            articles_in_window=run.articles_in_window,
            discovered_candidates=run.discovered_candidates,
            discovered_cards=run.discovered_cards,
            important_cards_selected=run.selected_cards,
            articles_selected_for_processing=run.articles_selected_for_processing,
            processed_cards=run.processed_cards,
            remaining_cards=run.remaining_cards,
            batch_size=run.selected_cards,
            batch_status=batch_status,
            cursor_advanced=run.cursor_advanced,
        )

    def _generate_headlines(
        self,
        candidates: list[ArticleCandidate],
        extracted_by_url,
        stats: IngestionStats,
    ) -> dict[str, str]:
        headlines: dict[str, str] = {}
        batch_size = max(1, self.settings.llm_batch_size)
        for start in range(0, len(candidates), batch_size):
            batch = candidates[start : start + batch_size]
            prompts = []
            for candidate in batch:
                extracted = extracted_by_url.get(candidate.normalized_url)
                content = extracted.content if extracted else None
                prompts.append(headline_prompt(candidate.item_title, candidate.item_description, content))
            try:
                generated_batch = self.llm_client.batch_generate(
                    HEADLINE_SYSTEM_PROMPT,
                    prompts,
                    max_tokens=80,
                )
            except LLMGenerationError:
                if self.settings.require_llm:
                    stats.headline_generation_failed += len(batch)
                    logger.exception("Skipping headline batch because local LLM generation failed")
                    continue
                generated_batch = [candidate.item_title for candidate in batch]

            for candidate, generated in zip(batch, generated_batch):
                headline = (generated or candidate.item_title).splitlines()[0]
                headlines[candidate.normalized_url] = enforce_word_limit(headline, 15)
        return headlines

SOURCE_TYPE_WEIGHTS = {
    "newspaper": 1.0,
    "business": 0.92,
    "publisher": 0.78,
    "government": 0.74,
}

CATEGORY_IMPORTANCE = {
    "Politics": 0.95,
    "India": 0.92,
    "World": 0.9,
    "Business": 0.86,
    "Health": 0.84,
    "Science": 0.8,
    "Tech": 0.78,
    "Sports": 0.56,
    "Entertainment": 0.36,
    "General": 0.52,
}

LOW_SIGNAL_PATTERNS = (
    r"\blive\b",
    r"\blive updates?\b",
    r"\blatest updates?\b",
    r"\bhoroscope\b",
    r"\bviral\b",
    r"\bwatch\b",
)

MAJOR_NEWS_TERMS = (
    "government", "minister", "parliament", "election", "policy", "court", "supreme court",
    "high court", "rbi", "reserve bank", "budget", "tax", "market", "stock", "economy",
    "bank", "company", "funding", "war", "china", "russia", "israel", "ukraine", "us",
    "india", "delhi", "mumbai", "bengaluru", "accident", "death", "killed", "arrest",
    "bail", "probe", "investigation", "health", "hospital", "vaccine", "disease", "climate",
    "isro", "nasa", "ai", "cyber", "launch", "cricket", "ipl", "match",
)

SOFT_NEWS_TERMS = (
    "horoscope", "viral", "watch", "photo", "photos", "pics", "web story", "quiz",
    "birthday", "fashion", "recipe", "astrology", "ott", "trailer", "box office",
)


def select_important_stories(stories: list[RankedStory], settings: Settings) -> list[RankedStory]:
    """Keep important/relevant stories from the broad RSS collection before LLM work.

    This intentionally happens before extraction + MLX generation so Refresh can process the full
    selected set once, while Load More remains a fast database pagination action.
    """
    max_cards = max(1, settings.important_story_max_cards)
    max_articles = max(1, settings.max_articles_per_run)
    selected: list[RankedStory] = []
    selected_article_count = 0

    for story in stories:
        if len(selected) >= max_cards or selected_article_count >= max_articles:
            break
        if not _story_is_important(story, settings):
            continue
        estimated_candidates = count_story_candidates([story], max_candidates=max_articles)
        if estimated_candidates <= 0:
            continue
        selected.append(story)
        selected_article_count += estimated_candidates

    # Safety fallback: never make a refresh appear broken because every story scored just below
    # the configured threshold. Take the highest-ranked clean stories inside the same caps.
    if not selected and stories:
        for story in stories:
            if len(selected) >= min(25, max_cards) or selected_article_count >= max_articles:
                break
            if _story_is_obviously_low_value(story):
                continue
            selected.append(story)
            selected_article_count += count_story_candidates([story], max_candidates=max_articles)

    for index, story in enumerate(selected, start=1):
        story.rank = index
    return selected


def _story_is_important(story: RankedStory, settings: Settings) -> bool:
    source_count = len({candidate.source.name for candidate in story.candidates})
    category = _dominant_story_category(story)
    if _story_is_obviously_low_value(story):
        return False
    if source_count >= 2:
        return story.score >= settings.important_multi_source_min_score

    threshold = settings.important_story_min_score
    if category in {"Politics", "India", "World", "Business", "Health", "Science", "Tech"}:
        threshold -= 0.35
    elif category == "Sports":
        threshold += 0.15
    elif category == "Entertainment":
        threshold += 1.0

    return story.score >= threshold and _has_major_news_signal(story)


def _story_is_obviously_low_value(story: RankedStory) -> bool:
    candidates = story.candidates
    if not candidates:
        return True
    source_count = len({candidate.source.name for candidate in candidates})
    text = " ".join(f"{candidate.item_title} {candidate.item_description or ''}" for candidate in candidates).lower()
    if any(term in text for term in SOFT_NEWS_TERMS) and source_count < 2:
        return True
    if any(term in text for term in MAJOR_NEWS_TERMS):
        return False
    avg_penalty = sum(_quality_penalty(candidate) for candidate in candidates) / max(1, len(candidates))
    return avg_penalty > 1.2 and source_count < 2


def _has_major_news_signal(story: RankedStory) -> bool:
    text = " ".join(f"{candidate.item_title} {candidate.item_description or ''}" for candidate in story.candidates).lower()
    if any(term in text for term in MAJOR_NEWS_TERMS):
        return True
    # High-quality sources with a well-formed title/description still deserve to pass even if
    # the simple keyword list misses the event.
    return any(_source_credibility(candidate.source) >= 0.92 for candidate in story.candidates)


def _dominant_story_category(story: RankedStory) -> str:
    counts: dict[str, int] = {}
    for category in story.categories or [_candidate_category(candidate) for candidate in story.candidates]:
        counts[category] = counts.get(category, 0) + 1
    if not counts:
        return "General"
    return max(counts.items(), key=lambda item: item[1])[0]


def candidates_for_stories(
    stories: list[RankedStory],
    *,
    per_story_limit: int | None = None,
    max_candidates: int | None = None,
) -> list[ArticleCandidate]:
    selected: list[ArticleCandidate] = []
    seen_urls: set[str] = set()
    global_limit = max_candidates if max_candidates and max_candidates > 0 else None
    story_limit = per_story_limit if per_story_limit and per_story_limit > 0 else None

    for story in stories:
        source_names: set[str] = set()
        for candidate in story.candidates:
            if global_limit is not None and len(selected) >= global_limit:
                return selected
            if candidate.normalized_url in seen_urls or candidate.source.name in source_names:
                continue
            selected.append(candidate)
            seen_urls.add(candidate.normalized_url)
            source_names.add(candidate.source.name)
            if story_limit is not None and len(source_names) >= story_limit:
                break
    return selected


def count_story_candidates(
    stories: list[RankedStory],
    *,
    per_story_limit: int | None = None,
    max_candidates: int | None = None,
) -> int:
    return len(candidates_for_stories(stories, per_story_limit=per_story_limit, max_candidates=max_candidates))


def rank_candidate_stories(
    candidates: list[ArticleCandidate],
    window,
    popularity_by_hash: dict[str, float] | None = None,
) -> list[RankedStory]:
    popularity_by_hash = popularity_by_hash or {}
    stories: list[RankedStory] = []
    for candidate in sorted(candidates, key=lambda item: item.item_published_at, reverse=True):
        profile = story_token_profile(candidate.item_title, candidate.item_description)
        category = _candidate_category(candidate)
        signal = build_signal_from_parts(
            identifier=candidate.url_hash,
            source_name=candidate.source.name,
            language=candidate.source.language,
            category=category,
            published_at=candidate.item_published_at,
            title=candidate.item_title,
            summary=candidate.item_description,
        )
        tokens = profile.all_tokens
        match = _find_candidate_story(stories, signal)
        if match is None:
            story_key_seed = "|".join(
                [
                    signal.fingerprint.main_event,
                    signal.fingerprint.event_type,
                    "-".join(signal.fingerprint.main_entities),
                    candidate.url_hash,
                ]
            )
            story_key = hashlib.sha256(story_key_seed.encode("utf-8")).hexdigest()[:32]
            stories.append(
                RankedStory(
                    story_key=story_key,
                    rank=0,
                    score=0.0,
                    score_components={},
                    candidates=[candidate],
                    tokens=set(tokens),
                    token_profiles=[profile],
                    signals=[signal],
                    categories=[category],
                )
            )
            continue
        match.candidates.append(candidate)
        match.tokens.update(tokens)
        match.token_profiles.append(profile)
        match.signals.append(signal)
        match.categories.append(category)

    for story in stories:
        story.candidates = sorted(
            story.candidates,
            key=lambda candidate: (
                _candidate_base_score(candidate, window, popularity_by_hash),
                candidate.item_published_at,
            ),
            reverse=True,
        )
        story.score_components = _story_score_components(story, window, popularity_by_hash)
        story.score = round(sum(story.score_components.values()), 4)

    ranked = sorted(stories, key=lambda story: (story.score, _latest_published_at(story)), reverse=True)
    for index, story in enumerate(ranked, start=1):
        story.rank = index
    return ranked


def _find_candidate_story(stories: list[RankedStory], signal: ArticleSignal) -> RankedStory | None:
    if len(signal.token_profile.all_tokens) < 3:
        return None
    best: tuple[float, RankedStory] | None = None
    for story in stories:
        if not story.signals:
            continue
        seed_decision = same_event_decision(signal, story.signals[0])
        log_merge_decision(seed_decision)
        if not seed_decision.final_decision:
            continue
        if any(cannot_link_reason(signal, existing) for existing in story.signals):
            continue
        if best is None or seed_decision.final_score > best[0]:
            best = (seed_decision.final_score, story)
    return best[1] if best is not None else None


def _story_score_components(
    story: RankedStory,
    window,
    popularity_by_hash: dict[str, float],
) -> dict[str, float]:
    candidates = story.candidates
    source_count = len({candidate.source.name for candidate in candidates})
    categories = [_candidate_category(candidate) for candidate in candidates]
    penalties = sum(_quality_penalty(candidate) for candidate in candidates) / max(1, len(candidates))
    return {
        "freshness": max(_freshness_score(candidate, window) for candidate in candidates) * 4.0,
        "multi_source_coverage": min(1.0, max(0, source_count - 1) / 3) * 3.0,
        "source_credibility": max(_source_credibility(candidate.source) for candidate in candidates) * 2.0,
        "category_importance": max(CATEGORY_IMPORTANCE.get(category, 0.52) for category in categories) * 2.0,
        "location_relevance": max(_location_relevance(candidate.source) for candidate in candidates) * 1.2,
        "public_interest": min(2.0, max(popularity_by_hash.get(candidate.url_hash, 0.0) for candidate in candidates) / 5.0),
        "quality_penalties": -penalties,
    }


def _candidate_base_score(
    candidate: ArticleCandidate,
    window,
    popularity_by_hash: dict[str, float],
) -> float:
    components = _story_score_components(
        RankedStory("", 0, 0.0, {}, [candidate]),
        window,
        popularity_by_hash,
    )
    return sum(components.values())


def _freshness_score(candidate: ArticleCandidate, window) -> float:
    published_at = _ensure_aware(candidate.item_published_at)
    age_seconds = max(0.0, (window.end_utc - published_at).total_seconds())
    return max(0.0, 1.0 - (age_seconds / (24 * 60 * 60)))


def _source_credibility(source: Source) -> float:
    return SOURCE_TYPE_WEIGHTS.get((source.type or "").lower(), 0.65)


def _candidate_category(candidate: ArticleCandidate) -> str:
    text = f"{candidate.item_title} {candidate.item_description or ''}"
    lower = text.lower()
    if any(value in lower for value in ("election", "minister", "government", "parliament", "court", "policy")):
        return "Politics"
    if any(value in lower for value in ("india", "delhi", "mumbai", "bengaluru", "supreme court", "modi")):
        return "India"
    if any(value in lower for value in ("war", "global", "world", "china", "russia", "israel", "ukraine")):
        return "World"
    if any(value in lower for value in ("market", "stock", "economy", "bank", "company", "funding", "rupee")):
        return "Business"
    if any(value in lower for value in ("health", "hospital", "doctor", "medicine", "vaccine", "disease")):
        return "Health"
    if any(value in lower for value in ("space", "science", "research", "climate", "isro", "nasa")):
        return "Science"
    if any(value in lower for value in ("technology", "ai", "software", "cyber", "iphone", "chip")):
        return "Tech"
    if any(value in lower for value in ("cricket", "football", "tennis", "ipl", "match")):
        return "Sports"
    if any(value in lower for value in ("film", "movie", "bollywood", "actor", "celebrity")):
        return "Entertainment"
    return "General"


def _location_relevance(source: Source) -> float:
    country = (source.country or "").lower()
    region = (source.region or "").lower()
    if country == "india" or region == "india":
        return 1.0
    if region in {"world", "global", "international", "north america", "europe", "middle east"}:
        return 0.82
    return 0.6


def _quality_penalty(candidate: ArticleCandidate) -> float:
    text = f"{candidate.item_title} {candidate.item_description or ''}".strip()
    penalty = 0.0
    if len(candidate.item_title.split()) < 4:
        penalty += 0.7
    if not candidate.item_description or len(candidate.item_description.split()) < 6:
        penalty += 0.5
    lower = text.lower()
    if any(re.search(pattern, lower) for pattern in LOW_SIGNAL_PATTERNS):
        penalty += 0.8
    if _candidate_category(candidate) == "Entertainment":
        penalty += 0.5
    return penalty


def _latest_published_at(story: RankedStory) -> datetime:
    return max(_ensure_aware(candidate.item_published_at) for candidate in story.candidates)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def run_retention_cleanup(
    *,
    settings: Settings | None = None,
    repository: ArticleRepository | None = None,
    now: datetime | None = None,
    rebuild: bool = True,
) -> RetentionStats:
    settings = settings or get_settings()
    repository = repository or ArticleRepository()
    window = rolling_utc_window(settings.app_timezone, now=now)
    with session_scope() as session:
        pruned = repository.prune_expired_news(session, window.start_utc)
    rebuilt_clusters = 0
    if rebuild:
        rebuilt_clusters = StoryClusteringService(repository).rebuild_for_window(
            window.collected_date,
            window.start_utc,
            window.end_utc,
        )
    return RetentionStats(
        retention_cutoff=window.start_utc.isoformat(),
        pruned_articles=pruned["articles"],
        pruned_clusters=pruned["clusters"],
        rebuilt_clusters=rebuilt_clusters,
    )


def run_previous_day_ingestion() -> IngestionStats:
    settings = get_settings()
    pipeline = IngestionPipeline(settings)
    return pipeline.run_previous_day()


def run_recent_ingestion(run_id: str | None = None) -> IngestionStats:
    settings = get_settings()
    pipeline = IngestionPipeline(settings)
    return pipeline.run_recent(run_id=run_id)


def run_date_ingestion(target_date: date) -> IngestionStats:
    settings = get_settings()
    pipeline = IngestionPipeline(settings)
    return pipeline.run_for_date(target_date)
