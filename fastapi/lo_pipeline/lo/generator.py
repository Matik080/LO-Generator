# imports
import json
import re
from typing import Dict, Any
import hashlib
from nltk.corpus import stopwords

from utilities import parse_json
from lexical_overlap_computation import tokenize
from llm_service import call_llm


def generate_id(source_statement):
    return hashlib.md5(source_statement.encode('utf-8')).hexdigest()[:8]


def get_type_prompt(unit: Dict[str, Any]) -> str:
    unit_type = unit.get("type", "fact")
    statement = unit["statement"]
    concepts = unit.get("concepts", [])
    source_title = unit.get("source_title", "")
    source_page = unit.get("source_page", "")
    source_ref = f"[Source: {source_title}, p. {source_page}]" if source_title else ""

    base = f"""
    Atomic unit type: {unit_type}
    Statement: {statement}
    Concepts: {concepts}
    Source reference: {source_ref}
    """

    type_instructions = {
        "definition": """
    Generate a learning object that tests whether the student understands the meaning of the concept, not just recalls the words.
    - Question: Ask the student to explain what the concept means or distinguish it from related concepts.
    - Answer: A concise, precise explanation based STRICTLY on the source statement. Do not add reasoning, benefits, or implications not explicitly stated in the source.
    - Hint: Point toward the key characteristic that defines this concept.
    - Do NOT just ask "What is X?" if the answer is just restating the definition word for word.
    - Do NOT add information not present in the source statement.
    """,
        "rule": """
    Generate a learning object that tests whether the student understands WHY the rule exists and what happens if it is violated.
    - Question: Ask why the rule is important, or what the consequence of breaking it is — based only on what the source statement states or directly implies.
    - Answer: Explain the reasoning and consequence using only information present in the source statement.
    - Hint: Guide toward the potential failure mode.
    - Do NOT just ask "What is the rule for X?"
    - Do NOT add information not present in the source statement.
    """,
        "algorithm_step": """
    Generate a coding or procedural exercise.
    - Question: The question must be 100% self-contained. Include ALL code inline. FORBIDDEN: "given", "above", "below", "shown", "provided", "the following" (unless content appears immediately after), "refer to", "see the", "the example", "the output".
    - Answer: The correct code solution.
    - Expected Output: If the code produces console output, provide the exact expected output string. If it does not produce output, use null.
    - Hint: Point toward the key function or concept.
    - Do NOT generate a plain factual question.
    - Do NOT add information not present in the source statement.
    """,
        "example": """
    Generate a learning object that uses the example to test deeper understanding.
    - Question: Include the actual code or example in the question itself.
      WRONG: "What principle does the given struct example demonstrate?"
      RIGHT: "What principle does the following code demonstrate? struct node { struct node *next_ptr; int value; }"
      The word "given" is FORBIDDEN. The question must contain the actual code or content.
    - Answer: Explain what principle the example illustrates.
    - Expected Output: If the example code produces console output, provide it. Otherwise null.
    - Hint: Point toward the general concept the example is showing.
    - Do NOT just ask the student to identify or name the example.
    - Do NOT add information not present in the source statement.
    """,
        "constraint": """
    Generate a learning object that tests whether the student understands the boundary condition or limitation.
    - Question: Ask what happens when the constraint is violated, or ask the student to identify whether a given scenario violates the constraint.
    - Answer: Describe the consequence of violating the constraint.
    - Hint: Guide toward the boundary condition.
    - Do NOT add information not present in the source statement.
    """,
        "property": """
    Generate a learning object that tests whether the student can apply or recognize the property.
    - Question: Ask the student to predict behavior based on the property, or identify whether a scenario exhibits the property.
    - Answer: Apply the property to give a concrete answer.
    - Hint: Reference the property without stating it directly.
    - Do NOT add information not present in the source statement.
    """,
        "theorem": """
    Generate a learning object that tests understanding of what the theorem states and when it applies.
    - Question: Ask the student to state when the theorem applies or what it guarantees.
    - Answer: Precise statement of the theorem's condition and conclusion.
    - Hint: Point toward the key condition or precondition.
    - Do NOT add information not present in the source statement.
    """,
        "fact": """
    Generate a learning object that goes beyond simple recall.
    - Question: Ask the student to explain the significance of the fact, or connect it to a related concept — but ONLY using information present in the source statement.
    - Answer: Explain the fact using ONLY what is stated in the source. Do not infer consequences or add context not explicitly present.
    - Hint: Guide toward the relevant context within the source.
    - Avoid questions where the answer is just repeating the fact verbatim.
    - Do NOT add information not present in the source statement.
    """,
    }

    instructions = type_instructions.get(unit_type, type_instructions["fact"])

    if unit_type in ("algorithm_step", "example"):
        schema = f"""
        Return valid JSON only:
        {{
        "question": "...",
        "answer": "...",
        "expected_output": "exact console output string, or null if no output",
        "hint": "...",
        "source_statement": "{statement}",
        "type": "{unit_type}"
        }}
        """
    else:
        schema = f"""
        Return valid JSON only:
        {{
        "question": "...",
        "answer": "...",
        "hint": "...",
        "source_statement": "{statement}",
        "type": "{unit_type}"
        }}
        """

    return f"""
        You are generating a pedagogically sound learning object from an atomic knowledge unit.

        IMPORTANT: The source material is in the same language as the atomic unit statement. Generate the entire learning object — question, answer, and hint — in the SAME LANGUAGE as the source statement. Do not switch languages.

        {instructions}

        {base}

        Requirements:
        - Stay faithful to the source statement.
        - Do not invent knowledge not present in the source.
        - Keep the answer concise but complete.
        - The hint must guide toward the answer WITHOUT containing any key term that appears in the correct answer.
        - HINT RULES: Do not use the name of the concept being tested. Do not use any word that would appear in the correct answer but not in a wrong answer. Point toward the category, context, or related concept — not the answer itself.
        - BAD hint example: question asks who developed least squares → hint says "Consider Gauss's work" (Gauss is in the answer)
        - GOOD hint example: question asks who developed least squares → hint says "Consider early 19th century contributions to astronomical measurement"

        {schema}
        """


def generate_learning_object(unit: Dict[str, Any]) -> Dict[str, Any]:
    prompt = get_type_prompt(unit)

    messages = [
        {
            "role": "system",
            "content": "You generate pedagogically sound learning objects from atomic knowledge units. Different unit types require different question formats. CRITICAL: Never add information not explicitly present in the source statement. Return JSON only. No markdown fences."
        },
        {
            "role": "user",
            "content": prompt
        }
    ]

    response = call_llm(messages, temperature=0.3)

    if not response:
        raise ValueError(f"LLM returned empty response for unit: {unit.get('statement', '?')[:60]}")

    response = re.sub(r"^```(?:json)?\s*", "", response.strip())
    response = re.sub(r"\s*```$", "", response).strip()

    return json.loads(response)


def generate_learning_object_batch(units, batch_size=5):
    """
    Generate learning objects for multiple atomic units in a single API call.
    Returns list of learning objects in same order as input units.
    """
    results = []

    # Process in batches
    for batch_start in range(0, len(units), batch_size):
        batch = units[batch_start:batch_start + batch_size]

        # Build the batch prompt
        units_text = ""
        for idx, unit in enumerate(batch):
            units_text += f"""
    Unit {idx + 1}:
    Type: {unit['type']}
    Statement: {unit['statement']}
    Concepts: {unit.get('concepts', [])}

    """
        prompt = f"""
    You are generating pedagogically sound learning objects from atomic knowledge units.

    IMPORTANT: The source material may be in a non-English language. Generate each learning object — question, answer, and hint — in the SAME LANGUAGE as its source statement. Do not switch languages.
    Generate one learning object for EACH of the {len(batch)} units below.
    Each unit type requires a different question format:

    - definition: test understanding of meaning, not just recall. Ask how/why not just what.
    - rule: test understanding of WHY the rule exists and consequences of breaking it.
    - algorithm_step: generate a coding or procedural exercise. Ask student to write code or order steps.
    - example: ask what principle the example demonstrates, or apply concept to new situation.
    - constraint: ask what happens when constraint is violated.
    - property: ask student to predict behavior or recognize the property in a scenario.
    - theorem: ask when theorem applies and what it guarantees.
    - fact: go beyond recall, ask significance or connection to related concept.

    Requirements for each:
    - Stay faithful to the source statement
    - Do not invent knowledge not present in the source
    - Hint must NOT contain any key term that appears in the correct answer. Do not name the concept being tested. Point toward the category or context, never the answer itself.
    - Answer should be concise but complete
    - For algorithm_step types: include expected_output as the exact console output, or null if no output
    - For all other types: set expected_output to null
    - Questions must be 100% self-contained. A student must be able to answer using only what is written in the question.
    - FORBIDDEN in questions: "given", "above", "below", "shown", "provided", "refer to", "see the", "the example", "the code", "the output", "the table", "the figure", "the passage", or "the following X" unless X appears inline immediately after.
    - For example/algorithm_step types: include the actual code directly in the question. Never reference code without showing it.

    {units_text}

    Return a JSON array with exactly {len(batch)} learning objects in the same order as the input units:

    [
      {{
      "question": "...",
      "answer": "...",
      "expected_output": "string or null",
      "hint": "...",
      "source_statement": "exact source statement from unit",
      "type": "exact type from unit"
    }},
      ...
    ]

    Return JSON array only. No markdown. No explanation.
    """

        messages = [
            {
                "role": "system",
                "content": "You generate pedagogically sound learning objects from atomic knowledge units. CRITICAL RULE: Every answer must be strictly grounded in the source statement. Never infer, extrapolate, or add information not explicitly stated in the source. Questions must be 100% self-contained — never reference code, examples, tables, or figures without including them inline in the question. FORBIDDEN words: given, above, below, shown, provided. Return a JSON array only. No markdown fences. No explanation."
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        print(f"[Generation] Processing batch {batch_start // batch_size + 1} ({len(batch)} units)...")
        response = call_llm(messages, temperature=0.3)

        if not response:
            print(f"[Generation] Empty response for batch starting at {batch_start}, skipping...")
            continue

        response = re.sub(r"^```(?:json)?\s*", "", response.strip())
        response = re.sub(r"\s*```$", "", response).strip()

        try:
            batch_results = json.loads(response)
            if not isinstance(batch_results, list):
                print(f"[Generation] Expected list, got {type(batch_results)}, skipping batch...")
                continue
            if len(batch_results) != len(batch):
                print(f"[Generation] Expected {len(batch)} results, got {len(batch_results)}, using what we have...")
            results.extend(batch_results[:len(batch)])
        except json.JSONDecodeError as e:
            print(f"[Generation] JSON parse error in batch: {e}, skipping...")
            continue

    return results


def extract_defined_concept(question, answer):
    text = question.lower()

    # "What is X?" / "What are X?"
    match = re.search(r"what (?:is|are) (?:a |an |the )?(.+?)\??$", text)
    if match:
        return match.group(1).strip()

    # "What characteristic/property/feature is unique to X?"
    match = re.search(r"unique to (.+?)\??$", text)
    if match:
        return match.group(1).strip()

    # Fallback: try the answer for "X is ..."
    match = re.search(r"^([^,.]+?)\s+(?:is|are)\b", answer.lower())
    if match:
        return match.group(1).strip()

    return None


STOPWORDS = set(stopwords.words('english'))


def extract_keywords(text):
    tokens = tokenize(text)
    return {token for token in tokens if token.lower() not in STOPWORDS and len(token) > 1}


def build_prerequisites(learning_objects):
    edges = []

    for lo_a in learning_objects:
        concept = lo_a.get("defines")
        if concept:
            for lo_b in learning_objects:
                if lo_a == lo_b:
                    continue
                if concept in lo_b["keywords"]:
                    edges.append((lo_a["id"], lo_b["id"]))
    return edges


def generate_mc_variants(lo: Dict[str, Any]) -> Dict[str, Any]:
    """
    Takes an existing LO and generates multiple choice variants.
    Adds mc_options (list of 4 strings) and mc_correct_index (int) to the LO.
    Only makes sense for non-code types, skipping algorithm_step.
    """
    import random

    prompt = f"""
You are creating multiple choice distractors for a learning object.

IMPORTANT: Generate all distractors in the SAME LANGUAGE as the question and answer.

Question: {lo['question']}
Correct Answer: {lo['answer']}
Source Statement: {lo['source_statement']}

Generate exactly 3 plausible but incorrect distractors.
Distractors must:
- Be plausible enough that a student who doesn't understand the concept might choose them
- Be clearly wrong to a student who does understand
- Be similar in length and style to the correct answer
- NOT be obviously absurd or unrelated

Return JSON only:
{{
  "distractors": ["wrong answer 1", "wrong answer 2", "wrong answer 3"]
}}
"""
    messages = [
        {
            "role": "system",
            "content": "You generate plausible incorrect distractors for multiple choice questions. Return JSON only. No markdown."
        },
        {"role": "user", "content": prompt}
    ]

    response = call_llm(messages, temperature=0.4)
    response = re.sub(r"^```(?:json)?\s*", "", response.strip())
    response = re.sub(r"\s*```$", "", response).strip()

    try:
        result = json.loads(response)
        distractors = result.get("distractors", [])
        if len(distractors) < 3:
            return lo  # Don't add MC if we didn't get enough distractors

        options = distractors[:3] + [lo["answer"]]
        random.shuffle(options)
        correct_index = options.index(lo["answer"])

        lo["mc_options"] = options
        lo["mc_correct_index"] = correct_index
    except (json.JSONDecodeError, ValueError):
        pass  # MC generation failed silently, LO still usable without it

    return lo


def generate_mc_variants_batch(learning_objects, batch_size=5):
    """
    Generates MC variants for a list of LOs.
    Skips algorithm_step and example types since code answers
    don't work well as multiple choice.
    """
    MC_ELIGIBLE_TYPES = {"definition", "rule", "property", "fact", "constraint", "theorem"}

    results = []
    eligible = [(i, lo) for i, lo in enumerate(learning_objects)
                if lo.get("type") in MC_ELIGIBLE_TYPES]

    print(f"[MC] Generating variants for {len(eligible)} eligible LOs "
          f"({len(learning_objects) - len(eligible)} skipped as code types)...")

    for batch_start in range(0, len(eligible), batch_size):
        batch = eligible[batch_start:batch_start + batch_size]

        los_text = ""
        for idx, (_, lo) in enumerate(batch):
            los_text += f"""
LO {idx + 1}:
Question: {lo['question']}
Correct Answer: {lo['answer']}
Source: {lo['source_statement']}
"""

        prompt = f"""
You are creating multiple choice distractors for learning objects.

IMPORTANT: Generate all distractors in the SAME LANGUAGE as the question and answer.

For each LO below, generate exactly 3 plausible but incorrect distractors.
Distractors must be plausible enough to fool a student who doesn't understand,
but clearly wrong to one who does. Match the length and style of the correct answer.

{los_text}

Return a JSON array with exactly {len(batch)} results in the same order:
[
  {{
    "lo": 1,
    "distractors": ["wrong 1", "wrong 2", "wrong 3"]
  }},
  ...
]

Return JSON array only. No markdown. No explanation.
"""

        messages = [
            {
                "role": "system",
                "content": "You generate plausible incorrect distractors for multiple choice questions. Return JSON array only."
            },
            {"role": "user", "content": prompt}
        ]

        print(f"[MC] Processing batch {batch_start // batch_size + 1} ({len(batch)} LOs)...")
        response = call_llm(messages, temperature=0.4)
        response = re.sub(r"^```(?:json)?\s*", "", response.strip())
        response = re.sub(r"\s*```$", "", response).strip()

        try:
            batch_results = json.loads(response)
            for result in batch_results:
                lo_idx = result.get("lo", 0) - 1
                if lo_idx < 0 or lo_idx >= len(batch):
                    continue
                original_idx, lo = batch[lo_idx]
                distractors = result.get("distractors", [])
                if len(distractors) >= 3:
                    import random
                    options = distractors[:3] + [lo["answer"]]
                    random.shuffle(options)
                    lo["mc_options"] = options
                    lo["mc_correct_index"] = options.index(lo["answer"])
                results.append(original_idx)
        except json.JSONDecodeError as e:
            print(f"[MC] JSON parse error: {e}, skipping batch...")
            continue

    print(f"[MC] Done. {len(results)} LOs got MC variants.")
    return learning_objects

# if __name__ == "__main__":
#     units = load_units("./output/atomic_units.json")
#     #Picking a sample because token limits, this must be enough for a quick demo
#     sample_units = units[:10]
#     learning_objects = []
#     for unit in sample_units:
#         lo = generate_learning_object(unit)
#         learning_objects.append(lo)
#     export_learning_objects(learning_objects, "./output/learning_objects.json")
#     print(f"Generated {len(learning_objects)} learning objects.")