import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_app.db import init_db
from news_app.logging_config import configure_logging
from news_app.services.ingestion_service import run_recent_ingestion


def main() -> None:
    configure_logging()
    init_db()
    stats = run_recent_ingestion()
    print(stats)


if __name__ == "__main__":
    main()
