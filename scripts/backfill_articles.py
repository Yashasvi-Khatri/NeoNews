import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_app.db import init_db
from news_app.logging_config import configure_logging
from news_app.services.backfill_service import backfill_article_enrichment


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill article category, reading-time, slug, and thumbnail data.")
    parser.add_argument("--thumbnails", action="store_true", help="Fetch publisher pages and cache compressed thumbnails.")
    parser.add_argument(
        "--feed-thumbnails",
        action="store_true",
        help="Fetch RSS feeds once and cache publisher-provided article images for matching articles.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of articles to inspect.")
    args = parser.parse_args()

    configure_logging()
    init_db()
    stats = backfill_article_enrichment(
        fetch_thumbnails=args.thumbnails,
        fetch_feed_thumbnails=args.feed_thumbnails,
        limit=args.limit,
    )
    print(
        "Backfill complete: "
        f"inspected={stats.inspected} updated={stats.updated} thumbnails_cached={stats.thumbnails_cached}"
    )


if __name__ == "__main__":
    main()
