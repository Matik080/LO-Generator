# imports
from typing import List, Dict, Any

from llm_service import call_llm
from utilities import parse_json
from embedding_service import get_embeddings_batch, cosine_similarity

def extract_atomic_units(section_text: str) -> List[Dict[str, Any]]:
    prompt = f"""
    You are extracting atomic knowledge units from a computer science textbook section.

    WHAT MAKES A GOOD ATOMIC UNIT:
    - It captures exactly ONE testable idea, rule, definition, or procedure step.
    - It is self-contained — a student can be assessed on it without needing other units.
    - It is non-trivial — skip obvious filler sentences with no educational value.
    - It is not a duplicate — if the same concept was already expressed, do not extract it again.

    HANDLING CODE:
    - If the section contains a code snippet that demonstrates a concept, extract it as an "example" or "algorithm_step" unit.
    - Include the relevant code in the statement field, formatted as plain text.
    - Do not extract code syntax as a definition unless it introduces a new construct.

    DEDUPLICATION RULES:
    - If two sentences express the same concept in different words, extract only the clearest one.
    - Do not extract both "malloc returns a pointer" and "malloc creates a variable and returns a pointer to it" — pick the more complete one.
    - Do not extract sentences that merely restate a concept already captured.

    ALLOWED TYPES (use exactly these strings):
    - definition: introduces and explains what a concept is
    - rule: a best practice, requirement, or constraint the programmer must follow
    - property: a characteristic or behavior of a concept
    - algorithm_step: a specific step in a procedure or code operation
    - constraint: a limitation or boundary condition
    - theorem: a provable statement about program behavior
    - fact: a standalone true statement that does not fit other categories
    - example: a concrete instance or code snippet illustrating a concept

    If unsure of type, use "fact".

    OUTPUT FORMAT — return a JSON array only, no markdown, no explanation:

    [
    {{
        "type": "definition | rule | property | algorithm_step | constraint | theorem | fact | example",
        "statement": "Clear, standalone statement. For code examples, include the code.",
        "concepts": ["concept1", "concept2"]
    }}
    ]

    Section text:
    \"\"\"
    {section_text}
    \"\"\"
    """

    messages = [
        {
            "role": "system",
            "content": (
                "You extract atomic knowledge units from textbook text. "
                "Be selective — quality over quantity. "
                "Do not extract duplicate or near-duplicate concepts. "
                "Do not extract meta-instructions or descriptions of atomic units. "
                "For code-heavy sections, prioritize algorithm_step and example types. "
                "Return ONLY a valid JSON array. "
                "No markdown fences. No explanation. Raw JSON only."
            )
        },
        {
            "role": "user",
            "content": prompt
        }
    ]
    response = call_llm(messages, temperature=0.2)
    units = parse_json(response)
    return units

def extract_atomic_units_batch(sections, batch_size=3):
    """
    Extract atomic units from multiple sections in a single API call.
    Returns flat list of all atomic units found.
    """
    all_units = []

    for batch_start in range(0, len(sections), batch_size):
        batch = sections[batch_start:batch_start + batch_size]
        batch_content = [s["content"] if isinstance(s, dict) else s for s in batch]
        combined_text = "\n\n--- SECTION BREAK ---\n\n".join(batch_content)

        prompt = f"""
You are extracting atomic knowledge units from computer science textbook sections.

An atomic knowledge unit is exactly ONE testable idea — a single definition, rule, 
property, algorithm step, constraint, theorem, fact, or example.

STRICT RULES:
- Extract only non-trivial, educationally valuable units
- Do NOT extract duplicate or near-duplicate concepts
- For code snippets, include the actual code in the statement field
- If two sentences say the same thing differently, extract only the clearest one

ALLOWED TYPES (use exactly these strings):
definition, rule, property, algorithm_step, constraint, theorem, fact, example

If unsure, use fact. Never invent new types.

Return a JSON array only. No markdown. No explanation.

[
  {{
    "type": "definition | rule | property | algorithm_step | constraint | theorem | fact | example",
    "statement": "Clear standalone statement. Include code if relevant.",
    "concepts": ["concept1", "concept2"]
  }}
]

Textbook sections:
\"\"\"
{combined_text}
\"\"\"
"""

        messages = [
            {
                "role": "system",
                "content": (
                    "You extract atomic knowledge units from textbook text. "
                    "Be selective — quality over quantity. "
                    "Do not extract duplicate or near-duplicate concepts. "
                    "For code-heavy sections, prioritize algorithm_step and example types. "
                    "Return ONLY a valid JSON array. No markdown. No explanation."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        print(f"[Extraction] Processing sections {batch_start + 1}-{min(batch_start + batch_size, len(sections))}...")
        response = call_llm(messages, temperature=0.2)
        units = parse_json(response)

        if isinstance(units, list):
            all_units.extend(units)
            print(f"[Extraction] Got {len(units)} units from this batch.")
        else:
            print(f"[Extraction] Unexpected response format, skipping batch.")

    return all_units

def clean_atomic_units(units: List[Dict[str, Any]],
                       similarity_threshold: float = 0.82) -> List[Dict[str, Any]]:
    ALLOWED_TYPES = {
        "definition", "rule", "property", "algorithm_step",
        "constraint", "theorem", "fact", "example",
    }

    # Fix types first
    for unit in units:
        if unit.get("type", "") not in ALLOWED_TYPES:
            unit["type"] = "fact"

    if not units:
        return []

    # Embed all statements in one batch — much faster than one by one
    statements = [u["statement"] for u in units]
    print(f"[Dedup] Computing embeddings for {len(statements)} units...")
    embeddings = get_embeddings_batch(statements)

    kept_indices = []
    kept_embeddings = []

    for i, (unit, embedding) in enumerate(zip(units, embeddings)):
        is_duplicate = False
        for j, kept_embedding in enumerate(kept_embeddings):
            sim = cosine_similarity(embedding, kept_embedding)
            if sim >= similarity_threshold:
                print(f"[Dedup] Dropping duplicate (sim={sim:.3f}):")
                print(f"  KEEP: {units[kept_indices[j]]['statement'][:80]}")
                print(f"  DROP: {unit['statement'][:80]}")
                is_duplicate = True
                break

        if not is_duplicate:
            kept_indices.append(i)
            kept_embeddings.append(embedding)

    cleaned = [units[i] for i in kept_indices]
    print(f"[Dedup] Kept {len(cleaned)} / {len(units)} units after semantic dedup.")
    return cleaned

def are_similar(a: str, b: str, threshold: float = 0.6) -> bool:
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a or not tokens_b:
        return False
    overlap = tokens_a & tokens_b
    return len(overlap) / min(len(tokens_a), len(tokens_b)) > threshold