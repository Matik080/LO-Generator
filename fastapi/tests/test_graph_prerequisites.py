import networkx as nx

from lo_pipeline.graph.prerequisites import break_cycles, graph_to_json


def _los(pages):
    return [{"id": i, "source_page": p} for i, p in pages.items()]


def test_cycles_sharing_an_edge_remove_it_once():
    # x->y->x and x->z->y->x both contain y->x. The old loop over simple_cycles removed the
    # "closing" edge of every cycle, so it could remove y->x twice (NetworkXError) or drop extra edges.
    KG = nx.DiGraph([("x", "y"), ("y", "x"), ("x", "z"), ("z", "y")])
    removed = break_cycles(KG, _los({"x": 1, "y": 2, "z": None}))
    assert nx.is_directed_acyclic_graph(KG)
    assert removed == [("y", "x")]


def test_cycle_breaks_on_edge_from_later_page():
    # "p3" (page 3) as a prerequisite of "p1" (page 1) is the suspicious edge.
    KG = nx.DiGraph([("p1", "p2"), ("p2", "p3"), ("p3", "p1")])
    removed = break_cycles(KG, _los({"p1": 1, "p2": 2, "p3": 3}))
    assert removed == [("p3", "p1")]
    assert set(KG.edges()) == {("p1", "p2"), ("p2", "p3")}


def test_dag_is_left_untouched():
    KG = nx.DiGraph([("a", "b"), ("b", "c")])
    assert break_cycles(KG, _los({"a": 1, "b": 2, "c": 3})) == []
    assert KG.number_of_edges() == 2


def test_graph_to_json_node_link_shape():
    G = nx.Graph()
    G.add_edge("x", "y", weight=3)
    data = graph_to_json(G)
    assert {n["id"] for n in data["nodes"]} == {"x", "y"}
    assert data["edges"] == [{"source": "x", "target": "y", "weight": 3}]
