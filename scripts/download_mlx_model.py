import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huggingface_hub import snapshot_download

from news_app.config import get_settings
from news_app.llm import LocalLLMClient
from news_app.logging_config import configure_logging


def main() -> None:
    configure_logging()
    settings = get_settings()
    if settings.llm_provider != "mlx":
        print(f"LLM_PROVIDER is {settings.llm_provider}; set LLM_PROVIDER=mlx to use local MLX.")
        return

    print(f"Downloading MLX model: {settings.llm_model}")
    print(f"MLX engine: {settings.mlx_engine}")
    model_path = snapshot_download(repo_id=settings.llm_model)
    print(f"Model cached at: {model_path}")

    print("Loading model through MLX and running warmup generation...")
    output = LocalLLMClient(settings).warm_up()
    print(f"Warmup output: {output}")


if __name__ == "__main__":
    main()
