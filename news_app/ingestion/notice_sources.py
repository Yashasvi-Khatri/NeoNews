from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class NoticeSource:
    name: str
    country: str
    region: str
    language: str
    type: str
    document_type: str
    homepage: str
    feed_url: str
    mode: str = "rss"
    enabled: bool = True


def load_notice_sources(path: Path) -> list[NoticeSource]:
    if not path.exists():
        raise FileNotFoundError(f"Notice sources file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    sources: list[NoticeSource] = []
    for raw in payload.get("sources", []):
        sources.append(
            NoticeSource(
                name=str(raw["name"]),
                country=str(raw.get("country", "India")),
                region=str(raw.get("region", "Government")),
                language=str(raw.get("language", "en")),
                type=str(raw.get("type", "government")),
                document_type=str(raw.get("document_type", "Notice")),
                homepage=str(raw["homepage"]),
                feed_url=str(raw["feed_url"]),
                mode=str(raw.get("mode", "rss")),
                enabled=bool(raw.get("enabled", True)),
            )
        )
    return sources
