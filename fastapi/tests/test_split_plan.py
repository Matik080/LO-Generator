"""The split step (split_lessons): every mode, the fallbacks and the warning codes (Mlynček A's)."""
import json
import re

import pytest

from lo_pipeline.split import plan as plan_module
from lo_pipeline.split import syllabus_text, titles
from lo_pipeline.split.plan import split_lessons

VOCAB = ["pointer", "malloc", "variance", "entropy"]


def fake_embed(texts):
    return [[t.lower().replace(".", " ").replace(",", " ").split().count(w) + 0.01 for w in VOCAB] for t in texts]


def lo(id_, concept, page, prereqs=(), file="a.pdf"):
    return {"id": id_, "source_title": file, "source_page": page, "anchor": concept, "keywords": [concept],
            "question": f"what is {concept}", "answer": concept, "generated_name": f"{concept} {id_}",
            "prerequisite_ids": list(prereqs), "time_to_handle": 120, "time_to_explain": 30}


# two clear topics: memory (pointer, malloc) and statistics (variance, entropy)
GRAPH = {"nodes": [{"id": n} for n in VOCAB],
         "edges": [{"source": "pointer", "target": "malloc", "weight": 5},
                   {"source": "variance", "target": "entropy", "weight": 5}]}
LOS = [lo("p1", "pointer", 1), lo("m1", "malloc", 2), lo("p2", "pointer", 3),
       lo("v1", "variance", 4), lo("e1", "entropy", 5), lo("v2", "variance", 6, prereqs=["m1"])]


@pytest.fixture
def llm(monkeypatch):
    """Fake LLM for titles ("T1", …) and the syllabus plan (`llm.plan`)."""
    class Fake:
        prompts = []
        plan = None
        titles_fail = False

        def __call__(self, messages, **kwargs):
            prompt = messages[-1]["content"]
            self.prompts.append(prompt)
            if "TEACHER'S DESCRIPTION" in prompt:
                return self.plan
            if self.titles_fail:
                return "not json"
            n = len(re.findall(r"^LESSON \d+:", prompt, flags=re.M))
            return json.dumps([{"lesson": k, "title": f"T{k}", "description": f"D{k}"} for k in range(1, n + 1)])
    fake = Fake()
    monkeypatch.setattr(titles, "call_llm", fake)
    monkeypatch.setattr(syllabus_text, "call_llm", fake)
    return fake


def run(split, los=LOS, graph=GRAPH, files=("a.pdf",)):
    result = split_lessons(los, graph, split, list(files), embed=fake_embed)
    json.dumps(result)  # must be JSON for the checkpoint
    return result


def codes(result):
    return [w["code"] for w in result["warnings"]]


def flat(result):
    return [i for lesson in result["lessons"] for i in lesson["lo_indices"]]


def test_single_is_one_lesson_with_llm_title_and_topics_for_the_report(llm):
    r = run({"mode": "single"})
    assert len(r["lessons"]) == 1 and sorted(flat(r)) == list(range(6))
    assert (r["lessons"][0]["title"], r["lessons"][0]["description"]) == ("T1", "D1")
    assert r["lo_topics"][0] == r["lo_topics"][1] != r["lo_topics"][3]
    assert codes(r) == []


def test_lessons_gives_the_requested_count_in_prerequisite_order(llm):
    r = run({"mode": "lessons", "lesson_count": 2})
    assert [lesson["lo_indices"] for lesson in r["lessons"]] == [[0, 1, 2], [3, 4, 5]]
    assert codes(r) == []


def test_lessons_more_than_los_is_reduced(llm):
    r = run({"mode": "lessons", "lesson_count": 10})
    assert len(r["lessons"]) == 6
    assert codes(r) == ["lesson_count_reduced"]


def test_topics_one_lesson_per_topic_with_topic_hint(llm):
    r = run({"mode": "topics"})
    assert [sorted(lesson["lo_indices"]) for lesson in r["lessons"]] == [[0, 1, 2], [3, 4, 5]]
    assert any("Main topic:" in p for p in llm.prompts)


def test_topics_without_any_topic_gives_one_lesson(llm):
    r = run({"mode": "topics"}, graph={"nodes": [], "edges": []})
    assert len(r["lessons"]) == 1
    assert codes(r) == ["no_document_topics"]
    assert r["lo_topics"] == [None] * 6


def test_syllabus_list_keeps_teacher_texts_and_reports_problems(llm):
    syllabus = [{"title": "Štatistika", "description": "Rozptyl a entropia.", "pages": "D1:4-6"},
                {"title": "Pamäť: pointer a malloc", "pages": "D1:1-2"},
                {"title": "Prázdna", "pages": "D1:50-60"}]
    r = run({"mode": "syllabus", "syllabus": syllabus, "lesson_minutes": 45})
    lessons = r["lessons"]
    assert [(lesson["title"], sorted(lesson["lo_indices"])) for lesson in lessons] == \
        [("Štatistika", [3, 4, 5]), ("Pamäť: pointer a malloc", [0, 1, 2])]  # LO 2 (page 3) by similarity
    assert lessons[0]["description"] == "Rozptyl a entropia." and lessons[1]["description"] == "D1"
    assert codes(r) == ["syllabus_lesson_minutes_ignored", "empty_lesson", "syllabus_unmentioned_sections",
                        "prerequisite_order"]


def test_syllabus_text_plan_with_skip(llm):
    llm.plan = json.dumps({"lessons": [{"title": "Pamäť", "pages": None, "covers": "pointer malloc"}],
                           "skip": ["variance entropy"]})
    r = run({"mode": "syllabus", "syllabus_text": "1 hodina o pamäti, štatistiku vynechaj"})
    assert [(lesson["title"], sorted(lesson["lo_indices"])) for lesson in r["lessons"]] == [("Pamäť", [0, 1, 2])]
    assert r["lessons"][0]["description"] == "D1"
    assert codes(r) == ["syllabus_skipped_sections"]


def test_unusable_syllabus_text_falls_back_to_topics(llm):
    llm.plan = "not json"
    r = run({"mode": "syllabus", "syllabus_text": "niečo"})
    assert len(r["lessons"]) == 2
    assert codes(r) == ["syllabus_plan_invalid"]


def test_failed_titles_use_fallback_and_warn(llm):
    llm.titles_fail = True
    r = run({"mode": "topics"})
    assert all(lesson["title"] for lesson in r["lessons"])
    assert codes(r) == ["lesson_titles_failed"]


def test_error_in_a_mode_gives_one_lesson(llm, monkeypatch):
    def broken(*args, **kwargs):
        raise KeyError("bug")
    monkeypatch.setattr(plan_module, "split_by_topics", broken)
    r = run({"mode": "topics"})
    assert len(r["lessons"]) == 1 and sorted(flat(r)) == list(range(6))
    assert codes(r) == ["split_failed"]


def test_timeout_or_crash_in_a_mode_is_not_caught(llm, monkeypatch):
    def timeout(*args, **kwargs):
        raise RuntimeError("job timeout")  # not a bug type: must stop the job
    monkeypatch.setattr(plan_module, "split_by_topics", timeout)
    with pytest.raises(RuntimeError):
        run({"mode": "topics"})


def test_no_los():
    assert split_lessons([], GRAPH, {"mode": "topics"}, ["a.pdf"]) == {"lessons": [], "lo_topics": [], "warnings": []}
