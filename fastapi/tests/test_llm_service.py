"""LLM calls (lo_pipeline/llm/service.py): provider from configuration, retries, clear errors.

A scripted fake replaces the provider's HTTP client: no network, and retries do not wait.
"""

from types import SimpleNamespace

import httpx
import openai
import pytest

from lo_pipeline.config import GROQ_BASE_URL, PipelineSettings
from lo_pipeline.llm import service as llm_service
from lo_pipeline.llm.service import LLMConfigError, LLMError, OpenAICompatibleClient

MESSAGES = [{"role": "user", "content": "hi"}]


def api_error(cls, status):
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    return cls(f"HTTP {status}", response=httpx.Response(status, request=request), body=None)


def answer(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class Script:
    """Fake OpenAI client: returns or raises the given items in order and records the requests."""

    def __init__(self, *items):
        self.items, self.requests = list(items), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return answer(item)


def client(script, base_url=None):
    waits = []
    return OpenAICompatibleClient("Test", "key", base_url, client=script, sleep=waits.append), waits


def test_answer_is_returned():
    c, waits = client(Script("  ok  "))
    assert c.chat(MESSAGES, "m", 0.2, max_retries=3) == "ok" and waits == []


@pytest.mark.parametrize("error", [api_error(openai.AuthenticationError, 401),
                                   api_error(openai.PermissionDeniedError, 403)])
def test_rejected_key_fails_at_once(error):
    script = Script(error, "never")
    c, waits = client(script)
    with pytest.raises(LLMConfigError, match="rejected the API key"):
        c.chat(MESSAGES, "m", 0.2, max_retries=6)
    assert len(script.requests) == 1 and waits == []  # no retries, no waiting


def test_unknown_model_fails_at_once():
    script = Script(api_error(openai.NotFoundError, 404))
    with pytest.raises(LLMConfigError, match="model 'm'"):
        client(script)[0].chat(MESSAGES, "m", 0.2, max_retries=6)
    assert len(script.requests) == 1


def test_temporary_errors_are_retried():
    script = Script(api_error(openai.InternalServerError, 503), "", "ok")  # 5xx, then an empty answer
    c, waits = client(script)
    assert c.chat(MESSAGES, "m", 0.2, max_retries=6) == "ok"
    assert len(script.requests) == 3 and waits == [1, 2]


def test_rate_limit_waits_longer_each_time_then_fails():
    script = Script(*[api_error(openai.RateLimitError, 429)] * 3)
    c, waits = client(script)
    with pytest.raises(LLMError, match="Rate limit persisted after 3 attempts"):
        c.chat(MESSAGES, "m", 0.2, max_retries=3, rate_limit_backoff=60)
    assert waits == [60, 90]


def test_reasoning_effort_only_for_gpt_oss_on_groq():
    for base_url, model, sent in ((GROQ_BASE_URL, "openai/gpt-oss-120b", True),
                                  (GROQ_BASE_URL, "qwen/qwen3.8-27b", False),
                                  ("http://vllm:8001/v1", "openai/gpt-oss-120b", False)):
        script = Script("ok")
        client(script, base_url)[0].chat(MESSAGES, model, 0.2, max_retries=1, reasoning_effort="low")
        assert ("reasoning_effort" in script.requests[0]) is sent


# --- provider and model from the configuration ---------------------------------------------

@pytest.fixture
def settings(monkeypatch):
    """Use these settings instead of the environment / fastapi/.env."""
    def use(**values):
        s = PipelineSettings(_env_file=None, **values)
        monkeypatch.setattr(llm_service, "get_settings", lambda: s)
        monkeypatch.setattr(llm_service, "_clients", {})
        return s
    return use


def test_nothing_configured_is_a_config_error(settings, monkeypatch):
    for name in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY", "GEN_MODEL"):
        monkeypatch.delenv(name, raising=False)
    settings(llm_model="m")
    with pytest.raises(LLMConfigError, match="No LLM provider configured"):
        llm_service._openai_call(MESSAGES)


def test_custom_provider_and_model(settings, monkeypatch):
    settings(llm_base_url="http://gpu-server:8001/v1", llm_model="qwen-27b", llm_max_retries=2)
    script = Script("ok")
    monkeypatch.setattr(llm_service, "OpenAICompatibleClient",
                        lambda name, key, base_url: OpenAICompatibleClient(name, key, base_url, client=script))
    assert llm_service._openai_call(MESSAGES) == "ok"
    assert script.requests[0]["model"] == "qwen-27b"
    assert llm_service._client_for_model("x")[0].base_url == "http://gpu-server:8001/v1"


def test_openrouter_only_model_needs_its_key(settings, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings(groq_api_key="gsk")
    with pytest.raises(LLMConfigError, match="OPENROUTER_API_KEY"):
        llm_service._client_for_model("llama-3.3-70b-versatile")
    settings(groq_api_key="gsk", openrouter_api_key="or")
    c, target = llm_service._client_for_model("llama-3.3-70b-versatile")
    assert c.name == "OpenRouter" and target == "meta-llama/llama-3.3-70b-instruct"
    assert llm_service._client_for_model("openai/gpt-oss-120b")[0].name == "Groq"


def test_import_does_not_need_api_key():
    # Importing already happened in conftest; reaching this line means no exit() at import time.
    assert hasattr(llm_service, "call_llm")


def test_call_llm_goes_through_backend(monkeypatch):
    seen = {}

    def fake(messages, **kwargs):
        seen["model"] = kwargs["model"]
        return "ok"

    monkeypatch.setattr(llm_service, "_backend", fake)
    assert llm_service.call_llm([{"role": "user", "content": "x"}], model="m") == "ok"
    assert seen["model"] == "m"
