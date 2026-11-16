"""Contract tests: models and JSON Schemas of units, report, options and job status.

Every rule that Laravel can check is tested twice: with our Pydantic model and with the
JSON Schema file from schemas/ (what Laravel and Mlynček A get).
"""

import copy
import json

import jsonschema
import pytest
from pydantic import ValidationError

from lo_pipeline.contract import JobOptions, JobStatus, OptionDefaults, Report, UnitsResult, resolve_options
from lo_pipeline.contract.schemas import EXAMPLES_DIR as FIXTURES
from lo_pipeline.contract.schemas import load_schema, outdated_schemas


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def lesson_schema():
    return jsonschema.Draft202012Validator(load_schema("lesson"))


@pytest.fixture(scope="module")
def report_schema():
    return jsonschema.Draft202012Validator(load_schema("report"))


@pytest.fixture(scope="module")
def options_schema():
    return jsonschema.Draft202012Validator(load_schema("options"))


@pytest.fixture
def units():
    return fixture("units_example.json")


@pytest.fixture
def report():
    return fixture("report_example.json")


def rejected_by_both(data, model, validator):
    with pytest.raises(ValidationError):
        model.model_validate(data)
    assert not validator.is_valid(data)


def first_question(data):
    return data["units"][0]["questions"][0]


# --- schema files -------------------------------------------------------------------------

def test_schema_files_are_up_to_date():
    assert outdated_schemas() == [], "run: python -m lo_pipeline.contract.schemas"


def test_schemas_are_valid_json_schemas():
    for name in ("lesson", "report", "options"):
        jsonschema.Draft202012Validator.check_schema(load_schema(name))


# --- units --------------------------------------------------------------------------------

def test_units_example_is_valid(units, lesson_schema):
    lesson_schema.validate(units)
    assert UnitsResult.model_validate(units).model_dump(mode="json") == units  # nothing added or lost


@pytest.mark.parametrize("path", [(), ("units", 0), ("units", 0, "questions", 0),
                                  ("units", 0, "questions", 0, "hints", 0),
                                  ("units", 0, "questions", 0, "choices", 0)])
def test_units_reject_extra_fields(units, lesson_schema, path):
    target = units
    for key in path:
        target = target[key]
    target["extra"] = "not allowed"
    rejected_by_both(units, UnitsResult, lesson_schema)


@pytest.mark.parametrize("field, length", [("name", 256), ("description", 4001)])
def test_question_length_limits(units, lesson_schema, field, length):
    first_question(units)[field] = "x" * length
    rejected_by_both(units, UnitsResult, lesson_schema)
    first_question(units)[field] = "x" * (length - 1)
    UnitsResult.model_validate(units)
    lesson_schema.validate(units)


def test_hint_and_choice_limits(units, lesson_schema):
    for change in (lambda q: q["hints"][0].update(name="x" * 51),
                   lambda q: q["hints"][0].update(url="x" * 2001),
                   lambda q: q["choices"][0].update(text="x" * 4001)):
        data = copy.deepcopy(units)
        change(first_question(data))
        rejected_by_both(data, UnitsResult, lesson_schema)


def test_unit_title_limit_and_no_empty_units(units, lesson_schema):
    data = copy.deepcopy(units)
    data["units"][0]["title"] = "x" * 256
    rejected_by_both(data, UnitsResult, lesson_schema)
    rejected_by_both({"units": []}, UnitsResult, lesson_schema)


def test_choice_needs_a_correct_option_and_two_options(units, lesson_schema):
    data = copy.deepcopy(units)
    for choice in first_question(data)["choices"]:
        choice["is_correct"] = False
    rejected_by_both(data, UnitsResult, lesson_schema)

    data = copy.deepcopy(units)
    first_question(data)["choices"] = [{"text": "only one", "is_correct": True}]
    rejected_by_both(data, UnitsResult, lesson_schema)


def test_choice_duplicate_texts_rejected_by_model(units):
    first_question(units)["choices"][1]["text"] = "  vráti SMERNÍK na začiatok alokovaného bloku "
    with pytest.raises(ValidationError):
        UnitsResult.model_validate(units)


def test_open_needs_exactly_one_correct_answer(units, lesson_schema):
    open_question = units["units"][0]["questions"][1]
    open_question["choices"].append({"text": "second answer", "is_correct": True})
    rejected_by_both(units, UnitsResult, lesson_schema)
    open_question["choices"] = [{"text": "wrong", "is_correct": False}]
    rejected_by_both(units, UnitsResult, lesson_schema)


def test_no_answer_has_no_choices(units, lesson_schema):
    units["units"][0]["questions"][2]["choices"] = [{"text": "x", "is_correct": True}]
    rejected_by_both(units, UnitsResult, lesson_schema)


@pytest.mark.parametrize("field, value", [("time_to_handle", 1.5), ("time_to_handle", "90"),
                                          ("time_to_explain", -1), ("type", "essay")])
def test_question_field_types(units, lesson_schema, field, value):
    first_question(units)[field] = value
    rejected_by_both(units, UnitsResult, lesson_schema)


def test_is_correct_must_be_a_boolean(units, lesson_schema):
    first_question(units)["choices"][0]["is_correct"] = "true"
    rejected_by_both(units, UnitsResult, lesson_schema)


# --- report -------------------------------------------------------------------------------

def test_report_example_is_valid(report, report_schema):
    report_schema.validate(report)
    assert Report.model_validate(report).model_dump(mode="json") == report


def test_report_rejects_bad_section_ids_and_extra_fields(report, report_schema):
    data = copy.deepcopy(report)
    data["sections"][1]["prerequisites"] = ["1fd448b1"]  # B's internal LO hash is not a section id
    rejected_by_both(data, Report, report_schema)

    data = copy.deepcopy(report)
    data["extensions"] = {}
    rejected_by_both(data, Report, report_schema)


def test_report_quality_is_on_a_0_to_1_scale(report, report_schema):
    report["quality"]["question_faithfulness_avg"] = 4.3  # a 1–5 score must be converted
    rejected_by_both(report, Report, report_schema)


def test_report_bloom_level_is_null_or_a_level(report, report_schema):
    report["question_meta"][0]["bloom_level"] = "understand"
    report_schema.validate(report)
    report["question_meta"][0]["bloom_level"] = "know"
    rejected_by_both(report, Report, report_schema)


# --- options ------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(fixture("options_examples.json")))
def test_options_examples_are_valid(options_schema, name):
    options = fixture("options_examples.json")[name]
    JobOptions.model_validate(options)
    options_schema.validate(options)


@pytest.mark.parametrize("options", [
    {"unknown": 1},
    {"language": "de"},
    {"questions": {"per_section": 0}},
    {"questions": {"types": []}},
    {"questions": {"hints": 3}},
    {"split": {"mode": "weekly"}},
    {"split": {"mode": "lessons"}},                                   # lesson_count missing
    {"split": {"mode": "single", "lesson_count": 3}},                 # field of another mode
    {"split": {"mode": "syllabus", "syllabus": [{"title": "A", "pages": "1-10"}]}},  # no D-number
])
def test_invalid_options_rejected_by_both(options_schema, options):
    rejected_by_both(options, JobOptions, options_schema)


@pytest.mark.parametrize("options", [
    {"questions": {"types": ["choice", "choice"]}},
    {"split": {"mode": "syllabus"}},                                  # neither list nor text
    {"split": {"mode": "syllabus", "syllabus": [{"title": "A"}], "syllabus_text": "B"}},
    {"split": {"mode": "syllabus", "syllabus_text": "   "}},
    {"split": {"mode": "syllabus", "syllabus": [{"title": "A", "pages": "D1:10-2"}]}},
    {"split": {"mode": "syllabus", "syllabus": [{"title": "A", "pages": "D0:1"}]}},
])
def test_invalid_options_rejected_by_model(options):
    with pytest.raises(ValidationError):
        JobOptions.model_validate(options)


def test_resolve_options_fills_defaults_and_keeps_sent_values():
    sent = JobOptions.model_validate({"external_ref": "gen-1", "questions": {"per_section": 5}})
    complete = resolve_options(sent, OptionDefaults())
    assert complete.external_ref == "gen-1"
    assert complete.language == "auto"
    assert complete.extraction.ocr is False
    assert complete.questions.types == ["choice", "open", "no_answer"]
    assert complete.questions.per_section == 5
    assert complete.questions.choice_options == 4
    assert complete.split.mode == "single"


def test_resolve_options_rejects_bad_defaults():
    with pytest.raises(ValidationError):
        resolve_options(JobOptions(), OptionDefaults(per_section=0))


# --- job status ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["running", "failed"])
def test_job_status_examples_are_valid(name):
    JobStatus.model_validate(fixture("job_status_examples.json")[name])


def test_job_status_rejects_unknown_error_code_and_naive_time():
    data = fixture("job_status_examples.json")["failed"]
    data["error"]["code"] = "no_text_extracted"  # older draft name; the contract uses extraction_failed
    with pytest.raises(ValidationError):
        JobStatus.model_validate(data)
    data = fixture("job_status_examples.json")["running"]
    data["created_at"] = "2026-11-03T10:12:01"  # no time zone
    with pytest.raises(ValidationError):
        JobStatus.model_validate(data)
