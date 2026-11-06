"""
Unit extraction when the LLM's answer is not valid JSON (baseline run 3 crashed on a cut-off answer).
call_llm is replaced by a script of answers; units/sources.py does the page assignment as usual.
"""
import json

import pytest

from lo_pipeline.units import extractor

SECTIONS = [
    {"page": 1, "source": "doc.pdf", "content": "Discrete data can only take certain values and is counted."},
    {"page": 2, "source": "doc.pdf", "content": "Continuous data can take any value in a range and is measured."},
    {"page": 3, "source": "doc.pdf", "content": "Ordinal data are categories whose numbers carry meaning."},
]
CUT_OFF = '[{"type": "definition", "statement": "Discrete data can only'


def _units(*statements_with_section):
    return json.dumps([{"type": "fact", "statement": s, "concepts": ["data"], "section": n}
                       for s, n in statements_with_section])


@pytest.fixture
def llm(monkeypatch):
    calls = []

    def install(*answers):
        script = list(answers)

        def fake(messages, **kwargs):
            calls.append(messages[-1]["content"])
            return script.pop(0)
        monkeypatch.setattr(extractor, "call_llm", fake)
        return calls
    return install


def test_valid_answer_needs_one_call(llm):
    calls = llm(_units(("Discrete data is counted.", 1), ("Continuous data is measured.", 2)))
    units = extractor.extract_atomic_units_batch(SECTIONS, batch_size=3)
    assert len(calls) == 1
    assert [u["source_page"] for u in units] == [1, 2]


def test_invalid_answer_is_asked_again(llm):
    calls = llm(CUT_OFF, _units(("Ordinal numbers carry meaning.", 3)))
    stats = {}
    units = extractor.extract_atomic_units_batch(SECTIONS, batch_size=3, stats=stats)
    assert len(calls) == 2
    assert [u["source_page"] for u in units] == [3]
    assert "sections_failed" not in stats


def test_still_invalid_falls_back_to_one_section_per_call(llm):
    calls = llm(CUT_OFF, CUT_OFF,
                _units(("Discrete data is counted.", 1)),
                _units(("Continuous data is measured.", 1)),
                _units(("Ordinal numbers carry meaning.", 1)))
    units = extractor.extract_atomic_units_batch(SECTIONS, batch_size=3)
    assert len(calls) == 5
    assert [u["source_page"] for u in units] == [1, 2, 3]
    # The fallback calls each contain exactly one section
    # (counted after "Textbook sections:", since the instructions above it also mention [SECTION 1], [SECTION 2])
    assert all(c.split("Textbook sections:")[1].count("[SECTION ") == 1 for c in calls[2:])


def test_section_without_any_valid_answer_is_skipped_not_fatal(llm):
    calls = llm(CUT_OFF, "not json at all",
                _units(("Discrete data is counted.", 1)),
                CUT_OFF,
                _units(("Ordinal numbers carry meaning.", 1)))
    stats = {}
    units = extractor.extract_atomic_units_batch(SECTIONS, batch_size=3, stats=stats)
    assert [u["source_page"] for u in units] == [1, 3]
    assert stats["sections_failed"] == 1
    assert len(calls) == 5
