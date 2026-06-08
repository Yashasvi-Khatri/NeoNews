import sys
import types

import pytest
from pydantic import ValidationError

from news_app.config import Settings
from news_app.llm.client import LocalLLMClient


def test_qwen35_uses_mlx_vlm_without_ollama(monkeypatch):
    calls = {}

    mlx_vlm = types.ModuleType("mlx_vlm")
    mlx_vlm_utils = types.ModuleType("mlx_vlm.utils")
    mlx_vlm_prompt_utils = types.ModuleType("mlx_vlm.prompt_utils")

    def fake_load(model_name):
        calls["loaded_model"] = model_name
        return "model", types.SimpleNamespace(tokenizer=types.SimpleNamespace())

    def fake_load_config(model_name):
        calls["loaded_config"] = model_name
        return {"model_type": "qwen3_5"}

    def fake_apply_chat_template(processor, config, prompt, num_images=0, enable_thinking=True):
        calls["template_prompt"] = prompt
        calls["num_images"] = num_images
        calls["enable_thinking"] = enable_thinking
        return f"FORMATTED::{prompt}"

    def fake_generate(model, processor, prompt, **kwargs):
        calls["prompt"] = prompt
        calls["kwargs"] = kwargs
        return types.SimpleNamespace(text='<think>hidden</think> "RBI cuts repo rate"')

    mlx_vlm.load = fake_load
    mlx_vlm.generate = fake_generate
    mlx_vlm_utils.load_config = fake_load_config
    mlx_vlm_prompt_utils.apply_chat_template = fake_apply_chat_template
    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_vlm)
    monkeypatch.setitem(sys.modules, "mlx_vlm.utils", mlx_vlm_utils)
    monkeypatch.setitem(sys.modules, "mlx_vlm.prompt_utils", mlx_vlm_prompt_utils)

    LocalLLMClient._mlx_loaded_cache_key = None
    LocalLLMClient._mlx_model = None
    LocalLLMClient._mlx_processor = None
    LocalLLMClient._mlx_config = None

    settings = Settings(
        llm_provider="mlx",
        llm_model="mlx-community/Qwen3.5-4B-MLX-8bit",
        mlx_engine="vlm",
        mlx_temperature=0.2,
        mlx_top_p=0.9,
    )

    result = LocalLLMClient(settings).generate("system", "article", max_tokens=120)

    assert result == "RBI cuts repo rate"
    assert calls["loaded_model"] == "mlx-community/Qwen3.5-4B-MLX-8bit"
    assert calls["loaded_config"] == "mlx-community/Qwen3.5-4B-MLX-8bit"
    assert calls["num_images"] == 0
    assert calls["enable_thinking"] is False
    assert calls["template_prompt"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "article"},
    ]
    assert calls["prompt"].startswith("FORMATTED::")
    assert calls["kwargs"]["max_tokens"] == 120
    assert calls["kwargs"]["temperature"] == 0.2
    assert calls["kwargs"]["top_p"] == 0.9


def test_ollama_provider_is_not_accepted():
    with pytest.raises(ValidationError):
        Settings(llm_provider="ollama")


def test_clean_response_removes_thinking_markdown_and_outer_quotes():
    settings = Settings(llm_provider="disabled")
    client = LocalLLMClient(settings)

    cleaned = client._clean_response('<think>reasoning</think>\n\n**"Markets rally after policy update"**')

    assert cleaned == "Markets rally after policy update"
