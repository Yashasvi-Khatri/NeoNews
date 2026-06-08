from news_app.services.article_enrichment import classify_article, estimate_reading_time, make_article_slug


def test_classify_article_uses_keyword_patterns():
    result = classify_article(
        "OpenAI launches new AI chip platform",
        "The company announced new software and data tools.",
        None,
        "North America",
    )

    assert result.category == "Tech"
    assert result.confidence > 0


def test_estimate_reading_time_has_one_minute_minimum():
    assert estimate_reading_time("") == 1
    assert estimate_reading_time("word " * 226, words_per_minute=225) == 2


def test_make_article_slug_adds_stable_hint():
    slug = make_article_slug("Markets rally after RBI policy update!", "abcdef123456")

    assert slug == "markets-rally-after-rbi-policy-update-abcdef12"
