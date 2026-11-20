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


def test_unsupported_options_give_warnings():
    opts = options(split={"mode": "lessons", "lesson_count": 3})  # default types include no_answer
    units, report = build([make_lo()], opts)
    codes = [w["code"] for w in report["warnings"]]
    assert len(units["units"]) == 1
    assert {"split_mode_unsupported", "question_type_unsupported", "per_section_unsupported"} <= set(codes)


def test_long_texts_are_shortened():
    lo = make_lo(generated_name="N" * 400, hint="H" * 3000 + " [Source: c.pdf, p. 3]",
                 mc_options=["A" * 5000, "b", "c", "d"])
    units, report = build([lo])
    q = units["units"][0]["questions"][0]
    assert len(q["name"]) == QUESTION_NAME_MAX and q["name"].endswith("…")
    assert len(q["choices"][0]["text"]) == CHOICE_TEXT_MAX
    assert len(q["hints"][1]["url"]) <= HINT_URL_MAX
    assert [w["code"] for w in report["warnings"]].count("field_too_long") == 1  # counted once


def test_long_hint_with_html_characters_fits():
    """Escaping makes '<' five times longer; the shortened hint must still fit."""
    lo = make_lo(hint="<" * 3000)
    url = build([lo])[0]["units"][0]["questions"][0]["hints"][1]["url"]
    assert len(url) <= HINT_URL_MAX and url.startswith("<p>&lt;") and url.endswith("…</p>")


@pytest.mark.parametrize("evil", [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    '<a href="javascript:alert(1)">klik</a>',
    "</p><iframe src='https://evil'></iframe><p>",
])
def test_dangerous_html_in_hint_is_shown_as_text(evil):
    url = build([make_lo(hint=evil)])[0]["units"][0]["questions"][0]["hints"][1]["url"]
    assert url.startswith("<p>") and url.endswith("</p>")
    inner = url[len("<p>"):-len("</p>")]
    assert "<" not in inner and ">" not in inner  # no tag at all, only escaped text


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
    assert (questions[0]["time_to_handle"], questions[0]["time_to_explain"]) == (120, 45)
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
    """The final check is a safety net for bugs: e.g. an empty lesson title (min length 1)."""
    monkeypatch.setattr(units_output, "_unit_title_and_description", lambda los: ("", ""))
    with pytest.raises(InvalidOutputError):
        build([make_lo()])
