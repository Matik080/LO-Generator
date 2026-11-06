from collections import defaultdict

from lo_pipeline.lo.generator import generate_learning_object_batch, generate_mc_variants_batch, MC_ELIGIBLE_TYPES
from lo_pipeline.lo.postprocess import postprocess_lo
import logging

logger = logging.getLogger(__name__)


def _aggregate_evals(evals):
    """
    Given a list of eval dicts (one per model per LO), return a dict keyed by lo_id with aggregated scores across all models.
    """
    by_lo = defaultdict(list)
    for e in evals:
        if "lo_id" in e:
            by_lo[e["lo_id"]].append(e)
    return by_lo


def _lo_fails_threshold(model_evals, faithfulness_threshold=3.0, hint_quality_threshold=2.0):
    """
    Returns (fails: bool, reasons: list[str]) for a single LO given evaluations from one or more models.

    Failure conditions (conservative, require consensus where possible):
      - Faithfulness: average across models < faithfulness_threshold
      - Atomic: ALL models say 'no'
      - Self-contained: ALL models say 'no'

    Hint quality is intentionally excluded, a weak hint doesn't fail students, it just doesn't help them. Not worth regenerating an otherwise good LO.
    """
    reasons = []

    # Faithfulness, average across models
    faith_vals = [e["faithfulness"] for e in model_evals
                  if e.get("faithfulness") is not None]
    if faith_vals:
        avg_faith = sum(faith_vals) / len(faith_vals)
        if avg_faith < faithfulness_threshold:
            reasons.append(f"faithfulness avg={avg_faith:.2f} < {faithfulness_threshold}")

    # Atomic - all models must agree
    atomic_vals = [e["atomic"] for e in model_evals
                   if e.get("atomic") is not None]
    if len(atomic_vals) >= 1 and all(v == "no" for v in atomic_vals):
        reasons.append(f"not atomic ({len(atomic_vals)} model(s) agree)")

    # Self-contained - all models must agree
    sc_vals = [e["self_contained"] for e in model_evals
               if e.get("self_contained") is not None]
    if len(sc_vals) >= 1 and all(v == "no" for v in sc_vals):
        reasons.append(f"not self-contained ({len(sc_vals)} model(s) agree)")

    # Hint quality - average across models must be above minimum threshold
    # Only scored when a hint exists (null = open question with no hint, skip)
    # Threshold is 2: score of 1 means actively misleading, which harms students
    hint_vals = [e["hint_quality"] for e in model_evals
                 if e.get("hint_quality") is not None]
    if hint_vals:
        avg_hint = sum(hint_vals) / len(hint_vals)
        if avg_hint < hint_quality_threshold:
            reasons.append(f"hint quality avg={avg_hint:.2f} < {hint_quality_threshold} (actively misleading)")

    return len(reasons) > 0, reasons



import re as _re

# Patterns indicating a question references external context not present in the question
_EXTERNAL_REF_PATTERNS = [
    _re.compile(r'\bthe (following|above|below|given|provided)\b', _re.I),
    _re.compile(r'\bthe (table|figure|diagram|chart|graph|image|passage|text|paragraph|excerpt)\b', _re.I),
    _re.compile(r'\b(the|this) (code|snippet|example|program|function|class|method) (above|below|shown|provided|given)\b', _re.I),
    _re.compile(r'\bas shown\b', _re.I),
    _re.compile(r'\brefer to\b', _re.I),
    _re.compile(r'\bsee (the|above|below)\b', _re.I),
]

def _question_is_self_contained(question: str):
    """
    Rule-based check independent of LLM evaluation.
    Returns (is_ok: bool, reason: str | None).
    """
    has_code = bool(_re.search(
        r'`|\bdef \b|\bclass \b|\bimport \b|<-\s|\bprint\(', question
    ))
    for pattern in _EXTERNAL_REF_PATTERNS:
        m = pattern.search(question)
        if m:
            if 'following' in pattern.pattern and has_code:
                continue
            return False, f"references external context: '{m.group()}'"
    return True, None

def _rank(model_evals, reasons):
    """Lower is better: fewer failure reasons first, then higher average faithfulness."""
    faith = [e["faithfulness"] for e in model_evals if isinstance(e.get("faithfulness"), (int, float))]
    return len(reasons), -(sum(faith) / len(faith) if faith else 0.0)


SCORE_FIELDS = ("faithfulness", "question_clarity", "hint_quality", "distractor_plausibility")


def score_averages(eval_lists):
    """
    Average of each numeric score, per evaluating model and over all models.
    `eval_lists`: one list of evaluations (one per model) per LO.
    Returns {"by_model": {model: {field: avg, "n": count}}, "all_models": {field: avg}}.
    """
    sums, counts, all_sums, all_counts = {}, {}, {}, {}
    for evals in eval_lists:
        for e in evals:
            model = e.get("model", "?")
            for field in SCORE_FIELDS:
                value = e.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    sums.setdefault(model, {}).setdefault(field, 0.0)
                    counts.setdefault(model, {}).setdefault(field, 0)
                    sums[model][field] += value
                    counts[model][field] += 1
                    all_sums[field] = all_sums.get(field, 0.0) + value
                    all_counts[field] = all_counts.get(field, 0) + 1
    by_model = {m: {f: round(sums[m][f] / counts[m][f], 3) for f in sums[m]} for m in sums}
    for m in by_model:
        by_model[m]["n"] = max(counts[m].values())
    return {"by_model": by_model,
            "all_models": {f: round(all_sums[f] / all_counts[f], 3) for f in all_sums}}


def filter_and_regenerate(learning_objects, atomic_units, eval_models=None, faithfulness_threshold=3.0,
                          hint_quality_threshold=2.0, max_attempts=2, mc_batch_size=5, progress=None,
                          quality=None):
    """
    Evaluates all LOs with one or more models, flags those that fail the quality threshold by consensus, and regenerates them from source units.
    `atomic_units` are the units the LOs were generated from (matched by statement for regeneration).
    `max_attempts` is the number of regeneration rounds. Every regenerated LO is evaluated again; an LO that
    still fails after the last round is kept in its best-scoring version. Regenerated LOs get MC variants once,
    at the end (so their distractors are not scored; distractor quality is not a pass/fail criterion).
    `progress(done, total)` counts LLM calls (optional); `total` only shrinks, so done/total never goes backwards.
    `quality` (optional dict) is filled with a summary for the baseline / report:
      initial: first evaluation, before any regeneration (LO count, evaluated, passed, pass rate, score averages)
      final:   the versions that are returned (same fields, plus regenerated / still_failing counts)

    Failure logic (conservative):
      - Faithfulness average across models < faithfulness_threshold
      - ALL models agree the LO is not atomic
      - ALL models agree the LO is not self-contained
    """
    from lo_pipeline.lo import evaluator

    if eval_models is None:
        eval_models = ["llama-3.3-70b-versatile"]

    # --- progress: LLM calls, starting from an upper bound (every LO fails every round) ---
    calls = {"done": 0, "total": 0}

    def _eval_calls(n):
        return sum(-(-n // evaluator.eval_batch_size(m)) for m in eval_models)

    def _round_calls(n):  # regenerate + re-evaluate
        return -(-n // 5) + _eval_calls(n)

    def _mc_calls(n):  # final MC for regenerated LOs (upper bound: all of them MC-eligible)
        return -(-n // mc_batch_size)

    def _tick():
        calls["done"] += 1
        if progress:
            progress(calls["done"], max(calls["total"], calls["done"]))

    def _set_total(total):
        calls["total"] = max(total, calls["done"])
        if progress and calls["done"]:
            progress(calls["done"], calls["total"])

    n = len(learning_objects)
    calls["total"] = _eval_calls(n) + max_attempts * _round_calls(n) + _mc_calls(n)

    logger.info(f"[Filter] Evaluating {len(learning_objects)} LOs with {eval_models}...")

    unit_map = {u["statement"]: u for u in atomic_units}
    evals_by_id = {}   # lo_id -> evaluations of the CURRENT version only
    best = {}          # source_statement -> (rank, lo, reasons) of the best failing version seen so far
    regenerated = set()  # id() of every LO object created by regeneration (they need MC variants at the end)
    version_evals = {}   # id() of an LO object -> its evaluations (each version keeps its own)
    version_fails = {}   # id() of an LO object -> True/False once it has been judged
    initial = None
    los_to_eval = learning_objects

    for attempt in range(max_attempts + 1):
        # Evaluate only the LOs that are new in this round; their old scores are replaced, not merged
        for lo in los_to_eval:
            evals_by_id.pop(lo["id"], None)
        for model in eval_models:
            for e in evaluator.evaluate_learning_objects_batch(los_to_eval, model, progress=_tick):
                evals_by_id.setdefault(e["lo_id"], []).append(e)
        for lo in los_to_eval:
            version_evals[id(lo)] = list(evals_by_id.get(lo["id"], []))

        # Find failing LOs
        failing = []
        for lo in learning_objects:
            model_evals = evals_by_id.get(lo["id"], [])
            if not model_evals:
                logger.warning(f"[Filter] No eval found for LO {lo['id']}, skipping.")
                continue
            fails, reasons = _lo_fails_threshold(model_evals, faithfulness_threshold, hint_quality_threshold)

            # Rule-based self-containment check
            sc_ok, sc_reason = _question_is_self_contained(lo.get("question", ""))
            if not sc_ok:
                reasons = list(reasons) + [sc_reason]
                fails = True

            version_fails[id(lo)] = fails
            if fails:
                failing.append(lo)
                rank = _rank(model_evals, reasons)
                key = lo["source_statement"]
                if key not in best or rank < best[key][0]:
                    best[key] = (rank, lo, reasons)
                logger.info(f"[Filter] FAIL {lo['id']}: {'; '.join(reasons)}")

        if attempt == 0:
            initial = _quality_section(learning_objects, version_evals, version_fails)

        if not failing:
            logger.info(f"[Filter] All LOs passed quality threshold after {attempt} regeneration round(s).")
            break

        if attempt == max_attempts:
            logger.warning(f"[Filter] {len(failing)} LO(s) still fail after {max_attempts} regeneration round(s); keeping their best version.")
            failing_keys = {lo["source_statement"] for lo in failing}
            learning_objects = [best[lo["source_statement"]][1] if lo["source_statement"] in failing_keys else lo
                                for lo in learning_objects]
            break

        logger.info(f"[Filter] Round {attempt + 1}: {len(failing)} LOs flagged, regenerating...")

        # Find source units for failing LOs
        failing_units, unmatched = [], []
        for lo in failing:
            unit = unit_map.get(lo["source_statement"])
            if unit:
                failing_units.append(unit)
            else:
                unmatched.append(lo["id"])

        if unmatched:
            logger.warning(f"[Filter] Could not find source units for {len(unmatched)} LOs: {unmatched[:5]}")

        if not failing_units:
            logger.warning(f"[Filter] No source units found for any failing LO, stopping.")
            break

        # Later rounds can only re-check these LOs, so the bound shrinks to them
        k = len(failing_units)
        _set_total(calls["done"] + (max_attempts - attempt) * _round_calls(k) + _mc_calls(k))

        # Regenerate (MC variants are added once at the end, for the versions that are kept)
        new_los_raw = generate_learning_object_batch(failing_units, batch_size=5)
        for _ in range(-(-len(failing_units) // 5)):
            _tick()
        pairs = list(zip(failing_units, new_los_raw))  # shorter than failing_units if a batch came back empty
        new_los = [postprocess_lo(lo, unit) for unit, lo in pairs]
        regenerated.update(id(lo) for lo in new_los)

        # Replace failing LOs that got a new version (by source unit; a regenerated LO usually keeps the same id).
        # Failing LOs without a new version stay and are re-checked against their best version at the end.
        regenerated_keys = {unit["statement"] for unit, _ in pairs}
        learning_objects = [lo for lo in learning_objects if lo["source_statement"] not in regenerated_keys] + new_los
        logger.info(f"[Filter] Regenerated {len(new_los)} LOs. Total now: {len(learning_objects)}")

        los_to_eval = new_los

    # Regenerated LOs don't have MC variants yet: add them once, for the final versions only
    missing_mc = [lo for lo in learning_objects
                  if id(lo) in regenerated and lo.get("type") in MC_ELIGIBLE_TYPES]
    _set_total(calls["done"] + -(-len(missing_mc) // mc_batch_size))
    if missing_mc:
        logger.info(f"[Filter] Generating MC variants for {len(missing_mc)} regenerated LO(s)...")
        generate_mc_variants_batch(missing_mc, batch_size=mc_batch_size, progress=lambda d, t: _tick())

    _set_total(calls["done"])  # the upper bound wasn't needed in full

    if quality is not None:
        final = _quality_section(learning_objects, version_evals, version_fails)
        final["regenerated"] = sum(1 for lo in learning_objects if id(lo) in regenerated)
        final["still_failing"] = sum(1 for lo in learning_objects if version_fails.get(id(lo)))
        quality.update({
            "eval_models": list(eval_models),
            "thresholds": {"faithfulness": faithfulness_threshold, "hint_quality": hint_quality_threshold,
                           "max_attempts": max_attempts},
            "initial": initial,
            "final": final,
        })
    return learning_objects


def _quality_section(los, version_evals, version_fails):
    """LO count, how many were evaluated / passed, pass rate and score averages for these LO versions."""
    evaluated = [lo for lo in los if version_evals.get(id(lo))]
    passed = sum(1 for lo in evaluated if version_fails.get(id(lo)) is False)
    return {
        "los": len(los),
        "evaluated": len(evaluated),
        "passed": passed,
        "pass_rate": round(passed / len(evaluated), 3) if evaluated else None,
        "scores": score_averages(version_evals[id(lo)] for lo in evaluated),
    }
