from __future__ import annotations

import logging
import re
import threading
from typing import Any

from news_app.config import Settings

logger = logging.getLogger(__name__)


class LLMGenerationError(RuntimeError):
    pass


class LocalLLMClient:
    """Local LLM client.

    The default provider is direct MLX inference. Qwen 3.5 MLX community
    checkpoints are currently served through ``mlx-vlm`` even for text-only
    generation, while older text-only MLX checkpoints can still use ``mlx-lm``.
    No Ollama server is required or used.
    """

    _mlx_lock = threading.Lock()
    _mlx_loaded_cache_key: tuple[str, str] | None = None
    _mlx_model = None
    _mlx_processor = None
    _mlx_config = None

    def __init__(self, settings: Settings):
        self.settings = settings

    def generate(self, system_prompt: str, user_prompt: str, max_tokens: int = 300) -> str:
        if self.settings.llm_provider == "disabled":
            raise LLMGenerationError("LLM provider is disabled")
        try:
            if self.settings.llm_provider == "mlx":
                return self._generate_mlx(system_prompt, user_prompt, max_tokens)
            if self.settings.llm_provider == "openai_compatible":
                return self._generate_openai_compatible(system_prompt, user_prompt, max_tokens)
        except Exception as exc:
            logger.exception("Local LLM request failed")
            raise LLMGenerationError("Local LLM request failed") from exc
        raise LLMGenerationError(f"Unsupported LLM provider: {self.settings.llm_provider}")

    def batch_generate(self, system_prompt: str, user_prompts: list[str], max_tokens: int = 300) -> list[str]:
        if not user_prompts:
            return []
        if self.settings.llm_provider == "mlx":
            return self._batch_generate_mlx(system_prompt, user_prompts, max_tokens)
        return [self.generate(system_prompt, prompt, max_tokens=max_tokens) for prompt in user_prompts]

    def warm_up(self) -> str:
        return self.generate("You write concise news text.", self.settings.mlx_warmup_prompt, max_tokens=16)

    def _resolve_mlx_engine(self) -> str:
        if self.settings.mlx_engine != "auto":
            return self.settings.mlx_engine
        model_name = self.settings.llm_model.lower()
        if "qwen3.5" in model_name or "qwen3_5" in model_name or "mlx-vlm" in model_name:
            return "vlm"
        return "lm"

    def _load_mlx(self) -> tuple[str, Any, Any, Any]:
        engine = self._resolve_mlx_engine()
        cache_key = (engine, self.settings.llm_model)
        with self._mlx_lock:
            if self._mlx_loaded_cache_key == cache_key and self._mlx_model is not None:
                return engine, self._mlx_model, self._mlx_processor, self._mlx_config

            logger.info("Loading MLX model with %s: %s", engine, self.settings.llm_model)
            if engine == "vlm":
                try:
                    from mlx_vlm import load
                    from mlx_vlm.utils import load_config
                except ImportError as exc:
                    raise LLMGenerationError(
                        "Qwen 3.5 MLX requires mlx-vlm. Run: pip install --upgrade mlx-vlm"
                    ) from exc

                model, processor = load(self.settings.llm_model)
                try:
                    config = load_config(self.settings.llm_model)
                except Exception:
                    logger.debug("mlx-vlm load_config failed; generation will use the raw prompt", exc_info=True)
                    config = None
            elif engine == "lm":
                try:
                    from mlx_lm import load
                except ImportError as exc:
                    raise LLMGenerationError("MLX LM provider requires mlx-lm. Run: pip install --upgrade mlx-lm") from exc

                model, processor = load(self.settings.llm_model)
                config = None
            else:
                raise LLMGenerationError(f"Unsupported MLX engine: {engine}")

            self.__class__._mlx_loaded_cache_key = cache_key
            self.__class__._mlx_model = model
            self.__class__._mlx_processor = processor
            self.__class__._mlx_config = config
            return engine, model, processor, config

    def _generate_mlx(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        engine, model, processor, config = self._load_mlx()
        if engine == "vlm":
            return self._generate_mlx_vlm(model, processor, config, system_prompt, user_prompt, max_tokens)
        return self._generate_mlx_lm(model, processor, system_prompt, user_prompt, max_tokens)

    def _batch_generate_mlx(self, system_prompt: str, user_prompts: list[str], max_tokens: int) -> list[str]:
        engine, model, processor, config = self._load_mlx()
        if engine == "vlm":
            # mlx-vlm generation is kept sequential because the public API is optimized
            # for one prompt/image request at a time. The model stays loaded in memory.
            return [
                self._generate_mlx_vlm(model, processor, config, system_prompt, prompt, max_tokens)
                for prompt in user_prompts
            ]
        return self._batch_generate_mlx_lm(model, processor, system_prompt, user_prompts, max_tokens)

    def _generate_mlx_lm(self, model, tokenizer, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        try:
            from mlx_lm import generate
        except ImportError as exc:
            raise LLMGenerationError("MLX LM provider requires mlx-lm. Run: pip install --upgrade mlx-lm") from exc

        prompt = self._format_chat_prompt(tokenizer, system_prompt, user_prompt)
        kwargs = self._mlx_lm_generate_kwargs(max_tokens)
        try:
            content = generate(
                model,
                tokenizer,
                prompt=prompt,
                verbose=False,
                **kwargs,
            )
        except Exception as exc:
            logger.exception("mlx-lm generation failed")
            raise LLMGenerationError("mlx-lm generation failed") from exc
        return self._clean_response(content)

    def _batch_generate_mlx_lm(self, model, tokenizer, system_prompt: str, user_prompts: list[str], max_tokens: int) -> list[str]:
        try:
            from mlx_lm import batch_generate
        except ImportError as exc:
            raise LLMGenerationError("MLX LM provider requires mlx-lm. Run: pip install --upgrade mlx-lm") from exc

        prompts = [self._format_chat_prompt(tokenizer, system_prompt, user_prompt) for user_prompt in user_prompts]
        prompt_tokens = [tokenizer.encode(prompt) for prompt in prompts]
        kwargs = self._mlx_lm_batch_generate_kwargs(max_tokens)
        try:
            response = batch_generate(
                model,
                tokenizer,
                prompts=prompt_tokens,
                verbose=False,
                **kwargs,
            )
        except Exception:
            logger.exception("mlx-lm batch generation failed; falling back to individual generation")
            return [self._generate_mlx_lm(model, tokenizer, system_prompt, prompt, max_tokens) for prompt in user_prompts]

        generations = getattr(response, "texts", getattr(response, "generations", response))
        return [self._clean_response(str(item)) for item in generations]

    def _generate_mlx_vlm(self, model, processor, config, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        try:
            from mlx_vlm import generate
        except ImportError as exc:
            raise LLMGenerationError("Qwen 3.5 MLX requires mlx-vlm. Run: pip install --upgrade mlx-vlm") from exc

        prompt = self._format_vlm_prompt(processor, config, system_prompt, user_prompt)
        kwargs = self._mlx_vlm_generate_kwargs(max_tokens)
        try:
            content = generate(
                model,
                processor,
                prompt=prompt,
                verbose=False,
                **kwargs,
            )
        except TypeError:
            # Some mlx-vlm versions expose positional prompt/image arguments.
            try:
                content = generate(model, processor, prompt, None, **kwargs)
            except Exception as exc:
                logger.exception("mlx-vlm generation failed")
                raise LLMGenerationError("mlx-vlm generation failed") from exc
        except Exception as exc:
            logger.exception("mlx-vlm generation failed")
            raise LLMGenerationError("mlx-vlm generation failed") from exc
        return self._clean_response(content)

    def _mlx_lm_generate_kwargs(self, max_tokens: int) -> dict:
        try:
            from mlx_lm.sample_utils import make_sampler

            sampler = make_sampler(temp=self.settings.mlx_temperature, top_p=self.settings.mlx_top_p)
        except Exception:
            logger.debug("MLX sampler helper unavailable; using default sampler", exc_info=True)
            sampler = None
        kwargs = {
            "max_tokens": max_tokens,
            "max_kv_size": self.settings.mlx_max_kv_size,
        }
        if sampler is not None:
            kwargs["sampler"] = sampler
        return kwargs

    def _mlx_lm_batch_generate_kwargs(self, max_tokens: int) -> dict:
        kwargs = self._mlx_lm_generate_kwargs(max_tokens)
        kwargs.pop("max_kv_size", None)
        kwargs["completion_batch_size"] = max(1, self.settings.llm_batch_size)
        kwargs["prefill_batch_size"] = max(1, self.settings.llm_batch_size)
        return kwargs

    def _mlx_vlm_generate_kwargs(self, max_tokens: int) -> dict:
        return {
            "max_tokens": max_tokens,
            "temperature": self.settings.mlx_temperature,
            "top_p": self.settings.mlx_top_p,
        }

    def _format_vlm_prompt(self, processor, config, system_prompt: str, user_prompt: str) -> str:
        messages = self._chat_messages(system_prompt, user_prompt)
        try:
            from mlx_vlm.prompt_utils import apply_chat_template

            if config is not None:
                kwargs = {"num_images": 0}
                if self.settings.mlx_no_think:
                    kwargs["enable_thinking"] = False
                try:
                    return apply_chat_template(processor, config, messages, **kwargs)
                except TypeError:
                    kwargs.pop("enable_thinking", None)
                    return apply_chat_template(processor, config, messages, **kwargs)
        except Exception:
            logger.debug("mlx-vlm chat template helper unavailable; using tokenizer/fallback template", exc_info=True)

        tokenizer = getattr(processor, "tokenizer", processor)
        return self._format_chat_prompt(tokenizer, system_prompt, user_prompt)

    def _chat_messages(self, system_prompt: str, user_prompt: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _format_chat_prompt(self, tokenizer, system_prompt: str, user_prompt: str) -> str:
        messages = self._chat_messages(system_prompt, user_prompt)
        apply_chat_template = getattr(tokenizer, "apply_chat_template", None)
        if apply_chat_template is not None:
            kwargs = {
                "tokenize": False,
                "add_generation_prompt": True,
            }
            if self.settings.mlx_no_think:
                kwargs["enable_thinking"] = False
            try:
                return apply_chat_template(messages, **kwargs)
            except TypeError:
                kwargs.pop("enable_thinking", None)
                try:
                    return apply_chat_template(messages, **kwargs)
                except Exception:
                    logger.debug("Tokenizer chat template failed; using Qwen fallback template", exc_info=True)
            except Exception:
                logger.debug("Tokenizer chat template failed; using Qwen fallback template", exc_info=True)
        assistant_prompt = "<|im_start|>assistant\n"
        if self.settings.mlx_no_think:
            assistant_prompt += "<think>\n\n</think>\n\n"
        return (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
            f"{assistant_prompt}"
        )

    def _generate_openai_compatible(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMGenerationError(
                "openai_compatible provider requires the openai package. Run: pip install openai"
            ) from exc

        extra_headers = {}
        if self.settings.openai_compatible_referer:
            extra_headers["HTTP-Referer"] = self.settings.openai_compatible_referer
        if self.settings.openai_compatible_title:
            extra_headers["X-Title"] = self.settings.openai_compatible_title

        client = OpenAI(
            base_url=self.settings.openai_compatible_base_url,
            api_key=self.settings.openai_compatible_api_key or "no-key",
            default_headers=extra_headers if extra_headers else None,
            timeout=self.settings.llm_timeout_seconds,
        )
        try:
            completion = client.chat.completions.create(
                model=self.settings.llm_model,
                max_tokens=max_tokens,
                temperature=self.settings.mlx_temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception as exc:
            logger.exception("OpenAI-compatible API call failed")
            raise LLMGenerationError("OpenAI-compatible API call failed") from exc

        content = completion.choices[0].message.content or ""
        return self._clean_response(content)

    def _clean_response(self, value: Any) -> str:
        raw_text = getattr(value, "text", value)
        text = re.sub(r"<think>.*?(?:</think>|$)", "", str(raw_text), flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
        text = text.strip()
        text = re.sub(r"^\*+", "", text).strip()
        text = re.sub(r"\*+$", "", text).strip()
        text = text.strip(" \"'“”")
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z0-9_-]*", "", text).strip()
            text = re.sub(r"```$", "", text).strip()
        return " ".join(text.split())
