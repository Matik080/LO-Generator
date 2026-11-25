"""A fake OpenAI-compatible LLM server for testing the service in containers at no cost.

It answers POST /v1/chat/completions from the test cassette (tests/fixtures/llm_cassette.json),
so a job with tests/fixtures/c_memory.html and default options completes with the same answers
the tests use. Requests are matched like in tests/conftest.py: hash of model + temperature +
messages (the cassette stores model=None for calls with the default model, so that is tried too).
The worker must run with PYTHONHASHSEED=0, otherwise the prompts come in another order and differ.

    python -m tools.fake_llm_server                # port 8080
    FAKE_LLM_DELAY_S=2 python -m tools.fake_llm_server

Settings (environment):
    FAKE_LLM_PORT      default 8080
    FAKE_LLM_DELAY_S   seconds before each answer (default 0.5), makes a job take a while
    FAKE_LLM_CASSETTE  default tests/fixtures/llm_cassette.json

An unknown prompt gets HTTP 500 (B retries, then the job fails with llm_error) and is logged.
GET /health answers 200, GET /stats the number of answered and unknown requests.
Used by docker-compose.fake-llm.yml.
"""

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lo_pipeline.llm.service import call_key

logger = logging.getLogger("fake_llm")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASSETTE = ROOT / "tests" / "fixtures" / "llm_cassette.json"


class FakeLLM:
    """The answers and counters; separate from HTTP so tests can use it directly."""

    def __init__(self, cassette: dict, delay_s: float = 0.0):
        self.cassette = cassette
        self.delay_s = delay_s
        self.stats = {"answered": 0, "unknown": 0}
        self._lock = threading.Lock()

    def answer(self, request: dict):
        """The recorded answer for an OpenAI chat request, or None when the prompt is unknown."""
        messages, temperature = request.get("messages"), request.get("temperature", 0.2)
        for model in (request.get("model"), None):
            entry = self.cassette.get(call_key(messages, model, temperature))
            if entry is not None:
                with self._lock:
                    self.stats["answered"] += 1
                return entry["response"]
        with self._lock:
            self.stats["unknown"] += 1
        return None


def completion(content: str, model: str) -> dict:
    """The body of an OpenAI chat completion with one answer."""
    return {
        "id": f"fake-{time.time_ns()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def make_handler(fake: FakeLLM):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok"})
            elif self.path == "/stats":
                self._send(200, dict(fake.stats))
            else:
                self._send(404, {"error": {"message": "not found"}})

        def do_POST(self):
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._send(404, {"error": {"message": "not found"}})
                return
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length) or b"{}")
            content = fake.answer(request)
            if content is None:
                logger.warning(f"Unknown prompt (model {request.get('model')}): HTTP 500")
                self._send(500, {"error": {"message": "fake LLM: no recorded answer for this prompt"}})
                return
            if fake.delay_s > 0:
                time.sleep(fake.delay_s)
            self._send(200, completion(content, request.get("model") or "fake"))

        def log_message(self, format, *args):  # one line per request via logging, not stderr
            logger.info(format % args)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.getenv("FAKE_LLM_PORT", "8080"))
    delay = float(os.getenv("FAKE_LLM_DELAY_S", "0.5"))
    path = Path(os.getenv("FAKE_LLM_CASSETTE", str(DEFAULT_CASSETTE)))
    fake = FakeLLM(json.loads(path.read_text(encoding="utf-8")), delay)
    logger.info(f"Fake LLM on port {port}: {len(fake.cassette)} recorded answers, {delay} s delay.")
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(fake)).serve_forever()


if __name__ == "__main__":
    main()
