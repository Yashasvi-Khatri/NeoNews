from __future__ import annotations

from dataclasses import dataclass
import math
import re
import unicodedata

from news_app.config import Settings
from news_app.llm import LLMGenerationError, LocalLLMClient

CATEGORIES = [
    "Politics",
    "Business",
    "Tech",
    "Sports",
    "Health",
    "Science",
    "Entertainment",
    "World",
    "India",
    "General",
]

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Politics": (
        "election",
        "government",
        "parliament",
        "minister",
        "president",
        "prime minister",
        "congress",
        "bjp",
        "senate",
        "policy",
        "court",
        "law",
        "bill",
        "campaign",
        "diplomacy",
    ),
    "Business": (
        "market",
        "stock",
        "economy",
        "inflation",
        "company",
        "startup",
        "profit",
        "trade",
        "bank",
        "funding",
        "investment",
        "revenue",
        "tariff",
        "rupee",
        "dollar",
    ),
    "Tech": (
        "technology",
        "ai",
        "artificial intelligence",
        "software",
        "cyber",
        "chip",
        "iphone",
        "android",
        "google",
        "apple",
        "microsoft",
        "openai",
        "startup",
        "data",
        "robot",
    ),
    "Sports": (
        "cricket",
        "football",
        "tennis",
        "olympic",
        "fifa",
        "ipl",
        "match",
        "tournament",
        "coach",
        "player",
        "score",
        "league",
        "world cup",
    ),
    "Health": (
        "health",
        "hospital",
        "doctor",
        "medicine",
        "virus",
        "vaccine",
        "disease",
        "covid",
        "mental health",
        "patient",
        "medical",
        "drug",
    ),
    "Science": (
        "science",
        "space",
        "nasa",
        "isro",
        "research",
        "climate",
        "study",
        "satellite",
        "moon",
        "mars",
        "physics",
        "discovery",
    ),
    "Entertainment": (
        "film",
        "movie",
        "actor",
        "actress",
        "bollywood",
        "hollywood",
        "music",
        "celebrity",
        "ott",
        "netflix",
        "trailer",
        "festival",
        "award",
    ),
    "World": (
        "world",
        "global",
        "international",
        "united nations",
        "war",
        "conflict",
        "russia",
        "china",
        "europe",
        "middle east",
        "africa",
        "america",
        "ukraine",
        "israel",
    ),
    "India": (
        "india",
        "delhi",
        "mumbai",
        "bengaluru",
        "kolkata",
        "chennai",
        "hyderabad",
        "maharashtra",
        "karnataka",
        "modi",
        "lok sabha",
        "rajya sabha",
        "supreme court",
    ),
}


@dataclass(frozen=True)
class CategoryResult:
    category: str
    confidence: float


def estimate_reading_time(text: str | None, words_per_minute: int = 225) -> int:
    words = re.findall(r"\b[\w'-]+\b", text or "")
    if not words:
        return 1
    return max(1, math.ceil(len(words) / max(1, words_per_minute)))


def slugify(value: str, max_length: int = 180) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug or "article")[:max_length].strip("-") or "article"


def make_article_slug(headline: str, unique_hint: str | None = None) -> str:
    base = slugify(headline)
    if unique_hint:
        return f"{base}-{unique_hint[:8]}"
    return base


def classify_article(
    headline: str | None,
    description: str | None = None,
    content: str | None = None,
    source_region: str | None = None,
) -> CategoryResult:
    haystack = " ".join(part for part in (headline, description, content[:1200] if content else None) if part).lower()
    scores = {category: 0.0 for category in CATEGORIES if category != "General"}

    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in haystack:
                scores[category] += 2.0 if " " in keyword else 1.0

    if source_region:
        region = source_region.lower()
        if region == "india":
            scores["India"] += 1.5
        elif region not in {"", "unknown"}:
            scores["World"] += 0.75

    best_category, best_score = max(scores.items(), key=lambda item: item[1])
    total = sum(scores.values())
    if best_score <= 0:
        return CategoryResult("General", 0.0)

    confidence = min(0.95, max(0.2, best_score / max(total, best_score)))
    return CategoryResult(best_category, round(confidence, 3))


class ArticleClassifier:
    def __init__(self, settings: Settings, llm_client: LocalLLMClient | None = None):
        self.settings = settings
        self.llm_client = llm_client

    def classify(
        self,
        headline: str,
        description: str | None,
        content: str | None,
        source_region: str | None,
    ) -> CategoryResult:
        result = classify_article(headline, description, content, source_region)
        if (
            not self.settings.category_llm_enabled
            or result.confidence >= self.settings.category_llm_threshold
            or self.llm_client is None
        ):
            return result

        llm_result = self._classify_with_llm(headline, description, content)
        return llm_result or result

    def _classify_with_llm(
        self,
        headline: str,
        description: str | None,
        content: str | None,
    ) -> CategoryResult | None:
        prompt = (
            "Classify this news article into exactly one category from this list: "
            f"{', '.join(CATEGORIES)}.\n"
            "Return only the category name.\n\n"
            f"Headline: {headline}\n"
            f"Description: {description or ''}\n"
            f"Content excerpt: {(content or '')[:1200]}"
        )
        try:
            generated = self.llm_client.generate("You classify news articles.", prompt, max_tokens=16)
        except LLMGenerationError:
            return None

        candidate = generated.strip().splitlines()[0].strip(" .,:;\"'")
        for category in CATEGORIES:
            if candidate.lower() == category.lower():
                return CategoryResult(category, 0.55)
        return None
