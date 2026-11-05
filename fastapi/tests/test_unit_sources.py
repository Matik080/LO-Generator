from lo_pipeline.units.sources import assign_sections

SECTIONS = [
    {"page": 1, "source": "c.html", "content": "A pointer is a variable that stores the memory address of another "
                                                "variable. Dereferencing a NULL pointer is undefined behaviour."},
    {"page": 2, "source": "c.html", "content": "The function malloc allocates a block of memory on the heap and "
                                                "returns a pointer to it. If allocation fails, malloc returns NULL."},
    {"page": 3, "source": "c.html", "content": "Every block allocated with malloc must be released with free. "
                                                "Memory that is never freed is called a memory leak."},
]

LEAK = "Memory that is allocated but never released with free is called a memory leak."
POINTER = "A pointer stores the memory address of another variable."


def _pages(units):
    return [u["source_page"] for u in units]


def test_correct_section_is_kept():
    stats = {}
    units = assign_sections([{"statement": LEAK, "section": 3}], SECTIONS, stats)
    assert _pages(units) == [3]
    assert units[0]["source_title"] == "c.html"
    assert "section" not in units[0]
    assert stats == {}


def test_wrong_section_is_corrected():
    stats = {}
    units = assign_sections([{"statement": LEAK, "section": 1}], SECTIONS, stats)
    assert _pages(units) == [3]
    assert stats == {"section_corrected": 1}


def test_missing_or_invalid_section_uses_best_match():
    stats = {}
    units = assign_sections([{"statement": POINTER}, {"statement": LEAK, "section": 7},
                             {"statement": LEAK, "section": "three"}], SECTIONS, stats)
    assert _pages(units) == [1, 3, 3]
    assert stats == {"section_missing": 3}


def test_plausible_claim_is_not_overruled():
    # malloc returning NULL is in section 2, but "NULL pointer" also appears in section 1.
    # The LLM said 2; that must stand even if section 1 is lexically close.
    statement = "If the allocation fails, malloc returns a NULL pointer."
    units = assign_sections([{"statement": statement, "section": 2}], SECTIONS)
    assert _pages(units) == [2]


def test_single_section_batch_needs_no_matching():
    units = assign_sections([{"statement": "anything", "section": 4}], SECTIONS[:1])
    assert _pages(units) == [1]
