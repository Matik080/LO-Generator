"""Job folder + Redis state (service/store.py), with fakeredis instead of a Redis server."""

import json
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from lo_pipeline.contract import JobError, JobOptions
from service.store import JobStore, StoredJob
from service.uploads import Upload

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
PDF = Upload("modul1.pdf", b"%PDF-1.4 test")


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


def test_create_writes_folder_and_queued_state(store):
    options = JobOptions(external_ref="gen-1")
    job_id, created = store.create(options, [PDF, Upload("page.html", b"<p>x</p>")])
    assert created

    job = store.load(job_id)
    assert job == StoredJob(job_id=job_id, options=options, files=["modul1.pdf", "page.html"])
    assert [p.read_bytes() for p in store.input_paths(job)] == [PDF.content, b"<p>x</p>"]

    status = store.status(job_id)
    assert (status.status, status.stage, status.progress, status.error) == ("queued", None, 0.0, None)
    assert status.external_ref == "gen-1"
    assert status.created_at == status.updated_at == T0
    assert store.all_ids() == [job_id] and store.active_count() == 1


def test_same_file_name_twice_is_kept_twice(store):
    job_id, _ = store.create(JobOptions(), [PDF, Upload("Modul1.PDF", b"%PDF other")])
    assert store.load(job_id).files == ["modul1.pdf", "Modul1-2.PDF"]


def test_same_external_ref_returns_existing_job(store):
    first, created = store.create(JobOptions(external_ref="gen-1"), [PDF])
    second, created_again = store.create(JobOptions(external_ref="gen-1"), [PDF])
    assert created and not created_again and second == first
    assert store.all_ids() == [first]  # nothing new was saved
    assert store.find("gen-1") == first and store.find("other") is None


def test_external_ref_is_free_again_after_delete(store):
    first, _ = store.create(JobOptions(external_ref="gen-1"), [PDF])
    store.delete(first)
    assert not store.exists(first) and not store.job_dir(first).exists()
    second, created = store.create(JobOptions(external_ref="gen-1"), [PDF])
    assert created and second != first


def test_failed_write_leaves_nothing_behind(store, monkeypatch):
    def broken(*args):
        raise OSError("disk full")
    monkeypatch.setattr(store, "_write_folder", broken)
    with pytest.raises(OSError):
        store.create(JobOptions(external_ref="gen-1"), [PDF])
    assert store.all_ids() == [] and store.find("gen-1") is None
    assert not store.jobs_dir.exists() or not any(store.jobs_dir.iterdir())


def test_update_progress_and_fail(store, clock):
    job_id, _ = store.create(JobOptions(), [PDF])
    clock.now = T0 + timedelta(minutes=1)
    store.update(job_id, status="running", stage="units", progress=0.2)
    status = store.status(job_id)
    assert (status.status, status.stage, status.progress, status.updated_at) == ("running", "units", 0.2, clock.now)

    store.mark_failed(job_id, JobError(code="llm_error", message="Groq down", details={"stage": "units"}))
    status = store.status(job_id)
    assert status.status == "failed" and status.error.code == "llm_error"
    assert status.error.details == {"stage": "units"}
    assert status.retryable_until == clock.now + timedelta(hours=24)
    assert store.active_count() == 0

    store.update(job_id, status="queued", error=None, retryable_until=None)  # a retry clears both
    status = store.status(job_id)
    assert status.error is None and status.retryable_until is None


def test_attempts_and_cancel_flag(store):
    job_id, _ = store.create(JobOptions(), [PDF])
    assert store.increment_attempts(job_id) == 1
    assert store.increment_attempts(job_id) == 2
    assert not store.cancel_requested(job_id)
    store.request_cancel(job_id)
    assert store.cancel_requested(job_id)


def test_output_round_trip(store):
    job_id, _ = store.create(JobOptions(), [PDF])
    store.save_output(job_id, {"units": []}, {"sections": []})
    assert store.read_output(job_id, "result") == {"units": []}
    assert store.read_output(job_id, "report") == {"sections": []}
    assert not list(store.job_dir(job_id).glob("*.tmp"))


def test_unknown_job(store):
    assert store.status("nope") is None and store.fields("nope") is None and not store.exists("nope")


def test_job_json_keeps_complete_options(store):
    from lo_pipeline.contract.options import resolve_options
    options = resolve_options(JobOptions(questions={"hints": 1}))
    job_id, _ = store.create(options, [PDF])
    saved = json.loads((store.job_dir(job_id) / "job.json").read_text(encoding="utf-8"))
    assert saved["options"]["questions"]["hints"] == 1
    assert saved["options"]["questions"]["choice_options"] == 4
    assert store.load(job_id).options == options


def test_heartbeat_claim_is_exclusive_and_owned(store):
    job_id, _ = store.create(JobOptions(), [PDF])
    assert not store.has_live_worker(job_id)
    assert store.claim_job(job_id, "worker-a")
    assert not store.claim_job(job_id, "worker-b")  # a live claim: the second worker must not run the job
    assert store.has_live_worker(job_id)
    assert 0 < store.redis.ttl(store._heartbeat_key(job_id)) <= store.heartbeat_ttl_s

    assert store.renew_claim(job_id, "worker-a")
    assert not store.renew_claim(job_id, "worker-b")  # cannot renew another worker's claim
    store.release_claim(job_id, "worker-b")           # nor release it
    assert store.has_live_worker(job_id)

    store.release_claim(job_id, "worker-a")
    assert not store.has_live_worker(job_id)
    assert not store.renew_claim(job_id, "worker-a")  # gone (like an expired claim)
    assert store.claim_job(job_id, "worker-b")
