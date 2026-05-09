import io
import sys
import queue
import asyncio
import pickle
import base64
import statistics as stats
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any

import networkx as nx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_log_queue: queue.Queue = queue.Queue(maxsize=2000)
_current_stage: str = "global"

def set_stage(name: str):
    global _current_stage
    _current_stage = name

class _LogCapture(io.TextIOBase):
    def __init__(self, original):
        self._original = original
    def write(self, text):
        self._original.write(text)
        self._original.flush()
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    _log_queue.put_nowait(f"{_current_stage}::{line}")
                except queue.Full:
                    pass
        return len(text)
    def flush(self):
        self._original.flush()

sys.stdout = _LogCapture(sys.stdout)

from concept_normalizer import normalize_units
from atomic_unit_extractor import extract_atomic_units_batch, clean_atomic_units
from graph_builder import (
    build_concept_graph, assign_anchor,
    build_layered_learning_paths, verify_prerequisites_batched, store_prerequisites,
)
from learning_object_evaluator import evaluate_learning_objects_batch
from learning_object_generator import (
    generate_learning_object_batch, generate_mc_variants_batch, generate_id, extract_keywords,
)
from ems import filter_and_regenerate
from lexical_overlap_computation import lexical_overlap
from post_statistics import analyze_evaluations
from utilities import extract_text_from_source, split_into_sections
from echub_format_converter import enrich_learning_objects, convert_by_source, convert_los_to_units

_executor = ThreadPoolExecutor(max_workers=2)

async def _run_blocking(fn):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, fn)

def _save(filename, data):
    import json, os
    try:
        os.makedirs("/app/output", exist_ok=True)
        with open(f"/app/output/{filename}", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[Save] {filename} saved to /app/output/")
    except Exception as e:
        print(f"[Save] Warning: could not save {filename}: {e}")

app = FastAPI(title="LO Generation Pipeline")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:8000"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def home():
    return FileResponse("/app/pipeline_ui.html")

@app.get("/logs")
async def log_stream():
    async def event_generator():
        yield "data: global::[Log stream connected]\n\n"
        while True:
            try:
                line = _log_queue.get_nowait()
                yield f"data: {line}\n\n"
            except queue.Empty:
                await asyncio.sleep(0.2)
    return StreamingResponse(event_generator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/extract-units")
async def extract_units_endpoint(file: UploadFile = File(...)):
    import tempfile, os
    filename = file.filename or ""
    allowed = (".pdf", ".html", ".htm", ".pptx")
    if not any(filename.lower().endswith(ext) for ext in allowed):
        raise HTTPException(status_code=400, detail=f"Unsupported file type. Allowed: {', '.join(allowed)}")
    file_bytes = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    set_stage("extract")
    def _run():
        try:
            extracted_text = extract_text_from_source(tmp_path, document_title=filename)
        finally:
            os.unlink(tmp_path)
        sections = split_into_sections(extracted_text)
        print(f'[Extract] "{filename}" -> {len(sections)} sections.')
        raw_units = extract_atomic_units_batch(sections, batch_size=3)
        print(f'[Extract] Deduplicating {len(raw_units)} raw units...')
        atomic_units = clean_atomic_units(raw_units)
        atomic_units = normalize_units(atomic_units)
        print(f'[Extract] {len(atomic_units)} units after dedup.')
        return atomic_units
    atomic_units = await _run_blocking(_run)
    _save("atomic_units.json", atomic_units)
    raw_concepts = {c for u in atomic_units for c in u.get("concepts", [])}
    norm_concepts = {c for u in atomic_units for c in u.get("normalized_concepts", [])}
    return {
        "units": atomic_units,
        "stats": {
            "total_units": len(atomic_units),
            "type_distribution": dict(Counter(u["type"] for u in atomic_units)),
            "unique_raw_concepts": len(raw_concepts),
            "unique_normalized_concepts": len(norm_concepts),
        },
    }

class UnitsInput(BaseModel):
    units: List[Dict[str, Any]]

@app.post("/generate-los")
async def generate_los_endpoint(body: UnitsInput):
    units = body.units
    batch_size = 5
    set_stage("generate")
    def _run():
        los = []
        total_batches = -(-len(units) // batch_size)
        print(f'[Generate] {len(units)} units -> {total_batches} batch(es).')
        for batch_start in range(0, len(units), batch_size):
            batch = units[batch_start:batch_start + batch_size]
            los_raw = generate_learning_object_batch(batch, batch_size=batch_size)
            for unit, lo in zip(batch, los_raw):
                lo["id"] = generate_id(lo["source_statement"])
                lo["keywords"] = list(extract_keywords(lo["question"] + " " + lo["answer"]))
                lo["source_title"] = unit.get("source_title", "")
                lo["source_page"] = unit.get("source_page", "")
                src = unit.get("source_title", "")
                page = unit.get("source_page", "")
                if src and page:
                    lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}, p. {page}]"
                elif src:
                    lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}]"
                los.append(lo)
            print(f'[Generate] Batch {batch_start // batch_size + 1}/{total_batches} done -- {len(los)} LOs so far.')
        print(f'[Generate] Generating MC variants for {len(los)} LOs...')
        los = generate_mc_variants_batch(los, batch_size=5)
        print(f'[Generate] Done. {len(los)} LOs with MC variants.')
        return los
    learning_objects = await _run_blocking(_run)
    return {
        "learning_objects": learning_objects,
        "stats": {
            "total_los": len(learning_objects),
            "type_distribution": dict(Counter(lo["type"] for lo in learning_objects)),
        },
    }

class EvalInput(BaseModel):
    learning_objects: List[Dict[str, Any]]
    units: List[Dict[str, Any]] = []
    models: List[str] = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"]
    faithfulness_threshold: float = 3.0
    max_attempts: int = 2

@app.post("/evaluate")
async def evaluate_endpoint(body: EvalInput):
    import tempfile, os, json as _json
    los_in = list(body.learning_objects)
    units = body.units
    models = body.models
    threshold = body.faithfulness_threshold
    attempts = body.max_attempts
    set_stage("evaluate")
    def _run():
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "atomic_units.json"), "w") as f:
                _json.dump(units, f)
            return filter_and_regenerate(
                los_in, output_dir=tmp, eval_models=models,
                faithfulness_threshold=threshold, max_attempts=attempts,
            )
    los = await _run_blocking(_run)
    return {
        "learning_objects": los,
        "stats": {
            "total_los": len(los),
            "type_distribution": dict(Counter(lo["type"] for lo in los)),
        },
    }

class GraphInput(BaseModel):
    units: List[Dict[str, Any]]
    learning_objects: List[Dict[str, Any]]

@app.post("/build-graph")
async def build_graph_endpoint(body: GraphInput):
    units = body.units
    los = body.learning_objects
    set_stage("graph")
    def _run():
        print(f"[Graph] Building concept graph from {len(units)} units...")
        G = build_concept_graph(units)
        centrality = nx.degree_centrality(G)
        print(f"[Graph] Concept graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges.")
        for lo in los:
            lo["anchor"] = assign_anchor(lo, centrality)
        print(f"[Graph] Building layered learning paths for {len(los)} LOs...")
        candidate_edges = build_layered_learning_paths(los, centrality)
        print(f"[Graph] {len(candidate_edges)} candidate edges.")
        verified_edges = verify_prerequisites_batched(candidate_edges, los, batch_size=10)
        edges = list(set(verified_edges))
        KG = nx.DiGraph()
        KG.add_edges_from(edges)
        for lo in los:
            if lo["id"] not in KG:
                KG.add_node(lo["id"])
        if not nx.is_directed_acyclic_graph(KG):
            print("[Graph] Cycle detected, removing feedback edges...")
            for cycle in list(nx.simple_cycles(KG)):
                KG.remove_edge(cycle[-1], cycle[0])
        result_los = store_prerequisites(KG, los)
        print(f"[Graph] Complete. {KG.number_of_nodes()} nodes, {KG.number_of_edges()} edges. DAG: {nx.is_directed_acyclic_graph(KG)}")
        def serialize(g):
            return base64.b64encode(pickle.dumps(g)).decode("utf-8")
        return {
            "learning_objects": result_los,
            "concept_graph_b64": serialize(G),
            "knowledge_graph_b64": serialize(KG),
            "stats": {
                "concept_graph_nodes": G.number_of_nodes(),
                "concept_graph_edges": G.number_of_edges(),
                "knowledge_graph_nodes": KG.number_of_nodes(),
                "knowledge_graph_edges": KG.number_of_edges(),
                "candidate_edges": len(candidate_edges),
                "verified_edges": len(edges),
                "is_dag": nx.is_directed_acyclic_graph(KG),
            },
        }
    return await _run_blocking(_run)

class StatsInput(BaseModel):
    learning_objects: List[Dict[str, Any]]
    evaluations: List[Dict[str, Any]]

@app.post("/statistics")
async def statistics_endpoint(body: StatsInput):
    overlaps = []
    los = body.learning_objects
    for lo in los:
        score = lexical_overlap(lo["source_statement"], lo["answer"])
        lo["lexical_overlap"] = score
        overlaps.append(score)
    overlap_stats = {
        "mean": round(stats.mean(overlaps), 4),
        "std": round(stats.stdev(overlaps), 4) if len(overlaps) > 1 else 0,
        "min": round(min(overlaps), 4),
        "max": round(max(overlaps), 4),
    }
    eval_summary = analyze_evaluations(body.evaluations)
    return {"learning_objects_with_overlap": los, "overlap_stats": overlap_stats, "evaluation_summary": eval_summary}

class AddSourcesInput(BaseModel):
    existing_units: List[Dict[str, Any]]
    existing_los: List[Dict[str, Any]]
    new_units: List[Dict[str, Any]]

@app.post("/add-sources")
async def add_sources_endpoint(body: AddSourcesInput):
    existing_units = body.existing_units
    existing_los = body.existing_los
    new_units = body.new_units
    batch_size = 5
    set_stage("add-sources")
    def _run():
        new_los = []
        total_batches = -(-len(new_units) // batch_size)
        print(f"[AddSources] {len(new_units)} new units -> {total_batches} batch(es)...")
        for batch_start in range(0, len(new_units), batch_size):
            batch = new_units[batch_start:batch_start + batch_size]
            los_raw = generate_learning_object_batch(batch, batch_size=batch_size)
            for unit, lo in zip(batch, los_raw):
                lo["id"] = generate_id(lo["source_statement"])
                lo["keywords"] = list(extract_keywords(lo["question"] + " " + lo["answer"]))
                lo["source_title"] = unit.get("source_title", "")
                lo["source_page"] = unit.get("source_page", "")
                src = unit.get("source_title", "")
                page = unit.get("source_page", "")
                if src and page:
                    lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}, p. {page}]"
                elif src:
                    lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}]"
                new_los.append(lo)
            print(f"[AddSources] Batch {batch_start // batch_size + 1}/{total_batches} -- {len(new_los)} new LOs.")
        new_los = generate_mc_variants_batch(new_los, batch_size=5)
        combined_units = existing_units + new_units
        combined_los = existing_los + new_los
        print(f"[AddSources] Rebuilding graph over {len(combined_los)} total LOs...")
        G = build_concept_graph(combined_units)
        centrality = nx.degree_centrality(G)
        for lo in combined_los:
            lo["anchor"] = assign_anchor(lo, centrality)
        candidate_edges = build_layered_learning_paths(combined_los, centrality)
        verified_edges = verify_prerequisites_batched(candidate_edges, combined_los, batch_size=10)
        edges = list(set(verified_edges))
        KG = nx.DiGraph()
        KG.add_edges_from(edges)
        for lo in combined_los:
            if lo["id"] not in KG:
                KG.add_node(lo["id"])
        if not nx.is_directed_acyclic_graph(KG):
            for cycle in list(nx.simple_cycles(KG)):
                KG.remove_edge(cycle[-1], cycle[0])
        combined_los = store_prerequisites(KG, combined_los)
        def serialize(g):
            return base64.b64encode(pickle.dumps(g)).decode("utf-8")
        print(f"[AddSources] Done. {len(combined_los)} total LOs.")
        return {
            "learning_objects": combined_los,
            "knowledge_graph_b64": serialize(KG),
            "stats": {
                "total_los": len(combined_los),
                "new_los": len(new_los),
                "knowledge_graph_nodes": KG.number_of_nodes(),
                "knowledge_graph_edges": KG.number_of_edges(),
                "is_dag": nx.is_directed_acyclic_graph(KG),
            },
        }
    return await _run_blocking(_run)

class ExportEchubInput(BaseModel):
    learning_objects: List[Dict[str, Any]]
    mode: str = "by_source"
    unit_title: str = "Generated Learning Objects"
    unit_description: str = "Learning objects generated by the LO Pipeline."
    batch_size: int = 10

@app.post("/export-echub")
async def export_echub_endpoint(body: ExportEchubInput):
    los = list(body.learning_objects)
    mode = body.mode
    unit_title = body.unit_title
    unit_description = body.unit_description
    batch_size = body.batch_size
    set_stage("echub")
    def _run():
        print(f"[ECHub] Starting export for {len(los)} LOs (mode={mode})...")
        enriched = enrich_learning_objects(los, batch_size=batch_size)
        if mode == "by_source":
            result = convert_by_source(enriched)
        else:
            result = convert_los_to_units(enriched, unit_title, unit_description)
        total_q = sum(len(u["questions"]) for u in result["units"])
        print(f"[ECHub] Done. {total_q} questions across {len(result['units'])} unit(s).")
        return result
    result = await _run_blocking(_run)
    _save("echub_export.json", result)
    return result

# Manual save

class SaveLOsInput(BaseModel):
    learning_objects: List[Dict[str, Any]]
    filename: str = "learning_objects_saved.json"

@app.post("/save-los")
def save_los_endpoint(body: SaveLOsInput):
    """Saves the current LO list (including any edits) to /app/output/."""
    _save(body.filename, body.learning_objects)
    return {"saved": len(body.learning_objects), "filename": body.filename}