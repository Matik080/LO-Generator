"""run_pipeline result -> the `units` format (GET /result) and the report (GET /report).

Sprint task 5. No LLM calls here: everything comes from the finished learning objects (LOs).

- One LO becomes one question: an LO with multiple-choice options -> `choice`, otherwise `open`.
- split: only "single" (one lesson) is implemented; other modes give one lesson + a warning.
- Hints: 1st = the source, made by code ("Zdroj": file, page); 2nd = the LLM-written hint text
  without the "[Source: ...]" suffix. options.questions.hints says how many (0, 1 or 2).
- Times (seconds) are fitted into the range of the question type (TIME_RANGES, design kap. 7, the
  same ranges as Mlynček A); a missing or invalid estimate gets the type's default.
- Texts longer than the ECEH columns are shortened (warning `field_too_long`); a question that
  still breaks the rules is left out (warning `question_skipped`).
- The finished output is checked against the models and the JSON Schema files; if it does not
  pass, InvalidOutputError is raised (the job then fails, it never returns invalid data).
"""

import html
import logging
import re
from typing import Optional

import jsonschema
import nh3
from pydantic import ValidationError

from lo_pipeline.contract.options import JobOptions
from lo_pipeline.contract.report import Report, section_id
from lo_pipeline.contract.schemas import load_schema
from lo_pipeline.contract.units import (
    CHOICE_TEXT_MAX, HINT_URL_MAX, QUESTION_DESCRIPTION_MAX, QUESTION_NAME_MAX,
    UNIT_DESCRIPTION_MAX, UNIT_TITLE_MAX, Question, UnitsResult,
)

logger = logging.getLogger(__name__)

SOURCE_HINT_NAME = "Zdroj"
TEXT_HINT_NAME = "Nápoveda"
# question type -> (time_to_handle min, max, default), (time_to_explain min, max, default) in seconds
TIME_RANGES = {
    "choice": ((30, 180, 120), (0, 90, 30)),
    "open": ((120, 900, 300), (0, 180, 0)),
    "no_answer": ((0, 1800, 300), (0, 600, 0)),
}

# Tags allowed in hints[].url (ECEH shows it as HTML). Everything else is removed by nh3.
ALLOWED_HINT_TAGS = {"p", "br", "strong", "em", "code"}

# "... [Source: file.pdf, p. 3]" at the end of an LO hint (added by lo/postprocess.py)
_SOURCE_SUFFIX = re.compile(r"\s*\[Source:[^\]]*\]\s*$")


class InvalidOutputError(Exception):
    """The mapped output does not pass the units/report schema (a bug, not a user error)."""


class _Warnings:
    """Collects report warnings; a code that repeats is reported once with a count."""

    def __init__(self):
        self.items: list[dict] = []
        self._counted: dict[str, dict] = {}

    def add(self, code: str, message: str):
        self.items.append({"code": code, "message": message})

    def count(self, code: str, message_for_count):
        """One warning per code; `message_for_count(n)` writes its text from the number of cases."""
        if code not in self._counted:
            self._counted[code] = {"code": code, "n": 0, "text": message_for_count}
            self.items.append(self._counted[code])
        self._counted[code]["n"] += 1

    def as_list(self) -> list[dict]:
        out = []
        for item in self.items:
            if "n" in item:
                out.append({"code": item["code"], "message": item["text"](item["n"])})
            else:
                out.append(item)
        return out


def _warn_too_long(warnings: _Warnings):
    warnings.count("field_too_long",
                   lambda n: f"{n} text(s) were longer than the ECEH limit and were shortened.")


def _shorten(text: str, limit: int, field: str, warnings: _Warnings) -> str:
    if len(text) <= limit:
        return text
    _warn_too_long(warnings)
    logger.info(f"[Units] Shortened {field} from {len(text)} to {limit} characters.")
    return text[:limit - 1].rstrip() + "…"


def _plain(value) -> str:
    """LLM text as one clean string (None and non-strings become '')."""
    return value.strip() if isinstance(value, str) else ""


def _seconds(value, default: int) -> int:
    """LLM time estimate as a whole number of seconds >= 0; default when it is not a number."""
    if isinstance(value, bool):
        return default
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return default
    if isinstance(value, (int, float)) and value >= 0:
        return int(round(value))
    return default


def question_times(q_type: str, time_to_handle, time_to_explain) -> tuple[int, int]:
    """LLM time estimates -> seconds within the range of the question type (default when missing)."""
    times = []
    for value, (low, high, default) in zip((time_to_handle, time_to_explain), TIME_RANGES[q_type]):
        times.append(min(max(_seconds(value, default), low), high))
    return times[0], times[1]


def hint_html(text: str, limit: int, warnings: _Warnings) -> str:
    """Plain hint text -> '<p>…</p>' that fits into `limit` characters and has only allowed tags.

    The LLM text is plain text, so it is escaped first ('<script>' is shown as text, never run);
    nh3 then guarantees that no tag outside ALLOWED_HINT_TAGS can get through.
    """
    text = text.replace(" ", " ")  # would become "&nbsp;" (6 characters) after cleaning
    budget = limit - len("<p></p>")
    escaped = html.escape(text, quote=False)
    if len(escaped) > budget:
        # escaping makes text longer ('<' -> '&lt;'): add escaped characters while they fit
        _warn_too_long(warnings)
        parts, used = [], len("…")
        for char in text:
            part = html.escape(char, quote=False)
            if used + len(part) > budget:
                break
            parts.append(part)
            used += len(part)
        escaped = "".join(parts).rstrip() + "…"
    return nh3.clean(f"<p>{escaped}</p>", tags=ALLOWED_HINT_TAGS, attributes={})


def source_hint_text(lo: dict) -> str:
    """'modul1.pdf, strana 12' (or just the file name when the page is unknown)."""
    file = _plain(lo.get("source_title"))
    page = lo.get("source_page")
    if file and isinstance(page, int) and not isinstance(page, bool) and page >= 1:
        return f"{file}, strana {page}"
    return file


def llm_hint_text(lo: dict) -> str:
    """The LO hint without the '[Source: …]' suffix (the source is in its own hint and in the report)."""
    return _SOURCE_SUFFIX.sub("", _plain(lo.get("hint"))).strip()


def _correct_indices(lo: dict, n_options: int) -> list[int]:
    """Indices of the correct options: `mc_correct_indices` (questions/multi_correct.py) when valid,
    otherwise the one `mc_correct_index`."""
    correct = lo.get("mc_correct_index")
    many = lo.get("mc_correct_indices")
    if (isinstance(many, list) and correct in many
            and all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n_options for i in many)):
        return sorted(set(many))
    return [correct]


def _choices(lo: dict, choice_options: int, warnings: _Warnings) -> Optional[list[dict]]:
    """MC options as choices: all correct ones plus distractors up to choice_options in total
    (at least one distractor when the LO has one).

    None when the LO has no usable MC options.
    """
    options = lo.get("mc_options")
    correct = lo.get("mc_correct_index")
    if (not isinstance(options, list) or not isinstance(correct, int) or isinstance(correct, bool)
            or not 0 <= correct < len(options)):
        return None
    texts = [_plain(o) for o in options]
    corrects = [i for i in _correct_indices(lo, len(options)) if texts[i]]
    if correct not in corrects:
        return None
    distractors = [i for i, t in enumerate(texts) if i not in corrects and t]
    if len(corrects) + len(distractors) < choice_options:
        warnings.count("choice_options_unavailable",
                       lambda n: f"{n} choice question(s) have fewer options than requested "
                                 f"(B generates up to 4).")
    n_distractors = max(choice_options - len(corrects), 1 if distractors else 0)
    keep = sorted(corrects + distractors[:n_distractors])  # keep the generated order
    return [{"text": _shorten(texts[i], CHOICE_TEXT_MAX, "choice", warnings), "is_correct": i in corrects}
            for i in keep]


def lo_to_question(lo: dict, options: JobOptions, warnings: _Warnings) -> Optional[dict]:
    """One LO -> one question in the `units` format, or None when it cannot be one."""
    q_opts = options.questions
    types = q_opts.types
    description = _plain(lo.get("question"))
    if not description:
        return None

    choices = _choices(lo, q_opts.choice_options, warnings) if "choice" in types else None
    if choices is not None:
        q_type = "choice"
    elif "open" in types and _plain(lo.get("answer")):
        q_type = "open"
        choices = [{"text": _shorten(_plain(lo["answer"]), CHOICE_TEXT_MAX, "answer", warnings),
                    "is_correct": True}]
    else:
        return None  # e.g. only "choice" requested and this LO has no MC options

    hints = []
    if q_opts.hints >= 1 and source_hint_text(lo):
        hints.append({"name": SOURCE_HINT_NAME, "url": hint_html(source_hint_text(lo), HINT_URL_MAX, warnings)})
    if q_opts.hints >= 2 and llm_hint_text(lo):
        hints.append({"name": TEXT_HINT_NAME, "url": hint_html(llm_hint_text(lo), HINT_URL_MAX, warnings)})

    name = _plain(lo.get("generated_name")) or description  # no name from the LLM: use the question
    time_to_handle, time_to_explain = question_times(q_type, lo.get("time_to_handle"), lo.get("time_to_explain"))
    return {
        "name": _shorten(name, QUESTION_NAME_MAX, "name", warnings),
        "description": _shorten(description, QUESTION_DESCRIPTION_MAX, "description", warnings),
        "type": q_type,
        "time_to_handle": time_to_handle,
        "time_to_explain": time_to_explain,
        "hints": hints,
        "choices": choices,
    }


def _unit_title_and_description(los: list[dict]) -> tuple[str, str]:
    """Lesson title and description for split mode 'single', from the source file names (no LLM)."""
    files = list(dict.fromkeys(_plain(lo.get("source_title")) for lo in los if _plain(lo.get("source_title"))))
    title = ", ".join(files) or "Vygenerované otázky"
    names = [_plain(lo.get("generated_name")) for lo in los if _plain(lo.get("generated_name"))]
    description = "Témy: " + "; ".join(names) if names else ""
    return title, description


def _to_01(score) -> Optional[float]:
    """1–5 score -> 0–1 ((x − 1) / 4); None stays None."""
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return None
    return round(min(1.0, max(0.0, (score - 1) / 4)), 3)


def build_units_and_report(result: dict, options: JobOptions) -> tuple[dict, dict]:
    """`run_pipeline` result + complete job options (resolve_options) -> (units, report) as dicts.

    Raises InvalidOutputError when the output does not pass the schemas.
    """
    los = result.get("learning_objects") or []
    warnings = _Warnings()
    # pipeline warnings first ("details" is for logs only; the report has code + message)
    for w in result.get("warnings") or []:
        warnings.add(w["code"], w["message"])

    if options.split.mode != "single":
        warnings.add("split_mode_unsupported",
                     f"Split mode '{options.split.mode}' is not supported yet; all questions are in one lesson.")
    if "no_answer" in options.questions.types:
        warnings.add("question_type_unsupported",
                     "Question type 'no_answer' is not generated yet; only 'choice' and 'open' questions.")
    if options.questions.per_section > 1:
        warnings.add("per_section_unsupported",
                     f"B generates one question per learning object (requested {options.questions.per_section}).")

    # sections = LOs, prerequisites by LO id
    id_to_section = {}
    for i, lo in enumerate(los):
        id_to_section.setdefault(lo.get("id"), section_id(i))
    sections = []
    for i, lo in enumerate(los):
        prereqs = [id_to_section[p] for p in lo.get("prerequisite_ids") or [] if p in id_to_section]
        sections.append({
            "id": section_id(i),
            "title": _plain(lo.get("generated_name")),
            "statement": _plain(lo.get("source_statement")),
            "lo_type": _plain(lo.get("type")) or "unknown",
            "prerequisites": list(dict.fromkeys(p for p in prereqs if p != section_id(i))),
            "topic": None,
        })

    questions, question_meta = [], []
    for i, lo in enumerate(los):
        question = lo_to_question(lo, options, warnings)
        if question is not None:
            try:
                Question.model_validate(question)
            except ValidationError as e:
                logger.info(f"[Units] LO {section_id(i)} left out: {e.errors()[0]['msg']}")
                question = None
        if question is None:
            warnings.count("question_skipped",
                           lambda n: f"{n} learning object(s) gave no valid question of the requested types.")
            continue
        file = _plain(lo.get("source_title"))
        page = lo.get("source_page")
        pages = [page] if isinstance(page, int) and not isinstance(page, bool) and page >= 1 else []
        question_meta.append({
            "unit": 0,
            "question": len(questions),
            "section_id": section_id(i),
            "bloom_level": None,
            "keywords": [k for k in lo.get("keywords") or [] if isinstance(k, str)],
            "source": [{"file": file, "pages": pages}] if file else [],
            "topic": None,
        })
        questions.append(question)

    title, description = _unit_title_and_description(los)
    units = {"units": [{
        "title": _shorten(title, UNIT_TITLE_MAX, "unit title", warnings),
        "description": _shorten(description, UNIT_DESCRIPTION_MAX, "unit description", warnings),
        "questions": questions,
    }]}

    final_scores = ((result.get("quality") or {}).get("final") or {}).get("scores") or {}
    report = {
        "sections": sections,
        "question_meta": question_meta,
        "quality": {
            "section_faithfulness_avg": None,
            "question_faithfulness_avg": _to_01((final_scores.get("all_models") or {}).get("faithfulness")),
            "answerability_avg": None,
            "topic_coverage": None,
        },
        "warnings": warnings.as_list(),
        "timings": {k: float(v) for k, v in (result.get("timings") or {}).items()},
    }

    check_output(units, report)
    return units, report


def check_output(units: dict, report: dict) -> None:
    """Both outputs must pass the Pydantic models and the JSON Schema files given to Laravel."""
    try:
        UnitsResult.model_validate(units)
        Report.model_validate(report)
        jsonschema.validate(units, load_schema("lesson"))
        jsonschema.validate(report, load_schema("report"))
    except (ValidationError, jsonschema.ValidationError) as e:
        raise InvalidOutputError(f"output does not pass the schema: {e}") from e
