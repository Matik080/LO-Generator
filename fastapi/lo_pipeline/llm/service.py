"""
LLM calls of the whole pipeline: call_llm(messages, model, temperature) -> answer text.

The provider is any OpenAI-compatible API (Groq, OpenRouter, OpenAI, GitHub Models, a local vLLM
or Ollama server), chosen only by configuration (lo_pipeline/config.py): LOGEN_LLM_BASE_URL,
LOGEN_LLM_API_KEY, LOGEN_LLM_MODEL, or the older GROQ_API_KEY / GEN_MODEL / ... This is the only
module that imports a provider library (openai).

Errors: LLMError when the provider still fails after the retries; LLMConfigError (a kind of
LLMError) at once when nothing is configured, the key is rejected or the model does not exist,
because retrying cannot help. A job then fails with llm_error (service/worker.py).
"""
import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Dict, List, Optional

from openai import AuthenticationError, NotFoundError, OpenAI, PermissionDeniedError, RateLimitError

from lo_pipeline.config import GROQ_BASE_URL, OPENROUTER_BASE_URL, get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The LLM provider failed even after retries (job error code llm_error)."""


class LLMConfigError(LLMError):
    """No provider configured, key rejected or model unknown: retrying cannot help."""


# Legacy / logical model names used throughout the codebase -> the provider model ID that actually serves them now.
# Callers keep passing the old names (eval lists, batch-size tables, saved results all key on them).
LEGACY_ROUTES = {
    # Groq shut this down 2026-08-16 (non-enterprise); same weights still served on OpenRouter
    "llama-3.3-70b-versatile": "meta-llama/llama-3.3-70b-instruct",
    # OpenRouter retired the "-instruct" ID; "moonshotai/kimi-k2" is the live one
    "moonshotai/kimi-k2-instruct": "moonshotai/kimi-k2",
}

# Models that must always route through OpenRouter (OPENROUTER_API_KEY).
# openai/gpt-oss-120b exists on both Groq and OpenRouter and goes to the main provider.
OPENROUTER_MODELS = {
    "meta-llama/llama-3.3-70b-instruct",
    "moonshotai/kimi-k2",
}


class OpenAICompatibleClient:
    """One OpenAI-compatible API (base URL + key) with retries and waiting on rate limits."""

    def __init__(self, name: str, api_key: str, base_url: Optional[str], client=None, sleep=time.sleep):
        self.name = name
        self.base_url = base_url
        self.client = client or (OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key))
        self.sleep = sleep  # tests replace it, so retries do not wait

    def chat(self, messages: List[Dict[str, str]], model: str, temperature: float, max_retries: int,
             rate_limit_backoff: float = 60.0, inter_call_delay: float = 0.0,
             reasoning_effort: Optional[str] = None) -> str:
        """
        - key rejected (401/403) or model unknown (404): LLMConfigError at once
        - rate limit: waits `rate_limit_backoff` seconds (x1.5 each time, at most 600) and retries
        - other errors (network, 5xx, empty answer): waits 1, 2, 4, … seconds and retries
        After `max_retries` attempts: LLMError.
        """
        if inter_call_delay > 0:
            self.sleep(inter_call_delay)
        extra = {}
        if reasoning_effort and model.startswith("openai/gpt-oss") and self.base_url == GROQ_BASE_URL:
            extra["reasoning_effort"] = reasoning_effort

        for attempt in range(max_retries):
            last = attempt == max_retries - 1
            try:
                response = self.client.chat.completions.create(
                    model=model, messages=messages, temperature=temperature, **extra)
                if not response.choices:
                    raise ValueError("No choices returned.")
                content = response.choices[0].message.content
                if not content:
                    raise ValueError("Empty response content.")
                return content.strip()

            except (AuthenticationError, PermissionDeniedError) as e:
                raise LLMConfigError(f"{self.name} rejected the API key (HTTP {e.status_code}). "
                                     f"Check LOGEN_LLM_API_KEY / GROQ_API_KEY.") from e
            except NotFoundError as e:
                raise LLMConfigError(f"{self.name} does not know the model '{model}' (HTTP 404). "
                                     f"Check LOGEN_LLM_MODEL / LOGEN_LLM_MODEL_EVAL.") from e

            except RateLimitError as e:
                if last:
                    raise LLMError(f"Rate limit persisted after {max_retries} attempts: {e}") from e
                logger.warning(f"[RATE LIMIT] Hit rate limit. Waiting {rate_limit_backoff}s before retry "
                               f"({attempt + 1}/{max_retries})...")
                self.sleep(rate_limit_backoff)
                rate_limit_backoff = min(rate_limit_backoff * 1.5, 600)

            except Exception as e:
                if last:
                    raise LLMError(f"LLM call failed after {max_retries} attempts: {e}") from e
                wait = 2 ** attempt
                logger.warning(f"[LLM ERROR] Attempt {attempt + 1} failed ({type(e).__name__}). Retrying in {wait}s...")
                self.sleep(wait)
        raise LLMError("LLM call made no attempt (max_retries < 1).")


# One client per provider, shared by the parallel threads (llm/parallel.py)
_clients: Dict[tuple, OpenAICompatibleClient] = {}
_clients_lock = threading.Lock()


def _client(name: str, api_key: str, base_url: Optional[str]) -> OpenAICompatibleClient:
    key = (base_url, api_key)
    with _clients_lock:
        if key not in _clients:
            logger.info(f"[Config] LLM provider: {name} ({base_url or 'api.openai.com'})")
            _clients[key] = OpenAICompatibleClient(name, api_key, base_url)
        return _clients[key]


def default_model() -> Optional[str]:
    """LOGEN_LLM_MODEL / GEN_MODEL, otherwise the provider's default model."""
    return get_settings().default_model()


def _client_for_model(model: str):
    """Return (client, provider_model_id) for a logical or provider model name."""
    settings = get_settings()
    provider = settings.provider()
    target = LEGACY_ROUTES.get(model, model)
    if target in OPENROUTER_MODELS and not (provider and provider.base_url == OPENROUTER_BASE_URL):
        key = settings.openrouter_api_key.get_secret_value().strip() if settings.openrouter_api_key else ""
        if not key:
            raise LLMConfigError(
                f"Model '{model}' is now served via OpenRouter ('{target}') but OPENROUTER_API_KEY is not set. "
                f"Add OPENROUTER_API_KEY to .env, or use a Groq model such as 'openai/gpt-oss-120b'."
            )
        return _client("OpenRouter", key, OPENROUTER_BASE_URL), target
    if provider is None:
        raise LLMConfigError("No LLM provider configured. Set LOGEN_LLM_BASE_URL and/or LOGEN_LLM_API_KEY "
                             "(or GROQ_API_KEY, OPENROUTER_API_KEY, GITHUB_TOKEN, OPENAI_API_KEY).")
    return _client(provider.name, provider.api_key, provider.base_url), target


def _openai_call(messages: List[Dict[str, str]], model: str | None = None, temperature: float = 0.2,
                 max_retries: int | None = None, rate_limit_backoff: float = 60.0) -> str:
    """The real LLM call (tests replace `_backend` with recorded answers)."""
    settings = get_settings()
    model = model or settings.default_model()
    if not model:
        raise LLMConfigError("No LLM model configured. Set LOGEN_LLM_MODEL (or GEN_MODEL).")
    client, provider_model = _client_for_model(model)
    return client.chat(messages, provider_model, temperature,
                       max_retries=max_retries or settings.llm_max_retries,
                       rate_limit_backoff=rate_limit_backoff,
                       inter_call_delay=settings.inter_call_delay,
                       reasoning_effort=settings.reasoning_effort)


def call_llm(
        messages: List[Dict[str, str]], model: str | None = None, temperature: float = 0.2,
        max_retries: int | None = None, rate_limit_backoff: float = 60.0,
) -> str | None:
    """
    Entry point used by all pipeline modules. Delegates to `_backend`, which tests swap for a fake
    (modules import `call_llm` by name, so patching this module attribute alone would not reach them).
    `model=None` means the configured default model.

    If a response cache is active (see `use_response_cache`), an identical earlier call is answered from it.
    That is how a resumed pipeline run skips the LLM calls it already made.

    If an output-language rule is active (see `use_output_language`), it is appended to the last message.
    """
    rule = _output_language_rule.get()
    if rule:
        messages = messages[:-1] + [{**messages[-1], "content": f"{messages[-1]['content']}\n\n{rule}"}]
    cache = _response_cache.get()
    slot = None
    if cache is not None:
        slot, hit = cache.lookup(call_key(messages, model or default_model(), temperature))
        if hit is not None:
            return hit
    response = _backend(messages, model=model, temperature=temperature,
                        max_retries=max_retries, rate_limit_backoff=rate_limit_backoff)
    if cache is not None and response is not None:
        cache.save(slot, response)
    return response


def call_key(messages, model, temperature) -> str:
    """Stable hash of one LLM request (same prompt + model + temperature -> same key)."""
    raw = json.dumps({"model": model, "temperature": temperature, "messages": messages},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _RunCache:
    """
    Saved answers for one pipeline run, keyed by request AND occurrence: the same prompt can legitimately be
    sent several times in a run (e.g. regenerating the same failing LO in two rounds), and each time must get
    its own answer. So the n-th identical request maps to the n-th saved answer, and a resumed run replays
    answers in exactly the order the original run received them.
    """

    def __init__(self, store):
        self.store = store  # object with get(slot) -> str | None and put(slot, response)
        self.seen = {}
        self.lock = threading.Lock()  # batches run in parallel threads (llm/parallel.py)

    def lookup(self, key):
        with self.lock:
            n = self.seen.get(key, 0)
            self.seen[key] = n + 1
            slot = f"{key}#{n}"
            return slot, self.store.get(slot)

    def save(self, slot, response):
        with self.lock:
            self.store.put(slot, response)


# Response cache of the current pipeline run. A ContextVar, not a global, so concurrent runs in different
# threads/tasks don't share it.
_response_cache: ContextVar = ContextVar("llm_response_cache", default=None)


@contextmanager
def use_response_cache(store):
    """
    Within this block, call_llm answers requests that `store` already has (same request, same occurrence)
    and saves every new answer to it. `store` needs get(slot) and put(slot, response).
    """
    token = _response_cache.set(_RunCache(store))
    try:
        yield
    finally:
        _response_cache.reset(token)


# Output-language rule of the current stage (job option `language` different from the documents' language).
_output_language_rule: ContextVar = ContextVar("llm_output_language_rule", default=None)

LANGUAGE_NAMES = {"sk": "Slovak", "en": "English"}


def output_language_rule(output: str, document: str) -> Optional[str]:
    """The rule appended to prompts when questions must be in another language than the documents; None
    when they are the same (then the prompts stay exactly as they are: 'same language as the source')."""
    if output == document or output not in LANGUAGE_NAMES:
        return None
    name, source = LANGUAGE_NAMES[output], LANGUAGE_NAMES.get(document, document)
    return (f"IMPORTANT - OUTPUT LANGUAGE: The source material is in {source}, but every text meant for "
            f"students or teachers (questions, answers, hints, answer options, names, tasks, lesson titles and "
            f"descriptions) must be written in {name}. This replaces any instruction above to use the language "
            f"of the source. Keep technical terms, code and formulas exact. If you evaluate learning objects, "
            f"expect them in {name} and judge their meaning against the {source} source.")


@contextmanager
def use_output_language(rule: Optional[str]):
    """Within this block (also in parallel batches, llm/parallel.py copies the context), call_llm appends `rule`."""
    token = _output_language_rule.set(rule)
    try:
        yield
    finally:
        _output_language_rule.reset(token)


# Swappable in tests (see tests/conftest.py)
_backend = _openai_call
