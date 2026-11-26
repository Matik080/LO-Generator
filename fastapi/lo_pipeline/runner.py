"""
run_pipeline: the whole LO pipeline in one call, without HTTP, global state or file writes.

Used by the CLI below, by the tests, and later by the job worker (M3).

    python -m lo_pipeline.runner modul1.pdf modul2.pptx --out result.json
"""
import argparse
import json
import os
import time
from contextlib import nullcontext
from dataclasses import dataclass, field, asdict
from typing import Callable, List, Optional
import logging

logger = logging.getLogger(__name__)

# (stage, done, total): done/total is the stage's progress, counted in LLM calls.
# (stage, 0, 0) = stage started; done == total = stage finished. `total` can change while a stage runs
# (e.g. LOs being regenerated), so use done/total as a fraction rather than a fixed count.
ProgressCallback = Callable[[str, int, int], None]

# Stage names, in order (names match the API design, Appendix A).
STAGES = ["extract", "language", "units", "learning_objects", "lo_evaluation", "graph", "questions", "split", "output"]
# Stages whose LLM calls write texts for students: they get the output-language rule (llm/service.py) when the
# job asks for another language than the documents'. Unit extraction stays in the documents' language.
OUTPUT_LANGUAGE_STAGES = ("learning_objects", "lo_evaluation", "questions", "split")


@dataclass
class PipelineOptions:
    """Settings of one run. The defaults here are the built-in values; run_pipeline without options
    uses from_settings() (LOGEN_* variables, lo_pipeline/config.py). They are part of the checkpoint
    fingerprint, so a changed setting never mixes with an older checkpoint."""
    extraction_batch_size: int = 3
    generation_batch_size: int = 5
    mc_batch_size: int = 5
    eval_models: List[str] = field(default_factory=lambda: ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"])
    faithfulness_threshold: float = 3.0
    hint_quality_threshold: float = 2.0
    max_attempts: int = 2
    verify_batch_size: int = 10
    enrich_batch_size: int = 10
    two_correct_share: float = 0.3   # share of choice questions with 2 correct options (made in the MC step)
    no_answer_tasks: bool = True     # False when the job does not ask for no_answer questions (no LLM calls)
    no_answer_batch_size: int = 5
    no_answer_group_size: int = 6
    dedup_threshold: float = 0.82
    # the job's split options (contract SplitOptions as a dict: mode, lesson_count, syllabus, …)
    split: dict = field(default_factory=lambda: {"mode": "single"})
    language: Optional[str] = None  # requested output language: "sk" | "en"; None or "auto" = the documents' language
    title_batch_size: int = 5
    ocr: bool = False  # job option extraction.ocr: read scanned PDF pages and PPTX pictures (extraction/ocr.py)

    @classmethod
    def from_settings(cls, settings=None) -> "PipelineOptions":
        from lo_pipeline.config import get_settings
        s = settings or get_settings()
        return cls(
            extraction_batch_size=s.extraction_batch_size, generation_batch_size=s.generation_batch_size,
            mc_batch_size=s.mc_batch_size, eval_models=list(s.eval_models),
            faithfulness_threshold=s.faithfulness_threshold, hint_quality_threshold=s.hint_quality_threshold,
            max_attempts=s.max_attempts, verify_batch_size=s.verify_batch_size,
            enrich_batch_size=s.enrich_batch_size, two_correct_share=s.two_correct_share,
            no_answer_batch_size=s.no_answer_batch_size, no_answer_group_size=s.no_answer_group_size,
            dedup_threshold=s.dedup_threshold,
        )


@dataclass
class Source:
    path: str
    title: Optional[str] = None  # shown in hints/ECHub names; defaults to the file name


def _ceil_div(a, b):
    return -(-a // b)


class _Progress:
    """Turns per-step `progress(done, total)` calls into progress_callback(stage, done, total)."""

    def __init__(self, callback: Optional[ProgressCallback]):
        self.callback = callback

    def report(self, stage, done, total):
        if self.callback:
            self.callback(stage, done, total)

    def step(self, stage, offset=0, extra_total=0):
        """A `progress(done, total)` for one loop inside `stage`, shifted by work done/expected around it."""
        return lambda done, total: self.report(stage, offset + done, offset + total + extra_total)


def run_pipeline(sources, options: Optional[PipelineOptions] = None,
                 progress_callback: Optional[ProgressCallback] = None, checkpoint=None) -> dict:
    """
    Runs every step on the given documents and returns all results in memory:
    atomic_units, learning_objects, no_answer_tasks, knowledge_graph / concept_graph (node-link JSON),
    split (lessons, topic per LO, split warnings; see split/plan.py), echub,
    quality (evaluation summary, see filter_and_regenerate), timings, warnings.

    `sources`: list of Source, {"path", "title"} dicts or plain paths (PDF, PPTX, HTML).
    `progress_callback(stage, done, total)`: optional; called after each LLM batch and at stage boundaries.
    `checkpoint`: optional lo_pipeline.checkpoint.Checkpoint. With it, an interrupted run continues where it
    stopped when called again with the same files and options; it is cleared after a successful run.
    """
    from lo_pipeline.extraction.documents import ExtractionError, extract_text_from_source, split_into_sections
    from lo_pipeline.units.extractor import extract_atomic_units_batch, clean_atomic_units
    from lo_pipeline.units.normalizer import normalize_units
    from lo_pipeline.lo.postprocess import generate_learning_objects
    from lo_pipeline.lo.generator import generate_mc_variants_batch
    from lo_pipeline.lo.quality_filter import filter_and_regenerate
    from lo_pipeline.graph.prerequisites import build_prerequisite_graph, graph_to_json
    from lo_pipeline.questions.echub import enrich_learning_objects, convert_by_source
    from lo_pipeline.questions.no_answer import anchor_groups, generate_no_answer_tasks
    from lo_pipeline.questions.shorten import shorten_long_texts
    from lo_pipeline.split.plan import split_lessons
    from lo_pipeline.language import detect_language, output_language, source_language
    from lo_pipeline.checkpoint import fingerprint
    from lo_pipeline.llm.service import output_language_rule, use_output_language, use_response_cache

    opts = options or PipelineOptions.from_settings()
    sources = [_as_source(s) for s in sources]
    progress = _Progress(progress_callback)
    timings = {}
    # Everything the stages produce. Each stage returns the keys it adds or changes; those are what a
    # checkpoint saves for the stage, so every value here must be JSON-serializable.
    state = {"warnings": []}

    # 1. Text and sections, per document
    def extract():
        sections_per_source = []
        for i, src in enumerate(sources, 1):
            text = extract_text_from_source(src.path, document_title=src.title, ocr=opts.ocr)
            sections_per_source.append(split_into_sections(text))
            logger.info(f'[Extract] "{src.title}" -> {len(sections_per_source[-1])} sections.')
            progress.report("extract", i, len(sources))
        if not any(sections_per_source):
            # e.g. a scanned PDF without a text layer and without OCR: nothing to send to the LLM
            raise ExtractionError("No text could be extracted from the documents.")
        return {"sections_per_source": sections_per_source}

    # 1b. Language of the documents (no LLM) and of the output (job option `language`)
    def language():
        detected = detect_language(s["content"] for sections in state["sections_per_source"] for s in sections)
        result = {"document": detected["language"], "confident": detected["confident"],
                  "requested": opts.language or "auto",
                  "output": output_language(opts.language, detected["language"])}
        logger.info(f"[Language] Documents: {result['document']} (confident: {result['confident']}), "
                    f"output: {result['output']}.")
        return {"language": result}

    # 2. Atomic units: extracted per document (a batch never mixes two documents), deduplicated across all
    def units():
        sections_per_source = state["sections_per_source"]
        total = sum(_ceil_div(len(s), opts.extraction_batch_size) for s in sections_per_source)
        raw_units, offset, source_stats = [], 0, {}
        for sections in sections_per_source:
            raw_units += extract_atomic_units_batch(
                sections, batch_size=opts.extraction_batch_size,
                progress=progress.step("units", offset, total - offset - _ceil_div(len(sections), opts.extraction_batch_size)),
                stats=source_stats,
            )
            offset += _ceil_div(len(sections), opts.extraction_batch_size)
        warnings = list(state["warnings"])
        if source_stats.get("sections_failed"):
            warnings.append({"code": "sections_skipped",
                             "message": f"{source_stats['sections_failed']} section(s) were skipped: the LLM gave "
                                        f"no valid answer for them, so they have no units."})
        fixed = source_stats.get("section_corrected", 0) + source_stats.get("section_missing", 0)
        if fixed:
            warnings.append({"code": "unit_section_reassigned",
                             "message": f"{fixed} of {len(raw_units)} unit(s) got their page from text matching "
                                        f"instead of the LLM's section number.",
                             "details": source_stats})
        logger.info(f"[Extract] Deduplicating {len(raw_units)} raw units...")
        atomic_units = normalize_units(clean_atomic_units(raw_units, similarity_threshold=opts.dedup_threshold))
        logger.info(f"[Extract] {len(atomic_units)} units after dedup.")
        # the units may be in another language than the documents (the LLM often writes them in English);
        # the student-facing prompts are built from them, so the output-language rule compares with them
        units_language = detect_language(u.get("statement") or "" for u in atomic_units)["language"]
        return {"atomic_units": atomic_units, "warnings": warnings,
                "language": {**state["language"], "units": units_language}}

    # 3. LOs + multiple-choice variants
    def learning_objects():
        atomic_units = state["atomic_units"]
        gen_calls = _ceil_div(len(atomic_units), opts.generation_batch_size)
        mc_estimate = _ceil_div(len(atomic_units), opts.mc_batch_size)  # upper bound until LO types are known
        los = generate_learning_objects(atomic_units, batch_size=opts.generation_batch_size,
                                        progress=progress.step("learning_objects", 0, mc_estimate))
        # MC batch count is exact now that LO types are known
        los = generate_mc_variants_batch(los, batch_size=opts.mc_batch_size,
                                         progress=progress.step("learning_objects", gen_calls),
                                         two_correct_share=opts.two_correct_share)
        return {"learning_objects": los}

    # 4. Quality evaluation and regeneration
    def lo_evaluation():
        quality = {}
        los = filter_and_regenerate(
            state["learning_objects"], state["atomic_units"], eval_models=opts.eval_models,
            faithfulness_threshold=opts.faithfulness_threshold,
            hint_quality_threshold=opts.hint_quality_threshold,
            max_attempts=opts.max_attempts,
            mc_batch_size=opts.mc_batch_size,
            two_correct_share=opts.two_correct_share,
            progress=progress.step("lo_evaluation"),
            quality=quality,
        )
        return {"learning_objects": los, "quality": quality}

    # 5. Prerequisite graph
    def graph():
        g = build_prerequisite_graph(state["atomic_units"], state["learning_objects"],
                                     verify_batch_size=opts.verify_batch_size, progress=progress.step("graph"))
        warnings = list(state["warnings"])
        for a, b in g["removed_cycle_edges"]:
            warnings.append({"code": "prerequisite_cycle_removed",
                             "message": f"Removed prerequisite edge {a} -> {b} to break a cycle."})
        return {"learning_objects": g["learning_objects"],
                "knowledge_graph": graph_to_json(g["knowledge_graph"]),
                "concept_graph": graph_to_json(g["concept_graph"]),
                "warnings": warnings}

    # 6. Question names and time estimates, and no_answer tasks from LOs that share a concept
    def questions():
        los = state["learning_objects"]
        enrich_calls = _ceil_div(len(los), opts.enrich_batch_size)
        groups = anchor_groups(los, opts.no_answer_group_size) if opts.no_answer_tasks else []
        task_calls = _ceil_div(len(groups), opts.no_answer_batch_size)
        los = enrich_learning_objects(los, batch_size=opts.enrich_batch_size,
                                      progress=progress.step("questions", 0, task_calls))
        tasks = []
        if opts.no_answer_tasks:
            tasks = generate_no_answer_tasks(los, batch_size=opts.no_answer_batch_size,
                                             group_size=opts.no_answer_group_size,
                                             progress=progress.step("questions", enrich_calls))
        # texts over the ECEH limits: one LLM shortening (usually none, then no call)
        shorten_long_texts(los, tasks)
        return {"learning_objects": los, "no_answer_tasks": tasks}

    # 7. Lessons: order, split mode, titles (lo_pipeline/split). Never fails; problems are warnings in it.
    def split():
        result = split_lessons(state["learning_objects"], state["concept_graph"], opts.split,
                               [src.title for src in sources],
                               progress=progress.step("split"), title_batch_size=opts.title_batch_size)
        return {"split": result}

    # 8. Output: today's ECHub format (one unit per source); the worker builds the `units` format from the result.
    def output():
        return {"echub": convert_by_source(state["learning_objects"])}

    stage_functions = {"extract": extract, "language": language, "units": units, "learning_objects": learning_objects,
                       "lo_evaluation": lo_evaluation, "graph": graph, "questions": questions, "split": split,
                       "output": output}
    assert list(stage_functions) == STAGES

    if checkpoint is not None:
        checkpoint.start(fingerprint(sources, opts))
    with use_response_cache(checkpoint) if checkpoint is not None else nullcontext():
        for stage, run_stage in stage_functions.items():
            saved = checkpoint.completed_stage(stage) if checkpoint is not None else None
            if saved is not None:
                logger.info(f"[Checkpoint] Skipping '{stage}' (finished in an earlier run).")
                state.update(saved["state"])
                timings[stage] = saved["seconds"]
                progress.report(stage, 0, 0)
                progress.report(stage, 1, 1)
                continue
            # stages that write for students get the output-language rule when it differs from the documents'
            lang = state.get("language") or {}
            rule = (output_language_rule(lang.get("output"), source_language(lang))
                    if stage in OUTPUT_LANGUAGE_STAGES else None)
            with _Timer(stage, timings, progress), use_output_language(rule):
                updates = run_stage()
                state.update(updates)
            if checkpoint is not None:
                checkpoint.save_stage(stage, {"state": updates, "seconds": timings[stage]})

    result = {
        "atomic_units": state["atomic_units"],
        "learning_objects": state["learning_objects"],
        "no_answer_tasks": state.get("no_answer_tasks", []),  # missing in a checkpoint from before task 1c
        "language": state.get("language"),
        "split": state.get("split"),
        "knowledge_graph": state["knowledge_graph"],
        "concept_graph": state["concept_graph"],
        "echub": state["echub"],
        "quality": state["quality"],
        "timings": timings,
        "warnings": state["warnings"],
        "options": asdict(opts),
    }
    if checkpoint is not None:
        checkpoint.clear()  # done: nothing left to resume
    return result


class _Timer:
    """Records the stage's wall time and reports the stage as finished."""

    def __init__(self, stage, timings, progress):
        self.stage, self.timings, self.progress = stage, timings, progress

    def __enter__(self):
        self.start = time.perf_counter()
        self.progress.report(self.stage, 0, 0)
        return self

    def __exit__(self, exc_type, *_):
        self.timings[self.stage] = round(time.perf_counter() - self.start, 2)
        if exc_type is None:
            self.progress.report(self.stage, 1, 1)
        return False


def _as_source(s) -> Source:
    if isinstance(s, Source):
        src = s
    elif isinstance(s, dict):
        src = Source(path=s["path"], title=s.get("title"))
    else:
        src = Source(path=str(s))
    if not src.title:
        src.title = os.path.basename(src.path)
    return src


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the whole LO pipeline on one or more documents.")
    parser.add_argument("files", nargs="+", help="PDF, PPTX or HTML files")
    parser.add_argument("--out", default="pipeline_result.json", help="where to write the result JSON")
    parser.add_argument("--debug", action="store_true", help="also log debug details (includes document text)")
    parser.add_argument("--checkpoint-dir", help="where to keep progress for resuming (default: <out>.checkpoint)")
    parser.add_argument("--no-checkpoint", action="store_true", help="don't save progress; an interrupted run starts over")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")

    def show(stage, done, total):
        if total:
            logger.info(f"[Progress] {stage}: {done}/{total}")

    checkpoint = None
    if not args.no_checkpoint:
        from lo_pipeline.checkpoint import FileCheckpoint
        # Re-running the same command after a crash continues from here
        checkpoint = FileCheckpoint(args.checkpoint_dir or args.out + ".checkpoint")

    result = run_pipeline(args.files, progress_callback=show, checkpoint=checkpoint)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[Runner] {len(result['learning_objects'])} LOs -> {args.out}. Timings: {result['timings']}")


if __name__ == "__main__":
    main()
