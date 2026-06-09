import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Local News Aggregator"
    app_timezone: str = "Asia/Kolkata"
    # On Vercel, SQLite lives in /tmp and is ephemeral (reset on each cold start).
    # For a persistent production deployment, set DATABASE_URL to a hosted Postgres or
    # Turso/libSQL URL and swap the SQLAlchemy engine accordingly.
    database_url: str = (
        "sqlite:////tmp/news.db"
        if (os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
        else "sqlite:///./data/news.db"
    )
    sources_file: Path = Path("config/sources.json")
    notice_sources_file: Path = Path("config/notice_sources.json")

    # Direct local MLX inference. No Ollama server is required or used.
    llm_provider: str = Field(default="disabled", pattern="^(mlx|openai_compatible|disabled)$")
    llm_model: str = "google/gemma-4-31b-it:free"
    mlx_engine: str = Field(default="vlm", pattern="^(auto|lm|vlm)$")
    require_llm: bool = False
    llm_batch_size: int = 8
    headline_input_chars: int = 2200
    summary_input_chars: int = 4500
    mlx_max_kv_size: int = 4096
    mlx_temperature: float = 0.2
    mlx_top_p: float = 0.9
    mlx_no_think: bool = True
    mlx_warmup_prompt: str = "Write one short news headline."
    openai_compatible_base_url: str = "https://openrouter.ai/api/v1"
    openai_compatible_api_key: str = ""
    openai_compatible_referer: str = "https://your-app.vercel.app"
    openai_compatible_title: str = "NewsApp"
    llm_timeout_seconds: int = 60

    request_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    )
    feed_fetch_timeout_seconds: int = 20
    article_fetch_timeout_seconds: int = 20
    feed_fetch_concurrency: int = 8
    article_fetch_concurrency: int = 12
    max_articles_per_run: int = 250
    # Refresh now performs a complete recent-news run. Load More only paginates saved rows.
    ingestion_batch_size: int = 25
    important_story_min_score: float = 5.2
    important_multi_source_min_score: float = 3.8
    important_story_max_cards: int = 250
    process_all_articles_per_important_story: bool = True
    story_supporting_articles_per_card: int = 3
    reading_words_per_minute: int = 225
    category_llm_enabled: bool = False
    category_llm_threshold: float = 0.42
    offline_article_limit: int = 20
    public_base_url: str = ""
    session_cookie_name: str = "news_user"
    session_secret: str = "local-news-aggregator-dev-secret"
    session_max_age_days: int = 180
    thumbnail_cache_dir: Path = Path("news_app/web/static/thumbnails")
    thumbnail_max_width: int = 480
    thumbnail_max_height: int = 270
    thumbnail_quality: int = 72
    cache_thumbnails_during_ingestion: bool = True

    scheduler_enabled: bool = True
    ingestion_hour: int = 6
    ingestion_minute: int = 30
    auto_ingest_on_startup: bool = False

    @field_validator("ingestion_hour")
    @classmethod
    def validate_hour(cls, value: int) -> int:
        if not 0 <= value <= 23:
            raise ValueError("ingestion_hour must be between 0 and 23")
        return value

    @field_validator("ingestion_minute")
    @classmethod
    def validate_minute(cls, value: int) -> int:
        if not 0 <= value <= 59:
            raise ValueError("ingestion_minute must be between 0 and 59")
        return value

    @field_validator("max_articles_per_run")
    @classmethod
    def validate_max_articles(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_articles_per_run must be positive")
        return value

    @field_validator(
        "reading_words_per_minute",
        "offline_article_limit",
        "session_max_age_days",
        "llm_batch_size",
        "headline_input_chars",
        "summary_input_chars",
        "mlx_max_kv_size",
        "feed_fetch_concurrency",
        "article_fetch_concurrency",
        "ingestion_batch_size",
        "important_story_max_cards",
        "story_supporting_articles_per_card",
    )
    @classmethod
    def validate_positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("value must be positive")
        return value

    @field_validator("category_llm_threshold", "mlx_temperature", "mlx_top_p")
    @classmethod
    def validate_probability_float(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("value must be between 0 and 1")
        return value

    @field_validator("important_story_min_score", "important_multi_source_min_score")
    @classmethod
    def validate_positive_float(cls, value: float) -> float:
        if value < 0:
            raise ValueError("value must be non-negative")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
