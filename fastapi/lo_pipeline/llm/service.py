import os
import time
from pathlib import Path
from typing import Dict, List

from openai import OpenAI, RateLimitError
from dotenv import load_dotenv
# fastapi/.env (this file is fastapi/lo_pipeline/llm/service.py)
load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=True)

GITHUB_TOKEN       = os.getenv("GITHUB_TOKEN")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")
GROQ_API_KEY       = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Optional: add a base delay (in seconds) between ALL calls to stay under rate limits
INTER_CALL_DELAY = float(os.getenv("INTER_CALL_DELAY", "0"))

# Optional: override the default generation model without touching code
GEN_MODEL = os.getenv("GEN_MODEL")

# Optional: reasoning effort for GPT-OSS models on Groq ("low" | "medium" | "high")
REASONING_EFFORT = os.getenv("REASONING_EFFORT")

# Provider selection
# Priority: GROQ > OpenRouter > GitHub > OpenAI
# Groq shut down llama-3.3-70b-versatile for non-enterprise tiers on 2026-08-16.
# The legacy name is still accepted everywhere and routed to OpenRouter's Llama 3.3 70B (see LEGACY_ROUTES below).
# Default generator: Llama 3.3 70B (thesis model) when OpenRouter is available, otherwise GPT-OSS 120B on Groq.
# The client is created on the first call, so importing this module never needs an API key (tests, tooling).
# Each entry: (provider name, api key, base_url or None, default model)
if GROQ_API_KEY:
    _PRIMARY = ("Groq", GROQ_API_KEY, "https://api.groq.com/openai/v1",
                "llama-3.3-70b-versatile" if OPENROUTER_API_KEY else "openai/gpt-oss-120b")
elif OPENROUTER_API_KEY:
    _PRIMARY = ("OpenRouter", OPENROUTER_API_KEY, "https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct")
elif GITHUB_TOKEN:
    _PRIMARY = ("GitHub Models", GITHUB_TOKEN, "https://models.inference.ai.azure.com", "gpt-4.1-nano")
elif OPENAI_API_KEY:
    _PRIMARY = ("OpenAI", OPENAI_API_KEY, None, "gpt-4.1-nano")
else:
    _PRIMARY = None

DEFAULT_MODEL = GEN_MODEL or (_PRIMARY[3] if _PRIMARY else None)


class LLMConfigError(RuntimeError):
    """No usable provider/API key is configured. Raised on the first call instead of exiting at import."""


_client = None

def _get_primary_client():
    global _client
    if _client is not None:
        return _client
    if _PRIMARY is None:
        raise LLMConfigError("No API key found. Set GROQ_API_KEY, OPENROUTER_API_KEY, GITHUB_TOKEN, or OPENAI_API_KEY.")
    name, key, base_url, _ = _PRIMARY
    print(f"[Config] Primary provider: {name}")
    print(f"[Config] Default generation model: {DEFAULT_MODEL}")
    _client = OpenAI(api_key=key, base_url=base_url) if base_url else OpenAI(api_key=key)
    return _client

# Secondary client for OpenRouter-only models (e.g. Kimi K2)
# If primary is already OpenRouter, we reuse that client.
# If primary is Groq/GitHub/OpenAI but OPENROUTER_API_KEY is also present,
# a second client is created so Kimi K2 evaluations can still run.
_openrouter_client = None

def _get_openrouter_client():
    global _openrouter_client
    if _openrouter_client is not None:
        return _openrouter_client
    if OPENROUTER_API_KEY:
        _openrouter_client = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
        )
        return _openrouter_client
    return None

# Legacy / logical model names used throughout the codebase -> the provider model ID that actually serves them now.
# Callers keep passing the old names (eval lists, batch-size tables, saved results all key on them).
LEGACY_ROUTES = {
    # Groq shut this down 2026-08-16 (non-enterprise); same weights still served on OpenRouter
    "llama-3.3-70b-versatile": "meta-llama/llama-3.3-70b-instruct",
    # OpenRouter retired the "-instruct" ID; "moonshotai/kimi-k2" is the live one
    "moonshotai/kimi-k2-instruct": "moonshotai/kimi-k2",
}

# Models that must always route through OpenRouter
# Note: openai/gpt-oss-120b is available on BOTH Groq and OpenRouter
# using the same model string, it routes through whichever is the primary provider
OPENROUTER_MODELS = {
    "meta-llama/llama-3.3-70b-instruct",
    "moonshotai/kimi-k2",
}

def _client_for_model(model: str):
    """Return (client, provider_model_id) for a logical or provider model name."""
    target = LEGACY_ROUTES.get(model, model)
    if target in OPENROUTER_MODELS:
        c = _get_openrouter_client()
        if c is None:
            raise ValueError(
                f"Model '{model}' is now served via OpenRouter ('{target}') but OPENROUTER_API_KEY is not set. "
                f"Add OPENROUTER_API_KEY to .env, or use a Groq model such as 'openai/gpt-oss-120b'."
            )
        return c, target
    return _get_primary_client(), target


def call_llm(
        messages: List[Dict[str, str]], model: str | None = None, temperature: float = 0.2, max_retries: int = 6, rate_limit_backoff: float = 60.0,
) -> str | None:
    """
    Entry point used by all pipeline modules. Delegates to `_backend`, which tests swap for a fake
    (modules import `call_llm` by name, so patching this module attribute alone would not reach them).
    """
    return _backend(messages, model=model, temperature=temperature,
                    max_retries=max_retries, rate_limit_backoff=rate_limit_backoff)


def _openai_call(
        messages: List[Dict[str, str]], model: str | None = None, temperature: float = 0.2, max_retries: int = 6, rate_limit_backoff: float = 60.0,
) -> str | None:
    """
    Stable LLM call wrapper with retries and rate limit handling.
    Automatically routes models in OPENROUTER_MODELS through OpenRouter.
    - General errors: exponential backoff (2^attempt seconds)
    - RateLimitError: waits `rate_limit_backoff` seconds before retrying
    Set INTER_CALL_DELAY env var to add a fixed pause between every call.
    Returns raw string output.
    """
    if INTER_CALL_DELAY > 0:
        time.sleep(INTER_CALL_DELAY)

    selected_client, resolved_model = _client_for_model(model or DEFAULT_MODEL)

    extra = {}
    if REASONING_EFFORT and resolved_model.startswith("openai/gpt-oss") and selected_client is _client and GROQ_API_KEY:
        extra["reasoning_effort"] = REASONING_EFFORT

    for attempt in range(max_retries):
        try:
            response = selected_client.chat.completions.create(
                model=resolved_model,
                messages=messages,
                temperature=temperature,
                **extra,
            )
            if not response.choices:
                raise Exception("No choices returned.")

            content = response.choices[0].message.content
            if not content:
                raise Exception("Empty response content.")

            return content.strip()

        except RateLimitError as e:
            if attempt < max_retries - 1:
                print(f"[RATE LIMIT] Hit rate limit. Waiting {rate_limit_backoff}s before retry ({attempt+1}/{max_retries})...")
                time.sleep(rate_limit_backoff)
                rate_limit_backoff = min(rate_limit_backoff * 1.5, 600)
            else:
                raise Exception(f"Rate limit persisted after {max_retries} attempts: {e}")

        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"[LLM ERROR] Attempt {attempt+1} failed. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise Exception(f"LLM call failed after {max_retries} attempts: {e}")

    return None

# Swappable in tests (see tests/conftest.py)
_backend = _openai_call
