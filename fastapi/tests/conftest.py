"""
Shared test setup.

LLM calls go through a record/replay "cassette" so tests run offline and deterministically:
  - replay (default): answers come from tests/fixtures/llm_cassette.json; an unknown prompt fails the test.
  - record (LLM_MODE=record): calls the real provider (needs an API key in .env) and saves every answer.

Run with PYTHONHASHSEED=0 so set ordering (and therefore prompt order) is the same in every run.

Limitation: the cassette keys answers by prompt only, so if a run sends the identical prompt twice (e.g. the same
failing LO regenerated in two rounds), both get the first recorded answer. Real runs (and checkpoints, see
llm/service.py _RunCache) give each repetition its own answer.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"
CASSETTE = FIXTURES / "llm_cassette.json"

from lo_pipeline.llm import service as llm_service  # noqa: E402  (needs the sys.path entry above)


def _key(messages, model, temperature):
    raw = json.dumps({"model": model, "temperature": temperature, "messages": messages},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Cassette:
    def __init__(self, path: Path, mode: str):
        self.path = path
        self.mode = mode
        self.entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.real_backend = llm_service._openai_call
        self.misses = []
        self.used = set()

    def __call__(self, messages, model=None, temperature=0.2, **kwargs):
        key = _key(messages, model, temperature)
        self.used.add(key)
        if key in self.entries:
            return self.entries[key]["response"]
        if self.mode != "record":
            self.misses.append(key)
            raise AssertionError(
                f"No recorded LLM answer for this prompt (key {key[:12]}). "
                "The prompt changed or the cassette is outdated; re-record with LLM_MODE=record."
            )
        response = self.real_backend(messages, model=model, temperature=temperature, **kwargs)
        # Prompt preview makes the cassette reviewable by hand
        self.entries[key] = {"model": model, "prompt_preview": messages[-1]["content"][:200], "response": response}
        return response

    def save(self):
        # Keep only answers this run used, so outdated prompts drop out of the cassette.
        # Answers already in the cassette are reused, so only new prompts call the real LLM.
        if self.mode == "record":
            used = {k: v for k, v in self.entries.items() if k in self.used}
            self.path.write_text(json.dumps(used, indent=1, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch):
    """
    Settings are read once and cached; a test that changes LOGEN_* variables gets them re-read.
    Tests do not read fastapi/.env (the developer's keys and limits must not change test results),
    except when recording real LLM answers (LLM_MODE=record needs the API key from .env).
    """
    from lo_pipeline.config import PipelineSettings, get_settings
    from service.settings import ServiceSettings

    if os.getenv("LLM_MODE") != "record":
        monkeypatch.setitem(PipelineSettings.model_config, "env_file", None)
        monkeypatch.setitem(ServiceSettings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_llm(monkeypatch):
    mode = os.getenv("LLM_MODE", "replay")
    cassette = Cassette(CASSETTE, mode)
    monkeypatch.setattr(llm_service, "_backend", cassette)
    yield cassette
    cassette.save()


@pytest.fixture
def update_snapshots():
    return os.getenv("LLM_MODE") == "record" or os.getenv("UPDATE_SNAPSHOTS") == "1"
