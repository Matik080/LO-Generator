"""
Topics = communities in B's concept graph (Louvain), and split mode `topics`.

- Each LO gets the topic of its `anchor` concept; if the anchor is not in the graph, the first of its
  keywords that is; otherwise the topic whose LOs are most similar (MiniLM, assign_missing_topics).
  Only if there are no topics at all, an LO takes the topic of its neighbour in the lesson order.
- A topic's name is the concept of the topic closest to its LOs by MiniLM (`topic` in the report).
- `topics` mode: the LOs are ordered by prerequisites, keeping each topic together where possible
  (order.lo_order with groups). Runs of the same topic become lessons; a run with fewer than
  MIN_LESSON_LOS LOs is merged into its smaller neighbour, a run with more than MAX_LESSON_LOS
  is cut into even parts (lessons.split_contiguous).
"""
import math
from collections import Counter
from typing import Callable, Optional, Sequence

import networkx as nx
import numpy as np

from lo_pipeline.split.lessons import split_contiguous
from lo_pipeline.split.order import lo_order

MIN_LESSON_LOS = 3
MAX_LESSON_LOS = 15


def concept_communities(concept_graph: dict, seed: int = 0) -> dict[str, int]:
    """Node-link JSON of the concept graph -> {concept: community number}. Deterministic (fixed seed)."""
    G = nx.Graph()
    G.add_nodes_from(n["id"] for n in concept_graph.get("nodes") or [])
    G.add_edges_from((e["source"], e["target"], {"weight": e.get("weight", 1)})
                     for e in concept_graph.get("edges") or [])
    if G.number_of_nodes() == 0:
        return {}
    communities = nx.community.louvain_communities(G, weight="weight", seed=seed)
    # number communities by their smallest member, so the numbers do not depend on Louvain's order
    communities = sorted(communities, key=lambda c: min(map(str, c)))
    return {node: k for k, members in enumerate(communities) for node in members}


def lo_topics(los: list[dict], communities: dict[str, int]) -> list[Optional[int]]:
    """Topic per LO from its anchor, else from its first keyword in the graph; None if neither."""
    topics = []
    for lo in los:
        concepts = [lo.get("anchor")] + [k.lower() for k in lo.get("keywords") or [] if isinstance(k, str)]
        topics.append(next((communities[c] for c in concepts if c in communities), None))
    return topics


def _default_embed():
    from lo_pipeline.llm.embeddings import get_embeddings_batch
    return get_embeddings_batch


def assign_missing_topics(los: list[dict], topics: list[Optional[int]],
                          embed: Optional[Callable[[list[str]], list]] = None) -> list[Optional[int]]:
    """LOs without a topic get the topic whose LOs are most similar on average (MiniLM, question + answer).

    Only LOs that have their own topic (from anchor or keyword) form the topic averages.
    If no LO has a topic, nothing changes.
    """
    missing = [i for i, t in enumerate(topics) if t is None]
    known = sorted({t for t in topics if t is not None})
    if not missing or not known:
        return list(topics)
    vectors = [_unit(v) for v in (embed or _default_embed())([_lo_text(lo) for lo in los])]
    centres = {t: _unit(np.mean([vectors[i] for i, x in enumerate(topics) if x == t], axis=0)) for t in known}
    filled = list(topics)
    for i in missing:
        filled[i] = max(known, key=lambda t: (float(vectors[i] @ centres[t]), -t))  # ties -> lower number
    return filled


def fill_missing_topics(order: Sequence[int], topics: list[Optional[int]]) -> list[Optional[int]]:
    """LOs without a topic take the topic of the LO before them in `order` (the first ones: after them)."""
    filled = list(topics)
    previous = None
    for i in order:
        if filled[i] is None:
            filled[i] = previous
        previous = filled[i]
    following = None
    for i in reversed(order):
        if filled[i] is None:
            filled[i] = following
        following = filled[i]
    return filled


def _lo_text(lo: dict) -> str:
    return f"{lo.get('question') or ''} {lo.get('answer') or ''}".strip()


def topic_names(los: list[dict], topics: Sequence[Optional[int]], communities: dict[str, int],
                embed: Optional[Callable[[list[str]], list]] = None) -> dict[int, str]:
    """{topic: name}: the concept that best describes the topic's LOs.

    Candidates = anchors and keywords of the topic's LOs that belong to its community. Score = number of
    the topic's LOs that have the candidate × MiniLM similarity of the candidate to the average embedding
    of the LOs' question + answer. The count prefers what most LOs share; the similarity keeps broad or
    vague words ("probability", "formula") from winning over the concept the LOs are about ("entropy").
    Without candidates: the anchor most LOs of the topic share (ties: first in pipeline order).
    `embed` = texts -> vectors (default: get_embeddings_batch).
    """
    embed = embed or _default_embed()
    members: dict[int, list[int]] = {}
    for i, t in enumerate(topics):
        if t is not None:
            members.setdefault(t, []).append(i)
    names = {}
    for t, idx in members.items():
        share = Counter()  # candidate -> number of the topic's LOs that have it (anchor or keyword)
        for i in idx:
            concepts = [los[i].get("anchor")] + [k.lower() for k in los[i].get("keywords") or [] if isinstance(k, str)]
            share.update(list(dict.fromkeys(c for c in concepts if c and communities.get(c) == t)))  # once per LO
        candidates = list(share)
        if candidates:
            centre = _unit(np.mean([_unit(v) for v in embed([_lo_text(los[i]) for i in idx])], axis=0))
            scores = [share[c] * float(_unit(v) @ centre) for c, v in zip(candidates, embed(candidates))]
            names[t] = candidates[int(np.argmax(scores))]  # ties -> first candidate
        else:
            anchors = Counter(los[i]["anchor"] for i in idx if los[i].get("anchor"))
            if anchors:
                names[t] = anchors.most_common(1)[0][0]
    return names


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    norm = np.linalg.norm(v)
    return v / norm if norm else v


def _runs(order: Sequence[int], topics: Sequence) -> list[list[int]]:
    """Consecutive LOs of the same topic in `order` -> lists of LO indices."""
    runs: list[list[int]] = []
    for i in order:
        if runs and topics[runs[-1][-1]] == topics[i]:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def _merge_small(runs: list[list[int]], min_los: int) -> list[list[int]]:
    """The smallest run below min_los joins its smaller neighbour, until none is left (or one run)."""
    runs = [list(r) for r in runs]
    while len(runs) > 1:
        small = [k for k, r in enumerate(runs) if len(r) < min_los]
        if not small:
            break
        k = min(small, key=lambda k: (len(runs[k]), k))
        if k == 0:
            other = 1
        elif k == len(runs) - 1:
            other = k - 1
        else:
            other = k - 1 if len(runs[k - 1]) <= len(runs[k + 1]) else k + 1
        a, b = sorted((k, other))
        runs[a:b + 1] = [runs[a] + runs[b]]
    return runs


def split_by_topics(los: list[dict], topics: list[Optional[int]],
                    min_los: int = MIN_LESSON_LOS, max_los: int = MAX_LESSON_LOS,
                    ) -> tuple[list[list[int]], list[Optional[int]], int]:
    """Lessons for mode `topics`; the limits count LOs, so a big topic is cut into parts of even LO count.

    Returns (lessons as lists of LO indices in order, topic per LO with gaps filled, cycle breaks).
    """
    if not los:
        return [], list(topics), 0
    # LOs still without a topic are not grouped together (each is its own group)
    groups = [t if t is not None else ("none", i) for i, t in enumerate(topics)]
    order, cycle_breaks = lo_order(los, groups=groups)
    filled = fill_missing_topics(order, topics)
    lessons = []
    for run in _merge_small(_runs(order, filled), min_los):
        parts = math.ceil(len(run) / max_los)
        for a, b in split_contiguous([1] * len(run), parts):
            lessons.append(run[a:b])
    return lessons, filled, cycle_breaks
