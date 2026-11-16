"""Overall job progress (0–1) from the pipeline's stage progress.

Each stage has a share of the whole run, roughly its share of the LLM calls in a typical run
(Testing_text.pdf baseline). Used by the mock now and by the worker in task 4a.
"""

STAGE_WEIGHTS: dict[str, float] = {
    "extract": 0.02,
    "units": 0.20,
    "learning_objects": 0.25,
    "lo_evaluation": 0.35,
    "graph": 0.10,
    "questions": 0.06,
    "output": 0.02,
}
STAGES: tuple[str, ...] = tuple(STAGE_WEIGHTS)


def overall_progress(stage: str, fraction: float) -> float:
    """Progress of the whole job when `stage` is `fraction` (0–1) done."""
    before = sum(STAGE_WEIGHTS[s] for s in STAGES[:STAGES.index(stage)])
    return round(min(1.0, before + STAGE_WEIGHTS[stage] * max(0.0, min(1.0, fraction))), 4)


def stage_at(progress: float) -> tuple[str, float]:
    """Inverse of overall_progress: the stage running at `progress` and how far it is (0–1)."""
    start = 0.0
    for stage in STAGES:
        end = start + STAGE_WEIGHTS[stage]
        if progress < end or stage == STAGES[-1]:
            return stage, max(0.0, min(1.0, (progress - start) / STAGE_WEIGHTS[stage]))
        start = end
    raise AssertionError("unreachable")
