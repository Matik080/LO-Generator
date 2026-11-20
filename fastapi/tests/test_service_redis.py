"""The /v1 API with the real backend (sprint task 4b): job folder + Redis (fakeredis) + worker.

The queue is a list here; the test runs the worker (run_job) itself with a fake pipeline, so
each state can be checked. The full path through RQ is in test_worker.py.
"""

import json
from datetime import datetime, timedelta, timezone

import fakeredis
import jsonschema
import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURES
from lo_pipeline.contract import CreateJobResponse, ErrorResponse, JobStatus
from lo_pipeline.contract.schemas import load_schema
from lo_pipeline.llm.service import LLMError
from service.main import create_app, make_backend
from service.redis_backend import RedisBackend
from service.settings import ServiceSettings
from service.store import JobStore
from service.worker import run_job

PDF = b"%PDF-1.4 test document"
SNAPSHOT_LOS = json.loads((FIXTURES / "snapshots" / "c_memory_chain.json").read_text(encoding="utf-8"))[
    "learning_objects"]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Service:
    """TestClient + the store and queue behind it."""

    def __init__(self, tmp_path, max_active_jobs=10, enqueue=None):
        self.clock = Clock()
        self.store = JobStore(fakeredis.FakeRedis(decode_responses=True), str(tmp_path), clock=self.clock)
        self.queue: list[str] = []
        backend = RedisBackend(self.store, enqueue or self.queue.append, max_active_jobs=max_active_jobs)
        settings = ServiceSettings(backend="redis", max_active_jobs=max_active_jobs)
        self.client = TestClient(create_app(settings, backend), raise_server_exceptions=False)

    def post(self, options=None, files=None):
        files = files or [("files", ("modul1.pdf", PDF, "application/pdf"))]
        data = {"options": json.dumps(options)} if options is not None else {}
        return self.client.post("/v1/jobs", files=files, data=data)

    def status(self, job_id) -> JobStatus:
        return JobStatus.model_validate(self.client.get(f"/v1/jobs/{job_id}").json())

    def work(self, pipeline):
        """The worker takes every queued job id."""
        while self.queue:
            run_job(self.store, self.queue.pop(0), pipeline=pipeline)


def finished_pipeline(sources, progress_callback, checkpoint):
    for stage in ("extract", "units", "learning_objects", "lo_evaluation", "graph", "questions", "output"):
        progress_callback(stage, 0, 0)
        progress_callback(stage, 1, 1)
    return {"learning_objects": json.loads(json.dumps(SNAPSHOT_LOS)), "timings": {"extract": 0.1}, "warnings": []}


def failing_pipeline(sources, progress_callback, checkpoint):
    progress_callback("units", 1, 3)
    raise LLMError("Groq is down")


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert ErrorResponse.model_validate(response.json()).error.code == code


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path)


# --- lifecycle ----------------------------------------------------------------------------

def test_job_is_saved_queued_processed_and_returned(service):
    created = service.post({"external_ref": "gen-1", "questions": {"hints": 1}})
    assert created.status_code == 202
    body = CreateJobResponse.model_validate(created.json())
    assert body.status == "queued" and not body.replayed
    assert service.queue == [body.job_id]
    job = service.store.load(body.job_id)  # files and complete options are on disk before 202
    assert job.files == ["modul1.pdf"] and job.options.questions.hints == 1
    assert job.options.questions.choice_options == 4  # default filled in
    assert_error(service.client.get(f"/v1/jobs/{body.job_id}/result"), 409, "result_not_ready")

    service.work(finished_pipeline)
    status = service.status(body.job_id)
    assert (status.status, status.stage, status.progress) == ("completed", "output", 1.0)
    result = service.client.get(f"/v1/jobs/{body.job_id}/result").json()
    report = service.client.get(f"/v1/jobs/{body.job_id}/report").json()
    jsonschema.validate(result, load_schema("lesson"))
    jsonschema.validate(report, load_schema("report"))
    assert [h["name"] for h in result["units"][0]["questions"][0]["hints"]] == ["Zdroj"]  # hints: 1 kept
    assert len(report["sections"]) == len(SNAPSHOT_LOS)


def test_same_external_ref_returns_the_existing_job(service):
    first = service.post({"external_ref": "gen-7"}).json()
    again = service.post({"external_ref": "gen-7"})
    assert again.status_code == 200 and again.json()["job_id"] == first["job_id"] and again.json()["replayed"]
    assert service.queue == [first["job_id"]]  # queued once
    listed = service.client.get("/v1/jobs", params={"external_ref": "gen-7"}).json()["jobs"]
    assert [j["job_id"] for j in listed] == [first["job_id"]]
    assert service.client.get("/v1/jobs", params={"external_ref": "nope"}).json() == {"jobs": []}


def test_failed_job_and_retry(service):
    job_id = service.post().json()["job_id"]
    assert_error(service.client.post(f"/v1/jobs/{job_id}/retry"), 409, "job_not_retryable")  # still queued
    service.work(failing_pipeline)
    failed = service.status(job_id)
    assert failed.status == "failed" and failed.error.code == "llm_error"
    assert failed.error.details == {"stage": "units", "attempt": 1}
    assert_error(service.client.get(f"/v1/jobs/{job_id}/result"), 409, "job_not_completed")

    retried = service.client.post(f"/v1/jobs/{job_id}/retry")
    assert retried.status_code == 200 and retried.json()["status"] == "queued"
    assert retried.json()["error"] is None and retried.json()["retryable_until"] is None
    assert service.queue == [job_id] and service.store.fields(job_id)["attempts"] == "0"
    service.work(finished_pipeline)
    assert service.status(job_id).status == "completed"


def test_retry_after_24_hours_is_refused(service):
    job_id = service.post().json()["job_id"]
    service.work(failing_pipeline)
    service.clock.now += timedelta(hours=24)
    assert_error(service.client.post(f"/v1/jobs/{job_id}/retry"), 409, "job_not_retryable")


# --- cancel and delete --------------------------------------------------------------------

def test_cancel_queued_job_then_delete_it(service):
    created = service.post({"external_ref": "gen-1"}).json()
    job_id = created["job_id"]
    cancelled = service.client.delete(f"/v1/jobs/{job_id}")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"

    service.work(lambda *a, **k: pytest.fail("a cancelled job must not run"))
    assert service.status(job_id).status == "cancelled"
    assert_error(service.client.get(f"/v1/jobs/{job_id}/result"), 409, "job_not_completed")

    assert service.client.delete(f"/v1/jobs/{job_id}").status_code == 204
    assert_error(service.client.get(f"/v1/jobs/{job_id}"), 404, "job_not_found")
    assert not service.store.job_dir(job_id).exists()
    assert service.post({"external_ref": "gen-1"}).status_code == 202  # the ref is free again


def test_cancel_running_job_stops_it(service):
    job_id = service.post().json()["job_id"]
    seen = []

    def pipeline(sources, progress_callback, checkpoint):
        progress_callback("units", 1, 4)
        seen.append(service.client.delete(f"/v1/jobs/{job_id}").json()["status"])  # DELETE while running
        progress_callback("units", 2, 4)
        pytest.fail("the worker must stop at the next progress report")
    service.work(pipeline)
    status = service.status(job_id)
    assert seen == ["cancelled"] and status.status == "cancelled" and status.stage == "units"


def test_cancel_after_last_progress_report_gives_no_result(service):
    job_id = service.post().json()["job_id"]

    def pipeline(sources, progress_callback, checkpoint):
        result = finished_pipeline(sources, progress_callback, checkpoint)
        service.client.delete(f"/v1/jobs/{job_id}")  # arrives while the output is being built
        return result
    service.work(pipeline)
    assert service.status(job_id).status == "cancelled"
    assert not (service.store.job_dir(job_id) / "result.json").exists()


def test_delete_during_run_leaves_nothing_behind(service):
    job_id = service.post().json()["job_id"]

    def pipeline(sources, progress_callback, checkpoint):
        progress_callback("units", 1, 4)
        service.client.delete(f"/v1/jobs/{job_id}")   # cancel ...
        service.client.delete(f"/v1/jobs/{job_id}")   # ... and delete before the worker notices
        progress_callback("units", 2, 4)
    service.work(pipeline)
    assert service.store.fields(job_id) is None and service.store.all_ids() == []


# --- limits and failures ------------------------------------------------------------------

def test_too_many_active_jobs(tmp_path):
    service = Service(tmp_path, max_active_jobs=2)
    assert service.post().status_code == 202
    assert service.post().status_code == 202
    response = service.post()
    assert_error(response, 429, "too_many_jobs")
    assert response.headers["Retry-After"]
    service.work(finished_pipeline)  # both finished: room again
    assert service.post().status_code == 202


def test_queue_unavailable_gives_500_and_no_job(tmp_path):
    def broken(job_id):
        raise ConnectionError("redis down")
    service = Service(tmp_path, enqueue=broken)
    assert_error(service.post({"external_ref": "gen-1"}), 500, "internal_error")
    assert service.store.all_ids() == [] and service.store.find("gen-1") is None


def test_health(service):
    assert service.client.get("/health").json() == {
        "status": "ok", "components": {"api": "ok", "redis": "ok", "backend": "redis"}}

    class DownRedis:
        def ping(self):
            raise ConnectionError("down")
    service.store.redis = DownRedis()
    response = service.client.get("/health")
    assert response.status_code == 503 and response.json()["components"]["redis"] == "error"


def test_info_names_the_backend(service):
    assert service.client.get("/v1/info").json()["backend"] == "redis"


def test_make_backend_from_settings(tmp_path):
    # no Redis server needed: connections open on first use
    backend = make_backend(ServiceSettings(backend="redis", redis_url="redis://nowhere:6379/0",
                                           data_dir=str(tmp_path)))
    assert isinstance(backend, RedisBackend)
    with pytest.raises(ValueError):
        make_backend(ServiceSettings(backend="other"))
