from lo_pipeline.lo.generator import generate_id, extract_keywords
import logging

logger = logging.getLogger(__name__)


def postprocess_lo(lo: dict, unit: dict) -> dict:
    """
    Fields added to every freshly generated LO: stable id, keywords, source info,
    and the source reference appended to the hint (moves to the report in M4).
    """
    lo["id"] = generate_id(lo["source_statement"])
    lo["keywords"] = list(extract_keywords(lo["question"] + " " + lo["answer"]))
    src = unit.get("source_title", "")
    page = unit.get("source_page", "")
    lo["source_title"] = src
    lo["source_page"] = page
    if src and page:
        lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}, p. {page}]"
    elif src:
        lo["hint"] = lo["hint"].rstrip(".") + f". [Source: {src}]"
    return lo


def generate_learning_objects(units: list, batch_size: int = 5, progress=None) -> list:
    """Generates one LO per unit in batches and post-processes each. `progress(done, total)` is optional."""
    from lo_pipeline.lo.generator import generate_learning_object_batch

    # all batches in one call, so they run in parallel (llm/parallel.py); one entry per unit, in order
    generated = generate_learning_object_batch(units, batch_size=batch_size, progress=progress)
    los = [postprocess_lo(lo, unit) for unit, lo in zip(units, generated)
           if lo is not None]  # None: no LO came back for this unit
    logger.info(f"[Generate] {len(los)} LOs from {len(units)} units.")
    return los
