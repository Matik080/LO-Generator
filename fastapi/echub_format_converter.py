import json
import re
from collections import defaultdict
from utilities import load_json, export_json
from llm_service import call_llm

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _source_prefix(source_title: str) -> str:
    """
    'Module 1 - Fundamentals of Statistical Learning' → 'Module 1'
    'Tutorial 2.1 - Atomic Vectors'                  → 'Tutorial 2.1'
    'Practicum 1.2 - EDA and Hypothesis Testing'     → 'Practicum 1.2'
    Falls back to the full title if the pattern doesn't match.
    """
    if " - " in source_title:
        return source_title.split(" - ")[0].strip()
    return source_title.strip()


def _split_hint_and_source(hint: str):
    """
    Split a hint string into (hint_text, source_ref).
    e.g. "Think about pointers. [Source: Module 1, p. 12]"
      -> ("Think about pointers", "[Source: Module 1, p. 12]")
    Returns (hint_text, None) if no source reference is present.
    """
    if "[Source:" in hint:
        parts = hint.split("[Source:")
        hint_text = parts[0].strip().rstrip(".")
        source_ref = "[Source:" + parts[1].strip()
        # Strip internal page numbers — they reflect our segmentation, not real pages
        import re
        source_ref = re.sub(r",\s*p\.\s*\d+", "", source_ref).strip()
        return hint_text, source_ref
    return hint.strip(), None


# ---------------------------------------------------------------------------
# LLM batch: generate name + time estimates for a batch of LOs
# ---------------------------------------------------------------------------

def _enrich_batch(los: list) -> list:
    """
    For each LO in the batch, ask the LLM to produce:
      - name: a concise descriptive title (5-8 words, no prefix yet)
      - time_to_handle: seconds a student needs to think and answer
      - time_to_explain: seconds needed to explain/review the answer

    Returns a list of dicts in the same order: [{name, time_to_handle, time_to_explain}, ...]
    Falls back to safe defaults on any parse failure.
    """
    los_text = ""
    for idx, lo in enumerate(los):
        los_text += f"""
LO {idx + 1}:
  Type: {lo.get('type', 'fact')}
  Question: {lo.get('question', '')}
  Answer: {lo.get('answer', '')}
"""

    prompt = f"""
You are helping build an educational system. For each learning object below,
produce a short name and realistic time estimates.

NAME rules:
- 5 to 8 words, title case
- Describes what concept or skill is being tested
- Do NOT start with "What", "How", "Define", or "Explain"
- Do NOT include the word "Question"
- Examples: "Malloc Return Value and Pointer Use",
            "Train-Test Split Purpose in ML",
            "ggplot2 Aesthetic Mapping Syntax"

TIME rules (in seconds):
- time_to_handle: how long a student needs to read, think, and write an answer
    open question, simple definition → 30–90s
    open question, procedural/code   → 90–240s
    multiple choice                  → 20–60s
- time_to_explain: how long a teacher needs to explain the correct answer
    simple fact or definition        → 20–45s
    rule, property, constraint       → 30–60s
    algorithm, example, code         → 60–120s

{los_text}

Return a JSON array with exactly {len(los)} objects in order:
[
  {{
    "name": "Short Descriptive Title Here",
    "time_to_handle": <int seconds>,
    "time_to_explain": <int seconds>
  }},
  ...
]

JSON array only. No markdown. No explanation.
"""

    messages = [
        {
            "role": "system",
            "content": (
                "You generate concise names and time estimates for educational questions. "
                "Follow the rules exactly. Return JSON array only."
            )
        },
        {"role": "user", "content": prompt}
    ]

    response = call_llm(messages, temperature=0.3)
    response = re.sub(r"^```(?:json)?\s*", "", response.strip())
    response = re.sub(r"\s*```$", "", response).strip()

    try:
        results = json.loads(response)
        if isinstance(results, list) and len(results) == len(los):
            return results
        print(f"[Enrich] Got {len(results)} results for {len(los)} LOs, padding with defaults.")
    except json.JSONDecodeError as e:
        print(f"[Enrich] JSON parse error: {e}, using defaults for this batch.")

    # Safe fallback
    return [{"name": "Learning Object", "time_to_handle": 90, "time_to_explain": 45}
            for _ in los]


def enrich_learning_objects(learning_objects: list, batch_size: int = 10) -> list:
    """
    Adds 'generated_name', 'time_to_handle', 'time_to_explain' to each LO in-place.
    Batches LLM calls for efficiency.
    """
    total = len(learning_objects)
    print(f"[Enrich] Generating names and time estimates for {total} LOs...")

    for batch_start in range(0, total, batch_size):
        batch = learning_objects[batch_start:batch_start + batch_size]
        print(f"[Enrich] Batch {batch_start // batch_size + 1} ({len(batch)} LOs)...")
        enriched = _enrich_batch(batch)

        for lo, meta in zip(batch, enriched):
            lo["generated_name"] = meta.get("name", "Learning Object")
            lo["time_to_handle"] = meta.get("time_to_handle", 90)
            lo["time_to_explain"] = meta.get("time_to_explain", 45)

    print(f"[Enrich] Done.")
    return learning_objects


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def lo_to_question(lo: dict) -> dict:
    """Convert a single enriched LO to the EChub question format."""
    has_mc = bool(lo.get("mc_options"))
    q_type = "choice" if has_mc else "open"

    # Name: "[Module 1] Malloc Return Value and Pointer Use"
    prefix = _source_prefix(lo.get("source_title", ""))
    raw_name = lo.get("generated_name", "Learning Object")
    name = f"[{prefix}] {raw_name}" if prefix else raw_name

    # Hints: one hint text + source reference, both unnamed per their format
    hint_text, source_ref = _split_hint_and_source(lo.get("hint", ""))
    hints = []
    if hint_text:
        hints.append({
            "name": "Hint",
            "url": f"<p>{hint_text}</p>"
        })
    if source_ref:
        hints.append({
            "name": "Source",
            "url": f"<p>{source_ref}</p>"
        })

    # Choices
    choices = []
    if has_mc:
        correct_idx = lo.get("mc_correct_index", 0)
        for i, option in enumerate(lo["mc_options"]):
            choices.append({
                "text": option,
                "is_correct": i == correct_idx
            })
    else:
        choices.append({
            "text": lo.get("answer", ""),
            "is_correct": True
        })

    return {
        "name": name,
        "description": lo.get("question", ""),
        "type": q_type,
        "time_to_handle": lo.get("time_to_handle", 90),
        "time_to_explain": lo.get("time_to_explain", 45),
        "hints": hints,
        "choices": choices
    }


def convert_by_source(learning_objects: list) -> dict:
    """Group LOs by source title, one unit per source."""
    grouped = defaultdict(list)
    for lo in learning_objects:
        source = lo.get("source_title") or "Unknown Source"
        grouped[source].append(lo)

    units = []
    for source_title, los in grouped.items():
        questions = [lo_to_question(lo) for lo in los]
        units.append({
            "title": source_title,
            "description": f"Learning objects generated from: {source_title}",
            "questions": questions
        })

    return {"units": units}


def convert_los_to_units(learning_objects: list,
                          unit_title: str,
                          unit_description: str) -> dict:
    """Convert all LOs into a single unit."""
    questions = [lo_to_question(lo) for lo in learning_objects]
    return {
        "units": [
            {
                "title": unit_title,
                "description": unit_description,
                "questions": questions
            }
        ]
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    learning_objects = load_json("./output/learning_objects.json")
    print(f"Loaded {len(learning_objects)} learning objects")

    # Step 1: enrich with LLM-generated names and time estimates
    learning_objects = enrich_learning_objects(learning_objects, batch_size=10)

    # Step 2: convert to EChub format grouped by source
    output = convert_by_source(learning_objects)

    output_path = "./output/units_team_format.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    total_questions = sum(len(u["questions"]) for u in output["units"])
    print(f"Exported {total_questions} questions across {len(output['units'])} units")
    print(f"Saved to {output_path}")