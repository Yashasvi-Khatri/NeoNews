"""Small manual smoke test for the configured local MLX model."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_app.config import get_settings
from news_app.llm import LocalLLMClient
from news_app.logging_config import configure_logging


def main() -> None:
    configure_logging()
    settings = get_settings()
    client = LocalLLMClient(settings)
    text = client.generate(
        "You are a concise news editor.",
        "Write one short headline for: The Reserve Bank of India reduced the repo rate by 25 basis points.",
        max_tokens=40,
    )
    print(text)


if __name__ == "__main__":
    main()
