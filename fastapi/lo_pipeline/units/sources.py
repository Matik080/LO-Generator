"""
Which section (and so which source file and page) each extracted unit comes from.

Units are extracted from several sections in one LLM call. The LLM is asked which numbered section each unit
comes from; that answer is then checked against the text, because a wrong page would end up in hints, the report
and the page-based lesson split. Each unit is compared with every section of its batch by:
  - word overlap: share of the unit's words that appear in the section (catches near-verbatim units), and
  - meaning: best embedding similarity between the unit and any sentence of the section (catches paraphrases).
The LLM's section is kept unless it is missing/invalid or another section matches clearly better.
"""
import logging
import re

from lo_pipeline.lo.lexical import tokenize

logger = logging.getLogger(__name__)

# The LLM's section is overruled only if another section scores at least this much higher (scores are 0..1).
CORRECTION_MARGIN = 0.15

_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are", "be", "by", "it", "its", "that",
    "this", "with", "as", "at", "from", "was", "were", "can", "not", "no", "if", "when", "which", "has", "have",
}


def _content_words(text):
    return {t for t in tokenize(text) if t not in _STOPWORDS and len(t) > 1}


def _sentences(text):
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if len(p.strip()) > 10] or [text.strip()]


def _word_overlap(unit_words, section_words):
    return len(unit_words & section_words) / len(unit_words) if unit_words else 0.0


def section_scores(statements, section_texts):
    """scores[u][s] in 0..1: how well statement u matches section s (average of word overlap and meaning)."""
    from lo_pipeline.llm.embeddings import get_embeddings_batch, cosine_similarity

    section_words = [_content_words(t) for t in section_texts]
    sentences = [_sentences(t) for t in section_texts]
    flat = [s for sents in sentences for s in sents]
    vectors = list(get_embeddings_batch(list(statements) + flat)) if statements else []
    stmt_vecs, sent_vecs = vectors[:len(statements)], vectors[len(statements):]

    scores = []
    for i, statement in enumerate(statements):
        words = _content_words(statement)
        row, offset = [], 0
        for s, sents in enumerate(sentences):
            meaning = max(cosine_similarity(stmt_vecs[i], v) for v in sent_vecs[offset:offset + len(sents)])
            offset += len(sents)
            row.append((_word_overlap(words, section_words[s]) + max(meaning, 0.0)) / 2)
        scores.append(row)
    return scores


def assign_sections(units, batch_sections, stats=None):
    """
    Sets `source_title` and `source_page` on each unit from its section in `batch_sections`
    (the sections sent in the same LLM call). Removes the LLM's `section` field.
    `stats` (optional dict) counts {"section_corrected": n, "section_missing": n}.
    """
    if not units:
        return units
    stats = stats if stats is not None else {}
    n = len(batch_sections)

    if n == 1:
        chosen = [0] * len(units)
    else:
        texts = [s["content"] if isinstance(s, dict) else str(s) for s in batch_sections]
        scores = section_scores([u.get("statement", "") for u in units], texts)
        chosen = []
        for unit, row in zip(units, scores):
            best = max(range(n), key=lambda s: row[s])
            claimed = _claimed_index(unit.get("section"), n)
            if claimed is None:
                stats["section_missing"] = stats.get("section_missing", 0) + 1
                chosen.append(best)
            elif row[best] - row[claimed] >= CORRECTION_MARGIN:
                stats["section_corrected"] = stats.get("section_corrected", 0) + 1
                logger.debug(f"[Sources] Section {claimed + 1} -> {best + 1} for: {unit.get('statement', '')[:60]}")
                chosen.append(best)
            else:
                chosen.append(claimed)

    for unit, s in zip(units, chosen):
        unit.pop("section", None)
        section = batch_sections[s] if isinstance(batch_sections[s], dict) else {}
        unit["source_title"] = section.get("source") or ""
        unit["source_page"] = section.get("page")
    return units


def _claimed_index(value, n):
    """The LLM's 1-based section number as a 0-based index, or None if missing/invalid."""
    try:
        i = int(value) - 1
    except (TypeError, ValueError):
        return None
    return i if 0 <= i < n else None
