from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from news_app.time_window import rolling_utc_window
from news_app.web import routes


def test_news_routes_use_rolling_window_filters(monkeypatch):
    window = rolling_utc_window("Asia/Kolkata", now=datetime(2026, 6, 5, 12, tzinfo=timezone.utc))
    calls = []

    class FakeNewsService:
        def __init__(self, *args, **kwargs):
            pass

        def ensure_user(self, request):
            return SimpleNamespace(id=1)

        def attach_user_cookie(self, response, current_user):
            pass

        def list_articles(self, target_date, published_after=None, published_before=None, **kwargs):
            calls.append(("articles", target_date, published_after, published_before))
            return []

        def list_story_clusters(self, target_date, published_after=None, published_before=None, **kwargs):
            calls.append(("clusters", target_date, published_after, published_before, kwargs.get("min_source_count")))
            return []

        def trending_cards(self, target_date=None, published_after=None, published_before=None, **kwargs):
            calls.append(("trending", target_date, published_after, published_before, kwargs.get("limit")))
            return []

        def categories(self, target_date=None, published_after=None, published_before=None):
            calls.append(("categories", target_date, published_after, published_before))
            return []

        def recently_viewed_articles(self, current_user, limit=5):
            calls.append(("viewed", None, None, None, limit))
            return []

        def get_story_cluster(self, cluster_id, current_user=None, record_view=False):
            calls.append(("cluster_detail", cluster_id, None, None))
            return {"id": cluster_id}

    monkeypatch.setattr(routes, "_recent_window", lambda: window)
    monkeypatch.setattr(routes, "NewsService", FakeNewsService)
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    assert client.get("/api/articles").status_code == 200
    assert client.get("/api/story-clusters").status_code == 200
    assert client.get("/api/story-clusters/123").status_code == 200
    assert client.get("/api/articles/trending").status_code == 200
    assert client.get("/api/articles/viewed?limit=50").status_code == 200
    assert client.get("/api/categories").status_code == 200

    by_name = {call[0]: call for call in calls}
    assert by_name["articles"][1] is None
    assert by_name["articles"][2] == window.start_utc
    assert by_name["articles"][3] == window.end_utc
    assert by_name["clusters"][1] is None
    assert by_name["clusters"][2] == window.start_utc
    assert by_name["clusters"][4] == 2
    assert by_name["trending"][1] is None
    assert by_name["trending"][2] == window.start_utc
    assert by_name["trending"][4] == 10
    assert by_name["viewed"][4] == 20
    assert by_name["cluster_detail"][1] == 123
    assert by_name["categories"][1] is None
    assert by_name["categories"][2] == window.start_utc
