"""The job worker (service/worker.py): real pipeline with recorded LLM answers, or a fake pipeline."""

import json

import fakeredis
import jsonschema
import pytest

from conftest import FIXTURES
from lo_pipeline.contract import JobOptions
from lo_pipeline.contract.options import resolve_options
from lo_pipeline.contract.schemas import load_schema
from lo_pipeline.llm import service as llm_service
from lo_pipeline.llm.service import LLMError
from service import worker
from service.store import JobStore
from service.uploads import Upload
from service.worker import run_job

DOC = FIXTURES / "c_memory.html"


@pytest.fixture
def store(tmp_path):
    return JobStore(fakeredis.FakeRedis(decode_responses=True), str(tmp_path))


def new_job(store, content=None, name="c_memory.html", **options) -> str:
    upload = Upload(name, DOC.read_bytes() if content is None else content)
    job_id, _ = store.create(resolve_options(JobOptions.model_validate(options)), [upload])
    return job_id


def record_updates(store, monkeypatch) -> list[dict]:
    updates = []
    original = store.update

    def update(job_id, **fields):
        updates.append(fields)
        original(job_id, **fields)
    monkeypatch.setattr(store, "update", update)
    return updates


def failing_pipeline(exc):
    def pipeline(sources, progress_callback, checkpoint):
        progress_callback("extract", 0, 0)
        progress_callback("units", 0, 0)
        raise exc
    return pipeline


# --- real pipeline ------------------------------------------------------------------------

def test_job_completes_with_result_and_report(store, fake_llm, monkeypatch):
    job_id = new_job(store, external_ref="gen-1")
    updates = record_updates(store, monkeypatch)
    run_job(store, job_id)

    status = store.status(job_id)
    assert (status.status, status.stage, status.progress, status.error) == ("completed", "output", 1.0, None)
    units, report = store.read_output(job_id, "result"), store.read_output(job_id, "report")
    jsonschema.validate(units, load_schema("lesson"))
    jsonschema.validate(report, load_schema("report"))
    assert units["units"][0]["questions"] and units["units"][0]["title"] == "c_memory.html"
    assert set(report["timings"]) == {"extract", "units", "learning_objects", "lo_evaluation",
                                      "graph", "questions", "output"}

    progress = [u["progress"] for u in updates if "progress" in u]
    assert progress == sorted(progress) and progress[-1] == 1.0
    assert all(p <= 0.99 for p in progress[:-1])
    stages = list(dict.fromkeys(u["stage"] for u in updates if u.get("stage")))
    assert stages == ["extract", "units", "learning_objects", "lo_evaluation", "graph", "questions", "output"]
    assert not store.checkpoint_dir(job_id).exists()  # cleared after success


def test_retry_continues_from_checkpoint(store, fake_llm, monkeypatch):
    """LLM fails after 5 calls -> failed; the retry pays only for the calls that were not made yet."""
    cassette, calls = llm_service._backend, []

    def flaky(*args, **kwargs):
        if len(calls) == 5:
            raise LLMError("LLM call failed after 6 attempts: 503")
        calls.append(1)
        return cassette(*args, **kwargs)
    monkeypatch.setattr(llm_service, "_backend", flaky)

    job_id = new_job(store)
    run_job(store, job_id)
    status = store.status(job_id)
    assert status.status == "failed" and status.error.code == "llm_error"
    assert status.error.details["attempt"] == 1
    assert store.checkpoint_dir(job_id).exists()

    counting = []
    monkeypatch.setattr(llm_service, "_backend", lambda *a, **k: counting.append(1) or cassette(*a, **k))
    store.update(job_id, status="queued", error=None, retryable_until=None)  # what POST /retry will do
    run_job(store, job_id)
    assert store.status(job_id).status == "completed"
    assert store.fields(job_id)["attempts"] == "2"

    # the full run makes len(calls) + len(counting) calls; none of the first 5 is repeated
    fresh = []
    monkeypatch.setattr(llm_service, "_backend", lambda *a, **k: fresh.append(1) or cassette(*a, **k))
    run_job(store, new_job(store))
    assert len(counting) == len(fresh) - 5


def test_document_without_text_fails_with_extraction_failed(store, fake_llm):
    job_id = new_job(store, content=b"<html><body><script>x()</script></body></html>", name="empty.html")
    run_job(store, job_id)
    status = store.status(job_id)
    assert status.status == "failed" and status.error.code == "extraction_failed"
    assert status.error.details["stage"] == "extract"
    assert status.retryable_until is not None


# --- fake pipeline ------------------------------------------------------------------------

def test_llm_error(store):
    job_id = new_job(store)
    run_job(store, job_id, pipeline=failing_pipeline(LLMError("Groq down")))
    error = store.status(job_id).error
    assert (error.code, error.message, error.details) == ("llm_error", "Groq down", {"stage": "units", "attempt": 1})


def test_unexpected_error_does_not_leak_its_text(store):
    job_id = new_job(store)
    run_job(store, job_id, pipeline=failing_pipeline(KeyError("secret document text")))
    error = store.status(job_id).error
    assert error.code == "internal_error" and error.details["reason"] == "KeyError"
    assert "secret" not in json.dumps(error.model_dump())


def test_invalid_output(store, monkeypatch):
    def invalid(result, options):
        raise worker.InvalidOutputError("bad")
    monkeypatch.setattr(worker, "build_units_and_report", invalid)
    job_id = new_job(store)
    run_job(store, job_id, pipeline=lambda sources, **kw: {"learning_objects": [{"id": "x"}]})
    error = store.status(job_id).error
    assert error.code == "internal_error" and error.details["reason"] == "invalid_output"


def test_no_learning_objects_is_an_llm_error(store):
    job_id = new_job(store)
    run_job(store, job_id, pipeline=lambda sources, **kw: {"learning_objects": []})
    assert store.status(job_id).error.code == "llm_error"
    assert not (store.job_dir(job_id) / "result.json").exists()


def test_cancel_during_run_stops_at_next_progress_report(store):
    job_id = new_job(store)
    reached = []

    def pipeline(sources, progress_callback, checkpoint):
        progress_callback("units", 1, 4)
        store.request_cancel(job_id)  # DELETE arrives while an LLM batch is running
        progress_callback("units", 2, 4)
        reached.append("after cancel")  # never reached
    run_job(store, job_id, pipeline=pipeline)
    status = store.status(job_id)
    assert status.status == "cancelled" and status.stage == "units" and not reached


def test_cancelled_before_start_does_not_run(store):
    job_id = new_job(store)
    store.request_cancel(job_id)
    run_job(store, job_id, pipeline=failing_pipeline(AssertionError("must not run")))
    assert store.status(job_id).status == "cancelled"
    assert store.fields(job_id)["attempts"] == "0"


@pytest.mark.parametrize("status", ["running", "completed", "failed", "cancelled"])
def test_job_that_is_not_queued_is_skipped(store, status):
    job_id = new_job(store)
    store.update(job_id, status=status)
    run_job(store, job_id, pipeline=failing_pipeline(AssertionError("must not run")))
    assert store.status(job_id).status == status


def test_deleted_job_is_skipped(store):
    job_id = new_job(store)
    store.delete(job_id)
    run_job(store, job_id, pipeline=failing_pipeline(AssertionError("must not run")))
    assert store.status(job_id) is None


def test_sources_use_file_names_as_titles(store):
    seen = []
    job_id, _ = store.create(resolve_options(JobOptions()),
                             [Upload("a.pdf", b"%PDF-1"), Upload("a.pdf", b"%PDF-2")])
    run_job(store, job_id, pipeline=lambda sources, **kw: seen.extend(sources) or {"learning_objects": []})
    assert [s["title"] for s in seen] == ["a.pdf", "a-2.pdf"]


def test_timeout_is_reported_as_such(store):
    from rq.timeouts import JobTimeoutException
    job_id = new_job(store)
    run_job(store, job_id, pipeline=failing_pipeline(JobTimeoutException("too long")))
    error = store.status(job_id).error
    assert error.code == "internal_error" and error.details["reason"] == "timeout"


# --- restart recovery ---------------------------------------------------------------------

class WorkerKilled(BaseException):
    """Like a killed process: not an Exception, so run_job cannot turn it into 'failed'."""


def test_recover_jobs(store):
    ids = {name: new_job(store) for name in
           ("queued", "lost_once", "lost_twice", "lost_cancelled", "completed", "failed")}
    for name in ("lost_once", "lost_twice", "lost_cancelled"):
        store.update(ids[name], status="running", stage="units")
        store.increment_attempts(ids[name])
    store.increment_attempts(ids["lost_twice"])
    store.request_cancel(ids["lost_cancelled"])
    store.update(ids["completed"], status="completed")
    store.update(ids["failed"], status="failed")

    queued = []
    counts = worker.recover_jobs(store, queued.append)

    assert counts == {"requeued": 2, "worker_lost": 1, "cancelled": 1}
    assert sorted(queued) == sorted([ids["queued"], ids["lost_once"]])
    assert store.status(ids["lost_once"]).status == "queued"
    lost = store.status(ids["lost_twice"])
    assert lost.status == "failed" and lost.error.code == "worker_lost"
    assert lost.error.details == {"stage": "units", "attempt": 2} and lost.retryable_until is not None
    assert store.status(ids["lost_cancelled"]).status == "cancelled"
    assert store.status(ids["completed"]).status == "completed"
    assert store.status(ids["failed"]).status == "failed"


def test_restarted_worker_continues_from_checkpoint(store, fake_llm, monkeypatch):
    cassette, calls = llm_service._backend, []

    def dies_after_5(*args, **kwargs):
        if len(calls) == 5:
            raise WorkerKilled()
        calls.append(1)
        return cassette(*args, **kwargs)
    monkeypatch.setattr(llm_service, "_backend", dies_after_5)

    job_id = new_job(store)
    with pytest.raises(WorkerKilled):
        run_job(store, job_id)
    assert store.status(job_id).status == "running"  # nobody marked it: the worker is "dead"

    counting = []
    monkeypatch.setattr(llm_service, "_backend", lambda *a, **k: counting.append(1) or cassette(*a, **k))
    queued = []
    worker.recover_jobs(store, queued.append)  # the new worker starts
    assert queued == [job_id]
    run_job(store, job_id)
    assert store.status(job_id).status == "completed"

    fresh = []
    monkeypatch.setattr(llm_service, "_backend", lambda *a, **k: fresh.append(1) or cassette(*a, **k))
    run_job(store, new_job(store))
    assert len(counting) == len(fresh) - 5  # the 5 answers from before the "crash" were reused


def test_job_runs_through_rq(store, fake_llm, monkeypatch):
    """The real path: enqueue_job -> RQ worker -> process_job -> run_job."""
    from rq import Queue, SimpleWorker
    from service.jobqueue import QUEUE_NAME, enqueue_job

    monkeypatch.setattr(worker, "store_from_settings", lambda settings: store)
    queue = Queue(QUEUE_NAME, connection=fakeredis.FakeRedis())
    job_id = new_job(store)
    enqueue_job(queue, job_id)
    SimpleWorker([queue], connection=queue.connection).work(burst=True)
    assert store.status(job_id).status == "completed"
    assert store.read_output(job_id, "result")["units"][0]["questions"]
