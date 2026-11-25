"""The fake LLM server (tools/fake_llm_server.py) answers B's real HTTP client from the cassette."""

import json
import threading
from http.server import ThreadingHTTPServer

import fakeredis
import pytest

from conftest import CASSETTE, FIXTURES
from lo_pipeline.contract import JobOptions
from lo_pipeline.contract.options import resolve_options
from service.store import JobStore
from service.uploads import Upload
from service.worker import run_job
from tools.fake_llm_server import FakeLLM, FakeWebhookReceiver, make_handler


@pytest.fixture
def server():
    fake = FakeLLM(json.loads(CASSETTE.read_text(encoding="utf-8")))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fake))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield fake, f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()


def test_job_completes_against_the_fake_server(server, tmp_path, monkeypatch):
    """The same path as in the containers: worker -> OpenAI client -> HTTP -> fake server."""
    fake, url = server
    monkeypatch.setenv("LOGEN_LLM_BASE_URL", url)
    monkeypatch.setenv("LOGEN_LLM_API_KEY", "fake")
    store = JobStore(fakeredis.FakeRedis(decode_responses=True), str(tmp_path))
    upload = Upload("c_memory.html", (FIXTURES / "c_memory.html").read_bytes())
    job_id, _ = store.create(resolve_options(JobOptions()), [upload])

    run_job(store, job_id)

    status = store.status(job_id)
    assert status.status == "completed", status.error
    assert store.read_output(job_id, "result")["units"][0]["questions"]
    assert fake.stats["unknown"] == 0 and fake.stats["answered"] > 20


def test_unknown_prompt_gets_an_error(server):
    from openai import InternalServerError, OpenAI
    fake, url = server
    client = OpenAI(api_key="fake", base_url=url, max_retries=0)
    with pytest.raises(InternalServerError):
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hello"}])
    assert fake.stats == {"answered": 0, "unknown": 1}


def test_webhook_receiver_refuses_first_attempt_and_checks_signature():
    from service.webhooks import signature
    receiver = FakeWebhookReceiver("secret")
    body = b'{"job_id": "j1", "event": "job.completed"}'

    def headers(secret, ts=1700000000, delivery="d1"):
        return {"X-QGen-Timestamp": str(ts), "X-QGen-Delivery": delivery, "X-QGen-Event": "job.completed",
                "X-QGen-Signature": signature(secret, ts, body)}
    assert receiver.receive(headers("secret"), body) == 500           # first attempt: refused
    assert receiver.receive(headers("secret", ts=1700000010), body) == 200  # retry, fresh timestamp
    assert receiver.receive(headers("wrong", delivery="d2"), body) == 401
    assert [a["answered"] for a in receiver.attempts] == [500, 200, 401]
    assert receiver.attempts[0]["job_id"] == "j1"
