"""syllabus_text -> lesson plan (one LLM call, scripted fake here) and skipped content."""
import json

import pytest

from lo_pipeline.llm.service import LLMError
from lo_pipeline.split import syllabus_text
from lo_pipeline.split.syllabus import split_by_syllabus
from lo_pipeline.split.syllabus_text import plan_from_text

from tests.test_split_syllabus import fake_embed, lesson, lo


@pytest.fixture
def llm(monkeypatch):
    """Scripted LLM: set `llm.answer` (str or exception); prompts are kept in `llm.prompts`."""
    class Fake:
        answer = None
        prompts = []

        def __call__(self, messages, **kwargs):
            self.prompts.append(messages[-1]["content"])
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
    fake = Fake()
    monkeypatch.setattr(syllabus_text, "call_llm", fake)
    return fake


def test_plan_keeps_titles_covers_valid_pages_and_skip(llm):
    llm.answer = json.dumps({
        "lessons": [
            {"title": "Úvod  do pamäte", "pages": "D1:1-5", "covers": "pointer basics"},
            {"title": "Alokácia", "pages": "D3:1-2", "covers": "malloc and free"},  # D3 does not exist
            {"title": "Záver", "pages": "strany 9-10", "covers": ""},  # not the contract format
            {"title": "", "covers": "no title"},  # dropped
        ],
        "skip": ["entropy", "entropy", " "],
    })
    plan = plan_from_text("3 hodiny: pamäť, alokácia, záver; entropiu vynechaj", ["a.pdf", "b.pdf"],
                          ["pointer", "malloc", "entropy"])
    assert plan.lessons == [
        {"title": "Úvod do pamäte", "description": None, "pages": "D1:1-5", "match": "pointer basics"},
        {"title": "Alokácia", "description": None, "pages": None, "match": "malloc and free"},
        {"title": "Záver", "description": None, "pages": None, "match": None},
    ]
    assert plan.skip == ["entropy"]
    prompt = llm.prompts[0]
    assert "entropiu vynechaj" in prompt and "D2: b.pdf" in prompt and "pointer, malloc, entropy" in prompt


@pytest.mark.parametrize("answer", [
    "not json at all",
    json.dumps(["a list", "not an object"]),
    json.dumps({"lessons": []}),
    json.dumps({"lessons": [{"title": " "}, "text"]}),
    None,
    LLMError("provider down"),
    RuntimeError("unexpected"),
])
def test_unusable_answer_gives_none(llm, answer):
    llm.answer = answer
    assert plan_from_text("2 hodiny", ["a.pdf"]) is None


def test_lesson_limit(llm):
    llm.answer = json.dumps({"lessons": [{"title": f"L{i}"} for i in range(150)]})
    assert len(plan_from_text("veľa hodín", ["a.pdf"]).lessons) == 100


def test_skipped_content_is_left_out_but_page_match_wins():
    los = [lo("1", page=50, question="pointer"), lo("2", page=60, question="entropy of a coin"),
           lo("3", page=2, question="entropy")]  # page 2 is in lesson 0's range: stays
    syllabus = [lesson("pointer basics", "D1:1-10"), lesson("malloc")]
    s = split_by_syllabus(los, syllabus, ["a.pdf"], embed=fake_embed, skip=["entropy"])
    assert s.lessons == [(0, [2, 0])]
    assert s.skipped == [1]
    assert (s.by_pages, s.by_similarity, s.empty_lessons) == (1, 1, [1])


def test_match_text_replaces_title_and_description():
    # the Slovak title would not match; `match` (document language) decides
    los = [lo("1", question="variance of a sample"), lo("2", question="entropy of a coin")]
    syllabus = [{"title": "Entropia", "match": "variance"}, {"title": "Rozptyl", "match": "entropy"}]
    s = split_by_syllabus(los, syllabus, ["a.pdf"], embed=fake_embed)
    assert s.lessons == [(0, [0]), (1, [1])]
