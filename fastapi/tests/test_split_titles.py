"""Lesson titles and descriptions: LLM (scripted fake), given texts kept, fallbacks."""
import json
import re

import pytest

from lo_pipeline.llm.service import LLMError
from lo_pipeline.split import titles
from lo_pipeline.split.titles import LessonSpec, lesson_titles


def lo(name):
    return {"generated_name": name, "question": f"Q about {name}?"}


LOS = [lo("Pointer Basics"), lo("Address Operator"), lo("Malloc Return Value"), lo("Freeing Memory")]


@pytest.fixture
def llm(monkeypatch):
    """Scripted LLM: `llm.reply(prompt)` builds the answer (default: a title per lesson in the prompt)."""
    class Fake:
        prompts = []

        def reply(self, prompt):
            n = len(re.findall(r"^LESSON \d+:", prompt, flags=re.M))
            return json.dumps([{"lesson": k, "title": f"T{k}", "description": f"D{k}"} for k in range(1, n + 1)])

        def __call__(self, messages, **kwargs):
            prompt = messages[-1]["content"]
            self.prompts.append(prompt)
            answer = self.reply(prompt)
            if isinstance(answer, Exception):
                raise answer
            return answer
    fake = Fake()
    monkeypatch.setattr(titles, "call_llm", fake)
    return fake


def test_llm_writes_title_and_description(llm):
    texts, failed = lesson_titles(LOS, [LessonSpec([0, 1], hint="pointer"), LessonSpec([2, 3])])
    assert texts == [("T1", "D1"), ("T2", "D2")] and failed == 0
    prompt = llm.prompts[0]
    assert "Pointer Basics" in prompt and "Freeing Memory" in prompt and "Main topic: pointer" in prompt
    assert "SAME LANGUAGE as the learning objects" in prompt


def test_given_title_and_description_need_no_call(llm):
    texts, failed = lesson_titles(LOS, [LessonSpec([0], title="Úvod", description="Smerníky.")])
    assert texts == [("Úvod", "Smerníky.")] and failed == 0
    assert llm.prompts == []


def test_given_title_is_kept_and_only_description_is_written(llm):
    texts, _ = lesson_titles(LOS, [LessonSpec([0, 1], title="Úvod do smerníkov")])
    assert texts == [("Úvod do smerníkov", "D1")]
    assert "Title (given, keep it): Úvod do smerníkov" in llm.prompts[0]


@pytest.mark.parametrize("answer", ["no json", json.dumps({"x": 1}), LLMError("down"), None])
def test_unusable_answer_gives_fallback(llm, answer):
    llm.reply = lambda prompt: answer
    specs = [LessonSpec([0, 1], hint="pointer"), LessonSpec([2, 3]), LessonSpec([1], title="Daný názov")]
    texts, failed = lesson_titles(LOS, specs, topic_names=[None, None, "malloc", "free"])
    assert texts == [("pointer", "Pointer Basics; Address Operator"),
                     ("malloc", "Malloc Return Value; Freeing Memory"),  # most frequent topic (tie: first)
                     ("Daný názov", "Address Operator")]
    assert failed == 3


def test_missing_or_empty_result_falls_back_only_for_that_lesson(llm):
    llm.reply = lambda prompt: json.dumps([{"lesson": 1, "title": "Smerníky", "description": "O smerníkoch."},
                                           {"lesson": 2, "title": " ", "description": "x"}])
    texts, failed = lesson_titles(LOS, [LessonSpec([0]), LessonSpec([2, 3])])
    assert texts == [("Smerníky", "O smerníkoch."), ("Malloc Return Value", "Malloc Return Value; Freeing Memory")]
    assert failed == 1


def test_lessons_are_batched_and_language_is_passed(llm):
    specs = [LessonSpec([i % 4]) for i in range(7)]
    texts, failed = lesson_titles(LOS, specs, language="sk", batch_size=5)
    assert len(llm.prompts) == 2
    assert [t for t, _ in texts] == ["T1", "T2", "T3", "T4", "T5", "T1", "T2"] and failed == 0
    assert "Write in this language: sk." in llm.prompts[0]


def test_any_error_in_the_llm_call_gives_fallback(llm):
    # not only LLMError: e.g. a bug in a provider client must not fail the job
    llm.reply = lambda prompt: RuntimeError("unexpected")
    texts, failed = lesson_titles(LOS, [LessonSpec([0], hint="pointer")])
    assert texts == [("pointer", "Pointer Basics")] and failed == 1
