"""
Quality numbers from the thesis evaluation files, in the same shape as run_pipeline's `quality` summary,
so the baseline can be compared with them.

    python -m analysis.thesis_quality_numbers ../OZNAL_GENERATION_OUTPUTS/output/evaluations_final_metrics_OZNAL.json

Differences to keep in mind when comparing:
  - the thesis files evaluate the FINAL LOs of a whole course; the baseline run usually covers one document;
  - the thesis evaluated with gpt-oss-120b, kimi-k2 and llama-3.3-70b; only models present in both are comparable;
  - the pass rate here uses the score-based rules only: the files have no question text, so the rule-based
    "references external context" check of the filter can't be applied.
"""
import argparse
import json
from collections import defaultdict

from lo_pipeline.lo.quality_filter import score_averages, _lo_fails_threshold


def thesis_numbers(evaluations, models=None, faithfulness_threshold=3.0, hint_quality_threshold=2.0):
    """`models`: only use evaluations from these models (e.g. the ones the baseline also uses)."""
    by_lo = defaultdict(list)
    for e in evaluations:
        if models is None or e.get("model") in models:
            by_lo[e["lo_id"]].append(e)
    passed = sum(1 for evals in by_lo.values()
                 if not _lo_fails_threshold(evals, faithfulness_threshold, hint_quality_threshold)[0])
    return {
        "models": sorted({e["model"] for evals in by_lo.values() for e in evals}),
        "los": len(by_lo),
        "passed": passed,
        "pass_rate": round(passed / len(by_lo), 3) if by_lo else None,
        "scores": score_averages(by_lo.values()),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("file", help="evaluations_final_metrics_*.json from the thesis outputs")
    parser.add_argument("--models", nargs="*", help="only these evaluating models (default: all in the file)")
    args = parser.parse_args(argv)
    with open(args.file, encoding="utf-8") as f:
        evaluations = json.load(f)
    print(json.dumps(thesis_numbers(evaluations, models=args.models), indent=2))


if __name__ == "__main__":
    main()
