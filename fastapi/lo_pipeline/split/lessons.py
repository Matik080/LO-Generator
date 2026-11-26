"""
Split mode `lessons`: the LO order is cut into `lesson_count` contiguous lessons.

Lessons should be as even as possible: the cost of a lesson is its squared relative deviation
from the average length (total weight / number of lessons). A cut inside a topic (both neighbours
have the same topic label) adds TOPIC_CUT_PENALTY, so cuts at topic boundaries win unless they make
the lessons clearly uneven. The best cuts are found by dynamic programming.

Because lessons are contiguous pieces of the order, they keep its prerequisite order.
"""
from typing import Optional, Sequence

# Moving a cut changes two neighbouring lessons. 0.05 = their cost when both are about 16 % off the
# average, so a topic boundary wins when using it keeps both lessons within about 16 % of the average.
TOPIC_CUT_PENALTY = 0.05


def split_contiguous(weights: Sequence[float], n_lessons: int,
                     topics: Optional[Sequence] = None) -> list[tuple[int, int]]:
    """Cuts positions 0..len(weights)-1 into min(n_lessons, len(weights)) non-empty ranges.

    weights: length of each item in order (e.g. question time); values <= 0 count as 0,
             if all are 0 every item counts as 1.
    topics: optional topic label per item; a cut between two items of the same topic is penalized.
    Returns (start, end) ranges, end exclusive, in order. Empty input -> [].
    """
    n = len(weights)
    if n == 0 or n_lessons < 1:
        return []
    k_max = min(n_lessons, n)
    w = [max(0.0, float(x)) for x in weights]
    if sum(w) == 0:
        w = [1.0] * n
    prefix = [0.0]
    for x in w:
        prefix.append(prefix[-1] + x)
    target = prefix[-1] / k_max

    def lesson_cost(a: int, b: int) -> float:
        return ((prefix[b] - prefix[a] - target) / target) ** 2

    def cut_penalty(k: int) -> float:  # cut before item k
        if topics is not None and topics[k - 1] == topics[k]:
            return TOPIC_CUT_PENALTY
        return 0.0

    inf = float("inf")
    # best[j][b] = lowest cost of items 0..b-1 in j lessons; back[j][b] = start of the last lesson
    best = [[inf] * (n + 1) for _ in range(k_max + 1)]
    back = [[0] * (n + 1) for _ in range(k_max + 1)]
    best[0][0] = 0.0
    for j in range(1, k_max + 1):
        # items left after b must fill the remaining k_max - j lessons
        for b in range(j, n - (k_max - j) + 1):
            for a in range(j - 1, b):
                if best[j - 1][a] == inf:
                    continue
                cost = best[j - 1][a] + lesson_cost(a, b) + (cut_penalty(a) if a > 0 else 0.0)
                if cost < best[j][b] - 1e-12:  # ties -> earliest cut
                    best[j][b], back[j][b] = cost, a
    ranges, b = [], n
    for j in range(k_max, 0, -1):
        a = back[j][b]
        ranges.append((a, b))
        b = a
        
    return ranges[::-1]
