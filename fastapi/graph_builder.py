#imports
import os
import pickle
import matplotlib.pyplot as plt
import networkx as nx
import re
import json
from llm_service import call_llm
from utilities import load_json, export_json

TYPE_LEVEL = {
    "definition": 1,
    "theorem": 1,
    "rule": 2,
    "constraint": 2,
    "property": 3,
    "fact": 3,
    "algorithm_step": 4,
    "example": 5,
}
def get_type_level(lo):
    return TYPE_LEVEL.get(lo.get("type", "fact"), 3)

def build_concept_graph(units):
    G = nx.Graph()

    for unit in units:
        concepts = unit.get("normalized_concepts", [])

        # Add nodes
        for concept in concepts:
            G.add_node(concept)

        # Add edges between all pairs in same unit
        for i in range(len(concepts)):
            for j in range(i + 1, len(concepts)):
                if G.has_edge(concepts[i], concepts[j]):
                    G[concepts[i]][concepts[j]]["weight"] += 1
                else:
                    G.add_edge(concepts[i], concepts[j], weight=1)
    return G

def visualize_graph(G, output_path="concept_graph.png"):
    plt.figure(figsize=(60, 45))

    pos = nx.kamada_kawai_layout(G)

    degrees = dict(G.degree())
    weights = [G[u][v]["weight"] for u, v in G.edges()]
    max_w = max(weights) if weights else 1
    edge_widths = [0.2 + 1.5 * (w / max_w) for w in weights]

    # Tiny nodes, just enough to show degree difference
    node_sizes = [20 + degrees[n] * 10 for n in G.nodes()]

    nx.draw_networkx_edges(G, pos, width=edge_widths, alpha=0.25, edge_color="gray")
    nx.draw_networkx_nodes(G, pos, node_size=node_sizes, node_color="steelblue", alpha=0.6)

    # Draw labels with a bbox so they're always readable regardless of what's behind them
    nx.draw_networkx_labels(
        G, pos,
        font_size=6,
        font_color="black",
        bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.7)
    )

    plt.title("Concept Graph", fontsize=24)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(output_path, dpi=200, bbox_inches="tight")
    print(f"[Graph] Saved to {output_path}")

def assign_anchor(lo, centrality):
    scored = [
        (kw.lower(), centrality.get(kw.lower(), 0))
        for kw in lo["keywords"]
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[0][0] if scored else None


def llm_determine_prerequisite(lo_a, lo_b) -> bool:
    prompt = f"""
You are an expert computer science educator.

Determine if Learning Object A is a prerequisite for Learning Object B.
A is a prerequisite of B if a student MUST understand A before they can 
meaningfully engage with B.

Learning Object A:
Type: {lo_a['type']}
Question: {lo_a['question']}
Answer: {lo_a['answer']}

Learning Object B:
Type: {lo_b['type']}
Question: {lo_b['question']}
Answer: {lo_b['answer']}

Answer with JSON only:
{{
  "is_prerequisite": true/false,
  "confidence": "high/medium/low",
  "reason": "one sentence explanation"
}}

Only return true if A is clearly necessary to understand B.
When in doubt, return false.
"""
    messages = [
        {
            "role": "system",
            "content": "You are an expert educator determining prerequisite relationships between learning objects. Be conservative — only mark as prerequisite if clearly necessary. Return JSON only."
        },
        {"role": "user", "content": prompt}
    ]

    response = call_llm(messages, temperature=0.0)
    response = re.sub(r"^```(?:json)?\s*", "", response.strip())
    response = re.sub(r"\s*```$", "", response).strip()
    result = json.loads(response)
    return result.get("is_prerequisite", False)


def build_prerequisite_edges(learning_objects, centrality):
    from embedding_service import get_embeddings_batch, cosine_similarity
    from llm_service import call_llm
    import re, json

    texts = [lo["question"] + " " + lo["answer"] for lo in learning_objects]
    print(f"[Graph] Computing embeddings for {len(texts)} learning objects...")
    embeddings = get_embeddings_batch(texts)

    for lo, emb in zip(learning_objects, embeddings):
        lo["_embedding"] = emb

    # Stage 1: embedding filter -find candidate pairs
    candidates = []
    for i in range(len(learning_objects)):
        for j in range(len(learning_objects)):
            if i == j:
                continue
            lo_a = learning_objects[i]
            lo_b = learning_objects[j]

            # Must be semantically related
            sim = cosine_similarity(lo_a["_embedding"], lo_b["_embedding"])
            if not (0.4 <= sim <= 0.92):
                continue

            # A must be more central than B
            anchor_a = lo_a.get("anchor")
            anchor_b = lo_b.get("anchor")
            if not anchor_a or not anchor_b:
                continue
            cent_a = centrality.get(anchor_a, 0)
            cent_b = centrality.get(anchor_b, 0)
            if not (cent_a > cent_b * 1.2):
                continue

            candidates.append((i, j, sim))

    print(f"[Graph] {len(candidates)} candidate pairs — sending to LLM...")

    # Stage 2: LLM verification of candidates only
    edges = []
    for i, j, sim in candidates:
        lo_a = learning_objects[i]
        lo_b = learning_objects[j]

        if llm_determine_prerequisite(lo_a, lo_b):
            edges.append((lo_a["id"], lo_b["id"]))
            print(f"[Graph] PREREQUISITE: {lo_a['question'][:50]} -> {lo_b['question'][:50]}")

    for lo in learning_objects:
        lo.pop("_embedding", None)

    print(f"[Graph] Generated {len(edges)} verified prerequisite edges.")
    return edges


def build_layered_learning_paths(learning_objects, centrality):
    """
    Builds prerequisite edges based on a layered type hierarchy.

    Layer 1: definition, theorem (introduce concepts)
    Layer 2: rule, constraint (govern correct usage)
    Layer 3: property, fact (describe behavior)
    Layer 4: algorithm_step (apply procedurally)
    Layer 5: example (concrete instances)

    Edges are drawn from lower layers to higher layers when
    learning objects share at least one concept keyword.
    Within the same layer, edges are drawn by embedding similarity
    when concepts overlap significantly.
    """
    from embedding_service import get_embeddings_batch, cosine_similarity

    TYPE_LAYER = {
        "definition": 1,
        "theorem": 1,
        "rule": 2,
        "constraint": 2,
        "property": 3,
        "fact": 3,
        "algorithm_step": 4,
        "example": 5,
    }

    # Embed all LOs for within-layer similarity
    texts = [lo["question"] + " " + lo["answer"] for lo in learning_objects]
    print(f"[Graph] Computing embeddings for {len(texts)} learning objects...")
    embeddings = get_embeddings_batch(texts)
    for lo, emb in zip(learning_objects, embeddings):
        lo["_embedding"] = emb
        lo["_layer"] = TYPE_LAYER.get(lo.get("type", "fact"), 3)

    edges = []

    for i in range(len(learning_objects)):
        for j in range(len(learning_objects)):
            if i == j:
                continue

            lo_a = learning_objects[i]
            lo_b = learning_objects[j]
            layer_a = lo_a["_layer"]
            layer_b = lo_b["_layer"]

            # Only draw edges from lower or equal layer to higher layer
            if layer_a > layer_b:
                continue

            # Must share at least one keyword
            keywords_a = set(lo_a.get("keywords", []))
            keywords_b = set(lo_b.get("keywords", []))
            shared = keywords_a & keywords_b
            if not shared:
                continue

            # Cross-layer edges: any shared keyword is enough
            if layer_a < layer_b:
                if len(shared) >= 2:
                    edges.append((lo_a["id"], lo_b["id"]))
                continue

            # Same-layer edges: require stronger semantic similarity
            sim = cosine_similarity(lo_a["_embedding"], lo_b["_embedding"])
            if sim >= 0.6:
                # Only add edge in one direction, from more central to less
                cent_a = centrality.get(lo_a.get("anchor", ""), 0)
                cent_b = centrality.get(lo_b.get("anchor", ""), 0)
                if cent_a > cent_b:
                    edges.append((lo_a["id"], lo_b["id"]))

    # Clean up temporary fields
    for lo in learning_objects:
        lo.pop("_embedding", None)
        lo.pop("_layer", None)

    # Deduplicate
    edges = list(set(edges))

    # Check for cycles and remove if needed
    KG = nx.DiGraph()
    KG.add_edges_from(edges)
    for lo in learning_objects:
        if lo["id"] not in KG:
            KG.add_node(lo["id"])

    # Remove cycles if any exist using feedback arc set
    if not nx.is_directed_acyclic_graph(KG):
        print("[Graph] Cycle detected, removing feedback edges...")
        cycles = list(nx.simple_cycles(KG))
        for cycle in cycles:
            # Remove the last edge in the cycle
            KG.remove_edge(cycle[-1], cycle[0])

    print(f"[Graph] Generated {KG.number_of_edges()} layered prerequisite edges.")
    print(f"[Graph] Is DAG: {nx.is_directed_acyclic_graph(KG)}")

    return list(KG.edges())

def print_graph_summary(KG, learning_objects):
    lo_map = {lo["id"]: lo for lo in learning_objects}

    print("\n=== ISOLATED NODES ===")
    for n in nx.isolates(KG):
        lo = lo_map.get(n, {})
        print(f"  [{lo.get('type', '')}] {lo.get('question', '?')[:80]}")

    print("\n=== HIGH IN-DEGREE (most dependent) ===")
    for n in sorted(KG.nodes(), key=lambda x: KG.in_degree(x), reverse=True)[:5]:
        lo = lo_map.get(n, {})
        print(f"  in={KG.in_degree(n)} [{lo.get('type', '')}] {lo.get('question', '?')[:80]}")

    print("\n=== ROOT NODES (most foundational) ===")
    for n in KG.nodes():
        if KG.in_degree(n) == 0 and KG.out_degree(n) > 0:
            lo = lo_map.get(n, {})
            print(f"  out={KG.out_degree(n)} [{lo.get('type', '')}] {lo.get('question', '?')[:80]}")

def verify_prerequisites_batched(candidate_edges, learning_objects, batch_size=10):
    """
    Takes candidate edges from the layered approach and verifies each one with an LLM. Returns only confirmed prerequisite edges.
    """
    lo_map = {lo["id"]: lo for lo in learning_objects}
    verified_edges = []

    # Convert edge list to list of (lo_a, lo_b) pairs
    pairs = []
    for (id_a, id_b) in candidate_edges:
        lo_a = lo_map.get(id_a)
        lo_b = lo_map.get(id_b)
        if lo_a and lo_b:
            pairs.append((lo_a, lo_b))

    print(f"[Graph] Verifying {len(pairs)} candidate edges with LLM...")
    print(f"[Graph] Will use ~{-(-len(pairs) // batch_size)} API calls.")

    for batch_start in range(0, len(pairs), batch_size):
        batch = pairs[batch_start:batch_start + batch_size]

        pairs_text = ""
        for idx, (lo_a, lo_b) in enumerate(batch):
            pairs_text += f"""
Pair {idx + 1}:
  A - [{lo_a['type']}] Q: {lo_a['question']}
                        A: {lo_a['answer']}
  B - [{lo_b['type']}] Q: {lo_b['question']}
                        A: {lo_b['answer']}
"""

        prompt = f"""
You are an expert computer science educator evaluating prerequisite relationships.

For each pair below, determine if A is a prerequisite of B.
A is a prerequisite of B ONLY if a student MUST understand A before they can
meaningfully engage with B. Be conservative — when in doubt, answer false.

{pairs_text}

Return a JSON array with exactly {len(batch)} results in the same order:
[
  {{
    "pair": 1,
    "is_prerequisite": true/false,
    "confidence": "high/medium/low"
  }},
  ...
]

Return JSON array only. No markdown. No explanation.
"""

        messages = [
            {
                "role": "system",
                "content": "You are an expert educator evaluating prerequisite relationships. Be conservative — only mark as prerequisite if clearly necessary. Return JSON array only."
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        print(f"[Graph] Verifying batch {batch_start // batch_size + 1} ({len(batch)} pairs)...")
        response = call_llm(messages, temperature=0.0)

        response = re.sub(r"^```(?:json)?\s*", "", response.strip())
        response = re.sub(r"\s*```$", "", response).strip()

        try:
            results = json.loads(response)
            for result in results:
                pair_idx = result.get("pair", 0) - 1
                if pair_idx < 0 or pair_idx >= len(batch):
                    continue
                if result.get("is_prerequisite") and result.get("confidence") in ["high", "medium"]:
                    lo_a, lo_b = batch[pair_idx]
                    verified_edges.append((lo_a["id"], lo_b["id"]))
                    print(f"[Graph] CONFIRMED: {lo_a['question'][:45]} -> {lo_b['question'][:45]}")
        except json.JSONDecodeError as e:
            print(f"[Graph] JSON parse error in batch: {e}, skipping...")
            continue

    print(f"[Graph] Verified {len(verified_edges)} / {len(pairs)} candidate edges.")
    return verified_edges

def store_prerequisites(KG, learning_objects):
    for lo in learning_objects:
        if lo["id"] in KG:
            prereqs = [pred for pred in KG.predecessors(lo["id"])]
        else:
            # isolated, not in graph
            prereqs = []
        lo["prerequisite_ids"] = prereqs
    return learning_objects

if __name__ == "__main__":
    output_dir = os.path.join(os.path.dirname(__file__), "output")
    units = load_json(os.path.join(output_dir, "atomic_units.json"))
    G = build_concept_graph(units)
    print("Number of nodes:", G.number_of_nodes())
    print("Number of edges:", G.number_of_edges())
    visualize_graph(G)

    learning_objects = load_json(os.path.join(output_dir, "learning_objects.json"))
    centrality = nx.degree_centrality(G)
    # Could be used instead of centrality in assign anchor
    # weighted_degree = dict(G.degree(weight="weight"))
    for lo in learning_objects:
        lo["anchor"] = assign_anchor(lo, centrality)
        #print(lo["anchor"])

    # edges = []
    # for i in range(len(learning_objects)):
    #     for j in range(i + 1, len(learning_objects)):
    #         if i == j:
    #             continue
    #         lo_a = learning_objects[i]
    #         lo_b = learning_objects[j]
    #         shared = set(lo_a["keywords"]) & set(lo_b["keywords"])
    #         if not shared:
    #             continue
    #
    #         anchor_a = lo_a.get("anchor")
    #         anchor_b = lo_b.get("anchor")
    #         if not anchor_a or not anchor_b:
    #             continue
    #
    #         # Debug printing
    #         # print(lo_a["anchor"], centrality.get(anchor_a))
    #         # print(lo_b["anchor"], centrality.get(anchor_b))
    #
    #         # The multiplication is there because B's centrality must be significantly higher.
    #         if centrality.get(anchor_a, 0) > centrality.get(anchor_b, 0) * 1.2:
    #             edges.append((lo_a["id"], lo_b["id"]))
    # edges = list(set(edges))

    # Stage 1: layered candidates
    candidate_edges = build_layered_learning_paths(learning_objects, centrality)
    # Stage 2: LLM verification
    verified_edges = verify_prerequisites_batched(candidate_edges, learning_objects, batch_size=10)
    edges = list(set(verified_edges))
    KG = nx.DiGraph()
    KG.add_edges_from(edges)
    for lo in learning_objects:
        if lo["id"] not in KG:
            KG.add_node(lo["id"])
    # Remove isolated nodes - LOs with no prerequisite relationships
    isolated = list(nx.isolates(KG))
    KG.remove_nodes_from(isolated)
    print(f"[Graph] Removed {len(isolated)} isolated nodes. "
            f"Remaining: {KG.number_of_nodes()} nodes.")

    print("Knowledge graph nodes:", KG.number_of_nodes())
    print("Knowledge graph edges:", KG.number_of_edges())
    print("Is DAG:", nx.is_directed_acyclic_graph(KG))

    H = nx.Graph()
    for u, v, data in G.edges(data=True):
        if data["weight"] >= 3:
            H.add_edge(u, v, weight=data["weight"])

    visualize_graph(H, "concept_graph_filtered.png")

    print_graph_summary(KG, learning_objects)

    learning_objects = store_prerequisites(KG, learning_objects)
    export_json(learning_objects, os.path.join(output_dir, "learning_objects.json"))

    with open(os.path.join(output_dir, "knowledge_graph.gpickle"), 'wb') as f:
        pickle.dump(KG, f, pickle.HIGHEST_PROTOCOL)
    with open(os.path.join(output_dir, "concept_graph.gpickle"), 'wb') as f:
        pickle.dump(G, f, pickle.HIGHEST_PROTOCOL)