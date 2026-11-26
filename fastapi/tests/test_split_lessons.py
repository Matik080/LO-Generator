"""Split mode `lessons`: contiguous, non-empty, even lessons; cuts preferred at topic boundaries."""
import random

from lo_pipeline.split.lessons import split_contiguous


def sizes(ranges, weights):
    return [sum(weights[a:b]) for a, b in ranges]


def test_empty_input():
    assert split_contiguous([], 3) == []


def test_one_lesson_takes_everything():
    assert split_contiguous([1, 2, 3], 1) == [(0, 3)]


def test_equal_weights_split_evenly():
    assert split_contiguous([1] * 6, 3) == [(0, 2), (2, 4), (4, 6)]


def test_uneven_weights_balance_by_weight_not_count():
    # one long LO alone vs three short ones together
    w = [9, 3, 3, 3]
    assert split_contiguous(w, 2) == [(0, 1), (1, 4)]


def test_more_lessons_than_items_gives_one_item_each():
    assert split_contiguous([1, 1], 5) == [(0, 1), (1, 2)]


def test_zero_weights_count_as_one():
    assert split_contiguous([0, 0, 0, 0], 2) == [(0, 2), (2, 4)]


def test_cut_moves_to_topic_boundary_when_balance_is_close():
    # without topics the cut is in the middle (10 | 10); the boundary after item 9 gives 9 | 11
    # (both lessons 10 % off the average), which is worth it
    w = [1] * 20
    assert split_contiguous(w, 2) == [(0, 10), (10, 20)]
    topics = ["A"] * 9 + ["B"] * 11
    assert split_contiguous(w, 2, topics) == [(0, 9), (9, 20)]


def test_topic_boundary_ignored_when_lessons_would_be_very_uneven():
    # the only boundary is after item 2 (2 | 4, both 33 % off): too uneven, the cut goes inside B
    w = [1] * 6
    topics = ["A", "A", "B", "B", "B", "B"]
    assert split_contiguous(w, 2, topics) == [(0, 3), (3, 6)]


def test_random_inputs_give_exact_count_of_contiguous_nonempty_lessons():
    rng = random.Random(0)
    for _ in range(200):
        n = rng.randint(1, 40)
        k = rng.randint(1, 12)
        w = [rng.choice([0, 30, 120, 300, 420]) for _ in range(n)]
        topics = [rng.choice("ABC") for _ in range(n)] if rng.random() < 0.5 else None
        ranges = split_contiguous(w, k, topics)
        assert len(ranges) == min(k, n)
        assert ranges[0][0] == 0 and ranges[-1][1] == n
        for (a, b), (c, _) in zip(ranges, ranges[1:]):
            assert b == c
        assert all(a < b for a, b in ranges)
