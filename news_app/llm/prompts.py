from __future__ import annotations

from news_app.config import get_settings

HEADLINE_SYSTEM_PROMPT = """You write factual news headlines.
Return one headline only. Do not include reasoning, bullets, quotes, or labels."""

SUMMARY_SYSTEM_PROMPT = """You summarize news articles neutrally and factually.
Return one concise paragraph only. Do not include reasoning, bullets, citations, or labels."""


def headline_prompt(title: str, description: str | None, content: str | None) -> str:
    settings = get_settings()
    body = content or description or ""
    return f"""Create a clear news heading of 15 words or less.

Original title:
{title}

Article text:
{body[:settings.headline_input_chars]}
"""


def summary_prompt(headline: str, source_name: str, content: str | None, description: str | None) -> str:
    settings = get_settings()
    body = content or description or headline
    return f"""Summarize this news item in 200 words or less.
Keep the summary neutral, specific, and useful. Do not invent facts.

Headline:
{headline}

Source:
{source_name}

Article text:
{body[:settings.summary_input_chars]}
"""


def story_summary_prompt(title: str, articles: list[dict[str, str | None]]) -> str:
    settings = get_settings()
    source_blocks = []
    for index, article in enumerate(articles[:8], start=1):
        source_blocks.append(
            f"""Source {index}: {article.get("source_name") or "Unknown"}
Headline: {article.get("headline") or ""}
Description: {article.get("description") or ""}
Text excerpt: {(article.get("content") or "")[:900]}
"""
        )
    body = "\n".join(source_blocks)
    return f"""Summarize this single news story using only the facts that appear across these source articles.
Write one neutral paragraph in 180 words or less. Mention uncertainty if the sources disagree.

Story title:
{title}

Sources:
{body[:settings.summary_input_chars]}
"""
