"""
atomic_unit_merger.py

Merges atomic units that are near-duplicate based on embedding similarity.
Two units with cosine similarity >= MERGE_THRESHOLD are submitted to the LLM,
which produces a single merged unit that preserves all unique information.

This runs AFTER extraction and cleaning, BEFORE LO generation.
"""

import json
import re
from typing import List, Dict, Any, Tuple

import numpy as np

from embedding_service import get_embeddings_batch, cosine_similarity
from llm_service import call_llm

# Cosine similarity threshold for merging
MERGE_THRESHOLD = 0.90


def _find_merge_pairs(units: List[Dict[str, Any]], threshold: float = MERGE_THRESHOLD) -> List[Tuple[int, int]]:
    """
    Returns a list of (i, j) index pairs where similarity >= threshold.
    Uses greedy grouping: once a unit is flagged for merging, it is not
    paired again (avoids transitive chain explosions).
    """
    if len(units) < 2:
        return []

    statements = [u.get("statement", "") for u in units]
    embeddings = get_embeddings_batch(statements)

    pairs = []
    merged_indices = set()

    for i in range(len(units)):
        if i in merged_indices:
            continue
        for j in range(i + 1, len(units)):
            if j in merged_indices:
                continue
            sim = cosine_similarity(embeddings[i], embeddings[j])
            if sim >= threshold:
                pairs.append((i, j))
                merged_indices.add(j)  # j will be absorbed into i
                break # i merges with the first match only, re-scan after merge

    return pairs


def _merge_pair_with_llm(unit_a: Dict[str, Any], unit_b: Dict[str, Any]) -> Dict[str, Any]:
    """
    Asks the LLM to merge two near-duplicate atomic units into one.
    Falls back to keeping unit_a if the LLM response is malformed.
    """
    prompt = f"""You are merging two near-duplicate knowledge units into a single, cleaner unit.

Unit A:
  Type: {unit_a.get('type', 'fact')}
  Statement: {unit_a.get('statement', '')}
  Concepts: {unit_a.get('concepts', [])}

Unit B:
  Type: {unit_b.get('type', 'fact')}
  Statement: {unit_b.get('statement', '')}
  Concepts: {unit_b.get('concepts', [])}

Instructions:
- Produce ONE merged unit that preserves all unique information from both.
- If the statements say the same thing, keep the clearer, more complete phrasing.
- The merged type should be the more specific of the two (prefer definition > fact, rule > fact, etc.).
- Combine the concept lists, removing duplicates.
- Keep source_title and source_page from whichever unit has them (prefer A if both have them).

Return a JSON object only, no markdown, no explanation:
{{
  "type": "<type>",
  "statement": "<merged statement>",
  "concepts": ["<concept1>", ...],
  "source_title": "<title or empty string>",
  "source_page": "<page or empty string>"
}}
"""
    messages = [
        {"role": "system", "content": "You merge near-duplicate knowledge units. Return JSON only."},
        {"role": "user", "content": prompt},
    ]

    try:
        response = call_llm(messages, temperature=0.0)
        if not response:
            return unit_a

        # Strip markdown fences if present
        cleaned = re.sub(r"^```(?:json)?\s*", "", response.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

        merged = json.loads(cleaned)

        # Carry over fields that the LLM doesn't know about
        merged["id"] = unit_a.get("id", "")
        merged["normalized_concepts"] = list(
            set(unit_a.get("normalized_concepts", [])) | set(unit_b.get("normalized_concepts", []))
        )
        # Prefer unit_a's source if LLM left it blank
        if not merged.get("source_title"):
            merged["source_title"] = unit_a.get("source_title", "") or unit_b.get("source_title", "")
        if not merged.get("source_page"):
            merged["source_page"] = unit_a.get("source_page", "") or unit_b.get("source_page", "")

        return merged

    except (json.JSONDecodeError, Exception) as e:
        print(f"[Merger] LLM merge failed ({e}), keeping unit A.")
        return unit_a


def merge_similar_units(
    units: List[Dict[str, Any]],
    threshold: float = MERGE_THRESHOLD,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Main entry point. Iteratively merges near-duplicate units until no more
    pairs above `threshold` remain.

    Returns:
        (merged_units, n_merges_performed)
    """
    if not units:
        return units, 0

    total_merges = 0
    current = list(units)

    # Iterate until stable (merged units might now be similar to others)
    max_passes = 10
    for pass_num in range(max_passes):
        pairs = _find_merge_pairs(current, threshold)
        if not pairs:
            break

        print(f"[Merger] Pass {pass_num + 1}: merging {len(pairs)} pair(s) "
              f"(threshold={threshold}, units before={len(current)})")

        # Build index of units to remove (absorbed into their pair partner)
        absorbed = set()
        result = []

        # Map j -> i for pairs
        merge_map = {j: i for i, j in pairs}

        for idx, unit in enumerate(current):
            if idx in absorbed:
                continue
            if idx in merge_map.values():
                # This is the 'i' side, find its partner and merge
                partner_idx = next(j for i, j in pairs if i == idx)
                merged = _merge_pair_with_llm(unit, current[partner_idx])
                result.append(merged)
                absorbed.add(partner_idx)
                total_merges += 1
            else:
                result.append(unit)

        current = result
        print(f"[Merger] After pass {pass_num + 1}: {len(current)} units remaining.")

    return current, total_merges