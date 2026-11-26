"""
The split step: LOs -> lessons for the job's `split` options (modes single / lessons / topics / syllabus).

`split_lessons` runs the chosen mode, names the lessons (titles.py) and returns plain JSON data:
  {"lessons": [{"lo_indices": [...], "title": str, "description": str}, ...],   # in lesson order
   "lo_topics": [topic name or None per LO],                                      # `topic` in the report
   "lo_topic_ids": [topic number or None per LO],                                 # for topic_coverage
   "document_topics": [topic numbers with >= 2 concepts],                         # for topic_coverage
   "warnings": [{"code", "message"}, ...]}                                        # Mlynček A's codes

It does not fail the job: a syllabus that cannot be used falls back to `topics` (syllabus_plan_invalid /
syllabus_fallback), and a bug in any mode (BUG_ERRORS) falls back to one lesson (split_failed). A job
timeout or crash is not caught.
"""
import logging
from collections import Counter
from typing import Callable, Optional, Sequence

from lo_pipeline.split.lessons import split_contiguous
from lo_pipeline.split.order import lo_order
from lo_pipeline.split.syllabus import split_by_syllabus
from lo_pipeline.split.syllabus_text import plan_from_text
from lo_pipeline.split.titles import LessonSpec, lesson_titles
from lo_pipeline.split.topics import (
    assign_missing_topics, concept_communities, lo_topics, split_by_topics, topic_names,
)

logger = logging.getLogger(__name__)

UNBALANCED = 0.5  # lessons_unbalanced: a lesson more than 50 % longer or shorter than the average (as A)
DEFAULT_SECONDS = (120, 30)  # LO without time estimates
# Errors a bug in the split code typically raises; these give one lesson instead of a failed job.
# Not Exception as a whole: RQ's job timeout (and a simulated crash in tests) must still stop the job.
BUG_ERRORS = (KeyError, TypeError, IndexError, ValueError, AttributeError, ZeroDivisionError)


def _seconds(value, default: int) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return default


def lo_seconds(lo: dict) -> int:
    """Length of an LO's question: time_to_handle + time_to_explain (defaults when missing)."""
    return _seconds(lo.get("time_to_handle"), DEFAULT_SECONDS[0]) + _seconds(lo.get("time_to_explain"), DEFAULT_SECONDS[1])


class _Warnings(list):
    def add(self, code: str, message: str):
        self.append({"code": code, "message": message})


def _unbalanced(lessons: list[list[int]], weights: Sequence[float]) -> bool:
    if len(lessons) < 2:
        return False
    sizes = [sum(weights[i] for i in lesson) for lesson in lessons]
    average = sum(sizes) / len(sizes)
    return average > 0 and any(abs(s - average) > UNBALANCED * average for s in sizes)


def split_lessons(los: list[dict], concept_graph: dict, split: dict, files: Sequence[str],
                  language: Optional[str] = None, embed: Optional[Callable] = None,
                  progress=None, title_batch_size: int = 5) -> dict:
    """split: the job's split options as a dict (mode, lesson_count, lesson_minutes, syllabus, syllabus_text).
    files: uploaded file names in upload order (D1 = files[0]).
    """
    warnings = _Warnings()
    if not los:
        return {"lessons": [], "lo_topics": [], "lo_topic_ids": [], "document_topics": [], "warnings": []}

    # topics are needed in every mode (report `topic`, cut preference, topics mode, plan prompt)
    communities = concept_communities(concept_graph or {})
    own_topics = lo_topics(los, communities)
    names = topic_names(los, own_topics, communities, embed=embed) if communities else {}
    topics = assign_missing_topics(los, own_topics, embed=embed)
    weights = [lo_seconds(lo) for lo in los]
    mode = split.get("mode") or "single"

    def topics_mode() -> list[LessonSpec]:
        if all(t is None for t in topics):
            warnings.add("no_document_topics", "No topics were found in the documents; one lesson made.")
            return single_mode()
        lessons, _, breaks = split_by_topics(los, topics)
        note_cycles(breaks)
        if _unbalanced(lessons, weights):
            warnings.add("lessons_unbalanced", "Some lessons are more than 50 % longer or shorter than the average.")
        return [LessonSpec(lesson, hint=_main_topic(lesson)) for lesson in lessons]

    def single_mode() -> list[LessonSpec]:
        order, breaks = lo_order(los)
        note_cycles(breaks)
        return [LessonSpec(order)]

    def lessons_mode() -> list[LessonSpec]:
        count = split.get("lesson_count") or 1
        order, breaks = lo_order(los)
        note_cycles(breaks)
        ranges = split_contiguous([weights[i] for i in order], count, [topics[i] for i in order])
        if len(ranges) < count:
            warnings.add("lesson_count_reduced",
                         f"{count} lessons were requested, but there are only {len(ranges)} learning objects.")
        lessons = [order[a:b] for a, b in ranges]
        if _unbalanced(lessons, weights):
            warnings.add("lessons_unbalanced", "Some lessons are more than 50 % longer or shorter than the average.")
        return [LessonSpec(lesson) for lesson in lessons]

    def syllabus_mode() -> list[LessonSpec]:
        if split.get("lesson_minutes"):
            warnings.add("syllabus_lesson_minutes_ignored", "lesson_minutes is not used with a syllabus.")
        skip: list[str] = []
        if split.get("syllabus_text"):
            ordered_names = [names[t] for t, _ in Counter(x for x in own_topics if x is not None).most_common()
                             if t in names]
            plan = plan_from_text(split["syllabus_text"], files, ordered_names)
            if plan is None:
                warnings.add("syllabus_plan_invalid", "The syllabus text could not be turned into lessons; "
                                                      "lessons are split by topics.")
                return topics_mode()
            syllabus, skip = plan.lessons, plan.skip
        else:
            syllabus = list(split.get("syllabus") or [])
        result = split_by_syllabus(los, syllabus, files, embed=embed, skip=skip)
        note_cycles(result.cycle_breaks)
        if not result.lessons:
            warnings.add("syllabus_fallback", "No learning object fits the syllabus; lessons are split by topics.")
            return topics_mode()
        for k in result.empty_lessons:
            warnings.add("empty_lesson", f"Lesson '{syllabus[k]['title']}' got no question; it is left out.")
        if result.skipped:
            warnings.add("syllabus_skipped_sections",
                         f"{len(result.skipped)} learning object(s) were left out because the syllabus skips their content.")
        if result.by_similarity and any(s.get("pages") for s in syllabus):
            warnings.add("syllabus_unmentioned_sections",
                         f"{result.by_similarity} learning object(s) are on pages outside the syllabus; "
                         f"each was added to the most similar lesson.")
        if result.prerequisites_later:
            warnings.add("prerequisite_order",
                         f"In {result.prerequisites_later} case(s) the syllabus puts a learning object "
                         f"before its prerequisite.")
        return [LessonSpec(idx, title=syllabus[k].get("title"), description=syllabus[k].get("description"))
                for k, idx in result.lessons]

    def note_cycles(breaks: int):
        if breaks:
            warnings.add("prerequisite_cycle", f"{breaks} learning object(s) were placed before a prerequisite "
                                               f"because of a cycle in the prerequisite graph.")

    def _main_topic(lesson: list[int]) -> Optional[str]:
        counts = Counter(names.get(topics[i]) for i in lesson if names.get(topics[i]))
        return counts.most_common(1)[0][0] if counts else None

    modes = {"single": single_mode, "lessons": lessons_mode, "topics": topics_mode, "syllabus": syllabus_mode}
    try:
        specs = modes[mode]()
    except BUG_ERRORS as e:  # a bug in the split must not fail the job (a timeout or crash still does)
        logger.exception(f"[Split] Mode '{mode}' failed ({type(e).__name__}); one lesson made.")
        warnings[:] = []
        warnings.add("split_failed", f"Split mode '{mode}' failed; all questions are in one lesson.")
        specs = [LessonSpec(lo_order(los)[0])]

    lo_topic_names = [names.get(t) for t in topics]
    texts, failed = lesson_titles(los, specs, topic_names=lo_topic_names, language=language,
                                  batch_size=title_batch_size, progress=progress)
    if failed:
        warnings.add("lesson_titles_failed", f"{failed} lesson title(s) could not be written; fallback titles used.")
    return {
        "lessons": [{"lo_indices": list(spec.lo_indices), "title": title, "description": description}
                    for spec, (title, description) in zip(specs, texts)],
        "lo_topics": lo_topic_names,
        # for the report's topic_coverage: topic number per LO and the document's topics (>= 2 concepts)
        "lo_topic_ids": list(topics),
        "document_topics": sorted(k for k, n in Counter(communities.values()).items() if n >= 2),
        "warnings": list(warnings),
    }
