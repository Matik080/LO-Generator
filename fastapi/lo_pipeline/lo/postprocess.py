from lo_pipeline.lo.generator import generate_id, extract_keywords


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

    los = []
    total_batches = -(-len(units) // batch_size)
    for i, batch_start in enumerate(range(0, len(units), batch_size), 1):
        batch = units[batch_start:batch_start + batch_size]
        for unit, lo in zip(batch, generate_learning_object_batch(batch, batch_size=batch_size)):
            los.append(postprocess_lo(lo, unit))
        print(f"[Generate] Batch {i}/{total_batches} done -- {len(los)} LOs so far.")
        if progress:
            progress(i, total_batches)
    return los
