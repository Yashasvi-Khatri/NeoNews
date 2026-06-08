from __future__ import annotations

from datetime import date, datetime, timezone

from news_app.db.models import NewsArticle
from news_app.services.story_clustering import StoryClusteringService


def _article(article_id: int, title: str, source: str, category: str = "Business", description: str = "") -> NewsArticle:
    article = NewsArticle()
    article.id = article_id
    article.url_hash = f"hash-{article_id}"
    article.headline = title
    article.original_title = title
    article.description = description
    article.summary = None
    article.content = description
    article.source_name = source
    article.language = "en"
    article.primary_category = category
    article.published_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    article.collected_for_date = date(2026, 6, 1)
    article.thumbnail_path = None
    return article


def _drafts(*articles: NewsArticle):
    service = StoryClusteringService(repository=object())
    return service._build_drafts(list(articles), constrain_date=False)


def _cluster_ids(drafts):
    return [set(article.id for article in draft.articles) for draft in drafts]


def test_rbi_same_event_recall_without_market_reaction_merge():
    drafts = _drafts(
        _article(1, "RBI cuts repo rate by 25 bps", "A", description="The Reserve Bank of India lowered the repo rate after the MPC meeting."),
        _article(2, "Reserve Bank lowers repo rate to 5.75%", "B", description="RBI lowered the repo rate to 5.75 percent after its policy meeting."),
        _article(3, "MPC reduces policy rate by 0.25%", "C", description="The monetary policy committee reduced the policy rate by 25 basis points."),
        _article(4, "Sensex rises after RBI rate cut", "D", description="Bank stocks rose after investors reacted to the RBI policy decision."),
    )

    clusters = _cluster_ids(drafts)

    assert {1, 2, 3} in clusters
    assert {4} in clusters


def test_sports_match_is_same_event_but_squad_news_is_related_not_merged():
    drafts = _drafts(
        _article(1, "India beats Australia by 6 wickets", "A", category="Sports", description="India defeated Australia in a cricket match."),
        _article(2, "India defeat Australia in ODI series opener", "B", category="Sports", description="India won against Australia in the ODI opener."),
        _article(3, "India announces squad for Australia series", "C", category="Sports", description="India named its squad for the Australia series."),
    )

    clusters = _cluster_ids(drafts)

    assert {1, 2} in clusters
    assert {3} in clusters


def test_bail_decision_blocks_different_people_and_opposite_outcome():
    drafts = _drafts(
        _article(1, "Court grants bail to Arjun Mehta in fraud case", "A", category="India", description="The court granted bail to Arjun Mehta in a fraud case."),
        _article(2, "Arjun Mehta gets bail from Delhi court", "B", category="India", description="A court granted bail to Arjun Mehta after a hearing."),
        _article(3, "Court rejects bail plea of Rohan Sharma", "C", category="India", description="The court rejected bail for Rohan Sharma."),
    )

    clusters = _cluster_ids(drafts)

    assert {1, 2} in clusters
    assert {3} in clusters


def test_broad_india_business_terms_do_not_create_multi_source_story():
    drafts = _drafts(
        _article(
            1,
            "India aims to nearly quadruple seafood exports to $30 billion by 2031",
            "A",
            category="India",
            description="Piyush Goyal said India can grow seafood exports with better fishing regulation.",
        ),
        _article(
            2,
            "China Surges 900% in Oil Meal Imports From India Amid Feed Diversification",
            "B",
            category="India",
            description="China is buying more Indian oil meal as it diversifies animal feed suppliers.",
        ),
        _article(
            3,
            "India Removes Capital Gains Tax for FPIs on Government Bonds",
            "C",
            category="India",
            description="India scrapped capital gains tax for foreign portfolio investors in government bonds.",
        ),
        _article(
            4,
            "AirTrunk plans $30 billion India investment by 2030 to build AI data centres",
            "D",
            category="India",
            description="AirTrunk plans data centre investments for AI and cloud infrastructure.",
        ),
        _article(
            5,
            "India Eyes Up to 200 Russian Regional Aircraft as Local Production Talks Advance",
            "E",
            category="India",
            description="Indian airlines are discussing Russian SJ-100 and Il-114 regional aircraft production.",
        ),
        _article(
            6,
            "Putin Revives Su-57 Offer for India as AMCA Takes Shape",
            "F",
            category="India",
            description="Russia renewed its offer of Su-57 fighter jets as India develops the AMCA combat aircraft.",
        ),
    )

    clusters = _cluster_ids(drafts)

    assert {1} in clusters
    assert {2} in clusters
    assert {3} in clusters
    assert {4} in clusters
    assert {5} in clusters
    assert {6} in clusters


def test_gilgit_baltistan_election_story_merges_but_trade_pact_stays_separate():
    drafts = _drafts(
        _article(
            1,
            "India Protests Pakistans Election Plan in Gilgit-Baltistan, Reasserts Territorial Claim",
            "A",
            category="India",
            description="India objected to Pakistan holding elections in Gilgit-Baltistan.",
        ),
        _article(
            2,
            "India Lodges Strong Protest Against Pakistan Over Gilgit-Baltistan Assembly Elections",
            "B",
            category="India",
            description="New Delhi protested the Gilgit-Baltistan Assembly elections and reiterated its territorial claim.",
        ),
        _article(
            3,
            "India Censures Pakistan Over Plans to Hold General Elections in Illegally Occupied Gilgit-Baltistan",
            "C",
            category="India",
            description="The government said Gilgit-Baltistan is part of Jammu and Kashmir and Ladakh.",
        ),
        _article(
            4,
            "India and US May Execute First Phase of Trade Pact By Next Month",
            "D",
            category="India",
            description="Officials are discussing the first phase of an India-US trade pact.",
        ),
    )

    clusters = _cluster_ids(drafts)

    assert {1, 2, 3} in clusters
    assert {4} in clusters


def test_same_jobs_report_merges_but_maritime_security_does_not():
    drafts = _drafts(
        _article(
            1,
            "US adds 172,000 jobs in May, unemployment holds steady at 4.3%",
            "A",
            category="World",
            description="The US labour market added 172,000 jobs in May and unemployment held at 4.3 percent.",
        ),
        _article(
            2,
            "May jobs report: US payrolls rise by 172,000 as jobless rate holds at 4.3%",
            "B",
            category="World",
            description="US nonfarm payrolls rose 172,000 in May while the jobless rate stayed at 4.3 percent.",
        ),
        _article(
            3,
            "India and UK launch initiatives on maritime security and critical minerals",
            "C",
            category="World",
            description="India and the UK announced cooperation on maritime security and critical minerals.",
        ),
    )

    clusters = _cluster_ids(drafts)

    assert {1, 2} in clusters
    assert {3} in clusters


def test_unrelated_event_and_energy_stories_do_not_merge_on_country_or_date():
    drafts = _drafts(
        _article(
            1,
            "Vegan India Conference 2026 Returns to Mumbai This Weekend with Record Attendance",
            "A",
            category="India",
            description="The Vegan India Conference returns to Mumbai with talks and exhibitors.",
        ),
        _article(
            2,
            "CJP Founder Abhijeet Dipke Heads to India for Protest Against Education Minister",
            "B",
            category="India",
            description="Abhijeet Dipke is travelling to Delhi for a Jantar Mantar protest.",
        ),
        _article(
            3,
            "Iran-US conflict risks long war, keeping oil markets and fuel prices on edge",
            "C",
            category="World",
            description="Analysts said US-Iran tensions could keep crude oil and fuel prices volatile.",
        ),
        _article(
            4,
            "India appoints Neelkanth Mishra as World Bank Executive Director",
            "D",
            category="World",
            description="Neelkanth Mishra will represent India at the World Bank.",
        ),
    )

    clusters = _cluster_ids(drafts)

    assert {1} in clusters
    assert {2} in clusters
    assert {3} in clusters
    assert {4} in clusters
