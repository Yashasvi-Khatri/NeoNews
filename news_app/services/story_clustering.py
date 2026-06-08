from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import math
import os
import re
from typing import Iterable

from news_app.db import session_scope
from news_app.db.models import NewsArticle
from news_app.db.repository import ArticleRepository

logger = logging.getLogger(__name__)

# Multi-source clustering now follows:
# broad candidate retrieval -> weighted cluster scoring -> hard cannot-link blocking.
# Keep these values in one place so tuning recall/precision does not require touching logic.
MULTI_SOURCE_CONFIDENCE_THRESHOLD = float(os.getenv("NEWS_APP_MULTI_SOURCE_CONFIDENCE_THRESHOLD", "0.80"))
DEFAULT_TIME_WINDOW_HOURS = int(os.getenv("NEWS_APP_STORY_TIME_WINDOW_HOURS", "72"))
SENSITIVE_TIME_WINDOW_HOURS = int(os.getenv("NEWS_APP_STORY_SENSITIVE_TIME_WINDOW_HOURS", "48"))
SEED_CREATION_SCORE = float(os.getenv("NEWS_APP_STORY_SEED_SCORE", "0.88"))
BORDERLINE_MERGE_SCORE = float(os.getenv("NEWS_APP_STORY_BORDERLINE_SCORE", "0.74"))
MIN_FINAL_SCORE = float(os.getenv("NEWS_APP_STORY_AUTO_SCORE", "0.80"))
MIN_SENSITIVE_FINAL_SCORE = float(os.getenv("NEWS_APP_STORY_SENSITIVE_SCORE", "0.88"))
MIN_TITLE_SIMILARITY = 0.65
MIN_SUMMARY_SIMILARITY = 0.65
MIN_EMBEDDING_SIMILARITY = 0.70
MIN_ENTITY_OVERLAP = 0.50
SOURCE_DIVERSITY_BOOST = 0.03

CATEGORY_AUTO_THRESHOLDS = {
    "Politics": 0.84,
    "India": 0.80,
    "Crime": 0.88,
    "Courts": 0.88,
    "Legal": 0.88,
    "Business": 0.80,
    "Economy": 0.80,
    "Sports": 0.84,
    "Entertainment": 0.82,
    "Weather": 0.84,
    "Disaster": 0.84,
    "General": 0.80,
    "Tech": 0.82,
    "Technology": 0.82,
}

CLUSTER_SCORE_WEIGHTS = {
    "centroid_similarity": 0.20,
    "best_article_similarity": 0.15,
    "title_similarity": 0.15,
    "summary_similarity": 0.10,
    "main_entity_overlap": 0.15,
    "event_phrase_similarity": 0.10,
    "action_object_compatibility": 0.07,
    "date_compatibility": 0.04,
    "location_compatibility": 0.02,
    "number_compatibility": 0.02,
}

RELATED_EVENT_TYPE_PAIRS = {
    frozenset({"monetary_policy", "market_reaction"}),
    frozenset({"sports_match", "sports_selection"}),
    frozenset({"court_order", "crime"}),
}

CONFLICTING_ACTION_PAIRS = {
    frozenset({"cut", "raise"}),
    frozenset({"cut", "hold"}),
    frozenset({"raise", "hold"}),
    frozenset({"grant_bail", "reject_bail"}),
    frozenset({"beat", "lose"}),
    frozenset({"market_rise", "market_fall"}),
}

STOPWORDS = {
    "about",
    "after",
    "against",
    "all",
    "also",
    "amid",
    "among",
    "and",
    "are",
    "around",
    "being",
    "but",
    "during",
    "from",
    "has",
    "have",
    "how",
    "including",
    "into",
    "least",
    "more",
    "mostly",
    "over",
    "says",
    "the",
    "this",
    "that",
    "with",
    "will",
    "for",
    "its",
    "new",
    "news",
    "live",
    "latest",
    "updates",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "why",
    "been",
    "had",
    "he",
    "her",
    "his",
    "not",
    "only",
    "said",
    "she",
    "their",
    "they",
    "who",
}

CANONICAL_TOKENS = {
    "blaze": "fire",
    "burning": "fire",
    "burns": "fire",
    "dead": "death",
    "death": "death",
    "deaths": "death",
    "died": "death",
    "dies": "death",
    "fatal": "death",
    "fatalities": "death",
    "injured": "injury",
    "injures": "injury",
    "injuries": "injury",
    "injuring": "injury",
    "killed": "death",
    "killing": "death",
    "kills": "death",
    "murdered": "murder",
    "murders": "murder",
    "foreigner": "foreign",
    "foreigners": "foreign",
    "nationals": "foreign",
    "bnb": "hotel",
}

EVENT_TOKENS = {
    "attack",
    "blast",
    "collapse",
    "crash",
    "death",
    "earthquake",
    "explosion",
    "fire",
    "flood",
    "injury",
    "killing",
    "murder",
    "protest",
    "riot",
    "strike",
    "war",
}

WEAK_TOKENS = EVENT_TOKENS | {
    "accused",
    "alleged",
    "area",
    "arrest",
    "arrested",
    "attack",
    "body",
    "case",
    "city",
    "college",
    "court",
    "dead",
    "death",
    "delhi",
    "family",
    "fir",
    "flat",
    "found",
    "government",
    "home",
    "hospital",
    "house",
    "india",
    "individual",
    "investigation",
    "injury",
    "karnataka",
    "maharashtra",
    "man",
    "mumbai",
    "official",
    "officials",
    "one",
    "police",
    "probe",
    "report",
    "reported",
    "resident",
    "state",
    "suspect",
    "suspected",
    "telangana",
    "two",
    "under",
    "victim",
    "woman",
    "work",
    "worker",
    "year",
}

ANCHOR_PHRASES = {
    ("delhi", "university"),
    ("east", "delhi"),
    ("supreme", "court"),
}

MIN_STRICT_SIMILARITY = 0.34
MIN_STRICT_SHARED_STRONG = 3
MIN_ANCHORED_SIMILARITY = 0.42
MIN_ANCHORED_SHARED_STRONG = 2
MIN_ANCHOR_TITLE_SIMILARITY = 0.58
MIN_ANCHOR_SUMMARY_SIMILARITY = 0.62
MIN_ANCHOR_EMBEDDING_SIMILARITY = 0.72

BROAD_ENTITY_VALUES = {
    "australia",
    "bengaluru",
    "china",
    "delhi",
    "india",
    "israel",
    "karnataka",
    "kolkata",
    "maharashtra",
    "mumbai",
    "new delhi",
    "pakistan",
    "russia",
    "telangana",
    "ukraine",
    "united kingdom",
    "united state",
    "west asia",
}

BROAD_EVENT_TYPES = {
    "business",
    "general",
    "health",
    "india",
    "market_reaction",
    "politics",
    "science",
    "technology",
    "tech",
    "world",
}

GENERIC_EVENT_OBJECTS = {
    "policy",
    "stock market",
}

TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
CAPITALIZED_PHRASE_RE = re.compile(r"\b[A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*){1,3}\b")
ENTITY_PHRASE_RE = re.compile(r"\b[A-Z][A-Za-z0-9&.-]*(?:\s+[A-Z][A-Za-z0-9&.-]*){0,5}\b")
ACRONYM_RE = re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]+)?\b")
NUMBER_RE = re.compile(
    r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\s?(?:%|percent|bps|basis points|crore|lakh|million|billion|runs?|wickets?|seats?|dead|injured|people)?\b"
    r"|\b\d+(?:\.\d+)?\s?(?:%|percent|bps|basis points|crore|lakh|million|billion|runs?|wickets?|seats?|dead|injured|people)?\b"
    r"|\b\d+\s*-\s*\d+\b",
    flags=re.IGNORECASE,
)
DATE_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|june|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|june|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\b",
    flags=re.IGNORECASE,
)

ENTITY_ALIASES = {
    "rbi": "reserve bank of india",
    "reserve bank": "reserve bank of india",
    "reserve bank india": "reserve bank of india",
    "reserve bank of india": "reserve bank of india",
    "mpc": "monetary policy committee",
    "monetary policy committee": "monetary policy committee",
    "sensex": "sensex",
    "nifty": "nifty",
    "bse": "bse",
    "nse": "nse",
    "supreme court": "supreme court",
    "delhi university": "delhi university",
    "du": "delhi university",
    "india": "india",
    "pakistan": "pakistan",
    "australia": "australia",
    "china": "china",
    "russia": "russia",
    "us": "united state",
    "u s": "united state",
    "usa": "united state",
    "united states": "united state",
    "united state": "united state",
    "uk": "united kingdom",
    "u k": "united kingdom",
    "united kingdom": "united kingdom",
    "israel": "israel",
    "ukraine": "ukraine",
    "pm": "prime minister",
    "prime minister": "prime minister",
    "hc": "high court",
    "high court": "high court",
    "sc": "supreme court",
}

KNOWN_ENTITY_PATTERNS = (
    (re.compile(r"\bRBI\b", re.IGNORECASE), "reserve bank of india"),
    (re.compile(r"\bReserve Bank(?: of India)?\b", re.IGNORECASE), "reserve bank of india"),
    (re.compile(r"\bMPC\b|\bMonetary Policy Committee\b", re.IGNORECASE), "monetary policy committee"),
    (re.compile(r"\bDelhi University\b|\bDU\b", re.IGNORECASE), "delhi university"),
    (re.compile(r"\bSupreme Court\b|\bSC\b", re.IGNORECASE), "supreme court"),
    (re.compile(r"\bHigh Court\b|\bHC\b", re.IGNORECASE), "high court"),
    (re.compile(r"\bPrime Minister\b|\bPM\b", re.IGNORECASE), "prime minister"),
    (re.compile(r"\bSensex\b", re.IGNORECASE), "sensex"),
    (re.compile(r"\bNifty\b", re.IGNORECASE), "nifty"),
    (re.compile(r"\bGilgit[- ]Baltistan\b", re.IGNORECASE), "gilgit baltistan"),
    (re.compile(r"\bPakistan\b", re.IGNORECASE), "pakistan"),
    (re.compile(r"\b(?:US|U\.S\.|United States)\b", re.IGNORECASE), "united state"),
    (re.compile(r"\b(?:UK|U\.K\.|United Kingdom)\b", re.IGNORECASE), "united kingdom"),
)

KNOWN_LOCATIONS = {
    "east delhi": "delhi",
    "new delhi": "delhi",
    "delhi": "delhi",
    "mumbai": "mumbai",
    "bengaluru": "bengaluru",
    "bangalore": "bengaluru",
    "kolkata": "kolkata",
    "chennai": "chennai",
    "hyderabad": "hyderabad",
    "telangana": "telangana",
    "maharashtra": "maharashtra",
    "karnataka": "karnataka",
    "india": "india",
    "australia": "australia",
}

ACTION_PATTERNS = (
    ("grant_bail", re.compile(r"\b(?:grants?|granted|gets|secures?)\s+bail\b", re.IGNORECASE)),
    ("reject_bail", re.compile(r"\b(?:rejects?|rejected|denies|denied)\s+bail\b", re.IGNORECASE)),
    ("cut", re.compile(r"\b(?:cuts?|cut|lowers?|lowered|slashes?|slashed|reduces?|reduced)\b", re.IGNORECASE)),
    ("raise", re.compile(r"\b(?:raises?|raised|hikes?|hiked|increases?|increased)\b", re.IGNORECASE)),
    ("hold", re.compile(r"\b(?:keeps?|kept|holds?|held|unchanged|maintains?|maintained)\b", re.IGNORECASE)),
    ("market_rise", re.compile(r"\b(?:rises?|rose|gains?|gained|jumps?|jumped|surges?|surged|rallies|rallied)\b", re.IGNORECASE)),
    ("market_fall", re.compile(r"\b(?:falls?|fell|drops?|dropped|slips?|slipped|tumbles?|tumbled)\b", re.IGNORECASE)),
    ("beat", re.compile(r"\b(?:beats?|beat|defeats?|defeated|wins?|won)\b", re.IGNORECASE)),
    ("lose", re.compile(r"\b(?:loses?|lost)\b", re.IGNORECASE)),
    ("announce", re.compile(r"\b(?:announces?|announced|names?|named|picks?|picked|selects?|selected)\b", re.IGNORECASE)),
    ("arrest", re.compile(r"\b(?:arrests?|arrested|detains?|detained)\b", re.IGNORECASE)),
    ("death", re.compile(r"\b(?:dies?|died|dead|killed|kills|death)\b", re.IGNORECASE)),
    ("murder", re.compile(r"\b(?:murdered|murder|murders?)\b", re.IGNORECASE)),
    ("injury", re.compile(r"\b(?:injures?|injured|injury|injuries)\b", re.IGNORECASE)),
    ("announce", re.compile(r"\b(?:launches?|launched|unveils?|unveiled)\b", re.IGNORECASE)),
    ("report", re.compile(r"\b(?:reports?|reported|posts?|posted)\b", re.IGNORECASE)),
    ("order", re.compile(r"\b(?:orders?|ordered|stays?|stayed|quashes?|quashed|sentences?|sentenced)\b", re.IGNORECASE)),
)

OBJECT_PATTERNS = (
    ("gilgit-baltistan election", re.compile(r"\bgilgit[- ]baltistan\b.*\belections?\b|\belections?\b.*\bgilgit[- ]baltistan\b", re.IGNORECASE)),
    ("election", re.compile(r"\b(?:elections?|polls?|votes?)\b", re.IGNORECASE)),
    ("trade pact", re.compile(r"\b(?:trade|tariff)\s+(?:deal|pact|agreement|talks?)\b|\bbilateral\s+trade\b", re.IGNORECASE)),
    ("forex reserves", re.compile(r"\b(?:forex|foreign\s+exchange)\s+reserves?\b", re.IGNORECASE)),
    ("debt inflows", re.compile(r"\b(?:debt|bond|fpi|fpis?)\s+inflows?\b|\bgovernment\s+bonds?\b", re.IGNORECASE)),
    ("seafood exports", re.compile(r"\b(?:seafood|fisher(?:y|ies)|fishery)\s+exports?\b|\bfishing\s+sector\b", re.IGNORECASE)),
    ("oil meal imports", re.compile(r"\boil\s+meal\s+imports?\b|\banimal\s+feed\b", re.IGNORECASE)),
    ("regional aircraft", re.compile(r"\b(?:regional\s+aircraft|sj-?100|il-?114|il-?114-?300)\b", re.IGNORECASE)),
    ("fighter aircraft", re.compile(r"\b(?:su-?57|amca|fighter\s+(?:jet|aircraft)|combat\s+aircraft)\b", re.IGNORECASE)),
    ("aircraft production", re.compile(r"\b(?:aircraft|licensed\s+production)\b", re.IGNORECASE)),
    ("gdp growth", re.compile(r"\b(?:gdp|gross\s+domestic\s+product|gva)\s+growth\b|\bfy\d{2}\s+growth\b", re.IGNORECASE)),
    ("drug safety", re.compile(r"\b(?:drug|medicine)\s+(?:safety|side\s+effects?|adverse\s+reactions?)\b", re.IGNORECASE)),
    ("vegan conference", re.compile(r"\bvegan\b.*\bconference\b|\bconference\b.*\bvegan\b", re.IGNORECASE)),
    ("jobs report", re.compile(r"\b(?:jobs?|payrolls?|unemployment|jobless)\b", re.IGNORECASE)),
    ("data centres", re.compile(r"\bdata\s+cent(?:er|re)s?\b|\bai\s+infrastructure\b", re.IGNORECASE)),
    ("fuel prices", re.compile(r"\b(?:petrol|diesel|fuel|oil)\s+prices?\b", re.IGNORECASE)),
    ("maritime security", re.compile(r"\bmaritime\s+security\b|\bcritical\s+minerals?\b", re.IGNORECASE)),
    ("world bank appointment", re.compile(r"\bworld\s+bank\b.*\b(?:director|appoint|appointment)\b|\b(?:director|appoint|appointment)\b.*\bworld\s+bank\b", re.IGNORECASE)),
    ("protest", re.compile(r"\bprotests?\b|\bjantar\s+mantar\b", re.IGNORECASE)),
    ("repo rate", re.compile(r"\b(?:repo|policy)\s+rate\b", re.IGNORECASE)),
    ("interest rate", re.compile(r"\binterest\s+rate\b", re.IGNORECASE)),
    ("bank stocks", re.compile(r"\b(?:bank|banking)\s+stocks?\b", re.IGNORECASE)),
    ("stock market", re.compile(r"\b(?:sensex|nifty|stocks?|equities|shares|bourses?)\b|\b(?:stock|equity|share)\s+markets?\b", re.IGNORECASE)),
    ("bail", re.compile(r"\bbail\b", re.IGNORECASE)),
    ("squad", re.compile(r"\bsquad|team\s+selection|line-?up\b", re.IGNORECASE)),
    ("series", re.compile(r"\bseries\b", re.IGNORECASE)),
    ("match", re.compile(r"\bmatch|game\b", re.IGNORECASE)),
    ("tournament", re.compile(r"\btournament|world\s+cup|ipl|league\b", re.IGNORECASE)),
    ("house fire", re.compile(r"\bhouse\s+fire\b", re.IGNORECASE)),
    ("fire", re.compile(r"\bfire|blaze\b", re.IGNORECASE)),
    ("wall collapse", re.compile(r"\bwall\s+collapse\b", re.IGNORECASE)),
    ("collapse", re.compile(r"\bcollapse\b", re.IGNORECASE)),
    ("explosion", re.compile(r"\bexplosion|blast\b", re.IGNORECASE)),
    ("crash", re.compile(r"\b(?:crash|collision)\b", re.IGNORECASE)),
    ("murder", re.compile(r"\bmurder(?:ed|s)?\b", re.IGNORECASE)),
    ("profit", re.compile(r"\bprofit|earnings\b", re.IGNORECASE)),
    ("revenue", re.compile(r"\brevenue|sales\b", re.IGNORECASE)),
    ("funding", re.compile(r"\bfunding|investment\b", re.IGNORECASE)),
    ("policy", re.compile(r"\bpolicy|bill|law\b", re.IGNORECASE)),
    ("election", re.compile(r"\belection|polls?|votes?\b", re.IGNORECASE)),
)

SENSITIVE_CATEGORIES = {"India", "Politics", "Business", "Sports", "World"}
SENSITIVE_EVENT_TYPES = {
    "accident",
    "court_order",
    "crime",
    "death",
    "election",
    "finance_result",
    "market_reaction",
    "monetary_policy",
    "sports_match",
    "sports_selection",
}


@dataclass(frozen=True)
class TokenProfile:
    all_tokens: set[str]
    strong_tokens: set[str]
    weak_tokens: set[str]
    phrase_tokens: set[str]


@dataclass(frozen=True)
class EventFingerprint:
    main_event: str
    event_type: str
    main_entities: tuple[str, ...]
    secondary_entities: tuple[str, ...]
    action: str | None
    event_object: str | None
    location: str | None
    event_date: str | None
    published_date: str | None
    important_numbers: tuple[str, ...]
    category: str
    source: str
    language: str = "en"
    normalized_event_phrase: str = ""

    def to_json(self) -> str:
        payload = asdict(self)
        payload["object"] = payload.pop("event_object")
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> EventFingerprint | None:
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        if "object" in payload:
            payload["event_object"] = payload.pop("object")
        for key in ("main_entities", "secondary_entities", "important_numbers"):
            payload[key] = tuple(payload.get(key) or ())
        payload.setdefault("normalized_event_phrase", payload.get("main_event", ""))
        try:
            return cls(**payload)
        except TypeError:
            return None


@dataclass(frozen=True)
class ArticleSignal:
    identifier: str
    source_name: str
    language: str
    category: str
    published_at: datetime | None
    title: str
    summary_text: str
    clustering_text: str
    fingerprint: EventFingerprint
    token_profile: TokenProfile
    title_embedding: tuple[float, ...]
    summary_embedding: tuple[float, ...]
    clustering_embedding: tuple[float, ...]


@dataclass(frozen=True)
class PairDecision:
    article_a: str
    article_b: str
    final_score: float
    title_similarity: float
    summary_similarity: float
    entity_overlap: float
    action_match: float
    object_match: float
    location_match: float
    date_match: float
    number_match: float
    category_match: float
    cannot_link_reason: str | None
    final_decision: bool
    final_label: str = "same_event"
    category_threshold: float = MIN_FINAL_SCORE
    centroid_similarity: float = 0.0
    seed_similarity: float = 0.0
    best_article_similarity: float = 0.0
    event_phrase_similarity: float = 0.0
    source_boost: float = 0.0
    explanation: str = ""


@dataclass
class ClusterDraft:
    collected_for_date: date
    primary_category: str
    articles: list[NewsArticle] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)
    profiles: list[TokenProfile] = field(default_factory=list)
    signals: list[ArticleSignal] = field(default_factory=list)
    seed_article_id: int | None = None
    cluster_fingerprint: EventFingerprint | None = None
    confidence_score: float = 1.0
    centroid_embedding: tuple[float, ...] = field(default_factory=tuple)
    rejected_decisions: list[dict] = field(default_factory=list)
    related_article_ids: list[str] = field(default_factory=list)

    @property
    def seed_signal(self) -> ArticleSignal:
        return self.signals[0]


class StoryClusteringService:
    def __init__(self, repository: ArticleRepository | None = None):
        self.repository = repository or ArticleRepository()

    def rebuild_for_date(self, target_date: date) -> int:
        with session_scope() as session:
            articles = self.repository.articles_for_story_clustering(session, target_date)
            drafts = self._build_drafts(articles, constrain_date=True)
            payloads = [self._cluster_payload(draft) for draft in drafts]
            return self.repository.replace_story_clusters(session, target_date, payloads)

    def rebuild_for_window(self, target_date: date, published_after: datetime, published_before: datetime) -> int:
        with session_scope() as session:
            articles = self.repository.articles_for_story_clustering(
                session,
                target_date=None,
                published_after=published_after,
                published_before=published_before,
            )
            drafts = self._build_drafts(articles, constrain_date=False)
            payloads = [self._cluster_payload(draft, collected_for_date=target_date) for draft in drafts]
            return self.repository.replace_live_story_clusters(session, target_date, payloads)

    def debug_article(self, article_id: int) -> dict:
        """Return explainable merge/reject information for one article.

        This powers the debug UI/API mode: selected cluster, rejected clusters,
        scores, conflict reasons, missing fields, and final decision labels.
        """
        with session_scope() as session:
            article = self.repository.get_article(session, article_id)
            if article is None:
                raise ValueError(f"Article {article_id} not found")
            signal = build_article_signal(article)
            window_start = article.published_at - timedelta(hours=DEFAULT_TIME_WINDOW_HOURS)
            window_end = article.published_at + timedelta(hours=DEFAULT_TIME_WINDOW_HOURS)
            neighbours = [
                item
                for item in self.repository.articles_for_story_clustering(
                    session,
                    target_date=None,
                    published_after=window_start,
                    published_before=window_end,
                )
                if item.id != article.id
            ]
            drafts = self._build_drafts(neighbours, constrain_date=False)
            candidates = find_candidate_clusters(signal, drafts, article_record=article, constrain_date=False)
            scored = [(draft, score_article_vs_cluster(signal, draft)) for draft in candidates]
            selected = next(((draft, decision) for draft, decision in sorted(scored, key=lambda item: item[1].final_score, reverse=True) if decision.final_label == "same_event" and not decision.cannot_link_reason), None)
            return {
                "article_id": article.id,
                "headline": article.headline,
                "fingerprint": asdict(signal.fingerprint),
                "missing_fields": missing_fingerprint_fields(signal.fingerprint),
                "candidate_count": len(candidates),
                "selected_cluster": debug_cluster_payload(selected[0], selected[1]) if selected else None,
                "rejected_clusters": [debug_cluster_payload(draft, decision) for draft, decision in scored if selected is None or draft is not selected[0]],
            }

    def _build_drafts(self, articles: list[NewsArticle], constrain_date: bool = True) -> list[ClusterDraft]:
        drafts: list[ClusterDraft] = []
        for article in articles:
            signal = build_article_signal(article)
            article.event_fingerprint = signal.fingerprint.to_json()
            match = self._find_match(drafts, article, signal, constrain_date=constrain_date)
            if match is None:
                drafts.append(
                    ClusterDraft(
                        collected_for_date=article.collected_for_date,
                        primary_category=article.primary_category or "General",
                        articles=[article],
                        tokens=set(signal.token_profile.all_tokens),
                        profiles=[signal.token_profile],
                        signals=[signal],
                        seed_article_id=article.id,
                        cluster_fingerprint=signal.fingerprint,
                        confidence_score=1.0,
                        centroid_embedding=signal.clustering_embedding,
                    )
                )
                continue

            draft, decision = match
            add_article_to_cluster(article, draft, signal=signal, decision=decision)
            if not validate_cluster(draft):
                # Defensive rollback: do not keep an article that weakens cluster coherence.
                removed_article = draft.articles.pop()
                removed_signal = draft.signals.pop()
                draft.profiles.pop()
                draft.tokens = set().union(*(item.token_profile.all_tokens for item in draft.signals)) if draft.signals else set()
                draft.cluster_fingerprint = draft.signals[0].fingerprint if draft.signals else None
                for existing_signal in draft.signals[1:]:
                    draft.cluster_fingerprint = merge_fingerprints(draft.cluster_fingerprint, existing_signal.fingerprint)
                update_cluster_centroid(draft)
                drafts.append(
                    ClusterDraft(
                        collected_for_date=removed_article.collected_for_date,
                        primary_category=removed_article.primary_category or "General",
                        articles=[removed_article],
                        tokens=set(removed_signal.token_profile.all_tokens),
                        profiles=[removed_signal.token_profile],
                        signals=[removed_signal],
                        seed_article_id=removed_article.id,
                        cluster_fingerprint=removed_signal.fingerprint,
                        confidence_score=1.0,
                        centroid_embedding=removed_signal.clustering_embedding,
                    )
                )
        return drafts

    def _find_match(
        self,
        drafts: list[ClusterDraft],
        article: NewsArticle,
        signal: ArticleSignal,
        constrain_date: bool = True,
    ) -> tuple[ClusterDraft, PairDecision] | None:
        if len(signal.token_profile.all_tokens) < 3:
            return None

        candidates = find_candidate_clusters(signal, drafts, article_record=article, constrain_date=constrain_date)
        best = find_best_cluster(signal, candidates)
        if best is None:
            for rejected in candidates:
                decision = score_article_vs_cluster(signal, rejected)
                log_cluster_decision(
                    article_id=signal.identifier,
                    candidate_cluster_id=rejected.seed_article_id,
                    final_decision=decision.final_label,
                    decision=decision,
                    explanation=decision.explanation,
                )
            return None

        draft, decision = best
        log_cluster_decision(
            article_id=signal.identifier,
            candidate_cluster_id=draft.seed_article_id,
            final_decision=decision.final_label,
            decision=decision,
            explanation=decision.explanation,
        )
        return draft, decision

    def _cluster_payload(self, draft: ClusterDraft, collected_for_date: date | None = None) -> dict:
        seed_id = draft.seed_article_id
        seed_article = next((article for article in draft.articles if article.id == seed_id), draft.articles[0])
        rest = sorted(
            [article for article in draft.articles if article is not seed_article],
            key=lambda item: item.published_at,
            reverse=True,
        )
        articles = [seed_article, *rest]
        cluster_date = collected_for_date or draft.collected_for_date
        fingerprint = draft.cluster_fingerprint or draft.seed_signal.fingerprint
        key_seed = "|".join(
            [
                cluster_date.isoformat(),
                draft.primary_category,
                fingerprint.main_event,
                "-".join(fingerprint.main_entities),
                str(seed_article.id or seed_article.url_hash),
            ]
        )
        return {
            "cluster_key": hashlib.sha256(key_seed.encode("utf-8")).hexdigest()[:32],
            "title": seed_article.headline,
            "primary_category": draft.primary_category,
            "thumbnail_path": next((article.thumbnail_path for article in articles if article.thumbnail_path), None),
            "articles": articles,
            "seed_article_id": seed_article.id,
            "cluster_fingerprint": fingerprint.to_json(),
            "confidence_score": round(draft.confidence_score, 4),
            "first_published_at": min(article.published_at for article in articles),
        }


def build_article_signal(article: NewsArticle) -> ArticleSignal:
    return build_signal_from_parts(
        identifier=str(article.id or article.url_hash),
        source_name=article.source_name,
        language=article.language,
        category=article.primary_category,
        published_at=article.published_at,
        title=article.headline or article.original_title,
        original_title=article.original_title,
        summary=article.summary or article.description,
        content=article.content,
    )


def build_signal_from_parts(
    *,
    identifier: str,
    source_name: str,
    language: str,
    category: str,
    published_at: datetime | None,
    title: str,
    original_title: str | None = None,
    summary: str | None = None,
    content: str | None = None,
) -> ArticleSignal:
    text_title = title or original_title or ""
    summary_text = summary or ""
    content_sample = first_paragraphs(content, limit=2)
    clustering_text = " ".join(part for part in (text_title, summary_text, content_sample) if part)
    fingerprint = extract_event_fingerprint(
        title=text_title,
        original_title=original_title,
        summary=summary_text,
        content=content,
        category=category,
        source=source_name,
        language=language,
        published_at=published_at,
    )
    return ArticleSignal(
        identifier=identifier,
        source_name=source_name,
        language=language or "en",
        category=category or "General",
        published_at=published_at,
        title=text_title,
        summary_text=" ".join(part for part in (summary_text, content_sample) if part),
        clustering_text=clustering_text,
        fingerprint=fingerprint,
        token_profile=story_token_profile(text_title, summary_text),
        title_embedding=text_embedding(text_title),
        summary_embedding=text_embedding(" ".join(part for part in (summary_text, content_sample) if part)),
        clustering_embedding=text_embedding(clustering_text),
    )


def event_fingerprint_json_for_parts(
    *,
    source_name: str,
    language: str,
    category: str,
    published_at: datetime | None,
    title: str,
    original_title: str | None = None,
    summary: str | None = None,
    content: str | None = None,
) -> str:
    return extract_event_fingerprint(
        title=title,
        original_title=original_title,
        summary=summary,
        content=content,
        category=category,
        source=source_name,
        language=language,
        published_at=published_at,
    ).to_json()


def extract_event_fingerprint(
    article: NewsArticle | None = None,
    *,
    title: str | None = None,
    original_title: str | None = None,
    summary: str | None = None,
    content: str | None = None,
    category: str | None = None,
    source: str | None = None,
    language: str | None = None,
    published_at: datetime | None = None,
) -> EventFingerprint:
    """Extract a same-event fingerprint from an article or article-like parts.

    The function is intentionally heuristic and missing-field-neutral. It uses the
    title, summary, and first two paragraphs only, so background content does not
    dominate story clustering.
    """
    if article is not None:
        title = title if title is not None else (article.headline or article.original_title)
        original_title = original_title if original_title is not None else article.original_title
        summary = summary if summary is not None else (article.summary or article.description)
        content = content if content is not None else article.content
        category = category if category is not None else article.primary_category
        source = source if source is not None else article.source_name
        language = language if language is not None else article.language
        published_at = published_at if published_at is not None else article.published_at

    title_text = title or original_title or ""
    summary_text = summary or ""
    content_sample = first_paragraphs(content, limit=2)
    text = " ".join(part for part in (title_text, original_title, summary_text, content_sample) if part)
    title_entities = extract_entities(title_text)
    all_entities = _dedupe([*title_entities, *extract_entities(" ".join(part for part in (summary_text, content_sample) if part))])
    action = extract_action(title_text) or extract_action(text)
    event_object = extract_object(title_text, text, all_entities, action)
    event_type = infer_event_type(category or "General", text, action, event_object)
    location = extract_location(title_text) or extract_location(text)
    event_date = extract_event_date(text)
    important_numbers = tuple(extract_important_numbers(text))
    main_entities = tuple(all_entities[:3])
    secondary_entities = tuple(all_entities[3:8])
    normalized_event_phrase = compose_normalized_event_phrase(main_entities, action, event_object, event_type, text)
    main_event = compose_main_event(main_entities, action, event_object, event_type)
    return EventFingerprint(
        main_event=main_event,
        event_type=event_type,
        main_entities=main_entities,
        secondary_entities=secondary_entities,
        action=action,
        event_object=event_object,
        location=location,
        event_date=event_date,
        published_date=published_at.date().isoformat() if published_at else None,
        important_numbers=important_numbers,
        category=category or "General",
        source=source or "",
        language=language or "en",
        normalized_event_phrase=normalized_event_phrase,
    )


def same_event_decision(left: ArticleSignal, right: ArticleSignal) -> PairDecision:
    """Pairwise decision used by ingestion ranking and as seed evidence.

    This is deliberately less brittle than the previous hard-AND rule: missing
    fields are neutral, matching fields add evidence, and only serious
    contradictions block a merge.
    """
    title_similarity = title_semantic_similarity(left, right)
    summary_similarity = summary_semantic_similarity(left, right)
    entity_overlap = main_entity_overlap(left.fingerprint, right.fingerprint)
    action_match = action_compatibility(left.fingerprint, right.fingerprint)
    object_match = object_compatibility(left.fingerprint, right.fingerprint)
    location_match = location_compatibility(left.fingerprint, right.fingerprint)
    date_match = date_compatibility(left, right)
    number_match = number_compatibility(left.fingerprint, right.fingerprint)
    category_match = category_compatibility_score(left.category, right.category)
    event_phrase_similarity = event_phrase_similarity_score(left.fingerprint, right.fingerprint)
    seed_similarity = cosine_similarity(left.clustering_embedding, right.clustering_embedding)

    reason = candidate_filter_reason(left, right, title_similarity, summary_similarity, seed_similarity)
    if reason is None:
        reason = cannot_link_reason(left, right)

    action_object = (action_match + object_match) / 2
    score = (
        seed_similarity * 0.20
        + max(seed_similarity, title_similarity) * 0.15
        + title_similarity * 0.15
        + summary_similarity * 0.10
        + entity_overlap * 0.15
        + event_phrase_similarity * 0.10
        + action_object * 0.07
        + date_match * 0.04
        + location_match * 0.02
        + number_match * 0.02
    )
    if left.source_name != right.source_name:
        score += SOURCE_DIVERSITY_BOOST
    score = round(min(1.0, max(0.0, score)), 4)

    threshold = SEED_CREATION_SCORE
    if has_strong_same_event_evidence(
        title_similarity=title_similarity,
        summary_similarity=summary_similarity,
        centroid_similarity=seed_similarity,
        best_article_similarity=seed_similarity,
        entity_overlap=entity_overlap,
        event_phrase_similarity=event_phrase_similarity,
        action_match=action_match,
        object_match=object_match,
        number_match=number_match,
        date_match=date_match,
    ):
        score = max(score, threshold)

    final_label = classify_pair_label(left, right, score, reason, threshold)
    final_decision = reason is None and final_label == "same_event" and score >= threshold
    return PairDecision(
        article_a=left.identifier,
        article_b=right.identifier,
        final_score=score,
        title_similarity=round(title_similarity, 4),
        summary_similarity=round(summary_similarity, 4),
        entity_overlap=round(entity_overlap, 4),
        action_match=round(action_match, 4),
        object_match=round(object_match, 4),
        location_match=round(location_match, 4),
        date_match=round(date_match, 4),
        number_match=round(number_match, 4),
        category_match=round(category_match, 4),
        cannot_link_reason=reason,
        final_decision=final_decision,
        final_label=final_label,
        category_threshold=threshold,
        centroid_similarity=round(seed_similarity, 4),
        seed_similarity=round(seed_similarity, 4),
        best_article_similarity=round(seed_similarity, 4),
        event_phrase_similarity=round(event_phrase_similarity, 4),
        source_boost=SOURCE_DIVERSITY_BOOST if left.source_name != right.source_name else 0.0,
        explanation=decision_explanation(final_label, score, threshold, reason),
    )


def replace_decision(decision: PairDecision, reason: str, final_decision: bool) -> PairDecision:
    label = "same_event" if final_decision else ("related_development" if is_related_reason(reason) else "different_event")
    return PairDecision(
        article_a=decision.article_a,
        article_b=decision.article_b,
        final_score=decision.final_score,
        title_similarity=decision.title_similarity,
        summary_similarity=decision.summary_similarity,
        entity_overlap=decision.entity_overlap,
        action_match=decision.action_match,
        object_match=decision.object_match,
        location_match=decision.location_match,
        date_match=decision.date_match,
        number_match=decision.number_match,
        category_match=decision.category_match,
        cannot_link_reason=reason,
        final_decision=final_decision,
        final_label=label,
        category_threshold=decision.category_threshold,
        centroid_similarity=decision.centroid_similarity,
        seed_similarity=decision.seed_similarity,
        best_article_similarity=decision.best_article_similarity,
        event_phrase_similarity=decision.event_phrase_similarity,
        source_boost=decision.source_boost,
        explanation=decision_explanation(label, decision.final_score, decision.category_threshold, reason),
    )


def log_merge_decision(decision: PairDecision) -> None:
    log_cluster_decision(
        article_id=decision.article_a,
        candidate_cluster_id=decision.article_b,
        final_decision=decision.final_label,
        decision=decision,
        explanation=decision.explanation,
    )


def candidate_filter_reason(
    left: ArticleSignal,
    right: ArticleSignal,
    title_similarity: float,
    summary_similarity: float | None = None,
    embedding_similarity: float | None = None,
) -> str | None:
    if (left.language or "en") != (right.language or "en"):
        return "language_mismatch"
    if category_compatibility_score(left.category, right.category) <= 0:
        return "category_mismatch"
    if hours_apart(left.published_at, right.published_at) > DEFAULT_TIME_WINDOW_HOURS:
        return "outside_time_window"

    summary_similarity = summary_similarity if summary_similarity is not None else summary_semantic_similarity(left, right)
    embedding_similarity = embedding_similarity if embedding_similarity is not None else cosine_similarity(left.clustering_embedding, right.clustering_embedding)
    shared_entities = specific_entity_overlap(left.fingerprint, right.fingerprint) > 0
    shared_named_entities = shared_specific_entities(left.fingerprint, right.fingerprint)
    shared_phrases = bool(left.token_profile.phrase_tokens.intersection(right.token_profile.phrase_tokens))
    phrase_similarity = event_phrase_similarity_score(left.fingerprint, right.fingerprint)

    if (
        has_minimum_same_event_anchor(
            left,
            right,
            title_similarity=title_similarity,
            summary_similarity=summary_similarity,
            embedding_similarity=embedding_similarity,
            event_phrase_similarity=phrase_similarity,
        )
        or title_similarity >= MIN_TITLE_SIMILARITY
        or summary_similarity >= MIN_SUMMARY_SIMILARITY
        or (embedding_similarity >= MIN_EMBEDDING_SIMILARITY and (shared_entities or shared_named_entities))
        or shared_entities
        or shared_named_entities
        or shared_phrases
        or (phrase_similarity >= 0.72 and (shared_entities or shared_named_entities))
    ):
        return None
    return "insufficient_candidate_evidence"


def cannot_link_reason(left: ArticleSignal, right: ArticleSignal) -> str | None:
    left_fp = left.fingerprint
    right_fp = right.fingerprint
    window = SENSITIVE_TIME_WINDOW_HOURS if is_sensitive_pair(left, right) else DEFAULT_TIME_WINDOW_HOURS
    if hours_apart(left.published_at, right.published_at) > window:
        return "event_time_conflict"
    if event_type_conflict(left_fp.event_type, right_fp.event_type):
        return "related_event_type_conflict" if frozenset({left_fp.event_type, right_fp.event_type}) in RELATED_EVENT_TYPE_PAIRS else "event_type_conflict"
    if action_conflict(left_fp.action, right_fp.action, left_fp.event_type, right_fp.event_type):
        return "action_conflict"
    if location_conflict(left_fp, right_fp):
        return "location_conflict"
    if left_fp.event_date and right_fp.event_date and left_fp.event_date != right_fp.event_date:
        return "event_date_conflict"
    if entity_conflict(left_fp, right_fp):
        return "main_entity_conflict"
    if object_conflict(left_fp, right_fp):
        return "object_conflict"
    if numeric_conflict(left_fp, right_fp):
        return "numeric_result_conflict"
    if not has_minimum_same_event_anchor(left, right):
        return "insufficient_specific_anchor"
    return None


def cluster_fingerprint_conflict(article_fingerprint: EventFingerprint, cluster_fingerprint: EventFingerprint | None) -> str | None:
    if cluster_fingerprint is None:
        return None
    if event_type_conflict(article_fingerprint.event_type, cluster_fingerprint.event_type):
        return "cluster_event_type_conflict"
    if action_conflict(article_fingerprint.action, cluster_fingerprint.action, article_fingerprint.event_type, cluster_fingerprint.event_type):
        return "cluster_action_conflict"
    if location_conflict(article_fingerprint, cluster_fingerprint):
        return "cluster_location_conflict"
    if entity_conflict(article_fingerprint, cluster_fingerprint):
        return "cluster_main_entity_conflict"
    if object_conflict(article_fingerprint, cluster_fingerprint):
        return "cluster_object_conflict"
    return None


def merge_fingerprints(current: EventFingerprint | None, incoming: EventFingerprint) -> EventFingerprint:
    if current is None:
        return incoming
    shared_entities = tuple(entity for entity in current.main_entities if entity in set(incoming.main_entities))
    main_entities = shared_entities or current.main_entities
    secondary_entities = tuple(
        _dedupe([*current.secondary_entities, *incoming.secondary_entities, *current.main_entities, *incoming.main_entities])[:8]
    )
    important_numbers = tuple(_dedupe([*current.important_numbers, *incoming.important_numbers])[:8])
    return EventFingerprint(
        main_event=current.main_event,
        event_type=current.event_type,
        main_entities=main_entities,
        secondary_entities=secondary_entities,
        action=current.action,
        event_object=current.event_object,
        location=current.location or incoming.location,
        event_date=current.event_date or incoming.event_date,
        published_date=min(filter(None, [current.published_date, incoming.published_date]), default=None),
        important_numbers=important_numbers,
        category=current.category,
        source=current.source,
        language=current.language,
        normalized_event_phrase=current.normalized_event_phrase or incoming.normalized_event_phrase or current.main_event,
    )



def find_candidate_clusters(
    article: ArticleSignal,
    clusters: list[ClusterDraft],
    *,
    article_obj: NewsArticle | None = None,
    article_: NewsArticle | None = None,
    article_record: NewsArticle | None = None,
    constrain_date: bool = True,
) -> list[ClusterDraft]:
    """Broad candidate retrieval. Prefer recall; final scoring protects precision."""
    current_article = article_obj or article_ or article_record
    candidates: list[ClusterDraft] = []
    for cluster in clusters:
        if not cluster.signals:
            continue
        if constrain_date and current_article is not None and cluster.collected_for_date != current_article.collected_for_date:
            continue
        seed = cluster.seed_signal
        if (article.language or "en") != (seed.language or "en"):
            continue
        if category_compatibility_score(article.category, cluster.primary_category) <= 0:
            continue
        if min(hours_apart(article.published_at, existing.published_at) for existing in cluster.signals) > DEFAULT_TIME_WINDOW_HOURS:
            continue
        if cluster_candidate_evidence(article, cluster):
            candidates.append(cluster)
    return candidates


def cluster_candidate_evidence(article: ArticleSignal, cluster: ClusterDraft) -> bool:
    cluster_fp = cluster.cluster_fingerprint or cluster.seed_signal.fingerprint
    title_similarity = max(title_semantic_similarity(article, existing) for existing in cluster.signals)
    summary_similarity = max(summary_semantic_similarity(article, existing) for existing in cluster.signals)
    embedding_similarity = max(cosine_similarity(article.clustering_embedding, existing.clustering_embedding) for existing in cluster.signals)
    shared_main_entity = specific_entity_overlap(article.fingerprint, cluster_fp) > 0
    shared_named_entity = shared_specific_entities(article.fingerprint, cluster_fp)
    shared_phrase = bool(set(article.token_profile.phrase_tokens).intersection(cluster.tokens))
    phrase_similarity = event_phrase_similarity_score(article.fingerprint, cluster_fp)
    return any(
        [
            any(
                has_minimum_same_event_anchor(
                    article,
                    existing,
                    title_similarity=title_semantic_similarity(article, existing),
                    summary_similarity=summary_semantic_similarity(article, existing),
                    embedding_similarity=cosine_similarity(article.clustering_embedding, existing.clustering_embedding),
                    event_phrase_similarity=event_phrase_similarity_score(article.fingerprint, existing.fingerprint),
                )
                for existing in cluster.signals
            ),
            title_similarity >= MIN_TITLE_SIMILARITY,
            summary_similarity >= MIN_SUMMARY_SIMILARITY,
            embedding_similarity >= MIN_EMBEDDING_SIMILARITY and (shared_main_entity or shared_named_entity),
            shared_main_entity,
            shared_named_entity,
            shared_phrase,
            phrase_similarity >= 0.72 and (shared_main_entity or shared_named_entity),
        ]
    )


def score_article_vs_cluster(article: ArticleSignal, cluster: ClusterDraft) -> PairDecision:
    cluster_fp = cluster.cluster_fingerprint or cluster.seed_signal.fingerprint
    cannot_link = check_cannot_link(article, cluster)
    title_similarity = max(title_semantic_similarity(article, existing) for existing in cluster.signals)
    summary_similarity = max(summary_semantic_similarity(article, existing) for existing in cluster.signals)
    centroid = cluster.centroid_embedding or update_cluster_centroid(cluster)
    centroid_similarity = cosine_similarity(article.clustering_embedding, centroid)
    article_similarities = sorted(
        (cosine_similarity(article.clustering_embedding, existing.clustering_embedding) for existing in cluster.signals),
        reverse=True,
    )
    best_article_similarity = article_similarities[0] if article_similarities else 0.0
    seed_similarity = cosine_similarity(article.clustering_embedding, cluster.seed_signal.clustering_embedding)
    entity_overlap = main_entity_overlap(article.fingerprint, cluster_fp)
    event_phrase_similarity = event_phrase_similarity_score(article.fingerprint, cluster_fp)
    action_match = action_compatibility(article.fingerprint, cluster_fp)
    object_match = object_compatibility(article.fingerprint, cluster_fp)
    date_match = max(date_compatibility(article, existing) for existing in cluster.signals)
    location_match = location_compatibility(article.fingerprint, cluster_fp)
    number_match = number_compatibility(article.fingerprint, cluster_fp)
    category_match = category_compatibility_score(article.category, cluster.primary_category)
    source_boost = SOURCE_DIVERSITY_BOOST if article.source_name not in {item.source_name for item in cluster.signals} else 0.0
    action_object = (action_match + object_match) / 2

    score = (
        centroid_similarity * CLUSTER_SCORE_WEIGHTS["centroid_similarity"]
        + best_article_similarity * CLUSTER_SCORE_WEIGHTS["best_article_similarity"]
        + title_similarity * CLUSTER_SCORE_WEIGHTS["title_similarity"]
        + summary_similarity * CLUSTER_SCORE_WEIGHTS["summary_similarity"]
        + entity_overlap * CLUSTER_SCORE_WEIGHTS["main_entity_overlap"]
        + event_phrase_similarity * CLUSTER_SCORE_WEIGHTS["event_phrase_similarity"]
        + action_object * CLUSTER_SCORE_WEIGHTS["action_object_compatibility"]
        + date_match * CLUSTER_SCORE_WEIGHTS["date_compatibility"]
        + location_match * CLUSTER_SCORE_WEIGHTS["location_compatibility"]
        + number_match * CLUSTER_SCORE_WEIGHTS["number_compatibility"]
        + source_boost
    )
    threshold = category_auto_threshold(article.category or cluster.primary_category)
    if len(cluster.signals) == 1:
        threshold = SEED_CREATION_SCORE

    if cannot_link is None and has_strong_same_event_evidence(
        title_similarity=title_similarity,
        summary_similarity=summary_similarity,
        centroid_similarity=centroid_similarity,
        best_article_similarity=best_article_similarity,
        entity_overlap=entity_overlap,
        event_phrase_similarity=event_phrase_similarity,
        action_match=action_match,
        object_match=object_match,
        number_match=number_match,
        date_match=date_match,
    ):
        score = max(score, SEED_CREATION_SCORE if len(cluster.signals) == 1 else threshold)
        if len(cluster.signals) == 1:
            score = max(score, 0.90)

    score = round(min(1.0, max(0.0, score)), 4)
    final_label = classify_cluster_label(article, cluster, score, cannot_link, threshold)
    final_decision = final_label == "same_event" and cannot_link is None
    return PairDecision(
        article_a=article.identifier,
        article_b=str(cluster.seed_article_id or cluster.seed_signal.identifier),
        final_score=score,
        title_similarity=round(title_similarity, 4),
        summary_similarity=round(summary_similarity, 4),
        entity_overlap=round(entity_overlap, 4),
        action_match=round(action_match, 4),
        object_match=round(object_match, 4),
        location_match=round(location_match, 4),
        date_match=round(date_match, 4),
        number_match=round(number_match, 4),
        category_match=round(category_match, 4),
        cannot_link_reason=cannot_link,
        final_decision=final_decision,
        final_label=final_label,
        category_threshold=threshold,
        centroid_similarity=round(centroid_similarity, 4),
        seed_similarity=round(seed_similarity, 4),
        best_article_similarity=round(best_article_similarity, 4),
        event_phrase_similarity=round(event_phrase_similarity, 4),
        source_boost=round(source_boost, 4),
        explanation=decision_explanation(final_label, score, threshold, cannot_link),
    )


def check_cannot_link(article: ArticleSignal, cluster: ClusterDraft) -> str | None:
    cluster_reason = cluster_fingerprint_conflict(article.fingerprint, cluster.cluster_fingerprint)
    if cluster_reason:
        return cluster_reason
    has_pair_anchor = False
    for existing in cluster.signals:
        reason = cannot_link_reason(article, existing)
        if reason == "insufficient_specific_anchor":
            continue
        if reason:
            return reason
        has_pair_anchor = True
    if not has_pair_anchor:
        return "insufficient_specific_anchor"
    return None


def find_best_cluster(article: ArticleSignal, clusters: list[ClusterDraft]) -> tuple[ClusterDraft, PairDecision] | None:
    best: tuple[float, ClusterDraft, PairDecision] | None = None
    for cluster in clusters:
        decision = score_article_vs_cluster(article, cluster)
        if decision.final_label != "same_event" or decision.cannot_link_reason:
            cluster.rejected_decisions.append(decision_to_debug_dict(decision))
            continue
        if best is None or decision.final_score > best[0]:
            best = (decision.final_score, cluster, decision)
    if best is None:
        return None
    return best[1], best[2]


def add_article_to_cluster(
    article: NewsArticle,
    cluster: ClusterDraft,
    *,
    signal: ArticleSignal | None = None,
    decision: PairDecision | None = None,
) -> ClusterDraft:
    signal = signal or build_article_signal(article)
    cluster.articles.append(article)
    cluster.tokens.update(signal.token_profile.all_tokens)
    cluster.profiles.append(signal.token_profile)
    cluster.signals.append(signal)
    cluster.cluster_fingerprint = merge_fingerprints(cluster.cluster_fingerprint, signal.fingerprint)
    if decision is not None:
        accepted_score = max(decision.final_score, MIN_FINAL_SCORE)
        if len(cluster.signals) == 2:
            accepted_score = max(accepted_score, 0.90)
        cluster.confidence_score = min(cluster.confidence_score, accepted_score)
    update_cluster_centroid(cluster)
    return cluster


def update_cluster_centroid(cluster: ClusterDraft) -> tuple[float, ...]:
    embeddings = [signal.clustering_embedding for signal in cluster.signals if signal.clustering_embedding]
    if not embeddings:
        cluster.centroid_embedding = tuple()
        return cluster.centroid_embedding
    dimensions = len(embeddings[0])
    averaged = [0.0] * dimensions
    usable = [embedding for embedding in embeddings if len(embedding) == dimensions]
    for embedding in usable:
        for index, value in enumerate(embedding):
            averaged[index] += value
    if usable:
        averaged = [value / len(usable) for value in averaged]
    norm = math.sqrt(sum(value * value for value in averaged))
    cluster.centroid_embedding = tuple(value / norm for value in averaged) if norm else tuple(averaged)
    return cluster.centroid_embedding


def validate_cluster(cluster: ClusterDraft) -> bool:
    if len(cluster.signals) <= 1:
        return True
    cluster_fp = cluster.cluster_fingerprint or cluster.seed_signal.fingerprint
    coherent_phrase_count = 0
    for index, left in enumerate(cluster.signals):
        if event_phrase_similarity_score(left.fingerprint, cluster_fp) >= 0.55 or left.fingerprint.event_type == cluster_fp.event_type:
            coherent_phrase_count += 1
        has_anchor_connection = False
        for right in cluster.signals[index + 1 :]:
            reason = cannot_link_reason(left, right)
            if reason and reason != "insufficient_specific_anchor":
                return False
            if has_minimum_same_event_anchor(left, right):
                has_anchor_connection = True
        for right in cluster.signals[:index]:
            if has_minimum_same_event_anchor(left, right):
                has_anchor_connection = True
        if not has_anchor_connection:
            return False
    if coherent_phrase_count < max(2, math.ceil(len(cluster.signals) * 0.66)):
        return False
    for signal in cluster.signals:
        if signal is cluster.seed_signal:
            continue
        similarities = sorted(
            [
                cosine_similarity(signal.clustering_embedding, other.clustering_embedding)
                for other in cluster.signals
                if other.identifier != signal.identifier
            ],
            reverse=True,
        )
        # Avoid bridge-only connected components: every article should connect to the centroid/fingerprint,
        # not just to one weakly related neighbour.
        if similarities and similarities[0] < 0.50 and event_phrase_similarity_score(signal.fingerprint, cluster_fp) < 0.50:
            return False
    return True


def generate_multi_source_card(cluster: ClusterDraft) -> dict:
    seed_id = cluster.seed_article_id
    seed_article = next((article for article in cluster.articles if article.id == seed_id), cluster.articles[0])
    fingerprint = cluster.cluster_fingerprint or cluster.seed_signal.fingerprint
    return {
        "title": seed_article.headline,
        "headline": seed_article.headline,
        "primary_category": cluster.primary_category,
        "seed_article_id": seed_article.id,
        "article_ids": [article.id for article in cluster.articles],
        "source_count": len({article.source_name for article in cluster.articles}),
        "sources": sorted({article.source_name for article in cluster.articles}),
        "cluster_fingerprint": fingerprint.to_json(),
        "confidence_score": round(cluster.confidence_score, 4),
        "thumbnail_path": next((article.thumbnail_path for article in cluster.articles if article.thumbnail_path), None),
        "first_published_at": min(article.published_at for article in cluster.articles),
        "latest_published_at": max(article.published_at for article in cluster.articles),
    }


def log_cluster_decision(
    article_id: str | int | None,
    candidate_cluster_id: str | int | None,
    final_decision: str,
    decision: PairDecision | None = None,
    **details,
) -> None:
    payload = {
        "article_id": article_id,
        "candidate_cluster_id": candidate_cluster_id,
        "final_decision": final_decision,
    }
    if decision is not None:
        payload.update(decision_to_debug_dict(decision))
    payload.update({key: value for key, value in details.items() if value is not None})
    logger.debug("story_cluster_decision %s", json.dumps(payload, sort_keys=True, default=str))


def decision_to_debug_dict(decision: PairDecision) -> dict:
    return {
        "final_decision": decision.final_label,
        "final_score": decision.final_score,
        "category_threshold": decision.category_threshold,
        "title_similarity": decision.title_similarity,
        "summary_similarity": decision.summary_similarity,
        "centroid_similarity": decision.centroid_similarity,
        "seed_similarity": decision.seed_similarity,
        "best_article_similarity": decision.best_article_similarity,
        "entity_overlap": decision.entity_overlap,
        "event_phrase_similarity": decision.event_phrase_similarity,
        "action_match": decision.action_match,
        "object_match": decision.object_match,
        "date_match": decision.date_match,
        "location_match": decision.location_match,
        "number_match": decision.number_match,
        "source_boost": decision.source_boost,
        "cannot_link_triggered": bool(decision.cannot_link_reason),
        "cannot_link_reason": decision.cannot_link_reason,
        "explanation": decision.explanation,
    }


def debug_cluster_payload(cluster: ClusterDraft, decision: PairDecision) -> dict:
    return {
        "seed_article_id": cluster.seed_article_id,
        "article_ids": [article.id for article in cluster.articles],
        "sources": sorted({article.source_name for article in cluster.articles}),
        "cluster_fingerprint": asdict(cluster.cluster_fingerprint or cluster.seed_signal.fingerprint),
        "decision": decision_to_debug_dict(decision),
    }


def missing_fingerprint_fields(fingerprint: EventFingerprint) -> list[str]:
    missing: list[str] = []
    for field_name in (
        "main_event",
        "event_type",
        "main_entities",
        "action",
        "event_object",
        "location",
        "event_date",
        "important_numbers",
        "normalized_event_phrase",
    ):
        value = getattr(fingerprint, field_name)
        if value is None or value == "" or value == () or value == []:
            missing.append("object" if field_name == "event_object" else field_name)
    return missing


def category_auto_threshold(category: str | None) -> float:
    return CATEGORY_AUTO_THRESHOLDS.get(category or "General", MIN_FINAL_SCORE)


def classify_cluster_label(article: ArticleSignal, cluster: ClusterDraft, score: float, reason: str | None, threshold: float) -> str:
    if reason:
        return "related_development" if is_related_reason(reason) or looks_related_development(article.fingerprint, cluster.cluster_fingerprint or cluster.seed_signal.fingerprint) else "different_event"
    if score >= threshold:
        return "same_event"
    if len(cluster.signals) >= 2 and BORDERLINE_MERGE_SCORE <= score < threshold and borderline_evidence(article, cluster) >= 3:
        return "same_event"
    if score >= BORDERLINE_MERGE_SCORE or looks_related_development(article.fingerprint, cluster.cluster_fingerprint or cluster.seed_signal.fingerprint):
        return "related_development"
    return "different_event"


def classify_pair_label(left: ArticleSignal, right: ArticleSignal, score: float, reason: str | None, threshold: float) -> str:
    if reason:
        return "related_development" if is_related_reason(reason) or looks_related_development(left.fingerprint, right.fingerprint) else "different_event"
    if score >= threshold:
        return "same_event"
    if score >= BORDERLINE_MERGE_SCORE or looks_related_development(left.fingerprint, right.fingerprint):
        return "related_development"
    return "different_event"


def borderline_evidence(article: ArticleSignal, cluster: ClusterDraft) -> int:
    cluster_fp = cluster.cluster_fingerprint or cluster.seed_signal.fingerprint
    centroid_similarity = cosine_similarity(article.clustering_embedding, cluster.centroid_embedding or update_cluster_centroid(cluster))
    similar_article_count = sum(
        1 for existing in cluster.signals if cosine_similarity(article.clustering_embedding, existing.clustering_embedding) >= 0.80
    )
    return sum(
        [
            centroid_similarity >= 0.78,
            similar_article_count >= 2,
            bool(set(article.fingerprint.main_entities).intersection(cluster_fp.main_entities)),
            event_phrase_similarity_score(article.fingerprint, cluster_fp) >= 0.72,
            article.fingerprint.event_type == cluster_fp.event_type,
            any(hours_apart(article.published_at, existing.published_at) <= DEFAULT_TIME_WINDOW_HOURS for existing in cluster.signals),
            location_compatibility(article.fingerprint, cluster_fp) >= 0.86,
            number_compatibility(article.fingerprint, cluster_fp) >= 0.82,
            action_compatibility(article.fingerprint, cluster_fp) >= 0.68,
        ]
    )


def has_minimum_same_event_anchor(
    left: ArticleSignal,
    right: ArticleSignal,
    *,
    title_similarity: float | None = None,
    summary_similarity: float | None = None,
    embedding_similarity: float | None = None,
    event_phrase_similarity: float | None = None,
) -> bool:
    """Require one specific shared event anchor before two articles can merge.

    This prevents broad category/location matches such as "India", "RBI",
    "markets", or same-day publication from becoming same-story evidence on
    their own. High text similarity can still merge terse rewrites of the same
    story.
    """
    title_similarity = title_similarity if title_similarity is not None else title_semantic_similarity(left, right)
    summary_similarity = summary_similarity if summary_similarity is not None else summary_semantic_similarity(left, right)
    embedding_similarity = embedding_similarity if embedding_similarity is not None else cosine_similarity(
        left.clustering_embedding,
        right.clustering_embedding,
    )
    event_phrase_similarity = (
        event_phrase_similarity
        if event_phrase_similarity is not None
        else event_phrase_similarity_score(left.fingerprint, right.fingerprint)
    )
    specific_overlap = specific_entity_overlap(left.fingerprint, right.fingerprint)
    has_specific_entity = specific_overlap > 0
    has_shared_phrase = bool(left.token_profile.phrase_tokens.intersection(right.token_profile.phrase_tokens))
    has_significant_number = bool(shared_significant_numbers(left.fingerprint, right.fingerprint))
    exact_action_object = (
        action_compatibility(left.fingerprint, right.fingerprint) >= 0.90
        and object_compatibility(left.fingerprint, right.fingerprint) >= 0.85
        and left.fingerprint.event_object not in GENERIC_EVENT_OBJECTS
        and right.fingerprint.event_object not in GENERIC_EVENT_OBJECTS
    )
    same_specific_object = (
        left.fingerprint.event_object
        and right.fingerprint.event_object
        and left.fingerprint.event_object == right.fingerprint.event_object
        and left.fingerprint.event_object not in GENERIC_EVENT_OBJECTS
    )
    broad_event = left.fingerprint.event_type in BROAD_EVENT_TYPES or right.fingerprint.event_type in BROAD_EVENT_TYPES
    text_is_strong = (
        title_similarity >= MIN_ANCHOR_TITLE_SIMILARITY
        or summary_similarity >= MIN_ANCHOR_SUMMARY_SIMILARITY
        or embedding_similarity >= MIN_ANCHOR_EMBEDDING_SIMILARITY
    )

    if title_similarity >= 0.78 or summary_similarity >= 0.78:
        return True
    if title_similarity >= MIN_ANCHOR_TITLE_SIMILARITY and (
        has_specific_entity or has_shared_phrase or has_significant_number or same_specific_object
    ):
        return True
    if summary_similarity >= MIN_ANCHOR_SUMMARY_SIMILARITY and (
        has_specific_entity or has_shared_phrase or has_significant_number or same_specific_object
    ):
        return True
    if embedding_similarity >= MIN_ANCHOR_EMBEDDING_SIMILARITY and (
        has_specific_entity or has_shared_phrase or has_significant_number or exact_action_object
    ):
        return True
    if has_shared_phrase and (title_similarity >= 0.45 or summary_similarity >= 0.45 or embedding_similarity >= 0.58):
        return True
    if has_significant_number and (title_similarity >= 0.45 or summary_similarity >= 0.45 or has_specific_entity):
        return True
    if has_specific_entity and exact_action_object and (
        event_phrase_similarity >= 0.72 or title_similarity >= 0.45 or summary_similarity >= 0.45
    ):
        return True
    if (
        left.fingerprint.event_type == right.fingerprint.event_type == "sports_match"
        and has_specific_entity
        and action_compatibility(left.fingerprint, right.fingerprint) >= 0.90
        and object_compatibility(left.fingerprint, right.fingerprint) >= 0.75
        and (title_similarity >= 0.35 or summary_similarity >= 0.35)
    ):
        return True
    if has_specific_entity and event_phrase_similarity >= 0.82 and not broad_event:
        return True
    if same_specific_object and text_is_strong:
        return True
    return False


def has_strong_same_event_evidence(
    *,
    title_similarity: float,
    summary_similarity: float,
    centroid_similarity: float,
    best_article_similarity: float,
    entity_overlap: float,
    event_phrase_similarity: float,
    action_match: float,
    object_match: float,
    date_match: float,
    number_match: float = 0.0,
) -> bool:
    exact_action_object = action_match >= 0.90 and object_match >= 0.85
    strong_text_signal = (
        centroid_similarity >= 0.78
        or best_article_similarity >= 0.80
        or title_similarity >= 0.78
        or summary_similarity >= 0.78
        or event_phrase_similarity >= 0.82
    )
    if date_match > 0 and entity_overlap >= 0.75 and exact_action_object and (
        strong_text_signal or title_similarity >= 0.62 or summary_similarity >= 0.62
    ):
        return True
    if date_match > 0 and object_match >= 0.90 and event_phrase_similarity >= 0.90 and title_similarity >= 0.55:
        return True
    if (
        date_match > 0
        and object_match >= 0.90
        and number_match >= 1.0
        and event_phrase_similarity >= 0.85
        and (title_similarity >= 0.55 or summary_similarity >= 0.55)
    ):
        return True
    if (
        date_match > 0
        and entity_overlap >= 0.75
        and action_match >= 0.90
        and object_match >= 0.75
        and (title_similarity >= 0.35 or summary_similarity >= 0.35)
        and event_phrase_similarity >= 0.55
    ):
        return True
    return (
        date_match > 0
        and exact_action_object
        and (
            centroid_similarity >= 0.78
            or best_article_similarity >= 0.80
            or title_similarity >= 0.78
            or summary_similarity >= 0.78
            or event_phrase_similarity >= 0.72
        )
        and (
            entity_overlap >= MIN_ENTITY_OVERLAP
            or (
                event_phrase_similarity >= 0.82
                and (
                    title_similarity >= 0.45
                    or summary_similarity >= 0.62
                    or centroid_similarity >= 0.72
                    or best_article_similarity >= 0.72
                )
            )
        )
    )


def is_related_reason(reason: str | None) -> bool:
    return bool(reason and ("related" in reason or reason in {"cluster_event_type_conflict"}))


def looks_related_development(left: EventFingerprint, right: EventFingerprint | None) -> bool:
    if right is None:
        return False
    if frozenset({left.event_type, right.event_type}) in RELATED_EVENT_TYPE_PAIRS:
        return True
    shared_entity = bool(set(left.main_entities).intersection(right.main_entities))
    if shared_entity and left.event_type != right.event_type:
        return True
    if event_phrase_similarity_score(left, right) >= 0.65 and action_compatibility(left, right) < 0.9:
        return True
    return False


def decision_explanation(label: str, score: float, threshold: float, reason: str | None) -> str:
    if reason:
        return f"{label}: blocked by {reason}; score={score:.4f}, threshold={threshold:.4f}"
    if label == "same_event":
        return f"same_event: score={score:.4f} met threshold={threshold:.4f} with no cannot-link conflict"
    return f"{label}: score={score:.4f} below same-event threshold={threshold:.4f}"


def event_phrase_similarity_score(left: EventFingerprint, right: EventFingerprint) -> float:
    left_phrase = left.normalized_event_phrase or left.main_event
    right_phrase = right.normalized_event_phrase or right.main_event
    if not left_phrase or not right_phrase:
        return 0.72
    return text_similarity(left_phrase, right_phrase)


def action_conflict(left_action: str | None, right_action: str | None, left_type: str | None = None, right_type: str | None = None) -> bool:
    if not left_action or not right_action or left_action == right_action:
        return False
    if frozenset({left_action, right_action}) in CONFLICTING_ACTION_PAIRS:
        return True
    if left_type == right_type and left_type in {"court_order", "monetary_policy", "sports_match"}:
        return True
    return False


def title_semantic_similarity(left: ArticleSignal, right: ArticleSignal) -> float:
    score = text_similarity(left.title, right.title, left.title_embedding, right.title_embedding)
    if (
        main_entity_overlap(left.fingerprint, right.fingerprint) >= MIN_ENTITY_OVERLAP
        and action_compatibility(left.fingerprint, right.fingerprint) >= 0.9
        and object_compatibility(left.fingerprint, right.fingerprint) >= 0.8
    ):
        score = max(score, MIN_TITLE_SIMILARITY)
    return score


def summary_semantic_similarity(left: ArticleSignal, right: ArticleSignal) -> float:
    if not left.summary_text or not right.summary_text:
        return 0.72
    score = text_similarity(left.summary_text, right.summary_text, left.summary_embedding, right.summary_embedding)
    if (
        main_entity_overlap(left.fingerprint, right.fingerprint) >= MIN_ENTITY_OVERLAP
        and action_compatibility(left.fingerprint, right.fingerprint) >= 0.9
        and object_compatibility(left.fingerprint, right.fingerprint) >= 0.8
    ):
        score = max(score, MIN_SUMMARY_SIMILARITY)
    return score


def text_similarity(left: str, right: str, left_embedding: tuple[float, ...] | None = None, right_embedding: tuple[float, ...] | None = None) -> float:
    left_tokens = set(_ordered_tokens(left))
    right_tokens = set(_ordered_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0
    lexical = _token_similarity(left_tokens, right_tokens)
    containment = len(left_tokens.intersection(right_tokens)) / max(1, min(len(left_tokens), len(right_tokens)))
    embedding = cosine_similarity(left_embedding or text_embedding(left), right_embedding or text_embedding(right))
    return min(1.0, max(embedding, lexical * 0.42 + containment * 0.58))


def text_embedding(text: str | None, dimensions: int = 96) -> tuple[float, ...]:
    semantic = sentence_transformer_embedding(text)
    if semantic is not None:
        return semantic
    vector = [0.0] * dimensions
    for token in _ordered_tokens(text or ""):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if int.from_bytes(digest[4:], "big") % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return tuple(vector)
    return tuple(value / norm for value in vector)


_SENTENCE_MODEL = None
_SENTENCE_MODEL_ATTEMPTED = False


def sentence_transformer_embedding(text: str | None) -> tuple[float, ...] | None:
    global _SENTENCE_MODEL, _SENTENCE_MODEL_ATTEMPTED
    if not text or os.getenv("NEWS_APP_USE_SENTENCE_TRANSFORMERS", "").lower() not in {"1", "true", "yes"}:
        return None
    if not _SENTENCE_MODEL_ATTEMPTED:
        _SENTENCE_MODEL_ATTEMPTED = True
        try:
            from sentence_transformers import SentenceTransformer

            model_name = os.getenv("NEWS_APP_SENTENCE_TRANSFORMER_MODEL", "all-MiniLM-L6-v2")
            _SENTENCE_MODEL = SentenceTransformer(model_name)
        except Exception:
            logger.exception("SentenceTransformer embedding model unavailable; falling back to hashed embeddings")
            _SENTENCE_MODEL = None
    if _SENTENCE_MODEL is None:
        return None
    embedding = _SENTENCE_MODEL.encode(text, normalize_embeddings=True)
    return tuple(float(value) for value in embedding)


def cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right))))


def specific_entity_overlap(left: EventFingerprint, right: EventFingerprint) -> float:
    left_entities = specific_entities(left)
    right_entities = specific_entities(right)
    if not left_entities or not right_entities:
        return 0.0
    return len(left_entities.intersection(right_entities)) / max(1, min(len(left_entities), len(right_entities)))


def shared_specific_entities(left: EventFingerprint, right: EventFingerprint) -> set[str]:
    return specific_entities(left).intersection(specific_entities(right))


def specific_entities(fingerprint: EventFingerprint) -> set[str]:
    if fingerprint.category == "Sports" or fingerprint.event_type in {"sports_match", "sports_selection"}:
        return set(fingerprint.main_entities).union(fingerprint.secondary_entities)
    return {
        entity
        for entity in set(fingerprint.main_entities).union(fingerprint.secondary_entities)
        if entity and entity not in BROAD_ENTITY_VALUES
    }


def shared_significant_numbers(left: EventFingerprint, right: EventFingerprint) -> set[str]:
    return significant_numbers(left).intersection(significant_numbers(right))


def significant_numbers(fingerprint: EventFingerprint) -> set[str]:
    result: set[str] = set()
    for value in fingerprint.important_numbers:
        if _is_significant_number(value):
            result.add(value)
    return result


def _is_significant_number(value: str) -> bool:
    if not value:
        return False
    if "%" in value or "inr" in value:
        return True
    number_match = re.search(r"\d+(?:\.\d+)?", value)
    if not number_match:
        return False
    try:
        numeric = float(number_match.group(0))
    except ValueError:
        return False
    if numeric <= 31:
        return False
    if numeric.is_integer() and 1900 <= numeric <= 2099:
        return False
    return numeric >= 100 or not numeric.is_integer()


def main_entity_overlap(left: EventFingerprint, right: EventFingerprint) -> float:
    left_entities = set(left.main_entities)
    right_entities = set(right.main_entities)
    if not left_entities and not right_entities:
        return 0.0
    if not left_entities or not right_entities:
        return 0.0
    specific = specific_entity_overlap(left, right)
    if specific > 0:
        return specific
    broad_direct = left_entities.intersection(right_entities).intersection(BROAD_ENTITY_VALUES)
    if broad_direct:
        return 0.15
    direct = len(left_entities.intersection(right_entities)) / max(1, min(len(left_entities), len(right_entities)))
    if direct > 0:
        return direct
    # Cluster fingerprints keep secondary entities from all member articles.
    # Treat a main-vs-secondary match as strong positive evidence, not a conflict.
    left_all = left_entities.union(left.secondary_entities)
    right_all = right_entities.union(right.secondary_entities)
    if specific_entities(left).intersection(specific_entities(right)):
        return 0.75
    return 0.0


def action_compatibility(left: EventFingerprint, right: EventFingerprint) -> float:
    if left.action and right.action:
        if left.action == right.action:
            return 1.0
        if action_conflict(left.action, right.action, left.event_type, right.event_type):
            return 0.0
        return 0.68
    return 0.72


def object_compatibility(left: EventFingerprint, right: EventFingerprint) -> float:
    if left.event_object and right.event_object:
        if {left.event_object, right.event_object}.issubset({"gilgit-baltistan election", "election"}):
            return 0.92
        if {left.event_object, right.event_object}.issubset({"repo rate", "interest rate", "policy"}):
            return 0.88
        if {left.event_object, right.event_object}.issubset({"match", "series", "tournament"}):
            return 0.78
        return text_similarity(left.event_object, right.event_object)
    return 0.72


def location_compatibility(left: EventFingerprint, right: EventFingerprint) -> float:
    if left.location and right.location:
        return 1.0 if left.location == right.location else 0.0
    return 0.86


def date_compatibility(left: ArticleSignal, right: ArticleSignal) -> float:
    hours = hours_apart(left.published_at, right.published_at)
    if hours <= 24:
        return 1.0
    if hours <= 48:
        return 0.94
    if hours <= 72:
        return 0.86
    return 0.0


def number_compatibility(left: EventFingerprint, right: EventFingerprint) -> float:
    left_numbers = set(left.important_numbers)
    right_numbers = set(right.important_numbers)
    if not left_numbers and not right_numbers:
        return 1.0
    if not left_numbers or not right_numbers:
        return 0.82
    if left_numbers.intersection(right_numbers):
        return 1.0
    if left.event_type == right.event_type == "monetary_policy":
        return 0.88
    return 0.45


def category_compatibility_score(left: str | None, right: str | None) -> float:
    left_category = left or "General"
    right_category = right or "General"
    if left_category == right_category:
        return 1.0
    if "General" in {left_category, right_category}:
        return 0.84
    return 0.0


def event_type_conflict(left: str, right: str) -> bool:
    if left == right:
        return False
    if frozenset({left, right}) in RELATED_EVENT_TYPE_PAIRS:
        return True
    if left in SENSITIVE_EVENT_TYPES and right in SENSITIVE_EVENT_TYPES:
        return True
    return False


def location_conflict(left: EventFingerprint, right: EventFingerprint) -> bool:
    if not left.location or not right.location or left.location == right.location:
        return False
    return left.event_type in SENSITIVE_EVENT_TYPES or right.event_type in SENSITIVE_EVENT_TYPES


def entity_conflict(left: EventFingerprint, right: EventFingerprint) -> bool:
    left_entities = set(left.main_entities)
    right_entities = set(right.main_entities)
    if not left_entities or not right_entities or left_entities.intersection(right_entities):
        return False
    if (
        left.event_type == right.event_type == "monetary_policy"
        and action_compatibility(left, right) >= 0.68
        and object_compatibility(left, right) >= 0.70
    ):
        return False
    if (
        left.event_type == right.event_type == "sports_match"
        and action_compatibility(left, right) >= 0.9
        and object_compatibility(left, right) >= 0.70
    ):
        return False
    if left.event_type in SENSITIVE_EVENT_TYPES or right.event_type in SENSITIVE_EVENT_TYPES:
        return True
    return left.category in SENSITIVE_CATEGORIES and right.category in SENSITIVE_CATEGORIES


def object_conflict(left: EventFingerprint, right: EventFingerprint) -> bool:
    if not left.event_object or not right.event_object:
        return False
    if object_compatibility(left, right) >= 0.65:
        return False
    return left.event_type in SENSITIVE_EVENT_TYPES or right.event_type in SENSITIVE_EVENT_TYPES


def numeric_conflict(left: EventFingerprint, right: EventFingerprint) -> bool:
    critical_types = {"accident", "death", "election", "finance_result", "sports_match"}
    if left.event_type not in critical_types and right.event_type not in critical_types:
        return False
    left_numbers = set(left.important_numbers)
    right_numbers = set(right.important_numbers)
    return bool(left_numbers and right_numbers and not left_numbers.intersection(right_numbers))


def is_sensitive_pair(left: ArticleSignal, right: ArticleSignal) -> bool:
    return (
        left.category in SENSITIVE_CATEGORIES
        or right.category in SENSITIVE_CATEGORIES
        or left.fingerprint.event_type in SENSITIVE_EVENT_TYPES
        or right.fingerprint.event_type in SENSITIVE_EVENT_TYPES
    )


def hours_apart(left: datetime | None, right: datetime | None) -> float:
    if left is None or right is None:
        return 0.0
    return abs((left - right).total_seconds()) / 3600


def extract_entities(text: str | None) -> list[str]:
    if not text:
        return []
    text = text[:1200]
    entities: list[str] = []
    for pattern, canonical in KNOWN_ENTITY_PATTERNS:
        if pattern.search(text):
            entities.append(canonical)
    lowered = text.lower()
    for alias, canonical in ENTITY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            entities.append(canonical)
    for match in ACRONYM_RE.finditer(text):
        entity = canonical_entity(match.group(0))
        if entity:
            entities.append(entity)
    for match in ENTITY_PHRASE_RE.finditer(text):
        entity = canonical_entity(match.group(0))
        if entity:
            entities.append(entity)
    return _dedupe(entities)


def canonical_entity(raw: str) -> str | None:
    clean = re.sub(r"\s+", " ", raw.strip(" .,;:!?\"'()[]{}"))
    if not clean:
        return None
    normalized_tokens = _ordered_tokens(clean)
    if not normalized_tokens:
        return None
    normalized = " ".join(normalized_tokens)
    if normalized in ENTITY_ALIASES:
        return ENTITY_ALIASES[normalized]
    if len(normalized_tokens) == 1:
        token = normalized_tokens[0]
        if token in ENTITY_ALIASES:
            return ENTITY_ALIASES[token]
        if token in KNOWN_LOCATIONS:
            return KNOWN_LOCATIONS[token]
        return None
    if any(token in EVENT_TOKENS for token in normalized_tokens):
        return None
    if any(token in {"grant", "grants", "reject", "rejects", "cuts", "cut", "rises", "beats", "announces"} for token in normalized_tokens):
        return None
    if normalized_tokens[0] in {"court", "police", "government", "official"} and len(normalized_tokens) < 3:
        return None
    return ENTITY_ALIASES.get(normalized, normalized)


def extract_action(text: str | None) -> str | None:
    if not text:
        return None
    matches: list[tuple[int, str]] = []
    for action, pattern in ACTION_PATTERNS:
        match = pattern.search(text)
        if match:
            matches.append((match.start(), action))
    if not matches:
        return None
    return sorted(matches, key=lambda item: item[0])[0][1]


def extract_object(title: str, text: str, entities: list[str], action: str | None) -> str | None:
    for value, pattern in OBJECT_PATTERNS:
        if pattern.search(title):
            return value
    for value, pattern in OBJECT_PATTERNS:
        if pattern.search(text):
            return value
    entity_tokens = set(_ordered_tokens(" ".join(entities)))
    action_tokens = set(_ordered_tokens(action or ""))
    tokens = [
        token
        for token in _ordered_tokens(title)
        if token not in entity_tokens and token not in action_tokens and token not in WEAK_TOKENS
    ]
    return " ".join(tokens[:4]) if tokens else None


def infer_event_type(category: str, text: str, action: str | None, event_object: str | None) -> str:
    tokens = set(_ordered_tokens(text))
    obj = event_object or ""
    financial_market_tokens = {"sensex", "nifty", "stock", "stocks", "equity", "equities", "share", "shares", "bourse"}
    if obj in {"repo rate", "interest rate"} and action in {"cut", "raise", "hold"}:
        return "monetary_policy"
    if action in {"market_rise", "market_fall"} and (obj == "stock market" or tokens.intersection(financial_market_tokens)):
        return "market_reaction"
    if obj == "stock market" and tokens.intersection(financial_market_tokens):
        return "market_reaction"
    if obj in {"gilgit-baltistan election", "election"} or tokens.intersection({"election", "poll", "polls", "vote", "votes"}):
        return "election"
    if obj in {"trade pact"}:
        return "trade_deal"
    if obj in {"forex reserves", "debt inflows", "gdp growth", "jobs report"}:
        return "economy"
    if obj in {"seafood exports", "oil meal imports"}:
        return "trade"
    if obj in {"regional aircraft", "fighter aircraft", "aircraft production", "drug safety", "data centres"}:
        return "industry"
    if obj in {"vegan conference", "protest"}:
        return "event"
    if obj in {"fuel prices"}:
        return "energy"
    if obj in {"maritime security", "world bank appointment"}:
        return "diplomacy"
    if obj == "bail" or "court" in tokens or action in {"grant_bail", "reject_bail", "order"}:
        return "court_order"
    if category == "Sports" and action == "announce":
        return "sports_selection"
    if category == "Sports" or action in {"beat", "lose"} or obj in {"match", "tournament"}:
        return "sports_match"
    if tokens.intersection({"profit", "revenue", "earnings"}) or obj in {"profit", "revenue"}:
        return "finance_result"
    if action == "murder" or "murder" in tokens:
        return "crime"
    if action == "death":
        return "death"
    if obj in {"fire", "house fire", "collapse", "wall collapse", "explosion", "crash"}:
        return "accident"
    if action in {"cut", "raise", "hold"} and category == "Business":
        return "business_policy"
    return (category or "general").lower()


def extract_location(text: str | None) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    for raw, canonical in sorted(KNOWN_LOCATIONS.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(raw)}\b", lowered):
            return canonical
    return None


def extract_event_date(text: str | None) -> str | None:
    if not text:
        return None
    match = DATE_RE.search(text)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(0).strip().lower())


def extract_important_numbers(text: str | None) -> list[str]:
    if not text:
        return []
    numbers = [normalize_number_phrase(match.group(0)) for match in NUMBER_RE.finditer(text)]
    return _dedupe(numbers)[:8]


def normalize_number_phrase(raw: str) -> str:
    value = re.sub(r"\s+", " ", raw.strip().lower().replace(",", ""))
    numeric_match = re.search(r"\d+(?:\.\d+)?", value)
    if not numeric_match:
        return value
    number = float(numeric_match.group(0))
    if "basis point" in value or "bps" in value:
        return f"{number / 100:g}%"
    if "percent" in value or "%" in value:
        return f"{number:g}%"
    if "crore" in value:
        return f"{int(number * 10000000)} inr"
    if "lakh" in value:
        return f"{int(number * 100000)}"
    if "million" in value:
        return f"{int(number * 1000000)}"
    if "billion" in value:
        return f"{int(number * 1000000000)}"
    return value


def compose_normalized_event_phrase(
    entities: tuple[str, ...],
    action: str | None,
    event_object: str | None,
    event_type: str,
    text: str | None = None,
) -> str:
    # Prefer the event skeleton over long text; long article bodies create background noise.
    primary_entity = entities[0] if entities else ""
    parts = [primary_entity, action or event_type, event_object or ""]
    phrase = " ".join(part for part in parts if part).strip()
    if phrase:
        return " ".join(_ordered_tokens(phrase))
    tokens = _ordered_tokens(text or "")
    return " ".join(tokens[:8]) if tokens else event_type


def compose_main_event(entities: tuple[str, ...], action: str | None, event_object: str | None, event_type: str) -> str:
    parts = []
    if entities:
        parts.append(entities[0])
    if action:
        parts.append(action)
    if event_object:
        parts.append(event_object)
    return " ".join(parts) if parts else event_type


def first_paragraphs(content: str | None, limit: int = 2) -> str:
    if not content:
        return ""
    paragraphs = [part.strip() for part in re.split(r"\n{2,}|\r\n{2,}", content) if part.strip()]
    if not paragraphs:
        paragraphs = [part.strip() for part in re.split(r"(?<=[.!?])\s+", content) if part.strip()]
    return " ".join(paragraphs[:limit])


def story_tokens(*parts: str | None) -> set[str]:
    return story_token_profile(*parts).all_tokens


def story_token_profile(*parts: str | None) -> TokenProfile:
    texts = [part for part in parts if part]
    ordered_tokens: list[str] = []
    phrase_tokens: set[str] = set()
    for text in texts:
        text_tokens = _ordered_tokens(text)
        ordered_tokens.extend(text_tokens)
        phrase_tokens.update(_phrase_tokens(text, text_tokens))
    all_tokens = set(ordered_tokens)
    weak_tokens = {token for token in all_tokens if _is_weak_token(token)}
    strong_tokens = (all_tokens - weak_tokens).union(phrase_tokens)
    return TokenProfile(
        all_tokens=all_tokens,
        strong_tokens=strong_tokens,
        weak_tokens=weak_tokens,
        phrase_tokens=phrase_tokens,
    )


def same_story_match(left: TokenProfile, right: TokenProfile) -> bool:
    if len(left.all_tokens) < 3 or len(right.all_tokens) < 3:
        return False

    shared_strong = left.strong_tokens.intersection(right.strong_tokens)
    if len(shared_strong) < MIN_ANCHORED_SHARED_STRONG:
        return False

    score = story_match_score(left, right)
    if score >= MIN_STRICT_SIMILARITY and len(shared_strong) >= MIN_STRICT_SHARED_STRONG:
        return True

    shared_phrases = left.phrase_tokens.intersection(right.phrase_tokens)
    return (
        score >= MIN_ANCHORED_SIMILARITY
        and len(shared_strong) >= MIN_ANCHORED_SHARED_STRONG
        and bool(shared_phrases)
    )


def story_match_score(left: TokenProfile, right: TokenProfile) -> float:
    return max(
        _token_similarity(left.all_tokens, right.all_tokens),
        _token_similarity(left.strong_tokens, right.strong_tokens),
    )


def categories_compatible(left: str | None, right: str | None) -> bool:
    left_category = left or "General"
    right_category = right or "General"
    return left_category == right_category or "General" in {left_category, right_category}


def _ordered_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for token in TOKEN_RE.findall(text.lower()):
        normalized = _normalize_token(token)
        if normalized and normalized not in STOPWORDS:
            tokens.append(normalized)
    return tokens


def _phrase_tokens(text: str, ordered_tokens: list[str]) -> set[str]:
    phrases: set[str] = set()
    lower = text.lower()
    token_set = set(ordered_tokens)

    if re.search(r"\bdu\b", text, flags=re.IGNORECASE) and token_set.intersection(
        {"assistant", "college", "professor", "university"}
    ):
        phrases.add("delhi_university")

    for size in (2, 3):
        for index in range(0, len(ordered_tokens) - size + 1):
            candidate = tuple(ordered_tokens[index : index + size])
            if _is_anchor_phrase(candidate):
                phrases.add("_".join(candidate))

    for match in CAPITALIZED_PHRASE_RE.finditer(text):
        candidate = tuple(_ordered_tokens(match.group(0)))
        if len(candidate) >= 2 and _is_anchor_phrase(candidate):
            phrases.add("_".join(candidate))

    for phrase in ANCHOR_PHRASES:
        raw_phrase = " ".join(phrase)
        if raw_phrase in lower:
            phrases.add("_".join(phrase))

    return phrases


def _is_anchor_phrase(tokens: tuple[str, ...]) -> bool:
    if len(tokens) < 2:
        return False
    if tokens in ANCHOR_PHRASES:
        return True
    if any(token in EVENT_TOKENS for token in tokens):
        return False
    if any(_is_weak_token(token) for token in tokens):
        return False
    return sum(1 for token in tokens if len(token) >= 4) >= 2


def _is_weak_token(token: str) -> bool:
    return token.isdigit() or token in WEAK_TOKENS


def _normalize_token(token: str) -> str | None:
    token = token.lower()
    if token.isdigit():
        return token
    normalized = CANONICAL_TOKENS.get(token, token)
    if len(normalized) > 4 and normalized.endswith("s"):
        normalized = normalized[:-1]
    if len(normalized) < 3:
        return None
    return CANONICAL_TOKENS.get(normalized, normalized)


def _token_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left.intersection(right)) / len(left.union(right))


def token_similarity(left: set[str], right: set[str]) -> float:
    return _token_similarity(left, right)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
