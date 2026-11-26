"""Lesson order of LOs: prerequisites first, then source file, page and pipeline position."""
from lo_pipeline.split.order import lo_order, prerequisite_violations


def lo(id_, file="a.pdf", page=1, prereqs=()):
    return {"id": id_, "source_title": file, "source_page": page, "prerequisite_ids": list(prereqs)}


def test_empty():
    assert lo_order([]) == ([], 0)


def test_no_prerequisites_keeps_file_page_and_position_order():
    los = [lo("a", page=3), lo("b", page=1), lo("c", file="b.pdf", page=1), lo("d", page=1)]
    order, breaks = lo_order(los)
    # a.pdf first (it appears first), pages ascending, ties by position
    assert order == [1, 3, 0, 2]
    assert breaks == 0


def test_missing_page_goes_last_within_its_file():
    los = [lo("a", page=None), lo("b", page="2"), lo("c", file="b.pdf", page=1)]
    assert lo_order(los)[0] == [1, 0, 2]


def test_prerequisite_comes_first_even_from_a_later_page():
    los = [lo("a", page=1, prereqs=["b"]), lo("b", page=5)]
    order, breaks = lo_order(los)
    assert order == [1, 0]
    assert breaks == 0
    assert prerequisite_violations(los, order) == 0


def test_chain_and_unrelated_los_interleave_by_page():
    los = [lo("a", page=1), lo("b", page=2, prereqs=["d"]), lo("c", page=3), lo("d", page=4)]
    order, _ = lo_order(los)
    assert order == [0, 2, 3, 1]
    assert prerequisite_violations(los, order) == 0


def test_two_files_keep_file_order_and_cross_file_prerequisite_wins():
    # b.pdf page 1 does not jump ahead of a.pdf; a p2 waits for its prerequisite b p4
    los = [lo("a1", "a.pdf", 1), lo("a2", "a.pdf", 2, prereqs=["b4"]), lo("a3", "a.pdf", 3),
           lo("b1", "b.pdf", 1), lo("b4", "b.pdf", 4)]
    order, breaks = lo_order(los)
    assert [los[i]["id"] for i in order] == ["a1", "a3", "b1", "b4", "a2"]
    assert breaks == 0
    assert prerequisite_violations(los, order) == 0


def test_random_graphs_never_violate_prerequisites():
    # prerequisites always win over file and page: random DAGs whose edges ignore pages and files
    import random
    rng = random.Random(0)
    for _ in range(200):
        n = rng.randint(1, 30)
        rank = list(range(n))
        rng.shuffle(rank)  # edge p -> i only when rank[p] < rank[i], so the graph has no cycle
        los = [lo(f"id{i}", rng.choice(["a.pdf", "b.pdf", "c.pdf"]), rng.choice([None, 1, 2, 3, 7]))
               for i in range(n)]
        for i in range(n):
            los[i]["prerequisite_ids"] = [f"id{p}" for p in range(n)
                                          if rank[p] < rank[i] and rng.random() < 0.2]
        order, breaks = lo_order(los)
        assert sorted(order) == list(range(n))
        assert breaks == 0
        assert prerequisite_violations(los, order) == 0


def test_unknown_and_self_prerequisites_are_ignored():
    los = [lo("a", prereqs=["a", "zzz"]), lo("b")]
    assert lo_order(los) == ([0, 1], 0)


def test_cycle_is_broken_at_the_earliest_lo_and_counted():
    # c has no prerequisites, so it is placed first; then the cycle a <-> b is broken at a (page 1)
    los = [lo("a", page=1, prereqs=["b"]), lo("b", page=2, prereqs=["a"]), lo("c", page=3)]
    order, breaks = lo_order(los)
    assert order == [2, 0, 1]
    assert breaks == 1
    assert prerequisite_violations(los, order) == 1


def test_shared_id_points_to_first_lo():
    los = [lo("x", page=1), lo("x", page=2), lo("y", page=0, prereqs=["x"])]
    order, breaks = lo_order(los)
    assert order.index(0) < order.index(2)
    assert breaks == 0
    assert sorted(order) == [0, 1, 2]
