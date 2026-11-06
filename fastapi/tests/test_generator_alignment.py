from lo_pipeline.lo.generator import _align_to_units

UNITS = [{"statement": "Alpha is first."}, {"statement": "Beta is second."}, {"statement": "Gamma is third."}]


def _lo(statement):
    return {"source_statement": statement, "question": "q", "answer": "a"}


def test_same_count_is_positional():
    results = [_lo("x"), _lo("y"), _lo("z")]
    assert _align_to_units(UNITS, results) == results


def test_fewer_results_are_matched_by_statement():
    beta, gamma = _lo("beta is  second."), _lo("Gamma is third.")  # case/space differences are fine
    assert _align_to_units(UNITS, [gamma, beta]) == [None, beta, gamma]


def test_unmatchable_results_are_dropped_not_guessed():
    assert _align_to_units(UNITS, [_lo("Something else entirely.")]) == [None, None, None]


def test_non_dict_entries_become_none():
    assert _align_to_units(UNITS[:2], ["oops", _lo("Beta is second.")]) == [None, _lo("Beta is second.")]
