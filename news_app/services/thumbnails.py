from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import logging
from pathlib import Path

import httpx

from news_app.config import Settings

logger = logging.getLogger(__name__)

try:
    from PIL import Image
except ImportError:  # pragma: no cover - exercised only when Pillow is not installed
    Image = None


def cache_thumbnail(settings: Settings, image_url: str | None, unique_hint: str) -> tuple[str, datetime] | None:
    if not image_url or Image is None:
        return None

    try:
        with httpx.Client(
            timeout=settings.article_fetch_timeout_seconds,
            headers={"User-Agent": settings.request_user_agent},
            follow_redirects=True,
        ) as client:
            response = client.get(image_url)
            response.raise_for_status()

        image = Image.open(BytesIO(response.content))
        image.thumbnail((settings.thumbnail_max_width, settings.thumbnail_max_height))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")

        cache_dir = Path(settings.thumbnail_cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{unique_hint[:16]}.jpg"
        output_path = cache_dir / filename
        image.save(output_path, format="JPEG", quality=settings.thumbnail_quality, optimize=True)
        return f"/static/thumbnails/{filename}", datetime.now(timezone.utc)
    except Exception:
        logger.info("Thumbnail caching failed for %s", image_url, exc_info=True)
        return None
