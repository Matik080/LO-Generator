"""
Prerequisite graph step: concept graph -> anchors -> candidate edges -> LLM verification -> DAG.
Shared by the /build-graph and /add-sources endpoints (and later run_pipeline).
"""
import networkx as nx

from lo_pipeline.graph.builder import (
    build_concept_graph, assign_anchor, build_layered_learning_paths,
    verify_prerequisites_batched, store_prerequisites,
)


def _page_number(lo):
    try:
        return int(lo.get("source_page"))
    except (TypeError, ValueError):
        return None


def break_cycles(KG: nx.DiGraph, learning_objects: list) -> list:
    """
    Removes edges until KG is a DAG and returns the removed edges.

    Edge (a, b) means "a is a prerequisite of b". In each cycle the edge removed is the one whose
    prerequisite comes latest in the source relative to its dependent (a later page should not be
    required by an earlier one). Without page info, the last edge of the cycle is removed.
    One cycle is fixed at a time, so an edge shared by several cycles is removed only once.
    """
    pages = {lo["id"]: _page_number(lo) for lo in learning_objects}
    removed = []
    while True:
        try:
            cycle = nx.find_cycle(KG)
        except nx.NetworkXNoCycle:
            return removed

        def badness(i):
            a, b = cycle[i][0], cycle[i][1]
            pa, pb = pages.get(a), pages.get(b)
            gap = (pa - pb) if pa is not None and pb is not None else 0
            return gap, i  # ties -> later edge in the cycle

        a, b = cycle[max(range(len(cycle)), key=badness)][:2]
        KG.remove_edge(a, b)
        removed.append((a, b))


def build_prerequisite_graph(units: list, learning_objects: list, verify_batch_size: int = 10, progress=None) -> dict:
    """
    Builds the concept graph and the verified prerequisite DAG. Sets `anchor` and `prerequisite_ids` on the LOs.
    Returns a dict with the LOs, both graphs, and counts for stats/reporting.
    `progress(done, total)` is called after each LLM verification batch (optional).
    """
    print(f"[Graph] Building concept graph from {len(units)} units...")
    G = build_concept_graph(units)
    centrality = nx.degree_centrality(G)
    print(f"[Graph] Concept graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges.")
    for lo in learning_objects:
        lo["anchor"] = assign_anchor(lo, centrality)

    print(f"[Graph] Building layered learning paths for {len(learning_objects)} LOs...")
    candidate_edges = build_layered_learning_paths(learning_objects, centrality)
    print(f"[Graph] {len(candidate_edges)} candidate edges.")
    verified = set(verify_prerequisites_batched(candidate_edges, learning_objects,
                                                batch_size=verify_batch_size, progress=progress))

    KG = nx.DiGraph()
    KG.add_edges_from(verified)
    for lo in learning_objects:
        if lo["id"] not in KG:
            KG.add_node(lo["id"])

    removed = break_cycles(KG, learning_objects)
    if removed:
        print(f"[Graph] Removed {len(removed)} edge(s) to break cycles: {removed}")

    learning_objects = store_prerequisites(KG, learning_objects)
    print(f"[Graph] Complete. {KG.number_of_nodes()} nodes, {KG.number_of_edges()} edges.")
    return {
        "learning_objects": learning_objects,
        "concept_graph": G,
        "knowledge_graph": KG,
        "candidate_edges": len(candidate_edges),
        "verified_edges": len(verified),
        "removed_cycle_edges": removed,
    }


def graph_to_json(G) -> dict:
    """Node-link JSON (the shape used in the /report extensions), instead of pickled graphs."""
    return {
        "nodes": [{"id": n, **attrs} for n, attrs in G.nodes(data=True)],
        "edges": [{"source": u, "target": v, **attrs} for u, v, attrs in G.edges(data=True)],
    }
