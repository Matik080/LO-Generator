#imports
import os
from collections import Counter, defaultdict

from concept_normalizer import normalize_units
from atomic_unit_extractor import extract_atomic_units_batch, clean_atomic_units
from learning_object_generator import *
from utilities import *
import json


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

def filter_and_regenerate(learning_objects, output_dir, eval_models=None, faithfulness_threshold=3.0, hint_quality_threshold=2.0, max_attempts=2):
    """
    Evaluates all LOs with one or more models, flags those that fail the quality threshold by consensus, and regenerates them from source units.

    Failure logic (conservative):
      - Faithfulness average across models < faithfulness_threshold
      - ALL models agree the LO is not atomic
      - ALL models agree the LO is not self-contained
    """
    from learning_object_evaluator import evaluate_learning_objects_batch

    if eval_models is None:
        eval_models = ["llama-3.3-70b-versatile"]

    print(f"[Filter] Evaluating {len(learning_objects)} LOs with {eval_models}...")

    # On the first attempt evaluate everything, on subsequent attempts evaluate only the newly regenerated LOs, no point re-scoring ones that already passed.
    los_to_eval = learning_objects
    passed_evals = {}  # lo_id -> eval results, carried over between attempts

    for attempt in range(max_attempts):
        # Collect evals for the current batch only
        all_evals = []
        for model in eval_models:
            model_evals = evaluate_learning_objects_batch(
                los_to_eval, model
            )
            all_evals.extend(model_evals)

        # Merge with evals from previous attempts
        for e in all_evals:
            lid = e["lo_id"]
            if lid not in passed_evals:
                passed_evals[lid] = []
            passed_evals[lid].append(e)

        # Build by_lo from the full merged set
        by_lo = {lid: evals for lid, evals in passed_evals.items()}

        # Find failing LOs
        failing = []
        for lo in learning_objects:
            model_evals = by_lo.get(lo["id"], [])
            if not model_evals:
                print(f"[Filter] No eval found for LO {lo['id']}, skipping.")
                continue
            fails, reasons = _lo_fails_threshold(model_evals, faithfulness_threshold, hint_quality_threshold)

            # Rule-based self-containment check
            sc_ok, sc_reason = _question_is_self_contained(lo.get("question", ""))
            if not sc_ok:
                reasons = list(reasons) + [sc_reason]
                fails = True

            if fails:
                lo["_fail_reasons"] = reasons
                failing.append(lo)
                print(f"[Filter] FAIL {lo['id']}: {'; '.join(reasons)}")

        if not failing:
            print(f"[Filter] All LOs passed quality threshold on attempt {attempt + 1}.")
            break

        print(f"[Filter] Attempt {attempt + 1}: {len(failing)} LOs flagged, regenerating...")

        # Find source units for failing LOs
        atomic_units = load_json(os.path.join(output_dir, "atomic_units.json"))
        unit_map = {u["statement"]: u for u in atomic_units}

        failing_units = []
        unmatched = []
        for lo in failing:
            unit = unit_map.get(lo["source_statement"])
            if unit:
                failing_units.append(unit)
            else:
                unmatched.append(lo["id"])

        if unmatched:
            print(f"[Filter] Could not find source units for {len(unmatched)} LOs: {unmatched[:5]}")

        if not failing_units:
            print(f"[Filter] No source units found for any failing LO, stopping.")
            break

        # Regenerate
        new_los_raw = generate_learning_object_batch(failing_units, batch_size=5)

        # Replace failing LOs
        failing_ids = {lo["id"] for lo in failing}
        surviving = [lo for lo in learning_objects if lo["id"] not in failing_ids]

        for unit, lo in zip(failing_units, new_los_raw):
            lo.pop("_fail_reasons", None)
            lo["id"] = generate_id(lo["source_statement"])
            lo["keywords"] = list(extract_keywords(lo["question"] + " " + lo["answer"]))
            lo["source_title"] = unit.get("source_title", "")
            lo["source_page"] = unit.get("source_page", "")
            src = unit.get("source_title", "")
            page = unit.get("source_page", "")
            if src and page:
                lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}, p. {page}]"
            elif src:
                lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}]"
            surviving.append(lo)

        learning_objects = surviving
        print(f"[Filter] Regenerated {len(failing_units)} LOs. Total now: {len(learning_objects)}")

        # Next attempt: only evaluate the newly regenerated LOs.
        # Since regenerated LOs come from the same source statements, their IDs will be identical to the failed ones, so track by source_statement instead.
        regen_source_statements = {u["statement"] for u in failing_units}
        los_to_eval = [lo for lo in learning_objects
                       if lo.get("source_statement") in regen_source_statements]
        print(f"[Filter] Next attempt will evaluate only {len(los_to_eval)} regenerated LOs.")

    # Strip any leftover internal fields
    for lo in learning_objects:
        lo.pop("_fail_reasons", None)

    return learning_objects


def process_book_with_checkpoints(sources, output_dir, extraction_batch_size=3, generation_batch_size=5):
    """
    sources: list of dicts: [{"path": "...", "title": "..."}] OR a single string path for backwards compatibility.
    """
    # Backwards compatibility, single path string
    if isinstance(sources, str):
        sources = [{"path": sources, "title": os.path.basename(sources)}]

    checkpoint_path = os.path.join(output_dir, "checkpoint.json")

    if os.path.exists(checkpoint_path):
        with open(checkpoint_path) as f:
            checkpoint = json.load(f)
        print(f"[Checkpoint] Resuming — {len(checkpoint['raw_units'])} units extracted, "
              f"{len(checkpoint['learning_objects'])} LOs generated so far.")
    else:
        checkpoint = {
            "last_source_index": 0,
            "last_section": 0,
            "raw_units": [],
            "last_lo_index": 0,
            "learning_objects": []
        }

    # Stage 1: Extract from all sources
    for source_idx, source in enumerate(sources):
        if source_idx < checkpoint.get("last_source_index", 0):
            print(f"[Book] Skipping already completed source: {source['title']}")
            continue

        title = source["title"]
        extracted_text = extract_text_from_source(source["path"], document_title=title)
        sections = split_into_sections(extracted_text)
        total_sections = len(sections)
        print(f"[Book] Source: {title} — {total_sections} sections.")

        start_section = checkpoint["last_section"] if source_idx == checkpoint.get("last_source_index", 0) else 0

        for batch_start in range(start_section, total_sections, extraction_batch_size):
            batch = sections[batch_start:batch_start + extraction_batch_size]

            try:
                units = extract_atomic_units_batch(batch, batch_size=extraction_batch_size)
                if isinstance(units, list):
                    for unit in units:
                        unit["source_title"] = batch[0].get("source", title) if isinstance(batch[0], dict) else title
                        unit["source_page"] = batch[0].get("page") if isinstance(batch[0], dict) else None
                    checkpoint["raw_units"].extend(units)
                    print(f"[Extraction] +{len(units)} units, total raw: {len(checkpoint['raw_units'])}")
            except Exception as e:
                print(f"[Extraction] Parse error, skipping batch: {e}")

            checkpoint["last_source_index"] = source_idx
            checkpoint["last_section"] = batch_start + extraction_batch_size
            with open(checkpoint_path, "w") as f:
                json.dump(checkpoint, f)

        checkpoint["last_source_index"] = source_idx + 1
        checkpoint["last_section"] = 0
        with open(checkpoint_path, "w") as f:
            json.dump(checkpoint, f)

    # Dedup and normalize
    print(f"[Book] Deduplicating {len(checkpoint['raw_units'])} raw units...")
    atomic_units = clean_atomic_units(checkpoint["raw_units"])
    atomic_units = normalize_units(atomic_units)
    print(f"[Book] {len(atomic_units)} units after dedup.")
    export_json(atomic_units, os.path.join(output_dir, "atomic_units.json"))

    # Stage 2: Generate LOs
    learning_objects = checkpoint["learning_objects"]
    start_lo = checkpoint["last_lo_index"]
    remaining_units = atomic_units[start_lo:]

    if remaining_units:
        print(f"[Book] Generating LOs for {len(remaining_units)} remaining units (starting at {start_lo})...")
        for batch_start in range(0, len(remaining_units), generation_batch_size):
            batch = remaining_units[batch_start:batch_start + generation_batch_size]
            los_raw = generate_learning_object_batch(batch, batch_size=generation_batch_size)

            for unit, lo in zip(batch, los_raw):
                lo["id"] = generate_id(lo["source_statement"])
                lo["keywords"] = list(extract_keywords(lo["question"] + " " + lo["answer"]))
                lo["source_title"] = unit.get("source_title", "")
                lo["source_page"] = unit.get("source_page", "")
                src = unit.get("source_title", "")
                page = unit.get("source_page", "")
                if src and page:
                    lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}, p. {page}]"
                elif src:
                    lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}]"
                learning_objects.append(lo)

            checkpoint["last_lo_index"] = start_lo + batch_start + len(batch)
            checkpoint["learning_objects"] = learning_objects
            with open(checkpoint_path, "w") as f:
                json.dump(checkpoint, f)

            print(f"[Generation] Batch {batch_start // generation_batch_size + 1}/{-(-len(remaining_units) // generation_batch_size)} — {len(learning_objects)} LOs total so far...")
    else:
        print(f"[Book] LO generation already complete, skipping.")

    # Stage 3: MC variants
    print(f"[Book] Generating multiple choice variants...")
    learning_objects = generate_mc_variants_batch(learning_objects, batch_size=5)

    # Stage 4: Filter and regenerate with two-model consensus
    print(f"[Book] Filtering and regenerating low-quality LOs...")
    learning_objects = filter_and_regenerate(
        learning_objects,
        output_dir,
        #eval_models=["llama-3.3-70b-versatile"],
        #eval_models=["llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct"],
        eval_models=["llama-3.3-70b-versatile", "openai/gpt-oss-120b"],
        faithfulness_threshold=3.0,
        max_attempts=2
    )

    export_json(learning_objects, os.path.join(output_dir, "learning_objects.json"))

    os.remove(checkpoint_path)
    print(f"[Book] Complete. {len(atomic_units)} units, {len(learning_objects)} LOs.")

    return atomic_units, learning_objects


# Testing
if __name__ == "__main__":
    sources = [
        # Lecture modules
        {"path": "./input/Example.pdf",
         "title": "Module 7 - Example Module", },
    ]
    output_dir = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(output_dir, exist_ok=True)

    atomic_units, learning_objects = process_book_with_checkpoints(
        sources,
        output_dir,
        extraction_batch_size=5,
        generation_batch_size=10
    )