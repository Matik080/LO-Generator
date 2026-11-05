"""
Shared test setup.

LLM calls go through a record/replay "cassette" so tests run offline and deterministically:
  - replay (default): answers come from tests/fixtures/llm_cassette.json; an unknown prompt fails the test.
  - record (LLM_MODE=record): calls the real provider (needs an API key in .env) and saves every answer.

Run with PYTHONHASHSEED=0 so set ordering (and therefore prompt order) is the same in every run.
"""
import hashlib
import json
import os
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"
CASSETTE = FIXTURES / "llm_cassette.json"

import llm_service  # noqa: E402  (needs the sys.path entry above)


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

    def __call__(self, messages, model=None, temperature=0.2, **kwargs):
        key = _key(messages, model, temperature)
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
        if self.mode == "record":
            self.path.write_text(json.dumps(self.entries, indent=1, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def fake_llm(monkeypatch):
    mode = os.getenv("LLM_MODE", "replay")
    cassette = Cassette(CASSETTE, mode)
    monkeypatch.setattr(llm_service, "_backend", cassette)
    random.seed(1234)  # MC option shuffling
    yield cassette
    cassette.save()


@pytest.fixture
def update_snapshots():
    return os.getenv("LLM_MODE") == "record" or os.getenv("UPDATE_SNAPSHOTS") == "1"
