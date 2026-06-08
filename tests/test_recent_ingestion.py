from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from news_app.config import Settings
from news_app.db.migrations import run_migrations
from news_app.db.models import Base, NewsArticle
from news_app.db.repository import ArticleRepository
from news_app.ingestion.extractor import ExtractedArticle
from news_app.ingestion.rss_client import FeedItem
from news_app.ingestion.sources import Source
from news_app.services.ingestion_service import IngestionPipeline, RECENT_NEWS_CURSOR_KEY, rank_candidate_stories
from news_app.services.ingestion_service import ArticleCandidate
from news_app.time_window import rolling_utc_window


def _source(name: str, source_type: str = "newspaper", region: str = "India") -> Source:
    return Source(
        name=name,
        country="India" if region == "India" else "United States",
        region=region,
        language="en",
        type=source_type,
        homepage=f"https://{name.lower().replace(' ', '')}.example",
        feed_url=f"https://{name.lower().replace(' ', '')}.example/feed",
    )


def _candidate(source: Source, title: str, published_at: datetime, description: str = "Major public policy update") -> ArticleCandidate:
    url = f"{source.homepage}/{abs(hash((source.name, title))) % 100000}"
    return ArticleCandidate(
        source=source,
        item_title=title,
        item_description=description,
        item_thumbnail_url=None,
        item_published_at=published_at,
        normalized_url=url,
        url_hash=str(abs(hash(url))),
    )


def test_general_importance_ranking_boosts_multi_source_public_news():
    now = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
    window = rolling_utc_window("Asia/Kolkata", now=now)
    source_one = _source("Source One", "newspaper")
    source_two = _source("Source Two", "publisher")
    entertainment = _source("Soft News", "publisher")
    candidates = [
        _candidate(source_one, "RBI keeps repo rate unchanged after policy meeting", now - timedelta(hours=2)),
        _candidate(source_two, "RBI repo rate unchanged after monetary policy meeting", now - timedelta(hours=3)),
        _candidate(entertainment, "Actor shares viral video with fans", now - timedelta(minutes=20), "Viral entertainment clip"),
    ]

    stories = rank_candidate_stories(candidates, window)

    assert stories[0].score_components["multi_source_coverage"] > 0
    assert {candidate.source.name for candidate in stories[0].candidates} == {"Source One", "Source Two"}


def test_recent_story_ranking_rejects_unrelated_event_overlap():
    now = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
    window = rolling_utc_window("Asia/Kolkata", now=now)
    ndtv = _source("NDTV", "newspaper")
    times_now = _source("Times Now", "publisher")
    hindustan_times = _source("Hindustan Times India", "newspaper")
    indian_express = _source("Indian Express", "newspaper")
    livemint = _source("Livemint", "publisher")
    candidates = [
        _candidate(
            ndtv,
            "Delhi University Professor Murdered in Locked Flat",
            now - timedelta(minutes=10),
            "Delhi University professor Debosmita Paul found in locked flat.",
        ),
        _candidate(
            times_now,
            "Delhi University Assistant Professor Murdered in East Delhi Flat",
            now - timedelta(minutes=20),
            "Assistant professor found murdered in East Delhi flat.",
        ),
        _candidate(
            hindustan_times,
            "Woman, 2 grandchildren killed in Telangana house fire; cylinder blast suspected",
            now - timedelta(minutes=30),
            "Victims found after a suspected cylinder blast and house fire.",
        ),
        _candidate(
            indian_express,
            "Delhi fire investigation highlights lack of ventilation and locked basement door",
            now - timedelta(minutes=40),
            "Police investigation into a separate Delhi building fire.",
        ),
        _candidate(
            livemint,
            "Delhi worker killed, two injured in wall collapse during sewer work",
            now - timedelta(minutes=50),
            "A worker died and two were injured in a wall collapse.",
        ),
    ]

    stories = rank_candidate_stories(candidates, window)
    du_story = next(story for story in stories if any(candidate.source.name == "NDTV" for candidate in story.candidates))

    assert {candidate.source.name for candidate in du_story.candidates} == {"NDTV", "Times Now"}
    assert all(len(story.candidates) <= 2 for story in stories)


def test_recent_ingestion_processes_full_selected_run_before_pagination(tmp_path, monkeypatch):
    now = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
    source = _source("Batch Source", "newspaper")
    sources_file = tmp_path / "sources.json"
    sources_file.write_text(json.dumps({"sources": [source.__dict__]}), encoding="utf-8")
    settings = Settings(
        sources_file=sources_file,
        require_llm=False,
        llm_provider="disabled",
        cache_thumbnails_during_ingestion=False,
        ingestion_batch_size=2,
        story_supporting_articles_per_card=1,
    )
    session = _session(tmp_path)

    @contextmanager
    def scope():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr("news_app.services.ingestion_service.session_scope", scope)
    monkeypatch.setattr("news_app.services.story_clustering.session_scope", scope)
    items = [
        FeedItem(source, "Central bank policy decision announced", f"{source.homepage}/one", now - timedelta(hours=1), "Rates and liquidity update"),
        FeedItem(source, "Space mission launches new satellite", f"{source.homepage}/two", now - timedelta(hours=2), "Science and research update"),
        FeedItem(source, "Hospital medicine shortage reported", f"{source.homepage}/three", now - timedelta(hours=3), "Health system update"),
        FeedItem(source, "Court issues major policy ruling", f"{source.homepage}/four", now - timedelta(hours=4), "Government policy update"),
        FeedItem(source, "Markets rise after earnings report", f"{source.homepage}/five", now - timedelta(hours=5), "Business market update"),
    ]
    pipeline = IngestionPipeline(
        settings,
        rss_client=_FakeRSS(items),
        extractor=_FakeExtractor(),
        llm_client=_FakeLLM(),
        repository=ArticleRepository(),
    )

    first = pipeline.run_recent(run_id="test-run", now=now)

    assert first.discovered_cards == 5
    assert first.important_cards_selected == 5
    assert first.processed_cards == 5
    assert first.remaining_cards == 0
    assert first.cursor_advanced is True
    assert cursor_end_utc(session) == now
    assert session.execute(select(func.count()).select_from(NewsArticle)).scalar_one() == 5

    second = pipeline.process_next_recent_batch(run_id="test-run", now=now)
    assert second.batch_status == "empty"
    assert second.remaining_cards == 0


def test_recent_ingestion_uses_cursor_after_completed_run(tmp_path, monkeypatch):
    first_now = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
    second_now = first_now + timedelta(hours=2)
    source = _source("Cursor Source", "newspaper")
    sources_file = tmp_path / "sources.json"
    sources_file.write_text(json.dumps({"sources": [source.__dict__]}), encoding="utf-8")
    settings = Settings(
        sources_file=sources_file,
        require_llm=False,
        llm_provider="disabled",
        cache_thumbnails_during_ingestion=False,
        ingestion_batch_size=10,
        story_supporting_articles_per_card=1,
    )
    session = _session(tmp_path)
    _patch_session(monkeypatch, session)
    rss = _FakeRSS(
        [
            FeedItem(source, "First policy story", f"{source.homepage}/one", first_now - timedelta(hours=1), "Policy update"),
        ]
    )
    pipeline = IngestionPipeline(settings, rss_client=rss, extractor=_FakeExtractor(), llm_client=_FakeLLM(), repository=ArticleRepository())

    first = pipeline.run_recent(run_id="first-run", now=first_now)
    assert first.cursor_advanced is True
    assert cursor_end_utc(session) == first_now

    rss.items = [
        FeedItem(source, "Duplicate older policy story", f"{source.homepage}/one", first_now - timedelta(minutes=30), "Policy update"),
        FeedItem(source, "Second policy story", f"{source.homepage}/two", first_now + timedelta(minutes=30), "Policy update"),
    ]
    second = pipeline.run_recent(run_id="second-run", now=second_now)

    assert second.previous_ingested_until == first_now.isoformat()
    assert second.effective_window_start == first_now.isoformat()
    assert second.articles_in_window == 1
    assert second.inserted == 1
    assert session.execute(select(func.count()).select_from(NewsArticle)).scalar_one() == 2
    assert cursor_end_utc(session) == second_now


def test_recent_ingestion_empty_success_advances_cursor(tmp_path, monkeypatch):
    now = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
    source = _source("Empty Source", "newspaper")
    sources_file = tmp_path / "sources.json"
    sources_file.write_text(json.dumps({"sources": [source.__dict__]}), encoding="utf-8")
    settings = Settings(sources_file=sources_file, require_llm=False, llm_provider="disabled")
    session = _session(tmp_path)
    _patch_session(monkeypatch, session)
    pipeline = IngestionPipeline(
        settings,
        rss_client=_FakeRSS([]),
        extractor=_FakeExtractor(),
        llm_client=_FakeLLM(),
        repository=ArticleRepository(),
    )

    stats = pipeline.run_recent(run_id="empty-run", now=now)

    assert stats.batch_status == "empty"
    assert stats.cursor_advanced is True
    assert cursor_end_utc(session) == now


def test_recent_ingestion_failure_does_not_advance_cursor(tmp_path, monkeypatch):
    now = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
    source = _source("Failing Source", "newspaper")
    sources_file = tmp_path / "sources.json"
    sources_file.write_text(json.dumps({"sources": [source.__dict__]}), encoding="utf-8")
    settings = Settings(
        sources_file=sources_file,
        require_llm=False,
        llm_provider="disabled",
        cache_thumbnails_during_ingestion=False,
        ingestion_batch_size=10,
    )
    session = _session(tmp_path)
    _patch_session(monkeypatch, session)
    pipeline = IngestionPipeline(
        settings,
        rss_client=_FakeRSS(
            [
                FeedItem(source, "Story that fails extraction", f"{source.homepage}/fail", now - timedelta(hours=1), "Policy update"),
            ]
        ),
        extractor=_FailingExtractor(),
        llm_client=_FakeLLM(),
        repository=ArticleRepository(),
    )

    with pytest.raises(RuntimeError):
        pipeline.run_recent(run_id="failing-run", now=now)

    assert repo_cursor(session) is None


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recent-test.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)()


def _patch_session(monkeypatch, session):
    @contextmanager
    def scope():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr("news_app.services.ingestion_service.session_scope", scope)
    monkeypatch.setattr("news_app.services.story_clustering.session_scope", scope)


def repo_cursor(session):
    return ArticleRepository().get_ingestion_cursor(session, RECENT_NEWS_CURSOR_KEY)


def cursor_end_utc(session):
    value = repo_cursor(session).last_successful_window_end
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class _FakeRSS:
    def __init__(self, items: list[FeedItem]):
        self.items = items

    def fetch_many(self, sources: list[Source]) -> dict[Source, list[FeedItem]]:
        return {source: list(self.items) for source in sources}


class _FakeExtractor:
    def extract_many(self, urls: list[str]) -> dict[str, ExtractedArticle]:
        return {url: ExtractedArticle(content=f"Content for {url}", thumbnail_url=None) for url in urls}


class _FailingExtractor:
    def extract_many(self, urls: list[str]) -> dict[str, ExtractedArticle]:
        raise RuntimeError("extract failed")


class _FakeLLM:
    def batch_generate(self, system_prompt: str, prompts: list[str], max_tokens: int) -> list[str]:
        return [f"Generated headline {index}" for index, _ in enumerate(prompts, start=1)]
