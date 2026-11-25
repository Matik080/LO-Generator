"""Second correct option for choice questions (lo_pipeline/questions/multi_correct.py), scripted LLM."""

import json

import pytest

from lo_pipeline.questions import multi_correct
from lo_pipeline.questions.multi_correct import add_correct_options, apply_extra_correct

SOURCE = "A pointer stores a memory address and can be dereferenced with the unary operator *."


def make_lo(i=1, **fields):
    lo = {
        "id": f"lo{i}",
        "type": "property",
        "question": "Which statements about pointers are true?",
        "answer": "It stores a memory address.",
        "source_statement": SOURCE,
        "mc_options": ["It stores a size.", "It stores a memory address.", "It stores a copy.", "It stores a name."],
        "mc_correct_index": 1,
    }
    lo.update(fields)
    return lo


@pytest.fixture
def llm(monkeypatch):
    """Answers each call with the next scripted JSON value; records the prompts."""
    calls, answers = [], []

    def fake(messages, temperature=0.2, **kwargs):
        calls.append(messages[-1]["content"])
        answer = answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer)
    monkeypatch.setattr(multi_correct, "call_llm", fake)
    return calls, answers


def test_second_correct_option_replaces_the_last_distractor(llm):
    calls, answers = llm
    answers.append([{"lo": 1, "extra_correct": "It can be dereferenced with *.",
                     "quote": "can be dereferenced with the unary operator *"}])
    lo = make_lo()
    add_correct_options([lo])

    assert lo["mc_options"] == ["It stores a size.", "It stores a memory address.", "It stores a copy.",
                                "It can be dereferenced with *."]
    assert lo["mc_correct_indices"] == [1, 3] and lo["mc_correct_index"] == 1
    assert "[correct]" in calls[0] and SOURCE in calls[0]


def test_null_and_bad_answers_change_nothing(llm):
    calls, answers = llm
    answers.append([
        {"lo": 1, "extra_correct": None, "quote": None},                                  # one-thing question
        {"lo": 2, "extra_correct": "It points to a function.", "quote": "points to a function always"},  # not in source
        {"lo": 3, "extra_correct": "it stores a  memory address.", "quote": "A pointer stores a memory address"},  # duplicate
        {"lo": 9, "extra_correct": "x", "quote": SOURCE},                                 # no such LO
        "not an object",
    ])
    los = [make_lo(i) for i in (1, 2, 3)]
    before = [dict(lo) for lo in los]
    add_correct_options(los)
    assert los == before


def test_invalid_json_and_empty_answer_leave_the_batch(llm):
    calls, answers = llm
    answers.extend(["not json {", ""])
    los = [make_lo(i) for i in range(1, 7)]  # 2 batches of 5 and 1
    before = [dict(lo) for lo in los]
    add_correct_options(los, batch_size=5)
    assert los == before and len(calls) == 2


def test_only_los_with_mc_options_are_sent(llm):
    calls, answers = llm
    answers.append([{"lo": 1, "extra_correct": None, "quote": None}])
    los = [make_lo(1, mc_options=None), make_lo(2), make_lo(3, mc_correct_index=7)]
    progress = []
    add_correct_options(los, progress=lambda done, total: progress.append((done, total)))
    assert len(calls) == 1 and calls[0].count("LO 1:") == 1 and "LO 2:" not in calls[0]
    assert progress == [(1, 1)]


def test_no_eligible_los_no_call(llm):
    calls, _ = llm
    add_correct_options([make_lo(mc_options=None)])
    assert calls == []


def test_quote_must_have_a_few_words():
    lo = make_lo()
    assert not apply_extra_correct(lo, "It can be dereferenced.", "pointer")
    assert not apply_extra_correct(lo, "", "can be dereferenced with the unary operator")
    assert not apply_extra_correct(lo, None, None)
    assert "mc_correct_indices" not in lo
