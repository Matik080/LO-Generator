"""Parallel LLM batches (lo_pipeline/llm/parallel.py)."""

import threading
import time

import pytest

from lo_pipeline.checkpoint import MemoryCheckpoint
from lo_pipeline.llm import service as llm_service
from lo_pipeline.llm.parallel import chunks, map_batches, max_concurrency


class Counter:
    """Counts how many calls run at the same time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = 0
        self.most = 0

    def __call__(self, item):
        with self.lock:
            self.running += 1
            self.most = max(self.most, self.running)
        time.sleep(0.05 if item % 2 else 0.01)  # odd items finish later: order must still hold
        with self.lock:
            self.running -= 1
        return item * 10


def test_results_in_batch_order_and_limited_concurrency(monkeypatch):
    monkeypatch.setenv("LOGEN_LLM_MAX_CONCURRENCY", "3")
    counter, progress = Counter(), []
    assert map_batches(counter, list(range(10)), lambda d, t: progress.append((d, t))) == [i * 10 for i in range(10)]
    assert 1 < counter.most <= 3
    assert progress == [(d, 10) for d in range(1, 11)]  # once per batch, from 1 to total


def test_concurrency_1_is_sequential(monkeypatch):
    monkeypatch.setenv("LOGEN_LLM_MAX_CONCURRENCY", "1")
    counter = Counter()
    assert map_batches(counter, [1, 2, 3]) == [10, 20, 30]
    assert counter.most == 1


@pytest.mark.parametrize("value, expected", [("", 4), ("8", 8), ("0", 1), ("x", 4)])
def test_max_concurrency_setting(monkeypatch, value, expected):
    monkeypatch.setenv("LOGEN_LLM_MAX_CONCURRENCY", value)
    assert max_concurrency() == expected


def test_error_in_a_batch_stops_new_batches(monkeypatch):
    monkeypatch.setenv("LOGEN_LLM_MAX_CONCURRENCY", "2")
    started = []

    def batch(item):
        started.append(item)
        if item == 0:
            raise RuntimeError("LLM down")
        time.sleep(0.05)
        return item

    with pytest.raises(RuntimeError):
        map_batches(batch, list(range(20)))
    assert len(started) < 20  # the queued batches were not started


def test_error_from_progress_stops_new_batches(monkeypatch):
    """A cancelled job raises from the progress callback."""
    monkeypatch.setenv("LOGEN_LLM_MAX_CONCURRENCY", "2")
    started = []

    def batch(item):
        started.append(item)
        time.sleep(0.02)
        return item

    def progress(done, total):
        raise KeyboardInterrupt  # any exception, also a non-Exception one

    with pytest.raises(KeyboardInterrupt):
        map_batches(batch, list(range(20)), progress)
    assert len(started) < 20


def test_threads_use_the_checkpoint_cache(monkeypatch):
    """The response cache is a ContextVar; every thread must see it, so a resumed run pays nothing."""
    monkeypatch.setenv("LOGEN_LLM_MAX_CONCURRENCY", "4")
    calls = []
    monkeypatch.setattr(llm_service, "_backend", lambda messages, **kw: calls.append(1) or "answer " + messages[0]["content"])
    checkpoint = MemoryCheckpoint()
    ask = lambda i: llm_service.call_llm([{"role": "user", "content": f"q{i}"}])

    with llm_service.use_response_cache(checkpoint):
        first = map_batches(ask, list(range(8)))
    assert len(calls) == 8 and len(checkpoint.responses) == 8

    with llm_service.use_response_cache(checkpoint):  # "resumed run"
        again = map_batches(ask, list(range(8)))
    assert again == first and len(calls) == 8  # all 8 answered from the checkpoint


def test_chunks():
    assert chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert chunks([], 3) == []
