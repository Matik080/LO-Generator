"""run_pipeline result -> the `units` format (GET /result) and the report (GET /report).

Sprint task 5. No LLM calls here: everything comes from the finished learning objects (LOs).

- One LO becomes one question: an LO with multiple-choice options -> `choice`, otherwise `open`.
- A no_answer task (questions/no_answer.py, from LOs that share a concept) becomes a `no_answer`
  question right after the LO of its group that comes last in the lessons, so the material comes first.
- Lessons come from the split step (result["split"], lo_pipeline/split): one unit per lesson, the
  LOs in the lesson's order; a lesson without any valid question is left out (warning `empty_lesson`).
  `topic` (concept-graph topic of the LO) is filled in `sections` and `question_meta`.
- Hints: 1st = the source, made by code ("Zdroj": file, page); 2nd = the LLM-written hint text
  without the "[Source: ...]" suffix. options.questions.hints says how many (0, 1 or 2).
- Times (seconds) are fitted into the range of the question type (TIME_RANGES, design kap. 7, the
  same ranges as Mlynček A); a missing or invalid estimate gets the type's default.
- Texts over the ECEH columns were already shortened once by the LLM (questions/shorten.py). What is
  still too long: a name or lesson text is cut at the end of a word, a hint is left out (warning
  `hint_too_long`), a question whose text or choice is too long is left out (warning `field_too_long`).
  A question that breaks other rules is left out (warning `question_skipped`).
- ECEH shows the question text, choices and hints as HTML (frontend: DOMPurify) and the name, lesson
  title and description as plain text. So: question text and choices are escaped (html_text; the
  student sees exactly "#include <stdio.h>", and no tag can run), hints are escaped into '<p>…</p>'
  and cleaned by nh3, plain-text fields have unsafe HTML removed (text_safety.clean_text).
- Report quality (0–1): question_faithfulness_avg = B's faithfulness, answerability_avg = B's
  question_clarity, topic_coverage = share of the document's topics with a question.
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
from lo_pipeline.questions.text_safety import clean_text, cut_at_word

logger = logging.getLogger(__name__)

SOURCE_HINT_NAME = "Zdroj"
TEXT_HINT_NAME = "Nápoveda"
# Texts written by code, in the output language (result["language"]["output"]; default sk)
TEXTS = {
    "sk": {"source": SOURCE_HINT_NAME, "hint": TEXT_HINT_NAME, "page": "strana", "pages": "strany",
           "lesson": "Hodina", "topics": "Témy", "generated": "Vygenerované otázky"},
    "en": {"source": "Source", "hint": "Hint", "page": "page", "pages": "pages",
           "lesson": "Lesson", "topics": "Topics", "generated": "Generated questions"},
}
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


class _TooLong(Exception):
    """A question text is still longer than its ECEH column after the LLM shortening (questions/shorten.py)."""


def _fit(text: str, limit: int, field: str) -> str:
    """Question text and choices are never cut: too long -> the question is left out (field_too_long)."""
    if len(text) > limit:
        raise _TooLong(f"{field}: {len(text)} > {limit} characters")
    return text


def _cut(text: str, limit: int, field: str) -> str:
    """Names and lesson texts are cut at the end of a word when still too long."""
    if len(text) > limit:
        logger.info(f"[Units] Cut {field} from {len(text)} to {limit} characters.")
    return cut_at_word(text, limit)


def _plain(value) -> str:
    """Text for a plain-text field (name, lesson title and description, report): no unsafe HTML
    (text_safety.clean_text). None and non-strings become ''."""
    return clean_text(value.strip()) if isinstance(value, str) else ""


def _raw(value) -> str:
    """LLM text as written (entities decoded), for the fields ECEH shows as HTML; see html_text."""
    return html.unescape(value.strip()) if isinstance(value, str) else ""


def html_text(value) -> str:
    """Text for a field ECEH shows as HTML (question text, choices): escaped, so the browser shows
    exactly the text ("#include <stdio.h>", a question about "<p>") and no tag can run. ECEH renders
    these fields through DOMPurify (frontend renderTextWithMath), which would drop "<stdio.h>" as a tag."""
    return html.escape(_raw(value), quote=False)


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


def hint_html(text: str, limit: int, warnings: _Warnings) -> Optional[str]:
    """Plain hint text -> '<p>…</p>' with only allowed tags, or None when it does not fit into `limit`.

    The text is escaped first ('<script>' is shown as text, never run); nh3 then guarantees that no
    tag outside ALLOWED_HINT_TAGS can get through. A hint still too long after the LLM shortening
    (questions/shorten.py) is left out; the question keeps its other hints (warning hint_too_long).
    """
    cleaned = _hint_markup(text)
    if len(cleaned) > limit:
        warnings.count("hint_too_long",
                       lambda n: f"{n} hint(s) were longer than the ECEH limit and were left out.")
        return None
    return cleaned


def hint_fits(text: str, limit: int = HINT_URL_MAX) -> bool:
    """Whether hint_html would keep this hint (questions/shorten.py uses it to find hints to shorten)."""
    return len(_hint_markup(text)) <= limit


def _hint_markup(text: str) -> str:
    text = text.replace(" ", " ")  # would become "&nbsp;" (6 characters) after cleaning
    return nh3.clean(f"<p>{html.escape(html.unescape(text), quote=False)}</p>", tags=ALLOWED_HINT_TAGS,
                     attributes={})


def source_hint_text(lo: dict, lang: str = "sk") -> str:
    """'modul1.pdf, strana 12' / 'modul1.pdf, page 12' (or just the file name when the page is unknown)."""
    file = _plain(lo.get("source_title"))
    page = lo.get("source_page")
    if file and isinstance(page, int) and not isinstance(page, bool) and page >= 1:
        return f"{file}, {TEXTS[lang]['page']} {page}"
    return file


def _page(lo: dict):
    page = lo.get("source_page")
    return page if isinstance(page, int) and not isinstance(page, bool) and page >= 1 else None


def group_sources(los: list[dict]) -> list[dict]:
    """[{"file", "pages"}] of several LOs: files in order of appearance, pages sorted, no repeats."""
    pages_by_file: dict[str, list[int]] = {}
    for lo in los:
        file = _plain(lo.get("source_title"))
        if not file:
            continue
        pages = pages_by_file.setdefault(file, [])
        if _page(lo) is not None and _page(lo) not in pages:
            pages.append(_page(lo))
    return [{"file": file, "pages": sorted(pages)} for file, pages in pages_by_file.items()]


def group_source_hint_text(sources: list[dict], lang: str = "sk") -> str:
    """'c.pdf, strana 3', 'c.pdf, strany 3, 5' (en: page / pages) or several files separated by '; '."""
    parts = []
    for source in sources:
        pages = source["pages"]
        if not pages:
            parts.append(source["file"])
        elif len(pages) == 1:
            parts.append(f"{source['file']}, {TEXTS[lang]['page']} {pages[0]}")
        else:
            parts.append(f"{source['file']}, {TEXTS[lang]['pages']} {', '.join(str(p) for p in pages)}")
    return "; ".join(parts)


def _hints(source: str, text: str, options: JobOptions, warnings: _Warnings, lang: str = "sk") -> list[dict]:
    """1st hint = the source, 2nd = the LLM hint text (options.questions.hints says how many);
    a hint that does not fit is left out (hint_html)."""
    wanted = []
    if options.questions.hints >= 1 and source:
        wanted.append((TEXTS[lang]["source"], source))
    if options.questions.hints >= 2 and text:
        wanted.append((TEXTS[lang]["hint"], text))
    hints = []
    for name, value in wanted:
        url = hint_html(value, HINT_URL_MAX, warnings)
        if url is not None:
            hints.append({"name": name, "url": url})
    return hints


def task_to_question(task: dict, group: list[dict], options: JobOptions, warnings: _Warnings,
                     lang: str = "sk") -> Optional[dict]:
    """A no_answer task + the LOs of its group -> one question of type no_answer, or None.

    Raises _TooLong when the task text does not fit into its ECEH column.
    """
    description, name = html_text(task.get("description")), _plain(task.get("name"))
    if not description or not name:
        return None
    hints = _hints(group_source_hint_text(group_sources(group), lang), _raw(task.get("hint")), options, warnings,
                   lang)
    time_to_handle, time_to_explain = question_times("no_answer", task.get("time_to_handle"),
                                                     task.get("time_to_explain"))
    return {
        "name": _cut(name, QUESTION_NAME_MAX, "name"),
        "description": _fit(description, QUESTION_DESCRIPTION_MAX, "description"),
        "type": "no_answer",
        "time_to_handle": time_to_handle,
        "time_to_explain": time_to_explain,
        "hints": hints,
        "choices": [],
    }


def _index_list(value, n_los: int) -> Optional[list[int]]:
    """A list of valid LO indices, or None."""
    if not isinstance(value, list) or not all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n_los
                                              for i in value):
        return None
    return value


def _lessons(result: dict, los: list[dict], lang: str = "sk") -> tuple[list[dict], list]:
    """(lessons [{"lo_indices", "title", "description"}], topic name per LO) from the split step.

    A result without a split (older checkpoint) or with broken lessons gives one lesson with all LOs
    in pipeline order and the title from the source files. An LO is used in one lesson only.
    """
    split = result.get("split") or {}
    lessons, used = [], set()
    for lesson in split.get("lessons") or []:
        indices = _index_list(lesson.get("lo_indices") if isinstance(lesson, dict) else None, len(los))
        if indices is None:
            lessons = []
            break
        indices = [i for i in dict.fromkeys(indices) if i not in used]
        used.update(indices)
        lessons.append({"lo_indices": indices, "title": lesson.get("title"), "description": lesson.get("description")})
    if not lessons:
        title, description = _unit_title_and_description(los, lang)
        lessons = [{"lo_indices": list(range(len(los))), "title": title, "description": description}]
    lo_topics = split.get("lo_topics") if isinstance(split.get("lo_topics"), list) else []
    return lessons, lo_topics


def _tasks_after_lo(result: dict, lessons: list[dict], n_los: int, options: JobOptions) -> dict[int, list[dict]]:
    """No_answer tasks by the LO they follow: the group's LO that comes last in the lessons (only when
    no_answer is requested). A task with invalid LO indices, or none of whose LOs is in a lesson
    (left out by the syllabus), is ignored."""
    if "no_answer" not in options.questions.types:
        return {}
    position = {i: n for n, i in enumerate(i for lesson in lessons for i in lesson["lo_indices"])}
    after: dict[int, list[dict]] = {}
    for task in result.get("no_answer_tasks") or []:
        indices = _index_list(task.get("lo_indices") if isinstance(task, dict) else None, n_los)
        placed = [i for i in indices or [] if i in position]
        if not placed:
            continue
        after.setdefault(max(placed, key=position.get), []).append(task)
    return after


def llm_hint_text(lo: dict) -> str:
    """The LO hint without the '[Source: …]' suffix (the source is in its own hint and in the report)."""
    return _SOURCE_SUFFIX.sub("", _raw(lo.get("hint"))).strip()  # raw: hint_html escapes it


def _correct_indices(lo: dict, n_options: int) -> list[int]:
    """Indices of the correct options: `mc_correct_indices` (lo/generator.py, two correct options) when valid,
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
    texts = [html_text(o) for o in options]  # ECEH shows choices as HTML
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
    return [{"text": _fit(texts[i], CHOICE_TEXT_MAX, "choice"), "is_correct": i in corrects} for i in keep]


def lo_to_question(lo: dict, options: JobOptions, warnings: _Warnings, lang: str = "sk") -> Optional[dict]:
    """One LO -> one question in the `units` format, or None when it cannot be one.

    Raises _TooLong when the question text or a choice does not fit into its ECEH column.
    """
    q_opts = options.questions
    types = q_opts.types
    description = html_text(lo.get("question"))  # ECEH shows the question text as HTML
    if not description:
        return None

    choices = _choices(lo, q_opts.choice_options, warnings) if "choice" in types else None
    if choices is not None:
        q_type = "choice"
    elif "open" in types and _raw(lo.get("answer")):
        q_type = "open"
        choices = [{"text": _fit(html_text(lo["answer"]), CHOICE_TEXT_MAX, "answer"), "is_correct": True}]
    else:
        return None  # e.g. only "choice" requested and this LO has no MC options

    hints = _hints(source_hint_text(lo, lang), llm_hint_text(lo), options, warnings, lang)
    name = _plain(lo.get("generated_name")) or _plain(lo.get("question"))  # no name from the LLM: the question
    time_to_handle, time_to_explain = question_times(q_type, lo.get("time_to_handle"), lo.get("time_to_explain"))
    return {
        "name": _cut(name, QUESTION_NAME_MAX, "name"),
        "description": _fit(description, QUESTION_DESCRIPTION_MAX, "description"),
        "type": q_type,
        "time_to_handle": time_to_handle,
        "time_to_explain": time_to_explain,
        "hints": hints,
        "choices": choices,
    }


def _unit_title_and_description(los: list[dict], lang: str = "sk") -> tuple[str, str]:
    """Lesson title and description for split mode 'single', from the source file names (no LLM)."""
    files = list(dict.fromkeys(_plain(lo.get("source_title")) for lo in los if _plain(lo.get("source_title"))))
    title = ", ".join(files) or TEXTS[lang]["generated"]
    names = [_plain(lo.get("generated_name")) for lo in los if _plain(lo.get("generated_name"))]
    description = f"{TEXTS[lang]['topics']}: " + "; ".join(names) if names else ""
    return title, description


def _to_01(score) -> Optional[float]:
    """1–5 score -> 0–1 ((x − 1) / 4); None stays None."""
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return None
    return round(min(1.0, max(0.0, (score - 1) / 4)), 3)


def output_texts_language(result: dict, options: JobOptions) -> str:
    """Language of the texts written by code: the pipeline's output language (language stage), else the
    requested one, else Slovak (results from before the language stage)."""
    lang = (result.get("language") or {}).get("output")
    if lang in TEXTS:
        return lang
    return options.language if options.language in TEXTS else "sk"


def topic_coverage(split: dict, asked: set[int]) -> Optional[float]:
    """Share of the document's topics (concept-graph communities with >= 2 concepts, split["document_topics"])
    that have at least one question in the output; None when the split did not report topics."""
    lo_topic_ids, document_topics = split.get("lo_topic_ids"), split.get("document_topics")
    if not isinstance(lo_topic_ids, list) or not isinstance(document_topics, list) or not document_topics:
        return None
    topics = set(document_topics)
    covered = {lo_topic_ids[i] for i in asked if i < len(lo_topic_ids) and lo_topic_ids[i] in topics}
    return round(len(covered) / len(topics), 3)


def build_units_and_report(result: dict, options: JobOptions) -> tuple[dict, dict]:
    """`run_pipeline` result + complete job options (resolve_options) -> (units, report) as dicts.

    Raises InvalidOutputError when the output does not pass the schemas.
    """
    los = result.get("learning_objects") or []
    lang = output_texts_language(result, options)
    warnings = _Warnings()
    # pipeline warnings first ("details" is for logs only; the report has code + message)
    for w in result.get("warnings") or []:
        warnings.add(w["code"], w["message"])

    if options.questions.per_section > 1:
        warnings.add("per_section_unsupported",
                     f"B generates one question per learning object (requested {options.questions.per_section}).")
    lessons, lo_topics = _lessons(result, los, lang)
    for w in (result.get("split") or {}).get("warnings") or []:
        warnings.add(w["code"], w["message"])

    def topic(i: int) -> Optional[str]:
        return _plain(lo_topics[i]) or None if i < len(lo_topics) else None

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
            "topic": topic(i),
        })

    def valid(question: Optional[dict], what: str) -> bool:
        if question is None:
            return False
        try:
            Question.model_validate(question)
            return True
        except ValidationError as e:
            logger.info(f"[Units] {what} left out: {e.errors()[0]['msg']}")
            return False

    def too_long(e: _TooLong, what: str) -> None:
        logger.info(f"[Units] {what} left out, text too long ({e}).")
        warnings.count("field_too_long",
                       lambda n: f"{n} question(s) were left out because a text was longer than the ECEH limit.")

    tasks_after = _tasks_after_lo(result, lessons, len(los), options)
    unit_list, question_meta, asked = [], [], set()  # asked = LOs with a question in the output
    for lesson in lessons:  # one unit per lesson; LOs in the lesson's order
        questions, meta, lesson_asked = [], [], set()

        def add(question: dict, i: int, keywords: list, source: list) -> None:
            meta.append({"question": len(questions), "section_id": section_id(i), "bloom_level": None,
                         "keywords": [k for k in (_plain(k) for k in keywords) if k], "source": source,
                         "topic": topic(i)})
            questions.append(question)
            lesson_asked.add(i)

        for i in lesson["lo_indices"]:
            lo = los[i]
            try:
                question = lo_to_question(lo, options, warnings, lang)
            except _TooLong as e:
                too_long(e, f"LO {section_id(i)}")
            else:
                if valid(question, f"LO {section_id(i)}"):
                    file = _plain(lo.get("source_title"))
                    pages = [_page(lo)] if _page(lo) is not None else []
                    add(question, i, [k for k in lo.get("keywords") or [] if isinstance(k, str)],
                        [{"file": file, "pages": pages}] if file else [])
                else:
                    warnings.count("question_skipped",
                                   lambda n: f"{n} learning object(s) gave no valid question of the requested types.")
            for task in tasks_after.get(i, []):  # after the last LO of the task's group
                group = [los[j] for j in task["lo_indices"]]
                try:
                    question = task_to_question(task, group, options, warnings, lang)
                except _TooLong as e:
                    too_long(e, f"no_answer task after {section_id(i)}")
                    continue
                if valid(question, f"no_answer task after {section_id(i)}"):
                    anchor = _plain(task.get("anchor"))
                    add(question, i, [anchor] if anchor else [], group_sources(group))
                else:
                    warnings.count("no_answer_skipped",
                                   lambda n: f"{n} no_answer task(s) were not valid and were left out.")

        if not questions and len(lessons) > 1:  # (one lesson only: kept, the result needs a unit)
            warnings.add("empty_lesson", f"Lesson '{_plain(lesson['title'])}' has no valid question; it is left out.")
            continue
        asked |= lesson_asked
        for m in meta:
            question_meta.append({"unit": len(unit_list), **m})
        unit_list.append({
            "title": _cut(_plain(lesson["title"]) or TEXTS[lang]["lesson"], UNIT_TITLE_MAX, "unit title"),
            "description": _cut(_plain(lesson["description"]), UNIT_DESCRIPTION_MAX, "unit description"),
            "questions": questions,
        })
    if not unit_list:  # every lesson was empty: one empty unit, as with a single lesson
        title, description = _unit_title_and_description(los, lang)
        unit_list.append({"title": _cut(title, UNIT_TITLE_MAX, "unit title"),
                          "description": _cut(description, UNIT_DESCRIPTION_MAX, "unit description"),
                          "questions": []})
    units = {"units": unit_list}

    all_models = (((result.get("quality") or {}).get("final") or {}).get("scores") or {}).get("all_models") or {}
    report = {
        "sections": sections,
        "question_meta": question_meta,
        "quality": {
            "section_faithfulness_avg": None,  # B's sections are the LOs: their faithfulness is the next one
            "question_faithfulness_avg": _to_01(all_models.get("faithfulness")),
            "answerability_avg": _to_01(all_models.get("question_clarity")),  # B's clarity/answerability score
            "topic_coverage": topic_coverage(result.get("split") or {}, asked),
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
