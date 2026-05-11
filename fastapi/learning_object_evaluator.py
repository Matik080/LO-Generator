import json
import re
from utilities import load_json, export_json
from llm_service import call_llm

# Per-model batch sizes, GPT OSS truncates on long prompts, Kimi rate limits
MODEL_BATCH_SIZES = {
    "openai/gpt-oss-120b": 2,
    "moonshotai/kimi-k2-instruct": 3,
}
DEFAULT_BATCH_SIZE = 8

# Extra delay between batches for models that struggle with back-to-back calls
MODEL_INTER_BATCH_DELAY = {
    "moonshotai/kimi-k2-instruct": 1,
    "openai/gpt-oss-120b": 1,
}

# Max chars per field to avoid blowing context on code-heavy LOs
MAX_FIELD_LEN = 400


def _truncate(text, max_len=MAX_FIELD_LEN):
    if not text:
        return ""
    return text if len(text) <= max_len else text[:max_len] + "…"


def _recover_partial_json(raw: str) -> list:
    """
    If the response is a truncated JSON array, recover whatever complete objects were returned before the truncation point.
    """
    objects = []
    # Find all complete {...} blocks at the top level
    depth = 0
    start = None
    for i, ch in enumerate(raw):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(raw[start:i+1])
                    objects.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None
    return objects


def evaluate_learning_objects_batch(learning_objects, model, batch_size=None):
    if batch_size is None:
        batch_size = MODEL_BATCH_SIZES.get(model, DEFAULT_BATCH_SIZE)

    all_evaluations = []

    for batch_start in range(0, len(learning_objects), batch_size):
        batch = learning_objects[batch_start:batch_start + batch_size]

        los_text = ""
        for idx, lo in enumerate(batch):
            has_hint = bool(lo.get("hint", "").strip())
            has_mc = bool(lo.get("mc_options"))
            los_text += f"""
Learning Object {idx + 1}:
  Type: {lo.get('type', '')}
  Question: {_truncate(lo.get('question', ''))}
  Answer: {_truncate(lo.get('answer', ''))}
  Source Statement: {_truncate(lo.get('source_statement', ''))}
  Hint: {_truncate(lo.get('hint', '(none)'))}
  Has hint: {'yes' if has_hint else 'no'}
  Has MC options: {'yes' if has_mc else 'no'}
  MC Options: {lo.get('mc_options', None)}
"""

        prompt = f"""
You are evaluating learning objects using the exact rubric below.
Apply every criterion mechanically — do not substitute your own judgment.
Follow the decision rules exactly as written.

----------------------
CRITERION 1 — FAITHFULNESS (1–5)
----------------------
Compare the answer ONLY to the source statement. Ignore all other knowledge.

Score the answer as follows:
5 → Every fact in the answer appears verbatim or is a direct restatement of the source.
    No new terms, no added context, no inferred steps.
4 → The answer contains one minor elaboration (e.g. a synonym, a brief clarifying phrase)
    that is not in the source but does not change the meaning.
3 → The answer adds one non-trivial fact or step that is absent from the source
    (e.g. mentions a second condition, adds a consequence not stated).
2 → The answer adds multiple facts not in the source, or makes a claim the source
    does not support.
1 → The answer contradicts the source, or ignores it almost entirely.

DECISION RULE: If you are unsure whether an addition is "minor", score it 3, not 4.

----------------------
CRITERION 2 — QUESTION CLARITY (1–5)
----------------------
Does the question ask for exactly one thing in unambiguous language?

5 → Grammatically correct, single unambiguous task, no missing context.
4 → One wording issue (awkward phrasing, minor typo) that does not affect meaning.
3 → Could be interpreted in two distinct ways, but one interpretation is clearly dominant.
2 → Two or more equally plausible interpretations; a student would be confused about what to answer.
1 → Incomprehensible, self-contradictory, or missing the subject entirely.

----------------------
CRITERION 3 — HINT QUALITY (1–5 or null)
----------------------
FIRST: Check the "Has hint" field.
If "Has hint: no" → set hint_quality to null. Do not score it. Stop here.

If a hint exists, score it:
5 → Names a specific concept, function, or term directly relevant to the answer,
    without stating the answer itself. A student who reads it knows WHERE to look.
4 → Points to the correct general topic or category but not the specific concept needed.
3 → Relevant to the question domain but too vague to narrow down the answer,
    OR slightly too specific (gives away a key term that appears in the answer).
2 → States the answer outright, or is about a completely unrelated concept.
1 → Actively misleading — points the student toward a wrong concept.

DECISION RULE: A hint that simply restates the question in different words scores 3.
A hint that contains the answer word-for-word scores 2.

----------------------
CRITERION 4 — SELF-CONTAINED (yes/no)
----------------------
Can a student answer this question using ONLY the text of the question itself?

Answer YES if: the question defines all terms it uses, or the terms are standard
    vocabulary a student in this course would know.
Answer NO if: the question references specific code, a figure, a table, an example,
    or external context that is not reproduced in the question text.

DECISION RULE: Treat course-level concepts (e.g. "linear regression", "a for loop")
as known vocabulary — do not mark NO just because a concept is not defined in the question.
Only mark NO if answering requires seeing something not present in the question text.

----------------------
CRITERION 5 — ATOMIC (yes/no)
----------------------
Does the question test exactly ONE concept or skill?

Answer YES if: removing any part of the question makes it unanswerable or trivial.
Answer NO if: the question has two independent sub-questions, OR answering it
    requires demonstrating knowledge of two unrelated concepts.

DECISION RULE: A question that applies one concept to one example is atomic (YES).
A question asking "what is X and why is it used" is NOT atomic (NO) — it asks two things.

----------------------
CRITERION 6 — DISTRACTOR PLAUSIBILITY (1–3 or null)
----------------------
FIRST: Check the "Has MC options" field.
If "Has MC options: no" → set distractor_plausibility to null. Stop here.

If MC options exist, evaluate ONLY the wrong answers:
3 → Every wrong answer could reasonably be chosen by a student who partially understands
    the material. No wrong answer is obviously absurd.
2 → At least one wrong answer is plausible, but at least one can be eliminated without
    any knowledge of the material (e.g. clearly off-topic, nonsensical).
1 → All wrong answers are obviously incorrect. A student with no knowledge
    could identify the correct answer by elimination alone.

----------------------

Learning objects to evaluate:
{los_text}

Return a JSON array with exactly {len(batch)} objects, one per learning object, in order:
[
  {{
    "faithfulness": <int 1-5>,
    "question_clarity": <int 1-5>,
    "hint_quality": <int 1-5 or null>,
    "self_contained": "yes" or "no",
    "atomic": "yes" or "no",
    "distractor_plausibility": <int 1-3 or null>
  }},
  ...
]

Return JSON array only. No markdown. No explanation.
"""

        messages = [
            {
                "role": "system",
                "content": (
                    "You evaluate learning objects using a mechanical rubric. "
                    "Apply every decision rule exactly as written. "
                    "For hint_quality: if 'Has hint: no', output null — never score a missing hint. "
                    "For distractor_plausibility: if 'Has MC options: no', output null. "
                    "Return a JSON array only. No markdown. No explanation."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        print(f"[Eval] Batch {batch_start // batch_size + 1} ({len(batch)} LOs) with {model}...")
        delay = MODEL_INTER_BATCH_DELAY.get(model, 0)
        if delay > 0 and batch_start > 0:
            import time
            time.sleep(delay)
        response = call_llm(messages, model=model, temperature=0.0)

        response = re.sub(r"^```(?:json)?\s*", "", response.strip())
        response = re.sub(r"\s*```$", "", response).strip()

        # Attempt 1: strict parse
        batch_results = None
        try:
            batch_results = json.loads(response)
        except json.JSONDecodeError:
            pass

        # Attempt 2: fix common GPT formatting issues then re-parse
        if batch_results is None:
            try:
                cleaned = response
                # Remove trailing commas before ] or }
                cleaned = re.sub(r',\s*([\]\}])', r'\1', cleaned)
                # Remove JS-style line comments
                cleaned = re.sub(r'//[^\n]*', '', cleaned)
                batch_results = json.loads(cleaned)
                print(f"[Eval] Parsed after cleanup.")
            except json.JSONDecodeError as e:
                print(f"[Eval] JSON parse error: {e}, attempting partial recovery...")
                batch_results = _recover_partial_json(response)
                if batch_results:
                    print(f"[Eval] Recovered {len(batch_results)} / {len(batch)} results.")
                else:
                    print(f"[Eval] Recovery failed, skipping batch.")
                    continue

        if not isinstance(batch_results, list):
            print(f"[Eval] Expected list, got {type(batch_results)}, skipping batch...")
            continue

        for result, lo in zip(batch_results, batch):
            result["model"] = model
            result["lo_id"] = lo["id"]
            all_evaluations.append(result)

    return all_evaluations


if __name__ == "__main__":
    learning_objects = load_json("./output/learning_objects.json")

    models = [
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile",
    ]

    evaluations = []
    for model in models:
        print(f"\nRunning evaluation with {model}")
        batch_evals = evaluate_learning_objects_batch(learning_objects, model)
        evaluations.extend(batch_evals)

    export_json(evaluations, "./output/evaluations_final_metrics.json")

    # Retry missing
    max_attempts = 3
    for attempt in range(max_attempts):
        missing = []
        for model in models:
            evaluated_ids = {d["lo_id"] for d in evaluations if d["model"] == model}
            missing_los = [lo for lo in learning_objects if lo["id"] not in evaluated_ids]
            if missing_los:
                print(f"[Retry {attempt+1}] {model} missing {len(missing_los)}, retrying...")
                new_evals = evaluate_learning_objects_batch(missing_los, model)
                evaluations.extend(new_evals)
                missing.extend(new_evals)
        if not missing:
            print("All evaluations complete.")
            break
        export_json(evaluations, "./output/evaluations_final_metrics.json")

    for model in models:
        count = sum(1 for d in evaluations if d["model"] == model)
        print(f"{model}: {count}/{len(learning_objects)}")

    export_json(evaluations, "./output/evaluations_final_metrics.json")
    print(f"Total evaluations: {len(evaluations)}")