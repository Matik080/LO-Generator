import os
import time
from pathlib import Path
from typing import Dict, List

try:
    from openai import OpenAI, RateLimitError
    import PyPDF2
except ImportError:
    print("ERROR: Missing necessary dependencies, please run:")
    print("pip install openai PyPDF2")
    exit(1)

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

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
if GROQ_API_KEY:
    print("[Config] Primary provider: Groq")
    client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    DEFAULT_MODEL = "llama-3.3-70b-versatile" if OPENROUTER_API_KEY else "openai/gpt-oss-120b"
elif OPENROUTER_API_KEY:
    print("[Config] Primary provider: OpenRouter")
    client = OpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
    DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct"
elif GITHUB_TOKEN:
    print("[Config] Primary provider: GitHub Models")
    client = OpenAI(api_key=GITHUB_TOKEN, base_url="https://models.inference.ai.azure.com")
    DEFAULT_MODEL = "gpt-4.1-nano"
elif OPENAI_API_KEY:
    print("[Config] Primary provider: OpenAI")
    client = OpenAI(api_key=OPENAI_API_KEY)
    DEFAULT_MODEL = "gpt-4.1-nano"
else:
    print("ERROR: No API key found. Set GROQ_API_KEY, OPENROUTER_API_KEY, GITHUB_TOKEN, or OPENAI_API_KEY.")
    exit(1)

if GEN_MODEL:
    DEFAULT_MODEL = GEN_MODEL

print(f"[Config] Default generation model: {DEFAULT_MODEL}")

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
    return client, target


def call_llm(
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
    if REASONING_EFFORT and resolved_model.startswith("openai/gpt-oss") and selected_client is client and GROQ_API_KEY:
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