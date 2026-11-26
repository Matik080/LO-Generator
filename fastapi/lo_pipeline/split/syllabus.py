"""
Split mode `syllabus`: the teacher's list of lessons (title, optional description and pages).

Each LO goes to a lesson:
1. by pages: the lesson whose `pages` ("D1:1-10, D2:3"; D<n> = n-th uploaded file) contain the LO's
   file and page; if several lessons contain it, the most similar of them (MiniLM);
2. otherwise (no page match, or no pages given) the most similar lesson of all: MiniLM between the
   LO's question + answer and the lesson's title + description.

With `skip` (from syllabus_text), an LO without a page match that is closer to a skipped item than to
every lesson is left out. Lessons keep the teacher's order; inside a lesson the LOs keep the prerequisite order (order.lo_order).
A prerequisite placed in a later lesson is only counted (the teacher decided the lessons), and
lessons that get no LO are left out and counted.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from lo_pipeline.contract.options import page_ranges
from lo_pipeline.split.order import lo_order, prerequisite_indices
from lo_pipeline.split.topics import default_embed, lo_text, unit_vector


@dataclass
class SyllabusSplit:
    lessons: list[tuple[int, list[int]]]  # (index in the teacher's syllabus, LO indices in order), no empty ones
    empty_lessons: list[int] = field(default_factory=list)  # syllabus indices that got no LO
    by_pages: int = 0  # LOs placed by their page
    by_similarity: int = 0  # LOs placed by MiniLM
    prerequisites_later: int = 0  # (LO, prerequisite) pairs where the prerequisite is in a later lesson
    cycle_breaks: int = 0
    skipped: list[int] = field(default_factory=list)  # LOs left out because the teacher skipped their content


def _page(lo: dict) -> Optional[int]:
    try:
        return int(lo.get("source_page"))
    except (TypeError, ValueError):
        return None


def lesson_ranges(syllabus: Sequence[dict], files: Sequence[str]) -> list[list[tuple[str, int, int]]]:
    """Per lesson: (file name, first page, last page); D<n> beyond the uploaded files is ignored."""
    result = []
    for lesson in syllabus:
        ranges = []
        for doc, first, last in page_ranges(lesson.get("pages") or ""):
            if 1 <= doc <= len(files):
                ranges.append((files[doc - 1], first, last))
        result.append(ranges)
    return result


def lessons_by_page(lo: dict, ranges: list[list[tuple[str, int, int]]]) -> list[int]:
    """Lessons whose page ranges contain the LO's file and page."""
    page = _page(lo)
    if page is None:
        return []
    file = lo.get("source_title")
    return [k for k, rs in enumerate(ranges) if any(f == file and a <= page <= b for f, a, b in rs)]


def split_by_syllabus(los: list[dict], syllabus: Sequence[dict], files: Sequence[str],
                      embed: Optional[Callable[[list[str]], list]] = None,
                      skip: Sequence[str] = ()) -> SyllabusSplit:
    """syllabus: lessons as dicts with `title`, optional `description` and `pages`.
    files: names of the uploaded files in upload order (D1 = files[0]); they match the LOs' `source_title`.
    skip: things the teacher wants left out (from syllabus_text). An LO without a page match that is
          more similar to one of them than to every lesson is left out (counted in `skipped`).
    """
    if not los or not syllabus:
        return SyllabusSplit(lessons=[], empty_lessons=list(range(len(syllabus))))
    ranges = lesson_ranges(syllabus, files)
    candidates = []  # per LO: lessons it may go to, and whether the page decided it
    for lo in los:
        by_page = lessons_by_page(lo, ranges)
        candidates.append((by_page, True) if by_page else (list(range(len(syllabus))), False))

    lesson_of: list[Optional[int]] = [c[0] if len(c) == 1 else None for c, _ in candidates]
    need_similarity = [i for i, (c, by_page) in enumerate(candidates) if len(c) > 1 or (skip and not by_page)]
    if need_similarity:
        embed = embed or default_embed()
        # `match` (from syllabus_text: what the lesson covers, in the document's language) replaces title + description
        lesson_texts = [s.get("match") or f"{s.get('title') or ''}. {s.get('description') or ''}".strip()
                        for s in syllabus]
        lesson_vectors = [unit_vector(v) for v in embed(lesson_texts)]
        skip_vectors = [unit_vector(v) for v in embed(list(skip))] if skip else []
        lo_vectors = [unit_vector(v) for v in embed([lo_text(los[i]) for i in need_similarity])]
        for i, v in zip(need_similarity, lo_vectors):
            best = max(candidates[i][0], key=lambda k: (float(v @ lesson_vectors[k]), -k))
            to_skip = not candidates[i][1] and any(float(v @ s) > float(v @ lesson_vectors[best])
                                                   for s in skip_vectors)
            lesson_of[i] = None if to_skip else best

    order, cycle_breaks = lo_order(los)
    lessons = [(k, [i for i in order if lesson_of[i] == k]) for k in range(len(syllabus))]
    later = sum(1 for i, ps in enumerate(prerequisite_indices(los)) for p in ps
                if lesson_of[p] is not None and lesson_of[i] is not None and lesson_of[p] > lesson_of[i])
    return SyllabusSplit(
        lessons=[(k, idx) for k, idx in lessons if idx],
        empty_lessons=[k for k, idx in lessons if not idx],
        by_pages=sum(1 for c, by_page in candidates if by_page),
        by_similarity=sum(1 for i, (c, by_page) in enumerate(candidates)
                          if not by_page and lesson_of[i] is not None),
        prerequisites_later=later,
        cycle_breaks=cycle_breaks,
        skipped=[i for i in order if lesson_of[i] is None],
    )
