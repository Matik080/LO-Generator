"""Webhooks (service/webhooks.py, sprint task 7): events, payload, signature, delivery and retries."""

import hashlib
import hmac
import json

import fakeredis
import httpx
import pytest
from pydantic import ValidationError

from conftest import FIXTURES
from lo_pipeline.contract import JobError, JobOptions, WebhookPayload
from lo_pipeline.contract.options import resolve_options
from lo_pipeline.llm.service import LLMError
from service import webhooks
from service.redis_backend import RedisBackend
from service.settings import ServiceSettings
from service.store import JobStore
from service.uploads import Upload
from service.webhooks import WebhookDeliveryFailed, WebhookNotifier, send, signature
from service.worker import recover_jobs, run_job

URL = "http://eceh-backend/api/internal/qgen/webhook"
SNAPSHOT_LOS = json.loads((FIXTURES / "snapshots" / "c_memory_chain.json").read_text(encoding="utf-8"))[
    "learning_objects"]


# --- signature and one delivery attempt ---------------------------------------------------

def test_signature_is_what_laravel_computes():
    """PHP: 'sha256=' . hash_hmac('sha256', $ts . '.' . $body, $secret)"""
    body = b'{"event":"job.completed"}'
    expected = "sha256=" + hmac.new(b"s3cret", b"1793701125." + body, hashlib.sha256).hexdigest()
    assert signature("s3cret", 1793701125, body) == expected


class Post:
    """Fake httpx.post: answers with the given status codes (or raises) and records the requests."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, url, content, headers, timeout):
        self.requests.append({"url": url, "content": content, "headers": headers, "timeout": timeout})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer)


def test_send_signs_each_attempt_with_its_own_timestamp():
    post = Post(500, 204)
    body = '{"event":"job.completed"}'
    with pytest.raises(WebhookDeliveryFailed, match="HTTP 500"):
        send(URL, body, "job.completed", "d-1", "s3cret", 5, now=lambda: 1000, post=post)
    assert send(URL, body, "job.completed", "d-1", "s3cret", 5, now=lambda: 1370, post=post) == 204

    first, retry = (r["headers"] for r in post.requests)
    assert first["X-QGen-Delivery"] == retry["X-QGen-Delivery"] == "d-1"  # same delivery
    assert (first["X-QGen-Timestamp"], retry["X-QGen-Timestamp"]) == ("1000", "1370")  # fresh time
    assert retry["X-QGen-Signature"] == signature("s3cret", 1370, body.encode())
    assert retry["X-QGen-Event"] == "job.completed" and retry["Content-Type"] == "application/json"
    assert post.requests[1]["content"] == body.encode() and post.requests[1]["timeout"] == 5


def test_send_network_error_is_a_failed_delivery():
    post = Post(httpx.ConnectTimeout("too slow"))
    with pytest.raises(WebhookDeliveryFailed, match="ConnectTimeout"):
        send(URL, "{}", "job.failed", "d-2", "s", 5, post=post)


# --- events from job state changes --------------------------------------------------------

class Recorder:
    """A WebhookNotifier whose queue is a list."""

    def __init__(self):
        self.deliveries = []
        self.notifier = WebhookNotifier(lambda url, body, event, delivery_id:
                                        self.deliveries.append((url, json.loads(body), event, delivery_id)))

    @property
    def events(self):
        return [event for _, _, event, _ in self.deliveries]


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture
def store(tmp_path, recorder):
    return JobStore(fakeredis.FakeRedis(decode_responses=True), str(tmp_path), on_event=recorder.notifier)


def new_job(store, callback_url=URL, external_ref="gen-1"):
    options = resolve_options(JobOptions(callback_url=callback_url, external_ref=external_ref))
    job_id, _ = store.create(options, [Upload("c_memory.html", (FIXTURES / "c_memory.html").read_bytes())])
    return job_id


def finished_pipeline(sources, progress_callback, checkpoint):
    for stage in ("extract", "units", "learning_objects", "lo_evaluation", "graph", "questions", "output"):
        progress_callback(stage, 0, 0)
        progress_callback(stage, 1, 2)  # several reports per stage: still one event per stage
        progress_callback(stage, 2, 2)
    return {"learning_objects": json.loads(json.dumps(SNAPSHOT_LOS)), "timings": {}, "warnings": []}


def test_completed_job_sends_each_stage_once_then_completed(store, recorder):
    job_id = new_job(store)
    run_job(store, job_id, pipeline=finished_pipeline)
    assert recorder.events == ["job.stage_changed"] * 7 + ["job.completed"]
    stages = [body["stage"] for _, body, event, _ in recorder.deliveries if event == "job.stage_changed"]
    assert stages == ["extract", "units", "learning_objects", "lo_evaluation", "graph", "questions", "output"]

    url, body, event, delivery_id = recorder.deliveries[-1]
    payload = WebhookPayload.model_validate(body)  # the contract model
    assert url == URL and payload.job_id == job_id and payload.external_ref == "gen-1"
    assert (payload.status, payload.stage, payload.progress, payload.error) == ("completed", "output", 1.0, None)
    assert len({d for _, _, _, d in recorder.deliveries}) == 8  # a new delivery id per event


def test_failed_job_sends_its_error(store, recorder):
    job_id = new_job(store)

    def pipeline(sources, progress_callback, checkpoint):
        progress_callback("units", 1, 3)
        raise LLMError("Groq is down")
    run_job(store, job_id, pipeline=pipeline)
    assert recorder.events == ["job.stage_changed", "job.stage_changed", "job.failed"]
    body = recorder.deliveries[-1][1]
    assert body["status"] == "failed" and body["error"]["code"] == "llm_error"


def test_cancel_by_api_and_worker_sends_one_event(store, recorder):
    job_id = new_job(store)
    backend = RedisBackend(store, enqueue=lambda job_id: None)

    def pipeline(sources, progress_callback, checkpoint):
        progress_callback("units", 1, 4)
        backend.cancel_or_delete(job_id)   # DELETE: the API sets cancelled
        progress_callback("units", 2, 4)   # the worker notices and sets cancelled again
    run_job(store, job_id, pipeline=pipeline)
    assert recorder.events.count("job.cancelled") == 1
    assert recorder.events[-1] == "job.cancelled"


def test_retry_and_worker_lost(store, recorder):
    job_id = new_job(store)
    store.update(job_id, status="running", stage="graph")
    store.increment_attempts(job_id)
    store.increment_attempts(job_id)
    recover_jobs(store, enqueue=lambda job_id: None)  # second lost worker -> failed
    assert recorder.events == ["job.stage_changed", "job.failed"]
    assert recorder.deliveries[-1][1]["error"]["code"] == "worker_lost"

    backend = RedisBackend(store, enqueue=lambda job_id: None)
    backend.retry(job_id)  # queued again: no event (not one of the four)
    assert recorder.events == ["job.stage_changed", "job.failed"]


def test_no_callback_url_no_webhook(store, recorder):
    job_id = new_job(store, callback_url=None, external_ref=None)
    run_job(store, job_id, pipeline=finished_pipeline)
    assert recorder.deliveries == []


def test_broken_webhook_queue_does_not_stop_the_job(tmp_path):
    def broken(store, job_id, event):
        raise ConnectionError("redis down")
    store = JobStore(fakeredis.FakeRedis(decode_responses=True), str(tmp_path), on_event=broken)
    job_id = new_job(store)
    run_job(store, job_id, pipeline=finished_pipeline)
    assert store.status(job_id).status == "completed"


# --- the queue with retries ---------------------------------------------------------------

def test_rq_delivery_with_retries(monkeypatch):
    from rq import Queue, SimpleWorker
    from rq.job import Job
    from rq.registry import ScheduledJobRegistry

    monkeypatch.setenv("LOGEN_WEBHOOK_ALLOWED_HOSTS", "eceh-backend")
    monkeypatch.setenv("LOGEN_WEBHOOK_SECRET", "s3cret")
    post = Post(503)
    monkeypatch.setattr(httpx, "post", post)

    connection = fakeredis.FakeRedis()
    queue = Queue(webhooks.WEBHOOK_QUEUE, connection=connection)
    webhooks.rq_enqueue(queue)(URL, '{"event":"job.completed"}', "job.completed", "d-9")
    job = queue.jobs[0]
    assert job.retries_left == 3 and job.retry_intervals == [10, 60, 300]

    SimpleWorker([queue], connection=connection).work(burst=True)  # first attempt: HTTP 503
    assert len(post.requests) == 1
    assert post.requests[0]["headers"]["X-QGen-Signature"].startswith("sha256=")
    job = Job.fetch(job.id, connection=connection)
    assert job.retries_left == 2  # the next attempt is scheduled (after 10 s)
    assert job.id in ScheduledJobRegistry(queue=queue).get_job_ids()


# --- settings -----------------------------------------------------------------------------

def test_webhooks_need_a_secret():
    with pytest.raises(ValidationError, match="LOGEN_WEBHOOK_SECRET"):
        ServiceSettings.from_env({"LOGEN_WEBHOOK_ALLOWED_HOSTS": "eceh-backend"})
    settings = ServiceSettings.from_env({"LOGEN_WEBHOOK_ALLOWED_HOSTS": "eceh-backend", "LOGEN_WEBHOOK_SECRET": "s"})
    assert settings.webhooks_enabled and settings.secret() == "s" and "'s'" not in repr(settings)
    assert not ServiceSettings.from_env({}).webhooks_enabled


def test_notifier_only_with_webhooks_on():
    assert webhooks.notifier_from_settings(ServiceSettings.from_env({})) is None
    on = ServiceSettings.from_env({"LOGEN_WEBHOOK_ALLOWED_HOSTS": "eceh", "LOGEN_WEBHOOK_SECRET": "s"})
    assert isinstance(webhooks.notifier_from_settings(on), WebhookNotifier)
