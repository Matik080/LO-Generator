"""Concept normalization (spaCy): lemmas without stop words; a concept of stop words only stays as written."""
from lo_pipeline.units.normalizer import normalize_concept, normalize_units


def test_lemmas_without_stop_words():
    assert normalize_concept("Test Results") == "test result"
    assert normalize_concept("the operational readiness") == "operational readiness"


def test_concept_of_stop_words_only_is_kept():
    # found on a real Slovak slide deck: "GO" became "" and "NO-GO" became "-"
    assert normalize_concept("GO") == "go"
    assert normalize_concept("NO-GO") == "no-go"
    unit = normalize_units([{"concepts": ["GO", "NO-GO", "release decision"]}])[0]
    assert sorted(unit["normalized_concepts"]) == ["go", "no-go", "release decision"]
