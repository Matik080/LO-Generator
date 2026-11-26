"""
Lesson titles and descriptions (all split modes, also `single`), written by the LLM from the names of
the lesson's LOs. Several lessons go into one call (`batch_size`).

Per lesson the caller says what is already fixed:
- `title` / `description`: kept as given (teacher's syllabus); the LLM writes only what is missing,
  and a lesson with both is not sent at all,
- `hint`: what the lesson is about (topic name in mode `topics`), given to the LLM.

If the LLM fails for a lesson (error, invalid JSON, missing or empty text), a fallback is used:
title = hint or the lesson's most frequent topic or the first LO name; description = the LO names.
The number of lessons with a fallback is returned (report warning `lesson_titles_failed`).
"""
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Optional, Sequence

from lo_pipeline.llm.json_load import parse_json
from lo_pipeline.llm.service import LLMError, call_llm

logger = logging.getLogger(__name__)

MAX_LO_NAMES = 30  # LO names per lesson in the prompt (longer lessons: the first ones)


@dataclass
class LessonSpec:
    lo_indices: list[int]
    title: Optional[str] = None  # fixed title (teacher)
    description: Optional[str] = None  # fixed description (teacher)
    hint: Optional[str] = None  # e.g. topic name


def _plain(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _lo_name(lo: dict) -> str:
    return _plain(lo.get("generated_name")) or _plain(lo.get("question"))


def _prompt(batch: Sequence[tuple[int, LessonSpec]], los: list[dict], language: Optional[str]) -> list[dict]:
    lessons_text = ""
    for n, (_, spec) in enumerate(batch, start=1):
        names = [_lo_name(los[i]) for i in spec.lo_indices[:MAX_LO_NAMES]]
        lines = [f"LESSON {n}:"]
        if spec.title:
            lines.append(f"Title (given, keep it): {spec.title}")
        if spec.hint:
            lines.append(f"Main topic: {spec.hint}")
        lines.append("Learning objects:")
        lines += [f"  - {name}" for name in names if name]
        lessons_text += "\n".join(lines) + "\n\n"
    language_rule = (f"Write in this language: {language}." if language else
                     "Write in the SAME LANGUAGE as the learning objects (or as the given title, if there is one).")
    prompt = f"""
You are naming lessons of a course. For each lesson below write:
- "title": a short lesson title (2-8 words) that covers its learning objects; if the lesson has a given
  title, return exactly that title,
- "description": one or two sentences for the teacher on what the students learn in the lesson.

{language_rule}
Use only what the learning objects say; do not add other content.

{lessons_text}Return a JSON array with exactly {len(batch)} results in the same order:
[
  {{"lesson": 1, "title": "...", "description": "..."}},
  ...
]

Return JSON array only. No markdown. No explanation.
"""
    return [
        {"role": "system", "content": "You write short lesson titles and descriptions. Return JSON array only."},
        {"role": "user", "content": prompt},
    ]


def fallback_text(spec: LessonSpec, los: list[dict], topic_names: Sequence[Optional[str]] = ()) -> tuple[str, str]:
    """(title, description) without the LLM."""
    names = [n for n in (_lo_name(los[i]) for i in spec.lo_indices) if n]
    topics = Counter(topic_names[i] for i in spec.lo_indices if i < len(topic_names) and topic_names[i])
    title = spec.title or spec.hint or (topics.most_common(1)[0][0] if topics else "") or (names[0] if names else "")
    description = spec.description or "; ".join(names)
    return title, description


def lesson_titles(los: list[dict], specs: Sequence[LessonSpec], topic_names: Sequence[Optional[str]] = (),
                  language: Optional[str] = None, batch_size: int = 5, progress=None,
                  ) -> tuple[list[tuple[str, str]], int]:
    """Returns ([(title, description)] per lesson, number of lessons that got a fallback text).

    topic_names: topic name per LO (for the fallback title). `progress(done, total)` after each batch.
    """
    from lo_pipeline.llm.parallel import chunks, map_batches

    texts: list[Optional[tuple[str, str]]] = [None] * len(specs)
    todo = []
    for k, spec in enumerate(specs):
        if spec.title and spec.description:
            texts[k] = (spec.title, spec.description)
        else:
            todo.append((k, spec))

    def one_batch(batch):
        try:
            response = call_llm(_prompt(batch, los, language), temperature=0.3)
            results = parse_json(response) if response else None
        except LLMError as e:
            logger.warning(f"[Split] Lesson titles: LLM failed ({e}); fallback titles used.")
            return {}
        except ValueError:  # json.JSONDecodeError is a ValueError
            logger.warning("[Split] Lesson titles: answer is not valid JSON; fallback titles used.")
            return {}
        found = {}
        for result in results if isinstance(results, list) else []:
            n = result.get("lesson") if isinstance(result, dict) else None
            if isinstance(n, int) and 1 <= n <= len(batch) and n not in found:
                title, description = _plain(result.get("title")), _plain(result.get("description"))
                if title and description:
                    found[n] = (title, description)
        return {batch[n - 1][0]: text for n, text in found.items()}

    for answers in map_batches(one_batch, chunks(todo, batch_size), progress=progress):
        for k, (title, description) in answers.items():
            spec = specs[k]
            texts[k] = (spec.title or title, spec.description or description)  # given texts always win

    failed = 0
    for k, spec in enumerate(specs):
        if texts[k] is None:
            texts[k] = fallback_text(spec, los, topic_names)
            failed += 1
    return texts, failed
