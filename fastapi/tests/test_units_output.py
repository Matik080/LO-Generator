"""Mapping of a run_pipeline result to `units` + report (sprint task 5). No LLM calls."""

import copy
import json
from pathlib import Path

import pytest

from lo_pipeline.contract.options import JobOptions, resolve_options
from lo_pipeline.contract.units import CHOICE_TEXT_MAX, HINT_URL_MAX, QUESTION_NAME_MAX
from lo_pipeline.questions import units_output
from lo_pipeline.questions.units_output import InvalidOutputError, build_units_and_report

SNAPSHOT = Path(__file__).parent / "fixtures" / "snapshots" / "c_memory_chain.json"


def options(**sent) -> JobOptions:
    return resolve_options(JobOptions.model_validate(sent))


def make_lo(**fields) -> dict:
    lo = {
        "id": "aaaa0001",
        "type": "definition",
        "question": "What is a pointer?",
        "answer": "A variable that stores a memory address.",
        "hint": "Think about addresses. [Source: c.pdf, p. 3]",
        "source_statement": "A pointer stores a memory address.",
        "source_title": "c.pdf",
        "source_page": 3,
        "keywords": ["pointer", "address"],
        "mc_options": ["Stores an address", "Stores a copy", "Stores a size", "Stores a name"],
        "mc_correct_index": 0,
        "prerequisite_ids": [],
        "generated_name": "Pointer Definition",
        "time_to_handle": 45,
        "time_to_explain": 30,
    }
    lo.update(fields)
    return lo


def build(los, opts=None, **result):
    return build_units_and_report({"learning_objects": los, **result}, opts or options())


@pytest.fixture(scope="module")
def snapshot_los():
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))["learning_objects"]


def test_snapshot_result_passes_schemas(snapshot_los):
    units, report = build(copy.deepcopy(snapshot_los))  # check_output inside raises when invalid
    questions = units["units"][0]["questions"]
    assert len(units["units"]) == 1
    assert len(questions) == len(snapshot_los) == len(report["sections"]) == len(report["question_meta"])
    assert {q["type"] for q in questions} <= {"choice", "open"}
    for q in questions:
        assert "[Source" not in q["name"]
        for hint in q["hints"]:
            assert "[Source" not in hint["url"]


def test_question_from_lo():
    units, report = build([make_lo()])
    q = units["units"][0]["questions"][0]
    assert q["name"] == "Pointer Definition"
    assert q["description"] == "What is a pointer?"
    assert q["type"] == "choice"
    assert q["choices"][0] == {"text": "Stores an address", "is_correct": True}
    assert sum(c["is_correct"] for c in q["choices"]) == 1 and len(q["choices"]) == 4
    assert q["hints"] == [{"name": "Zdroj", "url": "<p>c.pdf, strana 3</p>"},
                          {"name": "Nápoveda", "url": "<p>Think about addresses.</p>"}]
    meta = report["question_meta"][0]
    assert meta == {"unit": 0, "question": 0, "section_id": "S-1", "bloom_level": None,
                    "keywords": ["pointer", "address"], "source": [{"file": "c.pdf", "pages": [3]}],
                    "topic": None}


def test_lo_without_mc_is_open():
    q = build([make_lo(mc_options=None, mc_correct_index=None)])[0]["units"][0]["questions"][0]
    assert q["type"] == "open"
    assert q["choices"] == [{"text": "A variable that stores a memory address.", "is_correct": True}]


def test_hints_option():
    for count, names in ((0, []), (1, ["Zdroj"]), (2, ["Zdroj", "Nápoveda"])):
        units, _ = build([make_lo()], options(questions={"hints": count}))
        assert [h["name"] for h in units["units"][0]["questions"][0]["hints"]] == names


def test_types_option():
    choice_lo, open_lo = make_lo(), make_lo(id="b", mc_options=None, mc_correct_index=None)
    only_open, _ = build([choice_lo, open_lo], options(questions={"types": ["open"]}))
    assert [q["type"] for q in only_open["units"][0]["questions"]] == ["open", "open"]

    only_choice, report = build([choice_lo, open_lo], options(questions={"types": ["choice"]}))
    assert [q["type"] for q in only_choice["units"][0]["questions"]] == ["choice"]
    assert report["question_meta"][0]["section_id"] == "S-1"
    assert "question_skipped" in [w["code"] for w in report["warnings"]]


def test_choice_options_option():
    q = build([make_lo()], options(questions={"choice_options": 2}))[0]["units"][0]["questions"][0]
    assert [c["text"] for c in q["choices"]] == ["Stores an address", "Stores a copy"]

    lo = make_lo(mc_options=["x", "Right", "y", "z"], mc_correct_index=1)
    q = build([lo], options(questions={"choice_options": 2}))[0]["units"][0]["questions"][0]
    assert q["choices"] == [{"text": "x", "is_correct": False}, {"text": "Right", "is_correct": True}]

    _, report = build([make_lo()], options(questions={"choice_options": 6}))
    assert "choice_options_unavailable" in [w["code"] for w in report["warnings"]]


def test_per_section_warning_and_no_split_mode_warning():
    opts = options(split={"mode": "lessons", "lesson_count": 3})
    units, report = build([make_lo()], opts)
    codes = [w["code"] for w in report["warnings"]]
    assert len(units["units"]) == 1
    assert "per_section_unsupported" in codes and "split_mode_unsupported" not in codes
    assert "question_type_unsupported" not in codes  # no_answer is generated now (task 1c)


def codes_of(report):
    return [w["code"] for w in report["warnings"]]


def test_long_name_is_cut_at_a_word():
    units, report = build([make_lo(generated_name="Slovo " * 60)])  # 360 characters
    name = units["units"][0]["questions"][0]["name"]
    assert len(name) <= QUESTION_NAME_MAX and name.endswith("Slovo…")
    assert "field_too_long" not in codes_of(report)


@pytest.mark.parametrize("fields", [
    {"question": "Q" * 4001},
    {"mc_options": ["A" * 4001, "b", "c", "d"]},
    {"mc_options": None, "answer": "A" * 4001},  # open question: the answer is its choice
])
def test_question_with_a_too_long_text_or_choice_is_left_out(fields):
    units, report = build([make_lo(**fields), make_lo(id="b", generated_name="Kept")])
    assert [q["name"] for q in units["units"][0]["questions"]] == ["Kept"]
    assert codes_of(report).count("field_too_long") == 1
    assert "question_skipped" not in codes_of(report)  # counted once, as too long


def test_too_long_no_answer_task_is_left_out():
    units, report = build([make_lo(), make_lo(id="b")], no_answer_tasks=[make_task([0, 1], description="T" * 4001)])
    assert [q["type"] for q in units["units"][0]["questions"]] == ["choice", "choice"]
    assert "field_too_long" in codes_of(report)


@pytest.mark.parametrize("hint", ["H" * 3000, "<" * 500])  # '<' becomes '&lt;' (4 characters) in the HTML
def test_too_long_hint_is_left_out_but_the_question_stays(hint):
    units, report = build([make_lo(hint=hint + " [Source: c.pdf, p. 3]")])
    hints = units["units"][0]["questions"][0]["hints"]
    assert [h["name"] for h in hints] == ["Zdroj"]
    assert "hint_too_long" in codes_of(report) and "field_too_long" not in codes_of(report)


def test_long_lesson_title_is_cut_at_a_word():
    split = split_result([("Názov " * 60, [0])])
    units, _ = build([make_lo()], split=split)
    assert len(units["units"][0]["title"]) <= 255 and units["units"][0]["title"].endswith("Názov…")


EVIL = ['<script>alert(1)</script>', '<img src=x onerror="alert(1)">', '<a href="javascript:alert(1)">klik</a>',
        "</p><iframe src='https://evil'></iframe><p>", '<x onmouseover="alert(1)">x</x>', "&lt;script&gt;alert(1)"]


@pytest.mark.parametrize("evil", EVIL)
def test_unsafe_html_from_the_llm_never_reaches_the_output(evil):
    lo = make_lo(question=f"What is a pointer? {evil}", generated_name=f"Name {evil}", hint=f"Hint {evil}",
                 mc_options=[f"Stores an address {evil}", "b", "c", "d"], keywords=[f"pointer {evil}"])
    task = make_task([0, 1], description=f"Explain {evil}", name=f"Task {evil}", hint=f"Start {evil}")
    split = split_result([(f"Lesson {evil}", [0, 1])])
    split["lessons"][0]["description"] = f"About {evil}"
    units, report = build([lo, make_lo(id="b")], split=split, no_answer_tasks=[task])
    questions = units["units"][0]["questions"]
    # fields ECEH shows as HTML: escaped, so no tag at all ("<" only as "&lt;")
    for q in questions:
        for text in [q["description"]] + [c["text"] for c in q["choices"]]:
            assert "<" not in text and ">" not in text, text
        for hint in q["hints"]:  # only the <p> wrapper, everything else escaped
            inner = hint["url"][len("<p>"):-len("</p>")]
            assert hint["url"].startswith("<p>") and "<" not in inner and ">" not in inner
    # plain-text fields (shown as text): unsafe tags removed
    plain = [q["name"] for q in questions] + [units["units"][0]["title"], units["units"][0]["description"]]
    plain += [s["title"] for s in report["sections"]] + [k for m in report["question_meta"] for k in m["keywords"]]
    for text in plain:
        for bad in ("<script", "<img", "<iframe", "<a ", "<x ", "onerror", "onmouseover", "javascript:"):
            assert bad not in text.lower(), (bad, text)


def test_code_and_html_examples_are_shown_exactly():
    # ECEH renders the question text and choices as HTML: escaped, the student sees exactly this text
    lo = make_lo(question="Why does #include <stdio.h> come first, and what does <p> do?",
                 mc_options=["a < b && c > d", "b", "c", "d"], hint="Use <b>bold</b>.")
    q = build([lo])[0]["units"][0]["questions"][0]
    assert q["description"] == "Why does #include &lt;stdio.h&gt; come first, and what does &lt;p&gt; do?"
    assert q["choices"][0]["text"] == "a &lt; b &amp;&amp; c &gt; d"
    assert q["hints"][1]["url"] == "<p>Use &lt;b&gt;bold&lt;/b&gt;.</p>"


def test_entities_from_the_llm_are_not_escaped_twice():
    q = build([make_lo(question="Is a &lt; b?", mc_options=["Tom &amp; Jerry", "b", "c", "d"])])[0]
    q = q["units"][0]["questions"][0]
    assert q["description"] == "Is a &lt; b?" and q["choices"][0]["text"] == "Tom &amp; Jerry"


def test_question_text_length_counts_the_escaped_text():
    # 1500 '<' are 1500 characters as text, but 6000 escaped ('&lt;'): too long for the 4000 column
    units, report = build([make_lo(question="<" * 1500), make_lo(id="b", generated_name="Kept")])
    assert [q["name"] for q in units["units"][0]["questions"]] == ["Kept"]
    assert "field_too_long" in codes_of(report)


def test_quality_scores_and_topic_coverage():
    quality = {"final": {"scores": {"all_models": {"faithfulness": 4.5, "question_clarity": 4.0}}}}
    split = split_result([("Prvá", [0, 1])])
    split.update({"lo_topic_ids": [0, 1, 2], "document_topics": [0, 1, 2, 3]})
    _, report = build(three_los(), quality=quality, split=split)  # LO 2 is in no lesson
    assert report["quality"] == {"section_faithfulness_avg": None, "question_faithfulness_avg": 0.875,
                                 "answerability_avg": 0.75, "topic_coverage": 0.5}
    _, report = build(three_los())  # no split, no scores
    assert report["quality"]["topic_coverage"] is None and report["quality"]["answerability_avg"] is None


def test_bad_llm_values():
    los = [
        make_lo(question=""),  # no question text: left out
        make_lo(id="b", time_to_handle="120", time_to_explain=-5),
        make_lo(id="c", mc_options=["same", "Same ", "x"], mc_correct_index=0),  # duplicate choices: left out
        make_lo(id="d", mc_correct_index=9, generated_name=None),  # bad index: open question, name = question
    ]
    units, report = build(los)
    questions = units["units"][0]["questions"]
    assert len(questions) == 2
    assert (questions[0]["time_to_handle"], questions[0]["time_to_explain"]) == (120, 30)  # -5: choice default
    assert questions[1]["type"] == "open" and questions[1]["name"] == "What is a pointer?"
    assert [m["section_id"] for m in report["question_meta"]] == ["S-2", "S-4"]
    assert [m["question"] for m in report["question_meta"]] == [0, 1]
    assert "2 learning object(s)" in next(w["message"] for w in report["warnings"]
                                          if w["code"] == "question_skipped")


def test_report_sections_quality_and_warnings():
    los = [make_lo(id="a"), make_lo(id="b", prerequisite_ids=["a", "unknown", "b"], type="rule")]
    quality = {"final": {"scores": {"all_models": {"faithfulness": 4.5}}}}
    warnings = [{"code": "sections_skipped", "message": "1 section(s) were skipped", "details": {"x": 1}}]
    _, report = build(los, quality=quality, warnings=warnings, timings={"extract": 1, "units": 2.5})
    assert report["sections"][1] == {"id": "S-2", "title": "Pointer Definition",
                                     "statement": "A pointer stores a memory address.",
                                     "lo_type": "rule", "prerequisites": ["S-1"], "topic": None}
    assert report["quality"]["question_faithfulness_avg"] == 0.875  # (4.5 - 1) / 4
    assert report["warnings"][0] == {"code": "sections_skipped", "message": "1 section(s) were skipped"}
    assert report["timings"] == {"extract": 1.0, "units": 2.5}


def test_unit_title_and_description():
    units, _ = build([make_lo(), make_lo(id="b", source_title="d.pptx", generated_name="Malloc")])
    unit = units["units"][0]
    assert unit["title"] == "c.pdf, d.pptx"
    assert unit["description"] == "Témy: Pointer Definition; Malloc"


def test_no_questions_is_still_valid():
    units, report = build([])
    assert units["units"][0]["questions"] == [] and report["sections"] == []


def test_invalid_question_is_left_out(monkeypatch):
    monkeypatch.setattr(units_output, "SOURCE_HINT_NAME", "x" * 51)  # longer than helps.name
    units, report = build([make_lo()])
    assert units["units"][0]["questions"] == []
    assert "question_skipped" in [w["code"] for w in report["warnings"]]


def test_invalid_output_raises(monkeypatch):
    """The final check is a safety net for bugs: e.g. a section id that breaks the pattern."""
    monkeypatch.setattr(units_output, "section_id", lambda i: "bad id")
    with pytest.raises(InvalidOutputError):
        build([make_lo()])


def test_times_fit_the_range_of_the_question_type():
    from lo_pipeline.questions.units_output import question_times
    assert question_times("choice", 10, 500) == (30, 90)          # below / above the range
    assert question_times("choice", None, "x") == (120, 30)       # missing / not a number: default
    assert question_times("open", 45, 30) == (120, 30)            # short open answer still gets 2 min
    assert question_times("open", 2000, None) == (900, 0)
    assert question_times("no_answer", 1200, 900) == (1200, 600)
    assert question_times("no_answer", None, None) == (300, 0)

    units, _ = build([make_lo(time_to_handle=5), make_lo(id="b", mc_options=None, time_to_handle=5)])
    times = [(q["type"], q["time_to_handle"]) for q in units["units"][0]["questions"]]
    assert times == [("choice", 30), ("open", 120)]


def test_choice_with_two_correct_options():
    lo = make_lo(mc_options=["Stores an address", "Stores a copy", "Can be dereferenced", "Stores a name"],
                 mc_correct_index=0, mc_correct_indices=[0, 2])
    units, _ = build([lo])
    choices = units["units"][0]["questions"][0]["choices"]
    assert [(c["text"], c["is_correct"]) for c in choices] == [
        ("Stores an address", True), ("Stores a copy", False), ("Can be dereferenced", True), ("Stores a name", False)]

    # fewer options requested: both correct ones stay, plus at least one distractor
    units, _ = build([lo], options(questions={"choice_options": 2}))
    choices = units["units"][0]["questions"][0]["choices"]
    assert [c["is_correct"] for c in choices] == [True, False, True]

    # invalid list (index out of range): only mc_correct_index counts
    units, _ = build([make_lo(mc_correct_indices=[0, 9])])
    assert [c["is_correct"] for c in units["units"][0]["questions"][0]["choices"]] == [True, False, False, False]


def make_task(lo_indices, **fields):
    task = {"anchor": "pointer", "lo_indices": lo_indices, "description": "Explain how & and * work together.",
            "name": "Pointer Operators", "hint": "Start with what a pointer stores.",
            "time_to_handle": 2400, "time_to_explain": None}
    task.update(fields)
    return task


def test_no_answer_task_comes_after_the_last_lo_of_its_group():
    los = [make_lo(id="a", source_page=3), make_lo(id="b", source_page=7, question="Q b"),
           make_lo(id="c", source_page=5, question="Q c", source_title="d.pdf")]
    units, report = build(los, no_answer_tasks=[make_task([0, 1]), make_task([0, 2], anchor="free", name="Free")])
    questions = units["units"][0]["questions"]

    assert [q["type"] for q in questions] == ["choice", "choice", "no_answer", "choice", "no_answer"]
    task = questions[2]
    assert task["choices"] == [] and task["name"] == "Pointer Operators"
    assert (task["time_to_handle"], task["time_to_explain"]) == (1800, 0)  # fitted to the no_answer range
    assert task["hints"] == [{"name": "Zdroj", "url": "<p>c.pdf, strany 3, 7</p>"},
                             {"name": "Nápoveda", "url": "<p>Start with what a pointer stores.</p>"}]
    assert questions[4]["hints"][0]["url"] == "<p>c.pdf, strana 3; d.pdf, strana 5</p>"

    meta = report["question_meta"]
    assert [(m["question"], m["section_id"]) for m in meta] == [(0, "S-1"), (1, "S-2"), (2, "S-2"), (3, "S-3"), (4, "S-3")]
    assert meta[2]["keywords"] == ["pointer"]
    assert meta[2]["source"] == [{"file": "c.pdf", "pages": [3, 7]}]
    assert meta[4]["source"] == [{"file": "c.pdf", "pages": [3]}, {"file": "d.pdf", "pages": [5]}]


def test_no_answer_tasks_only_when_requested_and_valid():
    los = [make_lo(id="a"), make_lo(id="b")]
    tasks = [make_task([0, 1]), make_task([0, 9]), make_task([]), make_task([0, 1], description=" "), "x"]

    units, report = build(los, options(questions={"types": ["choice", "open"]}), no_answer_tasks=tasks)
    assert [q["type"] for q in units["units"][0]["questions"]] == ["choice", "choice"]

    units, report = build(los, no_answer_tasks=tasks)
    assert [q["type"] for q in units["units"][0]["questions"]] == ["choice", "choice", "no_answer"]
    assert "1 no_answer task(s)" in next(w["message"] for w in report["warnings"] if w["code"] == "no_answer_skipped")

    units, _ = build(los, options(questions={"hints": 1}), no_answer_tasks=tasks[:1])
    assert [h["name"] for h in units["units"][0]["questions"][2]["hints"]] == ["Zdroj"]


# --- lessons from the split step ------------------------------------------------------------

def three_los():
    return [make_lo(id="a", generated_name="A"), make_lo(id="b", generated_name="B"),
            make_lo(id="c", generated_name="C")]


def split_result(lessons, lo_topics=None, warnings=()):
    return {"lessons": [{"lo_indices": idx, "title": t, "description": f"About {t}."} for t, idx in lessons],
            "lo_topics": lo_topics or [], "warnings": list(warnings)}


def test_one_unit_per_lesson_in_lesson_order_with_topics():
    split = split_result([("Druhá", [2, 0]), ("Prvá", [1])], lo_topics=["pointer", "malloc", None],
                         warnings=[{"code": "lessons_unbalanced", "message": "uneven"}])
    units, report = build(three_los(), split=split)
    assert [(u["title"], u["description"], [q["name"] for q in u["questions"]]) for u in units["units"]] == \
        [("Druhá", "About Druhá.", ["C", "A"]), ("Prvá", "About Prvá.", ["B"])]
    assert [(m["unit"], m["question"], m["section_id"], m["topic"]) for m in report["question_meta"]] == \
        [(0, 0, "S-3", None), (0, 1, "S-1", "pointer"), (1, 0, "S-2", "malloc")]
    assert [s["topic"] for s in report["sections"]] == ["pointer", "malloc", None]
    assert "lessons_unbalanced" in [w["code"] for w in report["warnings"]]


def test_lesson_without_valid_question_is_left_out():
    los = three_los()
    los[1]["generated_name"] = ""  # no name -> no valid question
    los[1]["question"] = ""
    units, report = build(los, split=split_result([("Prvá", [0]), ("Prázdna", [1]), ("Tretia", [2])]))
    assert [u["title"] for u in units["units"]] == ["Prvá", "Tretia"]
    assert [m["unit"] for m in report["question_meta"]] == [0, 1]
    assert {"empty_lesson", "question_skipped"} <= {w["code"] for w in report["warnings"]}


def test_lo_left_out_by_the_split_has_a_section_but_no_question():
    units, report = build(three_los(), split=split_result([("Jediná", [0, 2])]))
    assert [q["name"] for q in units["units"][0]["questions"]] == ["A", "C"]
    assert len(report["sections"]) == 3


def test_no_answer_task_follows_the_group_lo_that_comes_last_in_the_lessons():
    split = split_result([("Prvá", [2]), ("Druhá", [0, 1])])
    tasks = [make_task([0, 2]), make_task([2], name="Only C"), make_task([1], name="Gone")]
    units, report = build(three_los(), split=split_result([("Prvá", [2]), ("Druhá", [0])]), no_answer_tasks=tasks)
    names = [[q["name"] for q in u["questions"]] for u in units["units"]]
    # task [0, 2]: LO 0 (lesson 2) comes after LO 2 (lesson 1); task [1]: LO 1 is in no lesson -> dropped
    assert names == [["C", "Only C"], ["A", "Pointer Operators"]]
    units, _ = build(three_los(), split=split, no_answer_tasks=tasks[:1])
    assert [q["name"] for q in units["units"][1]["questions"]] == ["A", "Pointer Operators", "B"]


def test_missing_or_broken_split_gives_one_lesson_from_the_files():
    for split in (None, {"lessons": [{"lo_indices": [0, 9], "title": "x", "description": ""}]},
                  {"lessons": []}):
        units, report = build(three_los(), split=split)
        assert len(units["units"]) == 1 and units["units"][0]["title"] == "c.pdf"
        assert [q["name"] for q in units["units"][0]["questions"]] == ["A", "B", "C"]
        assert all(s["topic"] is None for s in report["sections"])


def test_lo_in_two_lessons_is_used_once_and_empty_title_gets_a_fallback():
    units, _ = build(three_los(), split=split_result([("", [0, 1]), ("Druhá", [1, 2])]))
    assert [(u["title"], [q["name"] for q in u["questions"]]) for u in units["units"]] == \
        [("Hodina", ["A", "B"]), ("Druhá", ["C"])]
