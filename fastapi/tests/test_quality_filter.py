"""
filter_and_regenerate with scripted fakes instead of the LLM: the evaluator's scores are chosen per LO version,
so each test controls exactly which versions pass.
"""
import pytest

from lo_pipeline.lo import evaluator, quality_filter
from lo_pipeline.lo.quality_filter import filter_and_regenerate

MODELS = ["m1", "m2"]


def _unit(n):
    return {"statement": f"statement {n}", "type": "definition"}


def _lo(n, version=0):
    return {"id": f"id{n}", "source_statement": f"statement {n}", "type": "definition",
            "question": f"What is {n}? (v{version})", "answer": f"answer {n}", "hint": "hint",
            "version": version, "mc_options": ["a", "b", "c", "d"] if version == 0 else None}


@pytest.fixture
def fake(monkeypatch):
    """faith[(n, version)] = faithfulness score both models give that version (default 5 = passes)."""
    state = {"faith": {}, "eval_calls": [], "versions": {}, "mc_calls": []}

    def evaluate(los, model, batch_size=None, progress=None):
        state["eval_calls"].append([(lo["id"], lo["version"]) for lo in los])
        if progress:
            progress()
        return [{"lo_id": lo["id"], "model": model, "atomic": "yes", "self_contained": "yes",
                 "faithfulness": state["faith"].get((lo["id"], lo["version"]), 5)} for lo in los]

    def regenerate(units, batch_size=5):
        out = []
        for u in units:
            n = u["statement"].split()[-1]
            state["versions"][n] = state["versions"].get(n, 0) + 1
            lo = _lo(n, state["versions"][n])
            lo.pop("id")  # postprocess_lo sets it from source_statement
            out.append(lo)
        return out

    def mc(los, batch_size=5, progress=None):
        state["mc_calls"].append([lo["id"] for lo in los])
        for lo in los:
            lo["mc_options"] = ["x", "y", "z", lo["answer"]]
        if progress:
            progress(1, 1)
        return los

    monkeypatch.setattr(evaluator, "evaluate_learning_objects_batch", evaluate)
    monkeypatch.setattr(quality_filter, "generate_learning_object_batch", regenerate)
    monkeypatch.setattr(quality_filter, "generate_mc_variants_batch", mc)
    # postprocess_lo derives the id from source_statement; keep ids stable and readable in tests
    monkeypatch.setattr(quality_filter, "postprocess_lo", lambda lo, unit: {**lo, "id": "id" + unit["statement"].split()[-1]})
    return state


def _run(n_los=3, max_attempts=2):
    los = [_lo(i) for i in range(1, n_los + 1)]
    los = [{**lo, "id": f"id{i}"} for i, lo in enumerate(los, 1)]
    return filter_and_regenerate(los, [_unit(i) for i in range(1, n_los + 1)], eval_models=MODELS,
                                 max_attempts=max_attempts)


def test_regenerated_version_is_judged_on_its_own_scores(fake):
    # v0 scores 1 (fails), v1 scores 4 (passes). Averaging old and new scores (old bug) gives 2.5 -> fail.
    fake["faith"][("id1", 0)] = 1
    fake["faith"][("id1", 1)] = 4
    result = {lo["id"]: lo for lo in _run()}
    assert result["id1"]["version"] == 1
    # Round 1 evaluates all 3 per model, round 2 only the regenerated one, then it passes: no third round
    assert fake["eval_calls"] == [[("id1", 0), ("id2", 0), ("id3", 0)]] * 2 + [[("id1", 1)]] * 2


def test_last_regeneration_is_evaluated(fake):
    fake["faith"].update({("id1", 0): 1, ("id1", 1): 1, ("id1", 2): 4})
    result = {lo["id"]: lo for lo in _run(max_attempts=2)}
    assert result["id1"]["version"] == 2
    assert [("id1", 2)] in fake["eval_calls"], "the final regenerated version must be evaluated"


def test_still_failing_lo_keeps_its_best_version(fake):
    # All versions fail; v1 is the least bad (2 > 1)
    fake["faith"].update({("id1", 0): 1, ("id1", 1): 2, ("id1", 2): 1})
    result = {lo["id"]: lo for lo in _run(max_attempts=2)}
    assert result["id1"]["version"] == 1
    assert len(result) == 3


def test_regenerated_los_get_mc_options_once_at_the_end(fake):
    fake["faith"].update({("id1", 0): 1, ("id1", 1): 1, ("id1", 2): 4, ("id2", 0): 1})
    result = {lo["id"]: lo for lo in _run(max_attempts=2)}
    # One MC call, after all rounds, covering only the regenerated final versions
    assert len(fake["mc_calls"]) == 1
    assert sorted(fake["mc_calls"][0]) == ["id1", "id2"]
    assert all(lo["mc_options"] for lo in result.values())
    assert result["id3"]["mc_options"] == ["a", "b", "c", "d"], "untouched LOs keep their original options"


def test_progress_never_goes_backwards(fake, monkeypatch):
    fake["faith"].update({("id1", 0): 1, ("id1", 1): 1, ("id1", 2): 4})
    los = [{**_lo(i), "id": f"id{i}"} for i in range(1, 4)]
    events = []
    filter_and_regenerate(los, [_unit(i) for i in range(1, 4)], eval_models=MODELS, max_attempts=2,
                          progress=lambda d, t: events.append((d, t)))
    fractions = [d / t for d, t in events]
    assert fractions == sorted(fractions)
    assert events[-1][0] == events[-1][1]


def _run_with_quality(max_attempts=2):
    quality = {}
    los = [{**_lo(i), "id": f"id{i}"} for i in range(1, 4)]
    result = filter_and_regenerate(los, [_unit(i) for i in range(1, 4)], eval_models=MODELS,
                                   max_attempts=max_attempts, quality=quality)
    return result, quality


def test_quality_summary_initial_and_final(fake):
    fake["faith"].update({("id1", 0): 1, ("id1", 1): 4})
    _, q = _run_with_quality()

    assert q["eval_models"] == MODELS
    assert (q["initial"]["los"], q["initial"]["evaluated"], q["initial"]["passed"]) == (3, 3, 2)
    assert q["initial"]["pass_rate"] == 0.667
    # Initial scores are the first versions: (1 + 5 + 5) / 3; final ones are the kept versions: (4 + 5 + 5) / 3
    assert q["initial"]["scores"]["by_model"]["m1"]["faithfulness"] == 3.667
    assert q["final"]["scores"]["by_model"]["m1"]["faithfulness"] == 4.667
    assert q["final"]["scores"]["all_models"]["faithfulness"] == 4.667
    assert (q["final"]["passed"], q["final"]["regenerated"], q["final"]["still_failing"]) == (3, 1, 0)
    assert q["final"]["pass_rate"] == 1.0


def test_quality_summary_counts_lo_kept_in_best_failing_version(fake):
    fake["faith"].update({("id1", 0): 1, ("id1", 1): 2, ("id1", 2): 1})
    result, q = _run_with_quality()

    assert {lo["id"]: lo["version"] for lo in result}["id1"] == 1
    assert (q["final"]["passed"], q["final"]["still_failing"]) == (2, 1)
    # The kept version (v1, faithfulness 2) is what the final scores describe
    assert q["final"]["scores"]["by_model"]["m1"]["faithfulness"] == 4.0
