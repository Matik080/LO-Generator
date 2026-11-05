import pytest

import llm_service


def test_import_does_not_need_api_key():
    # Importing already happened in conftest; reaching this line means no exit() at import time.
    assert hasattr(llm_service, "call_llm")


def test_missing_key_raises_config_error(monkeypatch):
    monkeypatch.setattr(llm_service, "_PRIMARY", None)
    monkeypatch.setattr(llm_service, "_client", None)
    with pytest.raises(llm_service.LLMConfigError):
        llm_service._openai_call([{"role": "user", "content": "hi"}], model="openai/gpt-oss-120b", max_retries=1)


def test_call_llm_goes_through_backend(monkeypatch):
    seen = {}

    def fake(messages, **kwargs):
        seen["model"] = kwargs["model"]
        return "ok"

    monkeypatch.setattr(llm_service, "_backend", fake)
    assert llm_service.call_llm([{"role": "user", "content": "x"}], model="m") == "ok"
    assert seen["model"] == "m"
