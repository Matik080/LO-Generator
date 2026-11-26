"""Topics from the concept graph and split mode `topics`."""
import random

from lo_pipeline.split.order import lo_order, prerequisite_violations
from lo_pipeline.split.topics import (
    assign_missing_topics, concept_communities, fill_missing_topics, lo_topics, split_by_topics, topic_names,
    _merge_small,
)


def graph(edges):
    nodes = sorted({n for e in edges for n in e[:2]})
    return {"nodes": [{"id": n} for n in nodes],
            "edges": [{"source": a, "target": b, "weight": w} for a, b, w in edges]}


def lo(id_, page=1, anchor=None, keywords=(), prereqs=(), file="a.pdf"):
    return {"id": id_, "source_title": file, "source_page": page, "anchor": anchor,
            "keywords": list(keywords), "prerequisite_ids": list(prereqs)}


TWO_CLUSTERS = graph([("pointer", "malloc", 5), ("malloc", "free", 5), ("pointer", "free", 5),
                      ("mean", "variance", 5), ("variance", "sample", 5), ("mean", "sample", 5),
                      ("free", "mean", 1)])


def test_two_clusters_give_two_communities():
    c = concept_communities(TWO_CLUSTERS)
    assert c["pointer"] == c["malloc"] == c["free"]
    assert c["mean"] == c["variance"] == c["sample"]
    assert c["pointer"] != c["mean"]


def test_communities_are_deterministic_and_empty_graph_is_fine():
    assert concept_communities(TWO_CLUSTERS) == concept_communities(TWO_CLUSTERS)
    assert concept_communities({"nodes": [], "edges": []}) == {}


def test_topic_from_anchor_then_keyword_then_none():
    c = {"pointer": 0, "mean": 1}
    los = [lo("a", anchor="pointer"), lo("b", anchor="zzz", keywords=["Other", "Mean"]), lo("c", anchor=None)]
    assert lo_topics(los, c) == [0, 1, None]


def test_missing_topics_take_previous_then_next():
    assert fill_missing_topics([0, 1, 2, 3], [None, 5, None, 7]) == [5, 5, 5, 7]
    assert fill_missing_topics([2, 1, 0], [1, None, None]) == [1, 1, 1]  # the first ones take the next
    assert fill_missing_topics([0], [None]) == [None]


VOCAB = ["variance", "formula", "indicates", "pointer"]


def fake_embed(texts):
    """One dimension per VOCAB word: how often the word appears in the text."""
    return [[t.lower().split().count(w) for w in VOCAB] for t in texts]


def qa(id_, anchor, keywords, question, answer=""):
    return {**lo(id_, anchor=anchor, keywords=keywords), "question": question, "answer": answer}


def test_missing_topic_goes_to_most_similar_topic():
    los = [qa("a", "variance", [], "variance variance"), qa("b", "pointer", [], "pointer"),
           qa("c", "zzz", [], "what does the pointer point to"), qa("d", None, [], "variance formula")]
    assert assign_missing_topics(los, [0, 1, None, None], embed=fake_embed) == [0, 1, 1, 0]


def test_missing_topics_unchanged_when_no_lo_has_a_topic():
    los = [qa("a", None, [], "variance"), qa("b", None, [], "pointer")]
    assert assign_missing_topics(los, [None, None], embed=fake_embed) == [None, None]


def test_los_without_topic_are_not_pulled_together():
    # regression: "no topic" used to act as one more topic, so such LOs were grouped into one lesson
    los = [lo(str(i), page=i) for i in range(6)]
    topics = [0, None, 0, 1, None, 1]
    lessons, _, _ = split_by_topics(los, topics, min_los=1)
    flat = [i for lesson in lessons for i in lesson]
    assert flat.index(1) < flat.index(4) and abs(flat.index(1) - flat.index(4)) > 1


def test_topic_name_is_concept_closest_to_the_los():
    # "formula" is the anchor of most LOs, but the LOs are about variance
    los = [qa("a", "formula", ["Formula", "Variance"], "what formula gives the variance", "variance variance"),
           qa("b", "formula", ["indicates"], "what the variance indicates", "variance"),
           qa("c", "variance", [], "variance of a sample")]
    communities = {"formula": 0, "variance": 0, "indicates": 0}
    assert topic_names(los, [0, 0, 0], communities, embed=fake_embed) == {0: "variance"}


def test_topic_name_prefers_concept_most_los_share_when_similarity_is_equal():
    # both words are in every text (same similarity), but 3 LOs have "variance" and only 1 "pointer"
    los = [qa("a", "variance", ["pointer"], "variance pointer"), qa("b", "variance", [], "variance pointer"),
           qa("c", "variance", [], "variance pointer")]
    communities = {"variance": 0, "pointer": 0}
    assert topic_names(los, [0, 0, 0], communities, embed=fake_embed) == {0: "variance"}


def test_topic_name_candidates_only_from_its_community_else_most_shared_anchor():
    los = [qa("a", "pointer", ["variance"], "variance"), qa("b", "pointer", [], "variance"),
           qa("c", "zzz", [], "pointer")]
    # topic 0: "variance" belongs to community 1, so only "pointer" is a candidate
    # topic 1: no candidate in community 1 among its LOs -> most shared anchor
    communities = {"pointer": 0, "variance": 1}
    assert topic_names(los, [0, 0, 1], communities, embed=fake_embed) == {0: "pointer", 1: "zzz"}


def test_order_with_groups_keeps_topic_together():
    # pages alternate between topics; with groups each topic stays together
    los = [lo(str(i), page=i) for i in range(6)]
    groups = [0, 1, 0, 1, 0, 1]
    assert lo_order(los)[0] == [0, 1, 2, 3, 4, 5]
    assert lo_order(los, groups=groups)[0] == [0, 2, 4, 1, 3, 5]


def test_order_with_groups_still_respects_prerequisites():
    # topic 0 LO on page 5 needs the topic 1 LO on page 2, so topic 0 is interrupted
    los = [lo("a", page=1), lo("b", page=2), lo("c", page=5, prereqs=["b"])]
    order, _ = lo_order(los, groups=[0, 1, 0])
    assert order == [0, 1, 2]


def test_merge_small_runs_into_smaller_neighbour():
    runs = [[0, 1, 2, 3], [4], [5, 6, 7]]
    assert _merge_small(runs, 3) == [[0, 1, 2, 3], [4, 5, 6, 7]]
    assert _merge_small([[0], [1]], 3) == [[0, 1]]
    assert _merge_small([[0, 1, 2]], 3) == [[0, 1, 2]]


def test_split_by_topics_one_lesson_per_topic():
    los = [lo(str(i), page=i) for i in range(8)]
    topics = [0, 1, 0, 1, 0, 1, 0, 1]
    lessons, filled, breaks = split_by_topics(los, topics)
    assert lessons == [[0, 2, 4, 6], [1, 3, 5, 7]]
    assert filled == topics and breaks == 0


def test_big_topic_is_cut_and_small_topic_merged():
    los = [lo(str(i), page=i) for i in range(12)]
    topics = [0] * 10 + [1] * 2
    lessons, _, _ = split_by_topics(los, topics, min_los=3, max_los=6)
    # topic 1 (2 LOs) joins topic 0 -> 12 LOs -> two lessons of 6
    assert lessons == [list(range(6)), list(range(6, 12))]


def test_empty_input():
    assert split_by_topics([], []) == ([], [], 0)


def test_random_topics_split_keeps_prerequisites_and_all_los():
    rng = random.Random(1)
    for _ in range(100):
        n = rng.randint(1, 60)
        rank = list(range(n))
        rng.shuffle(rank)
        los = [lo(f"id{i}", page=rng.randint(1, 20)) for i in range(n)]
        for i in range(n):
            los[i]["prerequisite_ids"] = [f"id{p}" for p in range(n) if rank[p] < rank[i] and rng.random() < 0.1]
        topics = [rng.choice([0, 1, 2, 3, None]) for _ in range(n)]
        lessons, _, breaks = split_by_topics(los, topics)
        flat = [i for lesson in lessons for i in lesson]
        assert sorted(flat) == list(range(n))
        assert all(lessons)
        assert breaks == 0 and prerequisite_violations(los, flat) == 0
        assert all(len(lesson) <= 15 for lesson in lessons)
