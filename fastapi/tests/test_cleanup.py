"""Deleting old inputs and jobs (service/cleanup.py, sprint task 7). A fake clock moves time."""

import threading
import time
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from lo_pipeline.contract import JobError, JobOptions
from lo_pipeline.contract.options import resolve_options
from service.cleanup import sweep
from service.errors import ApiError
from service.redis_backend import RedisBackend
from service.scheduler import cleanup_loop
from service.settings import ServiceSettings
from service.store import JobStore
from service.uploads import Upload
from service.worker import run_job

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
WEEK = timedelta(days=7)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return JobStore(fakeredis.FakeRedis(decode_responses=True), str(tmp_path), clock=clock)


def new_job(store, ref=None):
    job_id, _ = store.create(resolve_options(JobOptions(external_ref=ref)), [Upload("a.pdf", b"%PDF-1.4 x")])
    (store.checkpoint_dir(job_id)).mkdir()
    (store.checkpoint_dir(job_id) / "llm_calls.jsonl").write_text("{}", encoding="utf-8")
    return job_id


def has_inputs(store, job_id):
    return (store.job_dir(job_id) / "input").exists() or store.checkpoint_dir(job_id).exists()


def test_inputs_of_finished_jobs_go_at_once_failed_after_24_hours(store, clock):
    completed, cancelled, failed, queued, running = (new_job(store) for _ in range(5))
    store.update(completed, status="completed")
    store.update(cancelled, status="cancelled")
    store.mark_failed(failed, JobError(code="llm_error", message="x"))
    store.update(running, status="running", stage="units")

    assert sweep(store, WEEK)["inputs_deleted"] == 2
    assert not has_inputs(store, completed) and not has_inputs(store, cancelled)
    assert has_inputs(store, failed) and has_inputs(store, queued) and has_inputs(store, running)
    assert (store.job_dir(completed) / "job.json").exists()  # state and job.json stay

    clock.now += timedelta(hours=23)
    sweep(store, WEEK)
    assert has_inputs(store, failed)  # still retryable
    clock.now += timedelta(hours=1)
    sweep(store, WEEK)
    assert not has_inputs(store, failed)
    assert has_inputs(store, queued) and has_inputs(store, running)  # active jobs are never touched


def test_finished_jobs_are_deleted_after_the_ttl(store, clock):
    old, new, active = new_job(store, ref="gen-old"), new_job(store), new_job(store)
    store.update(old, status="completed")
    clock.now += timedelta(days=3)
    store.update(new, status="failed")
    store.update(active, status="running", stage="graph")

    clock.now += timedelta(days=4)  # old: 7 days since its last change, new: 4
    counts = sweep(store, WEEK)
    assert counts["jobs_deleted"] == 1
    assert store.status(old) is None and not store.job_dir(old).exists()
    assert store.find("gen-old") is None  # the external_ref is free again
    assert store.status(new) is not None and store.status(active) is not None

    clock.now += timedelta(days=30)
    sweep(store, WEEK)
    assert store.status(active) is not None  # a running job is never deleted, however old


def test_folder_without_state_is_deleted_when_old(store, clock):
    orphan = store.jobs_dir / "lost-job"
    (orphan / "input").mkdir(parents=True)
    assert sweep(store, WEEK)["orphan_folders_deleted"] == 0  # new: could be a job being created
    clock.now = datetime.fromtimestamp(time.time(), tz=timezone.utc) + WEEK
    assert sweep(store, WEEK)["orphan_folders_deleted"] == 1 and not orphan.exists()


def test_worker_deletes_inputs_when_completed(store):
    job_id = new_job(store)
    run_job(store, job_id, pipeline=lambda sources, **kw: {"learning_objects": [], "timings": {}})
    assert store.status(job_id).status == "failed"  # 0 LOs -> llm_error: inputs kept for a retry
    assert has_inputs(store, job_id)

    import json
    from conftest import FIXTURES
    los = json.loads((FIXTURES / "snapshots" / "c_memory_chain.json").read_text(encoding="utf-8"))["learning_objects"]
    job_id = new_job(store)
    run_job(store, job_id, pipeline=lambda sources, **kw: {"learning_objects": los, "timings": {}, "warnings": []})
    assert store.status(job_id).status == "completed" and not has_inputs(store, job_id)
    assert (store.job_dir(job_id) / "result.json").exists()


def test_cancel_deletes_inputs(store):
    backend = RedisBackend(store, enqueue=lambda job_id: None)
    queued = new_job(store)
    backend.cancel_or_delete(queued)
    assert not has_inputs(store, queued)  # never ran: deleted at once

    running = new_job(store)

    def pipeline(sources, progress_callback, checkpoint):
        progress_callback("units", 1, 2)
        backend.cancel_or_delete(running)
        assert has_inputs(store, running)  # the API leaves a running job's files to its worker
        progress_callback("units", 2, 2)
    run_job(store, running, pipeline=pipeline)
    assert store.status(running).status == "cancelled" and not has_inputs(store, running)


def test_no_retry_without_inputs(store, clock):
    backend = RedisBackend(store, enqueue=lambda job_id: None)
    job_id = new_job(store)
    store.mark_failed(job_id, JobError(code="llm_error", message="x"))
    store.delete_inputs(job_id)  # e.g. deleted by hand
    with pytest.raises(ApiError) as error:
        backend.retry(job_id)
    assert error.value.code == "job_not_retryable"


def test_retry_window_from_settings(tmp_path, clock):
    store = JobStore(fakeredis.FakeRedis(decode_responses=True), str(tmp_path), clock=clock,
                     retryable_for=timedelta(hours=2))
    job_id = new_job(store)
    store.mark_failed(job_id, JobError(code="llm_error", message="x"))
    assert store.status(job_id).retryable_until == T0 + timedelta(hours=2)


def test_settings():
    s = ServiceSettings.from_env({})
    assert (s.result_ttl_days, s.failed_input_ttl_hours, s.cleanup_interval_s) == (7, 24, 600)
    s = ServiceSettings.from_env({"LOGEN_RESULT_TTL_DAYS": "1.5", "LOGEN_FAILED_INPUT_TTL_HOURS": "6",
                                  "LOGEN_CLEANUP_INTERVAL_S": "60"})
    assert (s.result_ttl_days, s.failed_input_ttl_hours, s.cleanup_interval_s) == (1.5, 6, 60)


def test_cleanup_loop_survives_errors_and_stops(store, monkeypatch):
    calls = []

    def flaky(store, ttl):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk busy")
        return {"inputs_deleted": 0}
    monkeypatch.setattr("service.cleanup.sweep", flaky)
    stop = threading.Event()
    thread = threading.Thread(target=cleanup_loop, args=(store, WEEK, 0.01, stop))
    thread.start()
    while len(calls) < 3:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()  # kept going after the error, stopped when asked
