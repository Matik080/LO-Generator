"""
Characterization test: runs the current pipeline chain (the same steps pipeline_ui.html triggers through
/extract-units -> /generate-los -> /evaluate -> /build-graph -> /export-echub) on a small fixture document
with recorded LLM answers, and compares the result to a stored snapshot.

The refactor (M1) must keep this snapshot unchanged, apart from deliberate, reviewed changes.
Update the snapshot with UPDATE_SNAPSHOTS=1 (or automatically when recording with LLM_MODE=record).
"""
import json

import networkx as nx

from conftest import FIXTURES

SNAPSHOT = FIXTURES / "snapshots" / "c_memory_chain.json"
EVAL_MODELS = ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]


def run_current_chain(doc_path, tmp_dir):
    """Mirrors the step bodies in main.py, minus HTTP, file saving and pickling."""
    from utilities import extract_text_from_source, split_into_sections
    from atomic_unit_extractor import extract_atomic_units_batch, clean_atomic_units
    from concept_normalizer import normalize_units
    from learning_object_generator import generate_learning_object_batch, generate_mc_variants_batch, generate_id, extract_keywords
    from ems import filter_and_regenerate
    from graph_builder import build_concept_graph, assign_anchor, build_layered_learning_paths, verify_prerequisites_batched, store_prerequisites
    from echub_format_converter import enrich_learning_objects, convert_by_source

    # /extract-units
    text = extract_text_from_source(str(doc_path), document_title=doc_path.name)
    sections = split_into_sections(text)
    units = normalize_units(clean_atomic_units(extract_atomic_units_batch(sections, batch_size=3)))

    # /generate-los
    los = []
    for start in range(0, len(units), 5):
        batch = units[start:start + 5]
        for unit, lo in zip(batch, generate_learning_object_batch(batch, batch_size=5)):
            lo["id"] = generate_id(lo["source_statement"])
            lo["keywords"] = list(extract_keywords(lo["question"] + " " + lo["answer"]))
            lo["source_title"] = unit.get("source_title", "")
            lo["source_page"] = unit.get("source_page", "")
            src, page = unit.get("source_title", ""), unit.get("source_page", "")
            if src and page:
                lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}, p. {page}]"
            elif src:
                lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}]"
            los.append(lo)
    los = generate_mc_variants_batch(los, batch_size=5)

    # /evaluate (main.py writes the units to a temp dir because filter_and_regenerate reads them from disk)
    (tmp_dir / "atomic_units.json").write_text(json.dumps(units), encoding="utf-8")
    los = filter_and_regenerate(los, output_dir=str(tmp_dir), eval_models=EVAL_MODELS,
                                faithfulness_threshold=3.0, max_attempts=2)

    # /build-graph
    G = build_concept_graph(units)
    centrality = nx.degree_centrality(G)
    for lo in los:
        lo["anchor"] = assign_anchor(lo, centrality)
    candidates = build_layered_learning_paths(los, centrality)
    KG = nx.DiGraph()
    KG.add_edges_from(set(verify_prerequisites_batched(candidates, los, batch_size=10)))
    for lo in los:
        if lo["id"] not in KG:
            KG.add_node(lo["id"])
    if not nx.is_directed_acyclic_graph(KG):
        for cycle in list(nx.simple_cycles(KG)):
            KG.remove_edge(cycle[-1], cycle[0])
    los = store_prerequisites(KG, los)

    # /export-echub (mode=by_source)
    echub = convert_by_source(enrich_learning_objects(los, batch_size=10))

    return {
        "atomic_units": units,
        "learning_objects": los,
        "knowledge_graph_edges": sorted([list(e) for e in KG.edges()]),
        "concept_graph": {"nodes": G.number_of_nodes(), "edges": G.number_of_edges()},
        "echub": echub,
    }


def test_chain_matches_snapshot(fake_llm, update_snapshots, tmp_path):
    result = run_current_chain(FIXTURES / "c_memory.html", tmp_path)
    # Round-trip through JSON so tuples/sets compare the same way as the stored snapshot
    result = json.loads(json.dumps(result, ensure_ascii=False, default=list))

    assert result["atomic_units"], "fixture produced no atomic units"
    assert result["learning_objects"], "fixture produced no learning objects"

    if update_snapshots or not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
        return

    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for section in expected:
        assert result[section] == expected[section], f"'{section}' differs from snapshot"
