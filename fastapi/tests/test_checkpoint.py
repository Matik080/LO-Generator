"""
Checkpoint resume: a run that crashes part-way and is started again must
  - give exactly the same result as an uninterrupted run,
  - not repeat any LLM call that was already answered,
  - start fresh if the input or options changed.
LLM answers come from the test cassette; a wrapper counts calls and can simulate a crash.
"""
import json

import pytest

from conftest import FIXTURES
from lo_pipeline import checkpoint as cp
from lo_pipeline.llm import service as llm_service
from lo_pipeline.runner import run_pipeline, PipelineOptions

DOC = FIXTURES / "c_memory.html"


class Crash(Exception):
    pass


@pytest.fixture
def llm(fake_llm, monkeypatch):
    """Counts real (non-cached) LLM calls; set .crash_after = n to fail on call n+1."""
    class Counter:
        calls = 0
        crash_after = None

        def __call__(self, messages, **kwargs):
            if self.crash_after is not None and self.calls >= self.crash_after:
                raise Crash(f"simulated crash after {self.calls} LLM calls")
            self.calls += 1
            return fake_llm(messages, **kwargs)

    counter = Counter()
    monkeypatch.setattr(llm_service, "_backend", counter)
    return counter


def _comparable(result):
    r = dict(result)
    r.pop("timings")  # wall time differs between runs
    return json.loads(json.dumps(r, default=list))


@pytest.fixture
def reference(llm):
    """An uninterrupted run without checkpoint, and how many LLM calls it needs."""
    result = _comparable(run_pipeline([DOC]))
    total, llm.calls = llm.calls, 0
    return result, total


@pytest.mark.parametrize("crash_fraction", [0.1, 0.5, 0.9])
def test_resume_after_crash_gives_same_result_without_repeating_calls(llm, reference, tmp_path, crash_fraction):
    expected, total_calls = reference
    checkpoint_dir = tmp_path / "ckpt"
    crash_after = max(1, int(total_calls * crash_fraction))

    llm.crash_after = crash_after
    with pytest.raises(Crash):
        run_pipeline([DOC], checkpoint=cp.FileCheckpoint(str(checkpoint_dir)))
    assert llm.calls == crash_after
    assert (checkpoint_dir / "llm_calls.jsonl").exists()

    llm.crash_after, llm.calls = None, 0
    resumed = run_pipeline([DOC], checkpoint=cp.FileCheckpoint(str(checkpoint_dir)))

    assert _comparable(resumed) == expected
    assert llm.calls == total_calls - crash_after, "calls answered before the crash must not be repeated"
    assert not checkpoint_dir.exists(), "checkpoint is removed after a successful run"


def test_finished_stages_are_skipped(llm, reference, tmp_path):
    expected, total_calls = reference
    ckpt = cp.MemoryCheckpoint()
    events = []

    llm.crash_after = total_calls - 1  # crash on the very last call (in the 'questions' stage)
    with pytest.raises(Crash):
        run_pipeline([DOC], checkpoint=ckpt)
    assert set(ckpt.stages) == {"extract", "units", "learning_objects", "lo_evaluation", "graph"}

    llm.crash_after, llm.calls = None, 0
    resumed = run_pipeline([DOC], checkpoint=ckpt, progress_callback=lambda s, d, t: events.append((s, d, t)))
    assert _comparable(resumed) == expected
    assert llm.calls == 1
    # Skipped stages still report start and finish, so a progress display stays consistent
    assert [(d, t) for s, d, t in events if s == "units"] == [(0, 0), (1, 1)]


def test_changed_options_start_fresh(llm, reference, tmp_path):
    _, total_calls = reference
    checkpoint_dir = str(tmp_path / "ckpt")

    llm.crash_after = total_calls // 2
    with pytest.raises(Crash):
        run_pipeline([DOC], checkpoint=cp.FileCheckpoint(checkpoint_dir))

    saved_keys = _saved_keys(checkpoint_dir)
    assert saved_keys

    # Different options -> different fingerprint -> nothing may be reused
    ckpt = cp.FileCheckpoint(checkpoint_dir)
    assert ckpt.start(cp.fingerprint([_src()], PipelineOptions(max_attempts=1))) is False
    assert ckpt.completed_stage("extract") is None
    assert all(ckpt.get(k) is None for k in saved_keys)


def test_changed_input_file_starts_fresh(tmp_path):
    doc = tmp_path / "doc.html"
    doc.write_text("<h1>A</h1><p>one</p>", encoding="utf-8")
    src = _src(doc)
    fp1 = cp.fingerprint([src], PipelineOptions())
    doc.write_text("<h1>A</h1><p>two</p>", encoding="utf-8")
    assert cp.fingerprint([src], PipelineOptions()) != fp1


def test_cut_off_last_line_is_ignored(tmp_path):
    ckpt = cp.FileCheckpoint(str(tmp_path / "c"))
    ckpt.start("fp")
    ckpt.put("k1", "answer one")
    ckpt.close()
    with open(tmp_path / "c" / "llm_calls.jsonl", "a", encoding="utf-8") as f:
        f.write('{"key": "k2", "respo')  # crash in the middle of writing

    again = cp.FileCheckpoint(str(tmp_path / "c"))
    assert again.start("fp") is True
    assert again.get("k1") == "answer one"
    assert again.get("k2") is None


def test_clear_keeps_unrelated_files(tmp_path):
    (tmp_path / "notes.txt").write_text("mine", encoding="utf-8")
    ckpt = cp.FileCheckpoint(str(tmp_path))
    ckpt.start("fp")
    ckpt.save_stage("extract", {"state": {}, "seconds": 0})
    ckpt.clear()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]


def _src(path=DOC):
    from lo_pipeline.runner import _as_source
    return _as_source(str(path))


def _saved_keys(checkpoint_dir):
    import pathlib
    lines = (pathlib.Path(checkpoint_dir) / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["key"] for line in lines if line.strip()]
