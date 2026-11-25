"""Tasks without an answer (`no_answer`) from LOs that share a concept (sprint 2, task 1c).

Runs at the end of the `questions` stage. The core (units, LOs, evaluation, graph) is not changed.

An atomic LO holds one fact, so a task that asks the student to explain, compare or apply needs
several of them. The graph step gives every LO an `anchor` (its most central concept in the concept
graph, graph/builder.py). LOs with the same anchor form a group:
  - only anchors with at least MIN_GROUP LOs (one fact makes a poor essay task),
  - in pipeline order; a big group is split into near-equal chunks of at most `group_size` LOs,
    so every task works with a few statements (an anchor with 20 LOs gives 4 tasks).
For every group the LLM writes one task that combines the group's source statements, with a short
name, a hint that does not give the answer away and time estimates. A task without text or name
is dropped; an answer that is not valid JSON drops the tasks of its batch.

Result: a list of tasks
    {"anchor", "lo_indices" (positions in learning_objects), "description", "name", "hint",
     "time_to_handle", "time_to_explain"}
The units output (questions/units_output.py) turns them into questions of type no_answer.
"""

import logging
from collections import OrderedDict
from typing import Any, Dict, List

from lo_pipeline.llm.json_load import parse_json
from lo_pipeline.llm.service import call_llm

logger = logging.getLogger(__name__)

MIN_GROUP = 2


def _plain(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def anchor_groups(learning_objects: List[Dict[str, Any]], group_size: int = 6) -> List[Dict[str, Any]]:
    """[{"anchor": "pointer", "lo_indices": [0, 3, 7]}, …] in the order of each group's first LO."""
    by_anchor: "OrderedDict[str, List[int]]" = OrderedDict()
    for i, lo in enumerate(learning_objects):
        anchor = _plain(lo.get("anchor"))
        if anchor and _plain(lo.get("source_statement")):
            by_anchor.setdefault(anchor, []).append(i)
    groups = []
    for anchor, indices in by_anchor.items():
        if len(indices) < MIN_GROUP:
            continue
        parts = -(-len(indices) // group_size)  # ceil: number of chunks
        start = 0
        for p in range(parts):
            size = len(indices) // parts + (1 if p < len(indices) % parts else 0)
            groups.append({"anchor": anchor, "lo_indices": indices[start:start + size]})
            start += size
    return groups


def _prompt(batch: List[Dict[str, Any]], learning_objects: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    groups_text = ""
    for g, group in enumerate(batch):
        statements = "\n".join(f"  - {learning_objects[i]['source_statement']}" for i in group["lo_indices"])
        groups_text += f"""
GROUP {g + 1}:
Concept: {group['anchor']}
Statements:
{statements}
"""
    prompt = f"""
You are writing open learning tasks that are not graded automatically (essays, explanations,
comparisons, applying the idea to an own example).

IMPORTANT: Write each task in the SAME LANGUAGE as its statements.

For each group below write ONE task that:
- makes the student combine several of the group's statements, not repeat a single one,
- can be done using only these statements (no outside facts needed),
- is clear about what the student should produce (e.g. "Explain …", "Compare …", "Give an example …"),
- has no single correct answer to check, but a teacher can judge it against the statements.

Also write:
- "name": a short title of the task (3-8 words),
- "hint": one sentence that helps the student start, without giving the answer away,
- "time_to_handle": seconds a student needs to do the task,
- "time_to_explain": seconds a teacher needs to discuss it afterwards.

{groups_text}

Return a JSON array with exactly {len(batch)} results in the same order:
[
  {{"group": 1, "task": "...", "name": "...", "hint": "...", "time_to_handle": <int>, "time_to_explain": <int>}},
  ...
]

Return JSON array only. No markdown. No explanation.
"""
    return [
        {"role": "system",
         "content": "You write open, ungraded learning tasks from given statements. Return JSON array only."},
        {"role": "user", "content": prompt},
    ]


def generate_no_answer_tasks(learning_objects: List[Dict[str, Any]], batch_size: int = 5,
                             group_size: int = 6, progress=None) -> List[Dict[str, Any]]:
    """One no_answer task per anchor group (see the module docstring), in group order.

    `progress(done, total)` is called after each LLM batch. Batches run in parallel (llm/parallel.py).
    """
    from lo_pipeline.llm.parallel import chunks, map_batches

    groups = anchor_groups(learning_objects, group_size)
    logger.info(f"[NoAnswer] Writing tasks for {len(groups)} concept group(s)...")

    def one_batch(batch):
        response = call_llm(_prompt(batch, learning_objects), temperature=0.4)
        if not response:
            return []
        try:
            results = parse_json(response)
        except ValueError:  # json.JSONDecodeError is a ValueError
            logger.warning("[NoAnswer] Answer is not valid JSON; tasks of this batch dropped.")
            return []
        if not isinstance(results, list):
            return []
        tasks = {}
        for result in results:
            if not isinstance(result, dict):
                continue
            g = result.get("group")
            if not isinstance(g, int) or not 1 <= g <= len(batch) or g in tasks:
                continue
            description, name = _plain(result.get("task")), _plain(result.get("name"))
            if not description or not name:
                continue
            group = batch[g - 1]
            tasks[g] = {
                "anchor": group["anchor"],
                "lo_indices": list(group["lo_indices"]),
                "description": description,
                "name": name,
                "hint": _plain(result.get("hint")),
                "time_to_handle": result.get("time_to_handle"),
                "time_to_explain": result.get("time_to_explain"),
            }
        return [tasks[g] for g in sorted(tasks)]  # group order, whatever order the LLM used

    tasks = [task for batch_tasks in map_batches(one_batch, list(chunks(groups, batch_size)), progress)
             for task in batch_tasks]
    logger.info(f"[NoAnswer] Done. {len(tasks)} task(s) for {len(groups)} group(s).")
    return tasks
