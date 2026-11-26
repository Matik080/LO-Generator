"""
Order of the learning objects for lessons: topological order of the prerequisite graph.

Every LO comes after its prerequisites (`prerequisite_ids`). When several LOs could come next,
the one from the earlier source file, then the earlier page, then the earlier pipeline position
goes first. The graph step already removes cycles; if one is still there (e.g. two LOs with the
same id), the LO that would come first by source and page is placed anyway and the case is counted.
"""
import heapq


def _page(lo: dict) -> float:
    try:
        return int(lo.get("source_page"))
    except (TypeError, ValueError):
        return float("inf")  # no page -> after the LOs with a page from the same file


def _sort_keys(los: list[dict]) -> list[tuple]:
    """(file rank, page, pipeline index) per LO; file rank = order of first appearance."""
    file_rank: dict = {}
    for lo in los:
        file_rank.setdefault(lo.get("source_title"), len(file_rank))
    return [(file_rank[lo.get("source_title")], _page(lo), i) for i, lo in enumerate(los)]


def prerequisite_indices(los: list[dict]) -> list[set[int]]:
    """For each LO the indices of its prerequisites (unknown ids and self-references ignored).

    An id shared by several LOs points to the first of them, as in the report's `sections`.
    """
    first_index: dict = {}
    for i, lo in enumerate(los):
        first_index.setdefault(lo.get("id"), i)
    prereqs = []
    for i, lo in enumerate(los):
        found = {first_index[p] for p in lo.get("prerequisite_ids") or [] if p in first_index}
        found.discard(i)
        prereqs.append(found)
    return prereqs


def lo_order(los: list[dict]) -> tuple[list[int], int]:
    """Returns (LO indices in lesson order, number of LOs placed before a prerequisite due to a cycle)."""
    keys = _sort_keys(los)
    prereqs = prerequisite_indices(los)
    dependents: list[list[int]] = [[] for _ in los]
    missing = [len(p) for p in prereqs]  # prerequisites not placed yet
    for i, ps in enumerate(prereqs):
        for p in ps:
            dependents[p].append(i)

    ready = [keys[i] for i in range(len(los)) if missing[i] == 0]
    heapq.heapify(ready)
    placed = [False] * len(los)
    order, cycle_breaks = [], 0
    while len(order) < len(los):
        if ready:
            i = heapq.heappop(ready)[2]
        else:  # only cycles left: take the earliest remaining LO
            i = min((keys[j] for j in range(len(los)) if not placed[j]))[2]
            cycle_breaks += 1
        placed[i] = True
        order.append(i)
        for d in dependents[i]:
            missing[d] -= 1
            if missing[d] == 0 and not placed[d]:
                heapq.heappush(ready, keys[d])
    return order, cycle_breaks


def prerequisite_violations(los: list[dict], order: list[int]) -> int:
    """Number of (LO, prerequisite) pairs where the prerequisite comes later in `order`."""
    position = {i: n for n, i in enumerate(order)}
    return sum(1 for i, ps in enumerate(prerequisite_indices(los))
               for p in ps if i in position and p in position and position[p] > position[i])
