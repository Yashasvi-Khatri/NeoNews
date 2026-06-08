from contextlib import contextmanager
from datetime import date, datetime, timezone
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from news_app.config import Settings
from news_app.db.migrations import run_migrations
from news_app.db.models import ArticleMetric, Base, NewsArticle
from news_app.db.repository import ArticleCreate, ArticleRepository, NoticeCreate
from news_app.services.news_service import CurrentUser, NewsService
from news_app.services.story_clustering import StoryClusteringService


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'news-test.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)()


def _article(url_hash: str, headline: str, category: str = "Tech") -> ArticleCreate:
    return ArticleCreate(
        url_hash=url_hash,
        content_hash=url_hash,
        article_url=f"https://example.com/{url_hash}",
        feed_url="https://example.com/feed",
        source_url="https://example.com",
        source_name="Example News",
        source_country="United States",
        source_region="North America",
        source_type="publisher",
        language="en",
        original_title=headline,
        headline=headline,
        description="AI software market update",
        content="Artificial intelligence software companies reported strong demand.",
        primary_category=category,
        category_confidence=0.8,
        reading_time_minutes=1,
        slug=f"{url_hash}-slug",
        published_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        collected_for_date=date(2026, 6, 1),
    )


def _source_article(url_hash: str, headline: str, source_name: str) -> ArticleCreate:
    item = _article(url_hash, headline, "Business")
    return ArticleCreate(
        **{
            **item.__dict__,
            "source_name": source_name,
            "article_url": f"https://{source_name.lower().replace(' ', '')}.example/{url_hash}",
            "feed_url": f"https://{source_name.lower().replace(' ', '')}.example/feed",
            "source_url": f"https://{source_name.lower().replace(' ', '')}.example",
        }
    )


def _rebuild_live_clusters(session, repo: ArticleRepository, service: StoryClusteringService, target_date: date, start: datetime, end: datetime) -> int:
    articles = repo.articles_for_story_clustering(session, None, published_after=start, published_before=end)
    drafts = service._build_drafts(articles, constrain_date=False)
    count = repo.replace_live_story_clusters(
        session,
        target_date,
        [service._cluster_payload(draft, collected_for_date=target_date) for draft in drafts],
    )
    session.commit()
    return count


def test_repository_searches_with_fts(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()

    repo.upsert_many(session, [_article("hashone", "AI software demand rises")])
    session.commit()

    results = repo.list_articles(session, date(2026, 6, 1), query="software", sort="relevance")

    assert [article.headline for article in results] == ["AI software demand rises"]


def test_article_serialization_marks_utc_and_displays_app_timezone(tmp_path, monkeypatch):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(
        session,
        [
            ArticleCreate(
                **{
                    **_article("timezone-story", "Fresh local-time story").__dict__,
                    "published_at": datetime(2026, 6, 5, 7, 3, 32),
                    "collected_for_date": date(2026, 6, 5),
                }
            )
        ],
    )
    session.commit()

    @contextmanager
    def scope():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr("news_app.services.news_service.session_scope", scope)
    service = NewsService(Settings(llm_provider="disabled", require_llm=False, app_timezone="Asia/Kolkata"), repository=repo)

    article = service.list_articles(date(2026, 6, 5))[0]

    assert article["published_at"] == "2026-06-05T07:03:32+00:00"
    assert article["published_at_display"] == "5 june 2026, 12:33 pm"


def test_bookmark_event_metrics_and_user_scores(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(session, [_article("hashtwo", "AI tools gain users")])
    user = repo.create_user(session, "public-user")
    session.commit()
    article = repo.list_articles(session, date(2026, 6, 1))[0]

    _, created = repo.bookmark_article(session, user.id, article.id)
    repo.record_event(session, user.id, article.id, "bookmark")
    session.commit()

    assert created is True
    assert repo.bookmarked_article_ids(session, user.id, [article.id]) == {article.id}
    metric = session.get(ArticleMetric, article.id)
    assert metric.bookmarks == 1
    assert repo.user_category_scores(session, user.id)["Tech"] == 3.0

    assert repo.remove_bookmark(session, user.id, article.id) is True
    repo.record_event(session, user.id, article.id, "unbookmark")
    session.commit()

    assert repo.bookmarked_article_ids(session, user.id, [article.id]) == set()
    assert session.get(ArticleMetric, article.id).bookmarks == 0


def test_retention_deletes_expired_unbookmarked_but_keeps_saved_articles(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    cutoff = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
    repo.upsert_many(
        session,
        [
            ArticleCreate(
                **{
                    **_article("expired-delete", "Expired unbookmarked story").__dict__,
                    "published_at": cutoff.replace(day=4, hour=10),
                    "collected_for_date": date(2026, 6, 4),
                }
            ),
            ArticleCreate(
                **{
                    **_article("expired-saved", "Expired saved story").__dict__,
                    "published_at": cutoff.replace(day=4, hour=11),
                    "collected_for_date": date(2026, 6, 4),
                }
            ),
            ArticleCreate(
                **{
                    **_article("live-story", "Live story").__dict__,
                    "published_at": cutoff.replace(hour=13),
                }
            ),
        ],
    )
    user = repo.create_user(session, "saved-reader")
    session.commit()
    saved = session.execute(select(NewsArticle).where(NewsArticle.url_hash == "expired-saved")).scalar_one()
    repo.bookmark_article(session, user.id, saved.id)
    session.commit()

    pruned = repo.prune_expired_news(session, cutoff)
    session.commit()

    assert pruned["articles"] == 1
    assert session.execute(select(NewsArticle).where(NewsArticle.url_hash == "expired-delete")).scalar_one_or_none() is None
    assert session.execute(select(NewsArticle).where(NewsArticle.url_hash == "expired-saved")).scalar_one_or_none() is not None
    assert [article.headline for article in repo.list_articles(session, None, published_after=cutoff)] == ["Live story"]
    saved_articles = repo.list_articles(session, None, user_id=user.id, view="read_later")
    assert [article.headline for article in saved_articles] == ["Expired saved story"]


def test_unbookmarking_expired_saved_article_deletes_it(tmp_path, monkeypatch):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(
        session,
        [
            ArticleCreate(
                **{
                    **_article("expired-unsave", "Expired saved story").__dict__,
                    "published_at": datetime(2026, 6, 3, 10, tzinfo=timezone.utc),
                    "collected_for_date": date(2026, 6, 3),
                }
            )
        ],
    )
    user = repo.create_user(session, "unsave-reader")
    session.commit()
    article = repo.list_articles(session, date(2026, 6, 3))[0]
    article_id = article.id
    repo.bookmark_article(session, user.id, article.id)
    session.commit()

    @contextmanager
    def scope():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr("news_app.services.news_service.session_scope", scope)
    service = NewsService(Settings(llm_provider="disabled", require_llm=False), repository=repo)
    current_user = CurrentUser(user.id, user.public_id, True, "cookie")

    result = service.remove_bookmark(article_id, current_user)

    assert result["removed"] is True
    assert result["deleted_after_unbookmark"] is True
    session.expire_all()
    assert session.get(NewsArticle, article_id) is None


def test_share_link_click_tracking(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(session, [_article("hashthr", "AI story shared widely")])
    user = repo.create_user(session, "public-user")
    session.commit()
    article = repo.list_articles(session, date(2026, 6, 1))[0]

    link = repo.create_share_link(session, article.id, user.id, "whatsapp")
    repo.record_event(session, user.id, article.id, "share", "whatsapp")
    session.commit()

    clicked = repo.record_share_click(session, link.token)
    session.commit()

    assert clicked is not None
    assert clicked.click_count == 1
    assert clicked.last_clicked_at is not None


def test_popular_articles_can_be_scoped_to_a_date(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(
        session,
        [
            _article("dated-one", "Fresh story", "Tech"),
            ArticleCreate(
                **{
                    **_article("dated-two", "Old story", "Tech").__dict__,
                    "collected_for_date": date(2026, 5, 31),
                    "published_at": datetime(2026, 5, 31, tzinfo=timezone.utc),
                }
            ),
        ],
    )
    session.commit()
    fresh = repo.list_articles(session, date(2026, 6, 1))[0]
    old = repo.list_articles(session, date(2026, 5, 31))[0]
    repo.record_event(session, None, fresh.id, "view")
    repo.record_event(session, None, old.id, "view")
    session.commit()

    current_day = repo.popular_articles(session, target_date=date(2026, 6, 1))
    old_day = repo.popular_articles(session, target_date=date(2026, 5, 31))

    assert [article.headline for article in current_day] == ["Fresh story"]
    assert [article.headline for article in old_day] == ["Old story"]


def test_story_clustering_groups_similar_articles_from_different_sources(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(
        session,
        [
            _source_article("clusterone", "RBI keeps repo rate unchanged after policy meeting", "Source One"),
            _source_article("clustertwo", "RBI repo rate unchanged after monetary policy meeting", "Source Two"),
            _source_article("clusterthree", "Chip startup launches new AI processor", "Source Three"),
        ],
    )
    session.commit()

    service = StoryClusteringService(repo)
    drafts = service._build_drafts(repo.articles_for_story_clustering(session, date(2026, 6, 1)))
    payloads = [service._cluster_payload(draft) for draft in drafts]
    count = repo.replace_story_clusters(session, date(2026, 6, 1), payloads)
    session.commit()
    clusters = repo.list_story_clusters(session, date(2026, 6, 1))

    assert count == 2
    assert len(clusters) == 2
    combined = next(cluster for cluster in clusters if cluster.source_count == 2)
    assert combined.source_count == 2
    assert {"Source One", "Source Two"} == set(combined.source_names.split(", "))

    remaining = repo.list_articles(session, date(2026, 6, 1), exclude_clustered=True)
    assert [article.source_name for article in remaining] == ["Source Three"]

    multi_source = repo.list_story_clusters(session, date(2026, 6, 1), min_source_count=2)
    assert len(multi_source) == 1
    assert multi_source[0].source_count == 2


def test_story_clustering_rejects_unrelated_event_overlap(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(
        session,
        [
            ArticleCreate(
                **{
                    **_source_article("duone", "Delhi University Professor Murdered in Locked Flat", "NDTV").__dict__,
                    "primary_category": "India",
                    "description": "Delhi University professor Debosmita Paul found in locked flat.",
                }
            ),
            ArticleCreate(
                **{
                    **_source_article("dutwo", "Delhi University Assistant Professor Murdered in East Delhi Flat", "Times Now").__dict__,
                    "primary_category": "India",
                    "description": "Assistant professor found murdered in East Delhi flat.",
                }
            ),
            ArticleCreate(
                **{
                    **_source_article("fireone", "Woman, 2 grandchildren killed in Telangana house fire; cylinder blast suspected", "Hindustan Times India").__dict__,
                    "primary_category": "India",
                    "description": "Victims found after a suspected cylinder blast and house fire.",
                }
            ),
            ArticleCreate(
                **{
                    **_source_article("collapseone", "Delhi Wall Collapse: 1 Dead, 2 Injured During Excavation Work", "Times Now").__dict__,
                    "primary_category": "India",
                    "description": "Worker killed and two injured during excavation work in Delhi.",
                }
            ),
            ArticleCreate(
                **{
                    **_source_article("gkfire", "Delhi GK-1 Fire: Battery Room Explosion Injures Two Firefighters", "Indian Express").__dict__,
                    "primary_category": "India",
                    "description": "Battery room fire injured two firefighters in Delhi.",
                }
            ),
        ],
    )
    session.commit()

    service = StoryClusteringService(repo)
    drafts = service._build_drafts(repo.articles_for_story_clustering(session, date(2026, 6, 1)))
    du_draft = next(draft for draft in drafts if any(article.url_hash == "duone" for article in draft.articles))

    assert {article.url_hash for article in du_draft.articles} == {"duone", "dutwo"}
    assert all(
        article.url_hash not in {"fireone", "collapseone", "gkfire"}
        for article in du_draft.articles
    )


def test_story_clustering_rejects_related_business_reaction(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(
        session,
        [
            ArticleCreate(
                **{
                    **_source_article("rbicutone", "RBI cuts repo rate by 25 bps", "Source One").__dict__,
                    "description": "The Reserve Bank of India lowered the repo rate after the MPC meeting.",
                    "content": "The Reserve Bank of India cut the repo rate by 25 bps to support growth.",
                }
            ),
            ArticleCreate(
                **{
                    **_source_article("rbicuttwo", "Reserve Bank lowers repo rate to 5.75%", "Source Two").__dict__,
                    "description": "RBI lowered the repo rate to 5.75 percent after its policy meeting.",
                    "content": "The monetary policy committee voted to reduce the repo rate.",
                }
            ),
            ArticleCreate(
                **{
                    **_source_article("sensexrise", "Sensex rises after RBI rate cut", "Source Three").__dict__,
                    "description": "Bank stocks rose after investors reacted to the RBI policy decision.",
                    "content": "Markets gained after the Reserve Bank of India cut the repo rate.",
                }
            ),
        ],
    )
    session.commit()

    service = StoryClusteringService(repo)
    drafts = service._build_drafts(repo.articles_for_story_clustering(session, date(2026, 6, 1)))
    rbi_draft = next(draft for draft in drafts if any(article.url_hash == "rbicutone" for article in draft.articles))

    assert {article.url_hash for article in rbi_draft.articles} == {"rbicutone", "rbicuttwo"}
    assert all(article.url_hash != "sensexrise" for article in rbi_draft.articles)

    payload = service._cluster_payload(rbi_draft)
    fingerprint = json.loads(payload["cluster_fingerprint"])

    assert payload["confidence_score"] >= 0.90
    assert fingerprint["event_type"] == "monetary_policy"
    assert fingerprint["object"] == "repo rate"


def test_story_clustering_prevents_bridge_merges_with_different_people(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(
        session,
        [
            ArticleCreate(
                **{
                    **_source_article("bailone", "Court grants bail to Arjun Mehta in fraud case", "Source One").__dict__,
                    "primary_category": "India",
                    "description": "The court granted bail to Arjun Mehta in a fraud case.",
                }
            ),
            ArticleCreate(
                **{
                    **_source_article("bailtwo", "Arjun Mehta gets bail in fraud case", "Source Two").__dict__,
                    "primary_category": "India",
                    "description": "A court granted bail to Arjun Mehta after a hearing in the fraud case.",
                }
            ),
            ArticleCreate(
                **{
                    **_source_article("bailthree", "Court grants bail to Rohan Sharma in fraud case", "Source Three").__dict__,
                    "primary_category": "India",
                    "description": "The court granted bail to Rohan Sharma in a separate fraud case.",
                }
            ),
        ],
    )
    session.commit()

    service = StoryClusteringService(repo)
    drafts = service._build_drafts(repo.articles_for_story_clustering(session, date(2026, 6, 1)))
    arjun_draft = next(draft for draft in drafts if any(article.url_hash == "bailone" for article in draft.articles))
    rohan_draft = next(draft for draft in drafts if any(article.url_hash == "bailthree" for article in draft.articles))

    assert {article.url_hash for article in arjun_draft.articles} == {"bailone", "bailtwo"}
    assert {article.url_hash for article in rohan_draft.articles} == {"bailthree"}


def test_story_cluster_storage_includes_seed_fingerprint_and_confidence(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(
        session,
        [
            _source_article("storedone", "RBI keeps repo rate unchanged after policy meeting", "Source One"),
            _source_article("storedtwo", "RBI repo rate unchanged after monetary policy meeting", "Source Two"),
        ],
    )
    session.commit()

    service = StoryClusteringService(repo)
    drafts = service._build_drafts(repo.articles_for_story_clustering(session, date(2026, 6, 1)))
    repo.replace_story_clusters(session, date(2026, 6, 1), [service._cluster_payload(draft) for draft in drafts])
    session.commit()

    article = session.execute(select(NewsArticle).where(NewsArticle.url_hash == "storedone")).scalar_one()
    cluster = repo.list_story_clusters(session, date(2026, 6, 1), min_source_count=2, min_confidence=0.90)[0]

    assert article.event_fingerprint is not None
    assert cluster.seed_article_id == article.id
    assert cluster.cluster_fingerprint is not None
    assert cluster.confidence_score >= 0.90
    assert cluster.first_published_at is not None


def test_live_story_rebuild_merges_new_sources_into_existing_story(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    window_start = datetime(2026, 6, 5, 8, tzinfo=timezone.utc)
    window_end = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
    repo.upsert_many(
        session,
        [
            ArticleCreate(
                **{
                    **_source_article("mergeone", "RBI keeps repo rate unchanged after policy meeting", "Source One").__dict__,
                    "published_at": datetime(2026, 6, 5, 10, tzinfo=timezone.utc),
                    "collected_for_date": date(2026, 6, 5),
                }
            )
        ],
    )
    session.commit()
    service = StoryClusteringService(repo)
    _rebuild_live_clusters(session, repo, service, date(2026, 6, 5), window_start, window_end)

    repo.upsert_many(
        session,
        [
            ArticleCreate(
                **{
                    **_source_article("mergetwo", "RBI repo rate unchanged after monetary policy meeting", "Source Two").__dict__,
                    "published_at": datetime(2026, 6, 5, 11, tzinfo=timezone.utc),
                    "collected_for_date": date(2026, 6, 5),
                }
            )
        ],
    )
    session.commit()
    _rebuild_live_clusters(session, repo, service, date(2026, 6, 5), window_start, window_end)
    clusters = repo.list_story_clusters(session, None, published_after=window_start, published_before=window_end, min_source_count=2)

    assert len(clusters) == 1
    assert clusters[0].source_count == 2


def test_live_story_rebuild_excludes_expired_articles(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    cutoff = datetime(2026, 6, 5, 8, tzinfo=timezone.utc)
    window_end = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
    repo.upsert_many(
        session,
        [
            ArticleCreate(
                **{
                    **_source_article("expiredcluster", "RBI keeps repo rate unchanged after policy meeting", "Source One").__dict__,
                    "published_at": datetime(2026, 6, 5, 7, 59, tzinfo=timezone.utc),
                    "collected_for_date": date(2026, 6, 5),
                }
            ),
            ArticleCreate(
                **{
                    **_source_article("livecluster", "RBI repo rate unchanged after monetary policy meeting", "Source Two").__dict__,
                    "published_at": datetime(2026, 6, 5, 9, tzinfo=timezone.utc),
                    "collected_for_date": date(2026, 6, 5),
                }
            ),
        ],
    )
    session.commit()

    service = StoryClusteringService(repo)
    _rebuild_live_clusters(session, repo, service, date(2026, 6, 5), cutoff, window_end)
    clusters = repo.list_story_clusters(session, None, published_after=cutoff, published_before=window_end)
    cluster_articles = repo.story_cluster_articles(session, clusters[0].id)

    assert [article.url_hash for article in cluster_articles] == ["livecluster"]


def test_live_story_rebuild_clusters_across_date_boundaries(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    window_start = datetime(2026, 6, 4, 12, tzinfo=timezone.utc)
    window_end = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
    repo.upsert_many(
        session,
        [
            ArticleCreate(
                **{
                    **_source_article("crossone", "RBI keeps repo rate unchanged after policy meeting", "Source One").__dict__,
                    "published_at": datetime(2026, 6, 4, 23, 30, tzinfo=timezone.utc),
                    "collected_for_date": date(2026, 6, 4),
                }
            ),
            ArticleCreate(
                **{
                    **_source_article("crosstwo", "RBI repo rate unchanged after monetary policy meeting", "Source Two").__dict__,
                    "published_at": datetime(2026, 6, 5, 0, 30, tzinfo=timezone.utc),
                    "collected_for_date": date(2026, 6, 5),
                }
            ),
        ],
    )
    session.commit()

    service = StoryClusteringService(repo)
    _rebuild_live_clusters(session, repo, service, date(2026, 6, 5), window_start, window_end)
    clusters = repo.list_story_clusters(session, None, published_after=window_start, published_before=window_end, min_source_count=2)

    assert len(clusters) == 1
    assert clusters[0].source_count == 2


def test_live_story_service_rebuild_uses_window_not_collected_date(tmp_path, monkeypatch):
    session = _session(tmp_path)
    repo = ArticleRepository()
    window_start = datetime(2026, 6, 5, 2, tzinfo=timezone.utc)
    window_end = datetime(2026, 6, 6, 2, tzinfo=timezone.utc)
    repo.upsert_many(
        session,
        [
            ArticleCreate(
                **{
                    **_source_article("previousdayone", "RBI keeps repo rate unchanged after policy meeting", "Source One").__dict__,
                    "published_at": datetime(2026, 6, 5, 15, tzinfo=timezone.utc),
                    "collected_for_date": date(2026, 6, 5),
                }
            ),
            ArticleCreate(
                **{
                    **_source_article("previousdaytwo", "RBI repo rate unchanged after monetary policy meeting", "Source Two").__dict__,
                    "published_at": datetime(2026, 6, 5, 15, 30, tzinfo=timezone.utc),
                    "collected_for_date": date(2026, 6, 5),
                }
            ),
        ],
    )
    session.commit()

    @contextmanager
    def scope():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr("news_app.services.story_clustering.session_scope", scope)

    service = StoryClusteringService(repo)
    count = service.rebuild_for_window(date(2026, 6, 6), window_start, window_end)
    clusters = repo.list_story_clusters(session, None, published_after=window_start, published_before=window_end, min_source_count=2)

    assert count == 1
    assert len(clusters) == 1
    assert clusters[0].collected_for_date == date(2026, 6, 6)
    assert clusters[0].source_count == 2


def test_recently_viewed_articles_are_user_scoped_and_recent_first(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(
        session,
        [
            _article("viewedone", "First viewed story"),
            _article("viewedtwo", "Second viewed story"),
        ],
    )
    user = repo.create_user(session, "reader")
    other = repo.create_user(session, "other-reader")
    session.commit()
    articles = {article.headline: article for article in repo.list_articles(session, date(2026, 6, 1))}

    repo.record_event(session, user.id, articles["First viewed story"].id, "view")
    session.commit()
    repo.record_event(session, user.id, articles["Second viewed story"].id, "view")
    repo.record_event(session, other.id, articles["First viewed story"].id, "view")
    session.commit()

    viewed = repo.recently_viewed_articles(session, user.id, limit=5)

    assert [article.headline for article in viewed] == ["Second viewed story", "First viewed story"]


def test_trending_cards_prioritize_multi_source_without_engagement(tmp_path, monkeypatch):
    session = _session(tmp_path)
    repo = ArticleRepository()
    repo.upsert_many(
        session,
        [
            _source_article("trendone", "RBI keeps repo rate unchanged after policy meeting", "Source One"),
            _source_article("trendtwo", "RBI repo rate unchanged after monetary policy meeting", "Source Two"),
            ArticleCreate(
                **{
                    **_article("trendthree", "Finance ministry announces market reform package", "Business").__dict__,
                    "source_name": "Single Source",
                    "source_type": "newspaper",
                    "description": "The policy package includes market reforms and banking updates.",
                }
            ),
        ],
    )
    session.commit()
    articles = {article.headline: article for article in repo.list_articles(session, date(2026, 6, 1))}
    for _ in range(20):
        repo.record_event(session, None, articles["Finance ministry announces market reform package"].id, "view")
    session.commit()
    clustering = StoryClusteringService(repo)
    drafts = clustering._build_drafts(repo.articles_for_story_clustering(session, date(2026, 6, 1)))
    repo.replace_story_clusters(session, date(2026, 6, 1), [clustering._cluster_payload(draft) for draft in drafts])
    session.commit()

    @contextmanager
    def scope():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr("news_app.services.news_service.session_scope", scope)
    service = NewsService(Settings(llm_provider="disabled", require_llm=False), repository=repo)
    cards = service.trending_cards(
        date(2026, 6, 1),
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 6, 2, tzinfo=timezone.utc),
        limit=2,
    )

    assert cards[0]["kind"] == "story"
    assert cards[0]["source_count"] == 2
    assert "RBI" in cards[0]["headline"]
    assert cards[0]["story_path"] == f"/stories/{cards[0]['id']}"
    assert cards[0]["article_path"] == cards[0]["story_path"]
    assert cards[0]["primary_article_path"].startswith("/articles/")
    assert cards[1]["kind"] == "article"


def test_repository_upserts_and_searches_government_notices(tmp_path):
    session = _session(tmp_path)
    repo = ArticleRepository()
    notice = NoticeCreate(
        url_hash="notice-one",
        document_url="https://example.gov.in/notice-one.pdf",
        feed_url="https://example.gov.in/rss.xml",
        source_url="https://example.gov.in/",
        source_name="Example Government",
        source_country="India",
        source_region="Government",
        source_type="government",
        language="en",
        document_type="Circular",
        title="New scholarship circular published",
        description="Official scholarship rules for schools.",
        content="Official scholarship rules for schools.",
        thumbnail_path="/static/fallbacks/notice.svg",
        published_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    inserted, updated = repo.upsert_notices(session, [notice])
    session.commit()

    assert (inserted, updated) == (1, 0)
    assert repo.notice_sources(session) == ["Example Government"]
    assert repo.notice_document_types(session) == ["Circular"]
    assert repo.notice_stats(session) == {"total": 1, "sources": 1}
    assert [item.title for item in repo.list_notices(session, query="scholarship")] == [
        "New scholarship circular published"
    ]

    inserted, updated = repo.upsert_notices(
        session,
        [
            NoticeCreate(
                **{
                    **notice.__dict__,
                    "title": "Updated scholarship circular published",
                }
            )
        ],
    )
    session.commit()

    assert (inserted, updated) == (0, 1)
    assert repo.list_notices(session)[0].title == "Updated scholarship circular published"
