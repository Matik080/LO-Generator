"""no_answer tasks from LOs that share a concept (lo_pipeline/questions/no_answer.py), scripted LLM."""

import json

import pytest

from lo_pipeline.questions import no_answer
from lo_pipeline.questions.no_answer import anchor_groups, generate_no_answer_tasks


def lo(anchor, n, **fields):
    return {"id": f"{anchor}{n}", "anchor": anchor, "source_statement": f"Statement {n} about {anchor}.", **fields}


def answer(group, **fields):
    return {"group": group, "task": f"Explain the idea of group {group}.", "name": f"Task {group}",
            "hint": "Start with the definition.", "time_to_handle": 600, "time_to_explain": 120, **fields}


@pytest.fixture
def llm(monkeypatch):
    calls, answers = [], []

    def fake(messages, temperature=0.4, **kwargs):
        calls.append(messages[-1]["content"])
        reply = answers.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply)
    monkeypatch.setattr(no_answer, "call_llm", fake)
    return calls, answers


def test_groups_follow_anchors_in_pipeline_order():
    los = [lo("pointer", 1), lo("malloc", 1), lo("pointer", 2), lo("free", 1), lo("malloc", 2),
           lo("pointer", 3), lo(None, 1), lo("", 2), lo("calloc", 1, source_statement="")]
    assert anchor_groups(los) == [
        {"anchor": "pointer", "lo_indices": [0, 2, 5]},
        {"anchor": "malloc", "lo_indices": [1, 4]},
    ]  # free / calloc: one LO only; no anchor or no statement: left out


def test_big_groups_are_split_into_near_equal_chunks():
    los = [lo("mean", i) for i in range(20)]
    sizes = [len(g["lo_indices"]) for g in anchor_groups(los, group_size=6)]
    assert sizes == [5, 5, 5, 5]
    assert [len(g["lo_indices"]) for g in anchor_groups(los[:7], group_size=6)] == [4, 3]
    assert [len(g["lo_indices"]) for g in anchor_groups(los[:6], group_size=6)] == [6]
    every = [i for g in anchor_groups(los, group_size=6) for i in g["lo_indices"]]
    assert every == list(range(20))  # every LO once, in order


def test_one_task_per_group(llm):
    calls, answers = llm
    answers.append([answer(2), answer(1)])  # the LLM may answer in another order
    los = [lo("pointer", 1), lo("malloc", 1), lo("pointer", 2), lo("malloc", 2)]
    tasks = generate_no_answer_tasks(los)

    assert [t["anchor"] for t in tasks] == ["pointer", "malloc"]
    assert tasks[0] == {"anchor": "pointer", "lo_indices": [0, 2], "description": "Explain the idea of group 1.",
                        "name": "Task 1", "hint": "Start with the definition.",
                        "time_to_handle": 600, "time_to_explain": 120}
    assert "Concept: pointer" in calls[0] and "Statement 2 about pointer." in calls[0]


def test_bad_results_drop_only_their_task(llm):
    calls, answers = llm
    answers.append([answer(1, task=""), answer(2, name=None), answer(3), answer(3, task="again"),
                    answer(7), "not an object", answer(4, hint=None)])
    los = [lo(a, n) for a in ("a", "b", "c", "d") for n in (1, 2)]
    tasks = generate_no_answer_tasks(los)
    assert [(t["anchor"], t["description"], t["hint"]) for t in tasks] == [
        ("c", "Explain the idea of group 3.", "Start with the definition."), ("d", "Explain the idea of group 4.", "")]


def test_invalid_json_drops_its_batch_only(llm):
    calls, answers = llm
    answers.extend(["not json [", [answer(1)]])
    los = [lo(f"a{g}", n) for g in range(6) for n in (1, 2)]  # 6 groups -> batches of 5 and 1
    tasks = generate_no_answer_tasks(los, batch_size=5)
    assert len(calls) == 2 and [t["anchor"] for t in tasks] == ["a5"]


def test_no_groups_no_call(llm):
    calls, _ = llm
    assert generate_no_answer_tasks([lo("a", 1), lo("b", 1)]) == [] and calls == []


def test_worker_asks_for_tasks_only_when_requested():
    from lo_pipeline.contract import JobOptions
    from lo_pipeline.contract.options import resolve_options
    from service.worker import pipeline_options
    assert pipeline_options(resolve_options(JobOptions())).no_answer_tasks
    only = resolve_options(JobOptions.model_validate({"questions": {"types": ["choice", "open"]}}))
    assert not pipeline_options(only).no_answer_tasks
