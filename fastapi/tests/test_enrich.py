"""Question names and times (enrich): tolerant parsing, and no meaningless name when a batch fails."""
import json

import pytest

from lo_pipeline.questions import echub
from lo_pipeline.questions.echub import enrich_learning_objects


def los(n):
    return [{"type": "definition", "question": f"Q{i}?", "answer": f"A{i}"} for i in range(n)]


def answer(n):
    return [{"name": f"Name {i}", "time_to_handle": 40, "time_to_explain": 30} for i in range(n)]


@pytest.fixture
def reply(monkeypatch):
    box = {}
    monkeypatch.setattr(echub, "call_llm", lambda messages, **kw: box["text"])
    return box


def test_answer_with_text_around_the_json_is_used(reply):
    reply["text"] = "Here you go:\n```json\n" + json.dumps(answer(3)) + "\n```"
    result = enrich_learning_objects(los(3))
    assert [lo["generated_name"] for lo in result] == ["Name 0", "Name 1", "Name 2"]


@pytest.mark.parametrize("text", ["not json", json.dumps(answer(2)), json.dumps({"name": "x"}), ""])
def test_failed_batch_gets_no_name_and_default_times(reply, text):
    reply["text"] = text
    result = enrich_learning_objects(los(3))
    assert [lo["generated_name"] for lo in result] == [None, None, None]
    assert all((lo["time_to_handle"], lo["time_to_explain"]) == (90, 45) for lo in result)


def test_empty_name_becomes_none(reply):
    reply["text"] = json.dumps([{"name": " ", "time_to_handle": 40, "time_to_explain": 30}])
    assert enrich_learning_objects(los(1))[0]["generated_name"] is None
