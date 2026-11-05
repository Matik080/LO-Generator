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


def run_current_chain(doc_path):
    """Mirrors the step bodies in main.py, minus HTTP and file saving."""
    from lo_pipeline.extraction.documents import extract_text_from_source, split_into_sections
    from lo_pipeline.units.extractor import extract_atomic_units_batch, clean_atomic_units
    from lo_pipeline.units.normalizer import normalize_units
    from lo_pipeline.lo.generator import generate_mc_variants_batch
    from lo_pipeline.lo.postprocess import generate_learning_objects
    from lo_pipeline.lo.quality_filter import filter_and_regenerate
    from lo_pipeline.graph.prerequisites import build_prerequisite_graph
    from lo_pipeline.questions.echub import enrich_learning_objects, convert_by_source

    # /extract-units
    text = extract_text_from_source(str(doc_path), document_title=doc_path.name)
    sections = split_into_sections(text)
    units = normalize_units(clean_atomic_units(extract_atomic_units_batch(sections, batch_size=3)))

    # /generate-los
    los = generate_mc_variants_batch(generate_learning_objects(units, batch_size=5), batch_size=5)

    # /evaluate
    los = filter_and_regenerate(los, units, eval_models=EVAL_MODELS, faithfulness_threshold=3.0, max_attempts=2)

    # /build-graph
    g = build_prerequisite_graph(units, los, verify_batch_size=10)
    los, G, KG = g["learning_objects"], g["concept_graph"], g["knowledge_graph"]
    assert nx.is_directed_acyclic_graph(KG)

    # /export-echub (mode=by_source)
    echub = convert_by_source(enrich_learning_objects(los, batch_size=10))

    return {
        "atomic_units": units,
        "learning_objects": los,
        "knowledge_graph_edges": sorted([list(e) for e in KG.edges()]),
        "concept_graph": {"nodes": G.number_of_nodes(), "edges": G.number_of_edges()},
        "echub": echub,
    }


def test_chain_matches_snapshot(fake_llm, update_snapshots):
    result = run_current_chain(FIXTURES / "c_memory.html")
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
