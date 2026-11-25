"""Configuration of the pipeline: LLM provider, models, embeddings, thresholds, batch sizes (task 3).

Read from environment variables with the prefix LOGEN_ and from fastapi/.env (variables in the
environment win over the file). Older names still work where they existed (GROQ_API_KEY,
GEN_MODEL, INTER_CALL_DELAY, …). The defaults are the values the pipeline always used.

    from lo_pipeline.config import get_settings
    get_settings().llm_model

Provider: either set LOGEN_LLM_BASE_URL / LOGEN_LLM_API_KEY (any OpenAI-compatible API: Groq,
OpenRouter, OpenAI, a local vLLM or Ollama server), or one of the older keys, in this order:
GROQ_API_KEY > OPENROUTER_API_KEY > GITHUB_TOKEN > OPENAI_API_KEY.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Optional
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"  # fastapi/.env

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GITHUB_BASE_URL = "https://models.inference.ai.azure.com"

DEFAULT_EVAL_MODELS = ("qwen/qwen3.8-27b", "openai/gpt-oss-120b")


def _env(*names: str):
    """An optional setting (default None) read from these variable names; the first one set wins."""
    return Field(None, validation_alias=AliasChoices(*names))


@dataclass(frozen=True)
class Provider:
    name: str
    api_key: str
    base_url: Optional[str]   # None = OpenAI's own API
    default_model: str


class PipelineSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOGEN_", env_file=ENV_FILE, env_file_encoding="utf-8",
                                      extra="ignore", frozen=True, populate_by_name=True)

    # --- LLM provider (OpenAI-compatible API) -------------------------------------------
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[SecretStr] = None
    llm_model: Optional[str] = _env("LOGEN_LLM_MODEL", "GEN_MODEL")
    eval_models: Annotated[tuple[str, ...], NoDecode] = Field(
        DEFAULT_EVAL_MODELS, validation_alias=AliasChoices("LOGEN_LLM_MODEL_EVAL"))
    llm_max_concurrency: int = Field(4, ge=1)
    llm_max_retries: int = Field(6, ge=1)
    inter_call_delay: float = Field(0.0, ge=0, validation_alias=AliasChoices("LOGEN_INTER_CALL_DELAY", "INTER_CALL_DELAY"))
    reasoning_effort: Optional[str] = _env("LOGEN_REASONING_EFFORT", "REASONING_EFFORT")

    # older provider keys (used when LOGEN_LLM_BASE_URL / LOGEN_LLM_API_KEY are not set);
    # OPENROUTER_API_KEY also serves the models that exist only on OpenRouter (LEGACY_ROUTES)
    groq_api_key: Optional[SecretStr] = _env("GROQ_API_KEY")
    openrouter_api_key: Optional[SecretStr] = _env("OPENROUTER_API_KEY")
    github_token: Optional[SecretStr] = _env("GITHUB_TOKEN")
    openai_api_key: Optional[SecretStr] = _env("OPENAI_API_KEY")

    # --- embeddings (independent of the LLM provider) -------------------------------------
    embed_provider: Literal["local", "openai_compatible"] = "local"
    embed_model: str = "all-MiniLM-L6-v2"
    embed_base_url: Optional[str] = None
    embed_api_key: Optional[SecretStr] = None

    # --- pipeline -------------------------------------------------------------------------
    dedup_threshold: float = Field(0.82, gt=0, le=1)   # calibrated for MiniLM
    faithfulness_threshold: float = 3.0
    hint_quality_threshold: float = 2.0
    max_attempts: int = Field(2, ge=0)                 # regeneration rounds of the quality filter
    extraction_batch_size: int = Field(3, ge=1)
    generation_batch_size: int = Field(5, ge=1)
    mc_batch_size: int = Field(5, ge=1)
    eval_batch_size: Optional[int] = Field(None, ge=1)  # None = per model (lo/evaluator.py)
    verify_batch_size: int = Field(10, ge=1)
    enrich_batch_size: int = Field(10, ge=1)
    multi_correct_batch_size: int = Field(5, ge=1)  # choice LOs per call when looking for a 2nd correct option
    no_answer_batch_size: int = Field(5, ge=1)      # concept groups per call when writing no_answer tasks
    no_answer_group_size: int = Field(6, ge=2)      # most LOs in one no_answer task (bigger groups are split)

    @field_validator("eval_models", mode="before")
    @classmethod
    def _comma_list(cls, value):
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if not value:
            raise ValueError("needs at least one model")
        return tuple(value)

    @field_validator("llm_model", "llm_base_url", "reasoning_effort", "embed_base_url", "eval_batch_size",
                     mode="before")
    @classmethod
    def _empty_is_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    def provider(self) -> Optional[Provider]:
        """The LLM provider to call, or None when nothing is configured."""
        def secret(value: Optional[SecretStr]) -> str:
            return value.get_secret_value().strip() if value is not None else ""

        if self.llm_base_url or secret(self.llm_api_key):
            # a local server may need no key; the OpenAI client still wants a non-empty one
            name = urlparse(self.llm_base_url).hostname if self.llm_base_url else "OpenAI"
            return Provider(name or self.llm_base_url, secret(self.llm_api_key) or "not-needed", self.llm_base_url,
                            "openai/gpt-oss-120b")
        if secret(self.groq_api_key):
            # with OpenRouter available the thesis model (Llama 3.3 70B, served there) is the default
            default = "llama-3.3-70b-versatile" if secret(self.openrouter_api_key) else "openai/gpt-oss-120b"
            return Provider("Groq", secret(self.groq_api_key), GROQ_BASE_URL, default)
        if secret(self.openrouter_api_key):
            return Provider("OpenRouter", secret(self.openrouter_api_key), OPENROUTER_BASE_URL,
                            "meta-llama/llama-3.3-70b-instruct")
        if secret(self.github_token):
            return Provider("GitHub Models", secret(self.github_token), GITHUB_BASE_URL, "gpt-4.1-nano")
        if secret(self.openai_api_key):
            return Provider("OpenAI", secret(self.openai_api_key), None, "gpt-4.1-nano")
        return None

    def default_model(self) -> Optional[str]:
        provider = self.provider()
        return self.llm_model or (provider.default_model if provider else None)


@lru_cache(maxsize=1)
def get_settings() -> PipelineSettings:
    """The settings, read once. Tests change the environment and call get_settings.cache_clear()."""
    return PipelineSettings()
