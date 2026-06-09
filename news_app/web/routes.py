from __future__ import annotations

from datetime import date, datetime
import os
from pathlib import Path
import threading
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from news_app.config import get_settings
from news_app.db import session_scope
from news_app.db.exceptions import DatabaseOperationError
from news_app.db.repository import ArticleRepository
from news_app.ingestion.rss_client import RSSClient
from news_app.ingestion.sources import Source, load_sources, save_sources
from news_app.llm import LLMGenerationError
from news_app.services.ingestion_service import (
    recent_ingestion_store,
    run_recent_ingestion,
)
from news_app.services.ingestion_status import ingestion_status_store
from news_app.services.news_service import ArticleNotFoundError, NewsService, ShareLinkNotFoundError, StoryClusterNotFoundError
from news_app.services.story_clustering import MULTI_SOURCE_CONFIDENCE_THRESHOLD
from news_app.services.story_clustering import StoryClusteringService
from news_app.services.notice_ingestion_service import run_notice_ingestion
from news_app.time_window import rolling_utc_window

router = APIRouter()
template_dir = Path(__file__).resolve().parent / "templates"
static_dir = Path(__file__).resolve().parent / "static"
frontend_build_dir = Path(__file__).resolve().parents[2] / "frontend" / "build"
templates = Jinja2Templates(directory=str(template_dir))
_ingest_lock = threading.Lock()
TRENDING_CARD_LIMIT = 10
VIEWED_CARD_LIMIT = 20


@router.get("/api/health")
def health_check():
    """Health check endpoint for monitoring serverless function status."""
    return {
        "status": "ok",
        "environment": "serverless" if os.environ.get("VERCEL") else "local",
        "llm_provider": os.environ.get("LLM_PROVIDER", "not set")
    }


@router.get("/")
def root():
    """Root endpoint for basic connectivity test."""
    return {"message": "NeoNews API is running", "health": "/api/health"}


class EventPayload(BaseModel):
    article_id: Optional[int] = None
    event_type: str
    event_value: Optional[str] = None


class SharePayload(BaseModel):
    platform: Optional[str] = None


class IngestDatePayload(BaseModel):
    date: str


class SourcePayload(BaseModel):
    name: str
    country: str
    region: str
    language: str = "en"
    type: str = "publisher"
    homepage: str
    feed_url: str
    enabled: bool = True


class PreferencesPayload(BaseModel):
    categories: list[str] = []
    sources: list[str] = []
    regions: list[str] = []


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    source: Optional[str] = None,
    region: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "date",
    view: str = "latest",
):
    app_shell = _svelte_app_shell()
    if app_shell is not None:
        response = FileResponse(app_shell, media_type="text/html")
        service = NewsService(get_settings())
        current_user = service.ensure_user(request)
        service.attach_user_cookie(response, current_user)
        return response

    settings = get_settings()
    service = NewsService(settings)
    current_user = service.ensure_user(request)
    window = _recent_window()
    target_date = None
    sort = _normalize_sort(sort)
    view = _normalize_view(view)
    try:
        articles = service.list_articles(
            target_date,
            published_after=None if view == "read_later" else window.start_utc,
            published_before=None if view == "read_later" else window.end_utc,
            source=source,
            region=region,
            category=category,
            query=q,
            sort=sort,
            current_user=current_user,
            view=view,
        )
        facets = service.facets()
        categories = service.categories(
            target_date,
            published_after=window.start_utc,
            published_before=window.end_utc,
        )
        stats = service.stats_for_window(window.start_utc, window.end_utc)
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    response = templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "target_date": window.collected_date.isoformat(),
            "articles": articles,
            "sources": facets["sources"],
            "regions": facets["regions"],
            "categories": categories,
            "selected_source": source or "",
            "selected_region": region or "",
            "selected_category": category or "",
            "selected_sort": sort,
            "selected_view": view,
            "query": q or "",
            "stats": stats,
            "current_user": current_user,
        },
    )
    service.attach_user_cookie(response, current_user)
    return response


@router.get("/articles/{article_id}/{slug}", response_class=HTMLResponse)
def article_page(request: Request, article_id: int, slug: str):
    app_shell = _svelte_app_shell()
    if app_shell is not None:
        service = NewsService(get_settings())
        current_user = service.ensure_user(request)
        try:
            service.record_article_event("view", article_id, current_user)
        except (ArticleNotFoundError, DatabaseOperationError):
            pass
        response = FileResponse(app_shell, media_type="text/html")
        service.attach_user_cookie(response, current_user)
        return response

    settings = get_settings()
    service = NewsService(settings)
    current_user = service.ensure_user(request)
    try:
        article = service.get_article(article_id, current_user=current_user, include_content=False)
        service.record_article_event("view", article_id, current_user)
    except ArticleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    response = templates.TemplateResponse(
        "article.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "article": article,
            "current_user": current_user,
        },
    )
    service.attach_user_cookie(response, current_user)
    return response


@router.get("/stories/{cluster_id}", response_class=HTMLResponse)
def story_page(request: Request, cluster_id: int):
    app_shell = _svelte_app_shell()
    if app_shell is not None:
        response = FileResponse(app_shell, media_type="text/html")
        service = NewsService(get_settings())
        current_user = service.ensure_user(request)
        service.attach_user_cookie(response, current_user)
        return response

    return RedirectResponse("/", status_code=302)


@router.get("/api/articles")
def api_articles(
    request: Request,
    response: Response,
    source: Optional[str] = None,
    region: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "date",
    view: str = "latest",
    limit: int = 100,
    offset: int = 0,
    exclude_clustered: bool = False,
):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    view = _normalize_view(view)
    window = _recent_window()
    target_date = None
    published_after = None if view == "read_later" else window.start_utc
    published_before = None if view == "read_later" else window.end_utc
    try:
        requested_limit = max(1, min(limit, 100))
        article_items = service.list_articles(
            target_date,
            published_after=published_after,
            published_before=published_before,
            source=source,
            region=region,
            category=category,
            query=q,
            sort=_normalize_sort(sort),
            limit=requested_limit + 1,
            offset=offset,
            current_user=current_user,
            view=view,
            exclude_clustered=exclude_clustered,
        )
        return {
            "date": window.collected_date.isoformat() if view != "read_later" else None,
            "window_start": published_after.isoformat() if published_after else None,
            "window_end": published_before.isoformat() if published_before else None,
            "view": view,
            "limit": requested_limit,
            "offset": max(0, offset),
            "has_more": len(article_items) > requested_limit,
            "articles": article_items[:requested_limit],
        }
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/story-clusters")
def api_story_clusters(
    request: Request,
    response: Response,
    source: Optional[str] = None,
    region: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    window = _recent_window()
    requested_limit = max(1, min(limit, 50))
    try:
        clusters = service.list_story_clusters(
            None,
            published_after=window.start_utc,
            published_before=window.end_utc,
            min_source_count=2,
            min_confidence=MULTI_SOURCE_CONFIDENCE_THRESHOLD,
            source=source,
            region=region,
            category=category,
            query=q,
            limit=requested_limit + 1,
            offset=offset,
            current_user=current_user,
        )
        return {
            "date": window.collected_date.isoformat(),
            "window_start": window.start_utc.isoformat(),
            "window_end": window.end_utc.isoformat(),
            "limit": requested_limit,
            "offset": max(0, offset),
            "has_more": len(clusters) > requested_limit,
            "clusters": clusters[:requested_limit],
        }
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/story-clusters/debug/{article_id}")
def api_story_clustering_debug(article_id: int):
    service = StoryClusteringService()
    try:
        return service.debug_article(article_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/story-clusters/rebuild")
def api_rebuild_story_clusters(payload: IngestDatePayload):
    service = NewsService()
    try:
        target_date = datetime.strptime(payload.date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    try:
        return service.rebuild_story_clusters(target_date)
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/story-clusters/{cluster_id}/summary")
def api_story_cluster_summary(request: Request, response: Response, cluster_id: int):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return service.summarize_story_cluster(cluster_id, current_user=current_user)
    except StoryClusterNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMGenerationError as exc:
        raise HTTPException(status_code=503, detail=f"Local LLM unavailable: {exc}") from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/story-clusters/{cluster_id}")
def api_story_cluster(request: Request, response: Response, cluster_id: int):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return service.get_story_cluster(cluster_id, current_user=current_user, record_view=True)
    except StoryClusterNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/articles/trending")
def api_trending_articles(request: Request, response: Response, limit: int = TRENDING_CARD_LIMIT):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        window = _recent_window()
        requested_limit = max(1, min(limit, TRENDING_CARD_LIMIT))
        return {
            "limit": requested_limit,
            "articles": service.trending_cards(
                limit=requested_limit,
                target_date=None,
                published_after=window.start_utc,
                published_before=window.end_utc,
                current_user=current_user,
            )
        }
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/articles/viewed")
def api_recently_viewed_articles(request: Request, response: Response, limit: int = VIEWED_CARD_LIMIT):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        requested_limit = max(1, min(limit, VIEWED_CARD_LIMIT))
        return {"limit": requested_limit, "articles": service.recently_viewed_articles(current_user, limit=requested_limit)}
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/articles/{article_id}")
def api_article(request: Request, response: Response, article_id: int):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return service.get_article(article_id, current_user=current_user, include_content=False)
    except ArticleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/articles/{article_id}/related")
def api_related_articles(request: Request, response: Response, article_id: int, limit: int = 6):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return {"articles": service.related_articles(article_id, limit=limit, current_user=current_user)}
    except ArticleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/articles/{article_id}/summary")
def api_summary(request: Request, response: Response, article_id: int):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return service.summarize_article(article_id, current_user=current_user)
    except ArticleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMGenerationError as exc:
        raise HTTPException(status_code=503, detail=f"Local LLM unavailable: {exc}") from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/categories")
def api_categories():
    service = NewsService()
    window = _recent_window()
    try:
        return {
            "categories": service.categories(
                None,
                published_after=window.start_utc,
                published_before=window.end_utc,
            )
        }
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/facets")
def api_facets():
    service = NewsService()
    try:
        return service.facets()
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/bookmarks")
def api_bookmarks(request: Request, response: Response, limit: int = 100, offset: int = 0):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return {
            "articles": service.list_articles(
                None,
                sort="date",
                limit=limit,
                offset=offset,
                current_user=current_user,
                view="read_later",
            )
        }
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/bookmarks/{article_id}")
def api_bookmark_article(request: Request, response: Response, article_id: int):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return service.bookmark_article(article_id, current_user)
    except ArticleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/api/bookmarks/{article_id}")
def api_remove_bookmark(request: Request, response: Response, article_id: int):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return service.remove_bookmark(article_id, current_user)
    except ArticleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/articles/{article_id}/share")
def api_share_article(
    request: Request,
    response: Response,
    article_id: int,
    payload: Optional[SharePayload] = None,
):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return service.create_share(article_id, current_user, _base_url(request), payload.platform if payload else None)
    except ArticleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/s/{token}")
def share_redirect(token: str):
    service = NewsService()
    try:
        return RedirectResponse(service.resolve_share(token), status_code=302)
    except ShareLinkNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ArticleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/events")
def api_events(request: Request, response: Response, payload: EventPayload):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return service.record_article_event(payload.event_type, payload.article_id, current_user, payload.event_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ArticleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/offline/top")
def api_offline_top(request: Request, response: Response, limit: int = 20):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return {"articles": service.offline_top(limit, current_user=current_user)}
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/me")
def api_me(request: Request, response: Response):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    return {
        "public_id": current_user.public_id,
        "is_anonymous": current_user.is_anonymous,
        "email": current_user.email,
        "display_name": current_user.display_name,
    }


@router.get("/api/preferences")
def api_preferences(request: Request, response: Response):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return service.preferences(current_user)
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/api/preferences")
def api_save_preferences(request: Request, response: Response, payload: PreferencesPayload):
    service = NewsService()
    current_user = service.ensure_user(request)
    service.attach_user_cookie(response, current_user)
    try:
        return service.save_preferences(
            current_user,
            {
                "categories": payload.categories,
                "sources": payload.sources,
                "regions": payload.regions,
            },
        )
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/notices")
def api_notices(
    source: Optional[str] = None,
    document_type: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    service = NewsService()
    requested_limit = max(1, min(limit, 100))
    try:
        notices = service.list_notices(
            source=source,
            document_type=document_type,
            query=q,
            limit=requested_limit + 1,
            offset=offset,
        )
        return {
            "limit": requested_limit,
            "offset": max(0, offset),
            "has_more": len(notices) > requested_limit,
            "notices": notices[:requested_limit],
        }
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/notices/meta")
def api_notice_meta():
    service = NewsService()
    try:
        return service.notice_meta()
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/notices/ingest")
def api_ingest_notices():
    try:
        stats = run_notice_ingestion()
        return {
            "sources_total": stats.sources_total,
            "sources_enabled": stats.sources_enabled,
            "sources_failed": stats.sources_failed,
            "items_seen": stats.items_seen,
            "prepared": stats.prepared,
            "inserted": stats.inserted,
            "updated": stats.updated,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/ingest/recent")
def api_ingest_recent(background_tasks: BackgroundTasks):
    run_id = ingestion_status_store.start()
    if run_id is None:
        return {"status": "already_running", **ingestion_status_store.snapshot()}
    background_tasks.add_task(_locked_ingest_recent, run_id)
    return {"status": "accepted", "run_id": run_id}


@router.post("/api/ingest/recent/more")
def api_ingest_recent_more():
    return {
        "status": "disabled",
        "message": "Refresh now processes the full important-news run. Use paginated GET /api/story-clusters or /api/articles for Load More.",
        **recent_ingestion_store.snapshot(),
    }


@router.post("/api/ingest/yesterday")
def api_ingest_yesterday(background_tasks: BackgroundTasks):
    return api_ingest_recent(background_tasks)


@router.get("/api/ingest/status")
def api_ingest_status():
    status = ingestion_status_store.snapshot()
    recent = recent_ingestion_store.snapshot()
    if recent.get("run_id") is None:
        recent.pop("run_id", None)
    return {**status, **recent}


@router.get("/api/sources")
def api_sources():
    settings = get_settings()
    return {"sources": [_source_to_dict(source) for source in load_sources(settings.sources_file)]}


@router.post("/api/sources")
def api_create_source(payload: SourcePayload):
    settings = get_settings()
    sources = load_sources(settings.sources_file)
    if any(source.name == payload.name for source in sources):
        raise HTTPException(status_code=409, detail="Source name already exists")
    sources.append(_payload_to_source(payload))
    save_sources(settings.sources_file, sources)
    return {"source": payload.model_dump()}


@router.put("/api/sources/{source_name}")
def api_update_source(source_name: str, payload: SourcePayload):
    settings = get_settings()
    sources = load_sources(settings.sources_file)
    index = _source_index(sources, source_name)
    if index is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if payload.name != source_name and any(source.name == payload.name for source in sources):
        raise HTTPException(status_code=409, detail="Source name already exists")
    sources[index] = _payload_to_source(payload)
    save_sources(settings.sources_file, sources)
    return {"source": payload.model_dump()}


@router.delete("/api/sources/{source_name}")
def api_disable_source(source_name: str):
    settings = get_settings()
    sources = load_sources(settings.sources_file)
    index = _source_index(sources, source_name)
    if index is None:
        raise HTTPException(status_code=404, detail="Source not found")
    source = sources[index]
    disabled = Source(
        name=source.name,
        country=source.country,
        region=source.region,
        language=source.language,
        type=source.type,
        homepage=source.homepage,
        feed_url=source.feed_url,
        enabled=False,
    )
    sources[index] = disabled
    save_sources(settings.sources_file, sources)
    return {"source": _source_to_dict(disabled)}


@router.post("/api/sources/{source_name}/test")
def api_test_source(source_name: str):
    settings = get_settings()
    sources = load_sources(settings.sources_file)
    index = _source_index(sources, source_name)
    if index is None:
        raise HTTPException(status_code=404, detail="Source not found")
    source = sources[index]
    items = RSSClient(settings).fetch(source)
    success = bool(items)
    reason = None if success else "Feed fetch failed or returned no valid items"
    repository = ArticleRepository()
    with session_scope() as session:
        repository.record_source_health(
            session,
            source.name,
            source.feed_url,
            success=success,
            items_seen=len(items),
            failure_reason=reason,
        )
    return {"success": success, "items_seen": len(items), "failure_reason": reason}


@router.get("/api/feed-health")
def api_feed_health():
    repository = ArticleRepository()
    with session_scope() as session:
        return {"sources": [_health_to_dict(health) for health in repository.source_health(session)]}


@router.get("/auth/login/google")
def auth_login_google():
    raise HTTPException(status_code=501, detail="Google OAuth is scaffolded but not configured.")


@router.get("/auth/callback/google")
def auth_callback_google():
    raise HTTPException(status_code=501, detail="Google OAuth callback is scaffolded but not configured.")


@router.get("/auth/login/apple")
def auth_login_apple():
    raise HTTPException(status_code=501, detail="Apple OAuth is scaffolded but not configured.")


@router.get("/auth/callback/apple")
def auth_callback_apple():
    raise HTTPException(status_code=501, detail="Apple OAuth callback is scaffolded but not configured.")


@router.get("/auth/logout")
def auth_logout():
    service = NewsService()
    response = RedirectResponse("/", status_code=302)
    service.clear_user_cookie(response)
    return response


@router.get("/manifest.json", include_in_schema=False)
def manifest():
    svelte_manifest = frontend_build_dir / "manifest.json"
    manifest_file = svelte_manifest if svelte_manifest.exists() else static_dir / "manifest.json"
    return FileResponse(manifest_file, media_type="application/manifest+json")


@router.get("/service-worker.js", include_in_schema=False)
def service_worker():
    svelte_worker = frontend_build_dir / "service-worker.js"
    worker = svelte_worker if svelte_worker.exists() else static_dir / "service-worker.js"
    return FileResponse(worker, media_type="application/javascript")


@router.get("/_app/{asset_path:path}", include_in_schema=False)
def svelte_assets(asset_path: str):
    target = frontend_build_dir / "_app" / asset_path
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(target)


def _locked_ingest_recent(run_id: str) -> None:
    try:
        with _ingest_lock:
            stats = run_recent_ingestion(run_id=run_id)
        ingestion_status_store.complete(stats)
    except Exception as exc:
        ingestion_status_store.fail(exc)
        raise


def _recent_window():
    settings = get_settings()
    return rolling_utc_window(settings.app_timezone)


def _normalize_sort(value: str) -> str:
    return value if value in {"relevance", "date"} else "date"


def _normalize_view(value: str) -> str:
    return value if value in {"latest", "read_later"} else "latest"


def _base_url(request: Request) -> str:
    settings = get_settings()
    return settings.public_base_url.rstrip("/") if settings.public_base_url else str(request.base_url).rstrip("/")


def _svelte_app_shell() -> Path | None:
    index = frontend_build_dir / "index.html"
    return index if index.exists() else None


def _payload_to_source(payload: SourcePayload) -> Source:
    return Source(
        name=payload.name.strip(),
        country=payload.country.strip(),
        region=payload.region.strip(),
        language=payload.language.strip() or "en",
        type=payload.type.strip() or "publisher",
        homepage=payload.homepage.strip(),
        feed_url=payload.feed_url.strip(),
        enabled=payload.enabled,
    )


def _source_to_dict(source: Source) -> dict:
    return {
        "name": source.name,
        "country": source.country,
        "region": source.region,
        "language": source.language,
        "type": source.type,
        "homepage": source.homepage,
        "feed_url": source.feed_url,
        "enabled": source.enabled,
    }


def _source_index(sources: list[Source], source_name: str) -> int | None:
    for index, source in enumerate(sources):
        if source.name == source_name:
            return index
    return None


def _health_to_dict(health) -> dict:
    return {
        "source_name": health.source_name,
        "feed_url": health.feed_url,
        "last_status": health.last_status,
        "last_checked_at": health.last_checked_at.isoformat() if health.last_checked_at else None,
        "last_success_at": health.last_success_at.isoformat() if health.last_success_at else None,
        "last_failure_at": health.last_failure_at.isoformat() if health.last_failure_at else None,
        "failure_reason": health.failure_reason,
        "items_seen": health.items_seen,
    }
