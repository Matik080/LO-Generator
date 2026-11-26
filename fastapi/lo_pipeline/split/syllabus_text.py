"""
Split mode `syllabus` with `syllabus_text`: the teacher's free text -> list of lessons (one LLM call).

The LLM only converts the text; it does not place any LO. It gets the text, the uploaded file names
(for D<n> page ranges) and the topic names B found in the document, and returns per lesson a title
(the teacher's words and language), optional pages and a short `covers` text, plus a list of things the
teacher wants to leave out (`skip`). `covers` and `skip` are written in the DOCUMENT's language with its
topic terms, because the local embedding model (MiniLM) compares well only within one language (English).
The LOs are then placed by syllabus.split_by_syllabus (pages, otherwise MiniLM against `covers`, kept in
the lesson's `match` key; `description` stays empty, the lesson description is written later).

Anything unusable (LLM error, invalid JSON, no lesson with a title) gives None; the caller then
falls back to mode `topics`, so the split never makes the job fail.
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from lo_pipeline.contract.options import PAGES_PATTERN, page_ranges
from lo_pipeline.llm.json_load import parse_json
from lo_pipeline.llm.service import LLMError, call_llm

logger = logging.getLogger(__name__)

MAX_LESSONS = 100  # same limit as split.syllabus in the contract
_PAGES = re.compile(PAGES_PATTERN)


@dataclass
class SyllabusPlan:
    lessons: list[dict]  # {"title", "description" (None), "pages", "match" (= covers)}
    skip: list[str] = field(default_factory=list)


def _plain(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _prompt(text: str, files: Sequence[str], topics: Sequence[str]) -> list[dict]:
    file_list = "\n".join(f"D{n}: {name}" for n, name in enumerate(files, start=1))
    topic_list = ", ".join(topics) if topics else "(none found)"
    prompt = f"""
A teacher described how to split the study material into lessons. Convert the description into
a list of lessons. Do not add lessons the teacher did not ask for, and keep the teacher's order.

TEACHER'S DESCRIPTION:
\"\"\"{text}\"\"\"

UPLOADED DOCUMENTS (number: file name):
{file_list}

TOPICS FOUND IN THE DOCUMENTS:
{topic_list}

For each lesson return:
- "title": the lesson title in the teacher's words and language (if the teacher gives only a number
  of lessons, write short titles from the topics above, in the teacher's language),
- "pages": only if the teacher names pages or slides, in the form "D1:1-10" or "D1:1-10, D2:3"
  (D<number> from the document list; one document -> D1); otherwise null,
- "covers": one or two sentences on what the lesson covers: name its concepts, including the matching
  terms from the topic list.

Also return "skip": the things the teacher wants left out, each as a short phrase that names the
content and the matching terms from the topic list (e.g. "skip the tools" -> "software tools: python,
matlab"); [] if nothing.

IMPORTANT: write "covers" and "skip" in the SAME LANGUAGE as the topic list (the language of the
documents), even when the teacher writes in another language. Only "title" uses the teacher's language.

Return a JSON object:
{{"lessons": [{{"title": "...", "pages": null, "covers": "..."}}, ...], "skip": ["..."]}}

Return JSON object only. No markdown. No explanation.
"""
    return [
        {"role": "system",
         "content": "You convert a teacher's lesson plan into structured JSON. Return JSON object only."},
        {"role": "user", "content": prompt},
    ]


def _valid_pages(value, n_files: int) -> Optional[str]:
    """The LLM's pages if they are in the contract format and name existing documents and pages."""
    pages = _plain(value)
    if not pages or not _PAGES.match(pages):
        return None
    for doc, first, last in page_ranges(pages):
        if not 1 <= doc <= n_files or first < 1 or last < first:
            return None
    return pages


def plan_from_text(text: str, files: Sequence[str], topics: Sequence[str] = ()) -> Optional[SyllabusPlan]:
    """Teacher's syllabus_text -> SyllabusPlan, or None if the answer cannot be used."""
    try:
        response = call_llm(_prompt(text, files, topics), temperature=0.2)
    except LLMError as e:
        logger.warning(f"[Split] Syllabus plan: LLM failed ({e}); falling back.")
        return None
    if not response:
        return None
    try:
        result = parse_json(response)
    except ValueError:  # json.JSONDecodeError is a ValueError
        logger.warning("[Split] Syllabus plan: answer is not valid JSON; falling back.")
        return None
    if not isinstance(result, dict) or not isinstance(result.get("lessons"), list):
        logger.warning("[Split] Syllabus plan: answer has no lesson list; falling back.")
        return None
    lessons = []
    for item in result["lessons"][:MAX_LESSONS]:
        if not isinstance(item, dict) or not _plain(item.get("title")):
            continue
        lessons.append({"title": _plain(item.get("title")),
                        "description": None,
                        "pages": _valid_pages(item.get("pages"), len(files)),
                        "match": _plain(item.get("covers")) or None})
    if not lessons:
        logger.warning("[Split] Syllabus plan: no lesson with a title; falling back.")
        return None
    skip = result.get("skip") if isinstance(result.get("skip"), list) else []
    return SyllabusPlan(lessons=lessons, skip=list(dict.fromkeys(_plain(s) for s in skip if _plain(s))))
