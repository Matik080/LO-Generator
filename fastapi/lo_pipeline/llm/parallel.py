"""Independent LLM batches at the same time (sprint task 3: LOGEN_LLM_MAX_CONCURRENCY).

Most of a run is waiting for the LLM provider. Batches that do not depend on each other (unit
extraction per group of sections, LO generation, MC options, evaluation, graph verification,
names and times) are sent up to LOGEN_LLM_MAX_CONCURRENCY at a time from a thread pool.
Threads are enough: Python releases the GIL while a thread waits for the network.

Results always come back in the order of the batches, so everything after the loop works
exactly as before. The checkpoint's response cache (llm/service.py) is passed to every thread,
so a resumed run reuses the saved answers just like a sequential one.
"""

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional, Sequence, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


def max_concurrency() -> int:
    """LOGEN_LLM_MAX_CONCURRENCY (default 4, lo_pipeline/config.py); 1 = one batch after another."""
    from lo_pipeline.config import get_settings
    return get_settings().llm_max_concurrency


def map_batches(func: Callable[[T], R], batches: Sequence[T],
                progress: Optional[Callable[[int, int], None]] = None) -> list[R]:
    """[func(batch) for batch in batches], up to max_concurrency() batches at the same time.

    `progress(done, total)` is called in the calling thread after each finished batch, so it
    may raise (e.g. a cancelled job): then no new batch starts, the running ones finish (their
    LLM answers are saved in the checkpoint) and the exception is raised. The same happens when
    a batch raises (e.g. LLMError).
    """
    total = len(batches)
    workers = min(max_concurrency(), total)
    if workers <= 1:
        results = []
        for done, batch in enumerate(batches, 1):
            results.append(func(batch))
            if progress:
                progress(done, total)
        return results

    results: list = [None] * total
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="llm") as pool:
        # each thread gets the caller's context: the checkpoint's response cache (a ContextVar)
        futures = {pool.submit(contextvars.copy_context().run, func, batch): i for i, batch in enumerate(batches)}
        try:
            for done, future in enumerate(as_completed(futures), 1):
                results[futures[future]] = future.result()
                if progress:
                    progress(done, total)
        except BaseException:
            for future in futures:
                future.cancel()  # not started yet: never starts; running: finishes (pool waits)
            raise
    return results


def chunks(items: Sequence[T], size: int) -> list[Sequence[T]]:
    """[items[0:size], items[size:2*size], …]"""
    return [items[start:start + size] for start in range(0, len(items), size)]
