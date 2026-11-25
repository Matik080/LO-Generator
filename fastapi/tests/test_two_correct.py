"""Choice questions with 2 correct options, made in the MC step (lo/generator.py), scripted LLM."""

import json

import pytest

from lo_pipeline.lo import generator
from lo_pipeline.lo.generator import generate_mc_variants_batch, two_correct_options, wants_two_correct


def make_lo(i=1, **fields):
    lo = {"id": f"lo{i}", "type": "definition",
          "question": f"How is a Type I Error defined, and what is its other name? ({i})",
          "answer": "Rejecting a true null hypothesis; it is also called a False Positive.",
          "source_statement": "A Type I Error is rejecting a true H0, also called a False Positive."}
    lo.update(fields)
    return lo


CORRECT = ["A Type I Error is rejecting a true null hypothesis.", "A Type I Error is also called a False Positive."]
WRONG = ["A Type I Error is failing to reject a false null hypothesis.", "A Type I Error is also called a False Negative."]


@pytest.fixture
def llm(monkeypatch):
    calls, answers = [], []

    def fake(messages, temperature=0.4, **kwargs):
        calls.append(messages[-1]["content"])
        return json.dumps(answers.pop(0))
    monkeypatch.setattr(generator, "call_llm", fake)
    return calls, answers


def test_about_the_share_of_los_is_chosen_and_always_the_same_ones():
    los = [make_lo(i) for i in range(400)]
    chosen = [wants_two_correct(lo, 0.3) for lo in los]
    assert 0.22 < sum(chosen) / len(los) < 0.38
    assert chosen == [wants_two_correct(lo, 0.3) for lo in los]  # same LO -> same choice
    assert not any(wants_two_correct(lo, 0.0) for lo in los)
    assert all(wants_two_correct(lo, 1.0) for lo in los)


def test_two_correct_options_in_a_fixed_order():
    options, correct, first = two_correct_options(make_lo(), CORRECT, WRONG + ["a third wrong one"])
    assert sorted(options) == sorted(CORRECT + WRONG)  # 2 + 2, extra distractors dropped
    assert [options[i] for i in correct] == sorted(CORRECT, key=options.index)
    assert options[first] == CORRECT[0]
    assert two_correct_options(make_lo(), CORRECT, WRONG) == (options, correct, first)


@pytest.mark.parametrize("correct, wrong", [
    (None, WRONG), (CORRECT[:1], WRONG), (CORRECT + ["third"], WRONG), ([CORRECT[0], CORRECT[0].upper()], WRONG),
    (CORRECT, WRONG[:1]), (CORRECT, "not a list"), (CORRECT, [CORRECT[0], WRONG[0]]), ([CORRECT[0], " "], WRONG),
])
def test_unusable_answers_are_refused(correct, wrong):
    assert two_correct_options(make_lo(), correct, wrong) is None


def test_batch_with_two_correct_los(llm, monkeypatch):
    calls, answers = llm
    monkeypatch.setattr(generator, "wants_two_correct", lambda lo, share: lo["id"] in ("lo1", "lo3"))
    los = [make_lo(1), make_lo(2), make_lo(3)]
    answers.append([
        {"lo": 1, "correct": CORRECT, "distractors": WRONG},
        {"lo": 2, "distractors": ["w1", "w2", "w3"]},
        {"lo": 3, "correct": CORRECT[:1], "distractors": ["w1", "w2", "w3"]},  # unusable split: 1 correct as before
    ])
    generate_mc_variants_batch(los, two_correct_share=0.3)

    assert len(los[0]["mc_correct_indices"]) == 2 and len(los[0]["mc_options"]) == 4
    assert "mc_correct_indices" not in los[1] and los[1]["mc_options"][los[1]["mc_correct_index"]] == los[1]["answer"]
    assert "mc_correct_indices" not in los[2] and len(los[2]["mc_options"]) == 4
    prompt = calls[0]
    assert prompt.count("[TWO CORRECT]") == 3  # LO 1, LO 3 and the instructions
    assert '{"lo": <n>, "correct": [' in prompt


def test_batch_without_two_correct_los_keeps_the_old_prompt(llm):
    calls, answers = llm
    answers.extend([[{"lo": 1, "distractors": ["w1", "w2", "w3"]}]] * 2)
    generate_mc_variants_batch([make_lo(1)], two_correct_share=0.0)
    generate_mc_variants_batch([make_lo(1)])  # default: no LO gets two correct options
    assert calls[0] == calls[1] and "TWO CORRECT" not in calls[0]
