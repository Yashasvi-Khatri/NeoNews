from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class Source:
    name: str
    country: str
    region: str
    language: str
    type: str
    homepage: str
    feed_url: str
    enabled: bool = True


def load_sources(path: Path) -> list[Source]:
    if not path.exists():
        raise FileNotFoundError(f"Sources file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_sources = payload.get("sources", [])
    sources: list[Source] = []
    for raw in raw_sources:
        source = Source(
            name=str(raw["name"]),
            country=str(raw["country"]),
            region=str(raw["region"]),
            language=str(raw.get("language", "en")),
            type=str(raw.get("type", "publisher")),
            homepage=str(raw["homepage"]),
            feed_url=str(raw["feed_url"]),
            enabled=bool(raw.get("enabled", True)),
        )
        sources.append(source)
    return sources


def save_sources(path: Path, sources: list[Source]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"sources": [source.__dict__ for source in sources]}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
