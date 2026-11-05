"""
Characterization test: runs the whole pipeline (run_pipeline) on a small fixture document with recorded
LLM answers and compares the result to a stored snapshot.

The snapshot was first recorded from the original step-by-step chain the UI triggers
(/extract-units -> /generate-los -> /evaluate -> /build-graph -> /export-echub), so a match proves the
refactor kept the behaviour. Update it only for deliberate, reviewed changes:
UPDATE_SNAPSHOTS=1 (or automatically when recording with LLM_MODE=record).
"""
import json

import networkx as nx

from conftest import FIXTURES
from lo_pipeline.runner import run_pipeline, STAGES

SNAPSHOT = FIXTURES / "snapshots" / "c_memory_chain.json"
DOC = FIXTURES / "c_memory.html"


def _snapshot_view(result):
    """The parts of run_pipeline's result that the snapshot stores."""
    kg = result["knowledge_graph"]
    view = {
        "atomic_units": result["atomic_units"],
        "learning_objects": result["learning_objects"],
        "knowledge_graph_edges": sorted([e["source"], e["target"]] for e in kg["edges"]),
        "concept_graph": {"nodes": len(result["concept_graph"]["nodes"]),
                          "edges": len(result["concept_graph"]["edges"])},
        "echub": result["echub"],
    }
    # Round-trip through JSON so tuples/sets compare the same way as the stored snapshot
    return json.loads(json.dumps(view, ensure_ascii=False, default=list))


def test_pipeline_matches_snapshot(fake_llm, update_snapshots):
    result = run_pipeline([DOC])
    view = _snapshot_view(result)

    assert view["atomic_units"], "fixture produced no atomic units"
    assert view["learning_objects"], "fixture produced no learning objects"
    kg = nx.DiGraph([(e["source"], e["target"]) for e in result["knowledge_graph"]["edges"]])
    assert nx.is_directed_acyclic_graph(kg)

    if update_snapshots or not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(view, indent=1, ensure_ascii=False), encoding="utf-8")
        return

    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for section in expected:
        assert view[section] == expected[section], f"'{section}' differs from snapshot"


def test_progress_is_reported_per_stage(fake_llm):
    events = []
    result = run_pipeline([DOC], progress_callback=lambda s, d, t: events.append((s, d, t)))

    # Every stage reports, in pipeline order, and nothing else
    seen = list(dict.fromkeys(s for s, _, _ in events))
    assert seen == STAGES

    for stage in STAGES:
        stage_events = [(d, t) for s, d, t in events if s == stage]
        assert stage_events[0] == (0, 0), f"{stage} should start with (0, 0)"
        assert all(0 <= d <= t for d, t in stage_events[1:]), f"{stage}: done must stay within total"
        fractions = [d / t for d, t in stage_events[1:]]
        assert fractions == sorted(fractions), f"{stage}: progress went backwards: {stage_events}"
        assert stage_events[-1][0] == stage_events[-1][1], f"{stage} should end finished"

    # LLM-heavy stages report per batch, not just start/end
    for stage in ["units", "learning_objects", "lo_evaluation", "questions"]:
        assert len([1 for s, _, _ in events if s == stage]) > 2, stage

    assert set(result["timings"]) == set(STAGES)
