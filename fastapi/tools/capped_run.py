"""
One real pipeline run with a hard cap on LLM calls (stops the run when the cap is reached).

    python tools/capped_run.py input/X.pdf --max-calls 150 [--language auto|sk|en] [--split '{"mode": "single"}']
    python tools/capped_run.py input/X.pdf --inspect      # no LLM: pages, text, detected language

Writes output/<name>-pipeline_result.json, -result.json (units) and -report.json, and prints the
number of LLM calls. Uses the settings from fastapi/.env like the worker does.
"""
import argparse
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class CapReached(RuntimeError):
    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    parser.add_argument("--max-calls", type=int, default=150)
    parser.add_argument("--language", default="auto", choices=["auto", "sk", "en"])
    parser.add_argument("--split", default='{"mode": "single"}')
    parser.add_argument("--inspect", action="store_true", help="no LLM: pages, text, language")
    args = parser.parse_args()

    from lo_pipeline.extraction.documents import extract_text_from_source, split_into_sections
    from lo_pipeline.language import detect_language

    if args.inspect:
        sections = split_into_sections(extract_text_from_source(args.file, document_title=os.path.basename(args.file)))
        words = sum(len(s["content"].split()) for s in sections)
        print(f"pages with text: {len(sections)}, words: {words}")
        for s in sections:
            print(f"  page {s['page']}: {len(s['content'].split())} words | {s['content'][:90]!r}")
        print("language:", detect_language(s["content"] for s in sections))
        return

    from lo_pipeline.contract.options import JobOptions, resolve_options
    from lo_pipeline.llm import service as llm_service
    from lo_pipeline.questions.units_output import build_units_and_report
    from lo_pipeline.runner import PipelineOptions, run_pipeline

    real_backend, calls, lock = llm_service._backend, [0], threading.Lock()

    def capped(messages, **kwargs):
        with lock:
            if calls[0] >= args.max_calls:
                raise CapReached(f"LLM call cap of {args.max_calls} reached; run stopped.")
            calls[0] += 1
        return real_backend(messages, **kwargs)

    llm_service._backend = capped
    job = resolve_options(JobOptions.model_validate({"language": args.language, "split": json.loads(args.split)}))
    opts = PipelineOptions.from_settings()
    opts.language, opts.split = job.language, job.split.model_dump()
    opts.no_answer_tasks = "no_answer" in job.questions.types
    try:
        result = run_pipeline([args.file], options=opts)
    finally:
        print(f"LLM calls: {calls[0]}")
    units, report = build_units_and_report(result, job)
    name = os.path.splitext(os.path.basename(args.file))[0]
    os.makedirs("output", exist_ok=True)
    for suffix, data in (("pipeline_result", result), ("result", units), ("report", report)):
        with open(f"output/{name}-{suffix}.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"language: {result['language']}, units: {len(result['atomic_units'])}, LOs: {len(result['learning_objects'])}, "
          f"questions: {sum(len(u['questions']) for u in units['units'])}, written to output/{name}-*.json")


if __name__ == "__main__":
    main()
