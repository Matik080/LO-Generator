"""Failure scenarios of the service in real containers, at no cost (sprint 2, task 5).

Starts its own compose stack (project "logen-resilience": api on 127.0.0.1:18000, 2 workers,
scheduler, Redis and the fake LLM from docker-compose.fake-llm.yml), runs the scenarios one after
another, prints PASS / FAIL for each and deletes the stack (with its volumes) at the end.
Your own stack and its data are not touched. No real LLM is called.

    cd fastapi
    docker build -t lo-generator-fastapi:latest .     # the image must contain the current code
    python tools/resilience_check.py                  # about 6-8 minutes
    python tools/resilience_check.py --keep           # leave the stack running afterwards (debugging)
    python tools/resilience_check.py --only 3 4       # only some scenarios

Runs on the host with the Python standard library only. Needs Docker and fastapi/.env
(LOGEN_REDIS_PASSWORD); the API key, webhook secret and short heartbeat times are set here.

Scenarios:
  1  API restarted right after 202          -> the job is not lost, completes
  2  Redis restarted while a job runs        -> the job completes
  3  worker killed while running a job       -> another attempt continues from the checkpoint
  4  worker killed twice during one job      -> failed with worker_lost
  5  retry after failed (the job from 4)     -> same job_id completes
  6  running job cancelled                   -> cancelled, no result
  7  waiting job cancelled                   -> cancelled, never started
  8  webhook refused once                    -> delivered again with a valid signature
  9  same job three times in the queue       -> runs once (one worker per job)
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

FASTAPI_DIR = Path(__file__).resolve().parent.parent
DOCUMENT = FASTAPI_DIR / "tests" / "fixtures" / "c_memory.html"
PROJECT = "logen-resilience"
API = "http://127.0.0.1:18000"
FAKE = "http://127.0.0.1:18080"
API_KEY = "resilience-key"
WEBHOOK_SECRET = "resilience-secret"

# short times so a lost worker is noticed within seconds (defaults: 60 s and 60 s)
OVERRIDE = f"""
services:
  api:
    ports: ["127.0.0.1:18000:8000"]
    environment: &env
      LOGEN_API_KEYS: {API_KEY}
      LOGEN_WEBHOOK_ALLOWED_HOSTS: fake-llm
      LOGEN_WEBHOOK_SECRET: {WEBHOOK_SECRET}
      LOGEN_HEARTBEAT_TTL_S: "6"
      LOGEN_RECOVERY_INTERVAL_S: "5"
  worker:
    environment: *env
  scheduler:
    environment: *env
  fake-llm:
    ports: ["127.0.0.1:18080:8080"]
    environment:
      FAKE_LLM_DELAY_S: "0.5"
      FAKE_WEBHOOK_SECRET: {WEBHOOK_SECRET}
"""


class Failed(Exception):
    """A scenario check did not hold."""


# --- docker --------------------------------------------------------------------------------

class Stack:
    def __init__(self, override_file: Path):
        self.files = ["-f", "docker-compose.yml", "-f", "docker-compose.fake-llm.yml", "-f", str(override_file)]

    def compose(self, *args, capture=False) -> str:
        result = subprocess.run(["docker", "compose", "-p", PROJECT, *self.files, *args], cwd=FASTAPI_DIR,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{result.stderr[-2000:]}")
        return result.stdout if capture else ""

    def redis(self, code: str) -> str:
        """Runs Python with a Redis client `r` inside the api container; returns what it prints."""
        script = ("import os\nfrom redis import Redis\n"
                  "r = Redis.from_url(os.environ['LOGEN_REDIS_URL'], decode_responses=True)\n" + code)
        return self.compose("exec", "-T", "api", "python", "-c", script, capture=True).strip()

    def job_field(self, job_id: str, field: str) -> str:
        return self.redis(f"print(r.hget('logen:job:{job_id}', '{field}') or '')")

    def job_worker(self, job_id: str) -> str:
        """Container id (= hostname) of the worker holding the job's heartbeat, '' when none."""
        owner = self.redis(f"print(r.get('logen:heartbeat:{job_id}') or '')")
        return owner.split(":")[0]

    def worker_logs(self) -> str:
        return self.compose("logs", "--no-color", "worker", capture=True)


def docker(*args) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


# --- HTTP ----------------------------------------------------------------------------------

def request(method: str, url: str, body: bytes = None, headers: dict = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method=method, headers={"X-API-Key": API_KEY, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = response.read()
            return response.status, json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        data = e.read()
        return e.code, json.loads(data) if data else {}


def create_job(**options) -> str:
    options.setdefault("external_ref", f"resilience-{uuid.uuid4().hex[:8]}")
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="options"\r\n\r\n{json.dumps(options)}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{DOCUMENT.name}"\r\n'
        f'Content-Type: text/html\r\n\r\n'.encode() + DOCUMENT.read_bytes() + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    status, body = request("POST", f"{API}/v1/jobs", b"".join(parts),
                           {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    if status != 202:
        raise Failed(f"POST /v1/jobs answered {status}: {body}")
    return body["job_id"]


def job(job_id: str) -> dict:
    status, body = request("GET", f"{API}/v1/jobs/{job_id}")
    if status != 200:
        raise Failed(f"GET /v1/jobs/{job_id} answered {status}: {body}")
    return body


def fake_stats() -> dict:
    return request("GET", f"{FAKE}/stats")[1]


def wait_for(what: str, check, timeout: float = 120, interval: float = 0.5):
    """Calls check() until it returns something truthy; Failed after `timeout` seconds.
    Connection errors (e.g. the API is restarting) count as 'not yet'."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = check()
            if value:
                return value
        except (urllib.error.URLError, ConnectionError, TimeoutError, RuntimeError):
            pass
        time.sleep(interval)
    raise Failed(f"timed out after {timeout:.0f} s waiting for: {what}")


def wait_status(job_id: str, wanted: str, timeout: float = 120) -> dict:
    def check():
        state = job(job_id)
        if state["status"] in ("completed", "failed", "cancelled") and state["status"] != wanted:
            raise Failed(f"job ended as {state['status']} (error {state['error']}), expected {wanted}")
        return state if state["status"] == wanted else None
    return wait_for(f"job {job_id[:8]} {wanted}", check, timeout)


def wait_running(job_id: str, min_progress: float = 0.15) -> dict:
    return wait_for(f"job {job_id[:8]} running past {min_progress:.0%}",
                    lambda: (lambda s: s if s["status"] == "running" and s["progress"] >= min_progress else None)(
                        job(job_id)), timeout=90, interval=0.2)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise Failed(message)


# --- scenarios -----------------------------------------------------------------------------

def s1_api_restart_after_202(stack: Stack, memo: dict) -> str:
    before = fake_stats()["answered"]
    job_id = create_job()
    stack.compose("restart", "api")
    wait_for("API healthy again", lambda: request("GET", f"{API}/health")[0] == 200, timeout=90)
    wait_status(job_id, "completed")
    memo["answers_per_job"] = fake_stats()["answered"] - before  # one job alone, no crash
    return f"completed after API restart ({memo['answers_per_job']} LLM answers)"


def s2_redis_restart(stack: Stack, memo: dict) -> str:
    job_id = create_job()
    wait_running(job_id)
    stack.compose("restart", "redis")
    state = wait_status(job_id, "completed", timeout=240)
    return f"completed after Redis restart (attempts {stack.job_field(job_id, 'attempts')}, stage {state['stage']})"


def s3_worker_killed_once(stack: Stack, memo: dict) -> str:
    before = fake_stats()["answered"]
    job_id = create_job()
    wait_running(job_id, 0.3)
    container = wait_for("worker of the job", lambda: stack.job_worker(job_id))
    docker("kill", container)
    time.sleep(2)
    docker("start", container)  # like a restart after a crash
    wait_status(job_id, "completed", timeout=180)
    attempts = stack.job_field(job_id, "attempts")
    answers = fake_stats()["answered"] - before
    expect(attempts == "2", f"expected 2 attempts, got {attempts}")
    single = memo.get("answers_per_job")
    if single:  # the second attempt reused the answers saved before the crash
        expect(answers < 2 * single, f"{answers} LLM answers for one job (alone: {single}): checkpoint not used")
    return f"completed in attempt 2, {answers} LLM answers (one job alone: {single})"


def s4_worker_killed_twice(stack: Stack, memo: dict) -> str:
    job_id = create_job()
    wait_running(job_id, 0.3)
    first = wait_for("worker of the job", lambda: stack.job_worker(job_id))
    docker("kill", first)
    # the other worker takes the job over once the heartbeat expired (6 s) and recovery ran (5 s)
    second = wait_for("second attempt on another worker",
                      lambda: (lambda w: w if w and w != first and stack.job_field(job_id, "attempts") == "2"
                               else None)(stack.job_worker(job_id)), timeout=60)
    docker("kill", second)
    try:
        state = wait_status(job_id, "failed", timeout=60)
    finally:
        docker("start", first)
        docker("start", second)
    expect(state["error"]["code"] == "worker_lost", f"error {state['error']}, expected worker_lost")
    memo["failed_job"] = job_id
    return f"failed with worker_lost after 2 attempts ({state['error']['message']})"


def s5_retry_after_failed(stack: Stack, memo: dict) -> str:
    job_id = memo.get("failed_job")
    if not job_id:
        raise Failed("needs the failed job of scenario 4")
    status, body = request("POST", f"{API}/v1/jobs/{job_id}/retry")
    expect(status == 200 and body["status"] == "queued", f"retry answered {status}: {body}")
    wait_status(job_id, "completed", timeout=180)
    status, result = request("GET", f"{API}/v1/jobs/{job_id}/result")
    expect(status == 200 and result["units"][0]["questions"], f"result answered {status}")
    return "same job_id completed after POST /retry"


def s6_cancel_running(stack: Stack, memo: dict) -> str:
    job_id = create_job()
    wait_running(job_id)
    status, body = request("DELETE", f"{API}/v1/jobs/{job_id}")
    expect(status == 200, f"DELETE answered {status}: {body}")
    wait_status(job_id, "cancelled", timeout=30)
    time.sleep(5)
    expect(job(job_id)["status"] == "cancelled", "status changed after cancel")
    status, _ = request("GET", f"{API}/v1/jobs/{job_id}/result")
    expect(status == 409, f"/result after cancel answered {status}, expected 409")
    return "cancelled while running, no result"


def s7_cancel_waiting(stack: Stack, memo: dict) -> str:
    stack.compose("stop", "worker")
    try:
        job_id = create_job()
        status, body = request("DELETE", f"{API}/v1/jobs/{job_id}")
        expect(status == 200 and body.get("status") == "cancelled", f"DELETE answered {status}: {body}")
    finally:
        stack.compose("start", "worker")
    time.sleep(10)  # the workers take the queued id and must skip it
    expect(job(job_id)["status"] == "cancelled", "a cancelled job changed its status")
    attempts = stack.job_field(job_id, "attempts")
    expect(attempts == "0", f"the cancelled job was started ({attempts} attempts)")
    return "cancelled before it started; workers skipped it"


def s8_webhook_retry(stack: Stack, memo: dict) -> str:
    job_id = create_job(callback_url="http://fake-llm:8080/webhook")
    wait_status(job_id, "completed")

    def completed_delivered():
        attempts = [a for a in request("GET", f"{FAKE}/webhooks")[1]["attempts"]
                    if a["job_id"] == job_id and a["event"] == "job.completed"]
        return attempts if any(a["answered"] == 200 for a in attempts) else None
    # 10 s retry; up to 60 s more when Redis restarted before (scenario 2), see service/scheduler.py
    attempts = wait_for("job.completed delivered after a retry", completed_delivered, timeout=120)
    expect([a["answered"] for a in attempts] == [500, 200], f"attempts {attempts}")
    expect(len({a["delivery"] for a in attempts}) == 1, "the retry has another delivery id")
    every = [a for a in request("GET", f"{FAKE}/webhooks")[1]["attempts"] if a["job_id"] == job_id]
    expect(all(a["signature_ok"] for a in every), "a webhook had an invalid signature")
    return f"refused once, delivered on retry; {len(every)} signed attempts for this job"


def s9_one_worker_per_job(stack: Stack, memo: dict) -> str:
    stack.compose("stop", "worker")
    try:
        job_id = create_job()
        stack.compose("exec", "-T", "api", "python", "-c",
                      "import os\nfrom service.jobqueue import enqueue_job, make_queue\n"
                      f"q = make_queue(os.environ['LOGEN_REDIS_URL'])\n"
                      f"enqueue_job(q, '{job_id}')\nenqueue_job(q, '{job_id}')")
    finally:
        stack.compose("start", "worker")
    wait_status(job_id, "completed")
    time.sleep(3)  # the copies are taken and skipped
    started = stack.worker_logs().count(f"Job {job_id}: attempt")
    attempts = stack.job_field(job_id, "attempts")
    expect(started == 1 and attempts == "1", f"started {started} times, {attempts} attempts")
    return "three times in the queue, run once"


SCENARIOS = [s1_api_restart_after_202, s2_redis_restart, s3_worker_killed_once, s4_worker_killed_twice,
             s5_retry_after_failed, s6_cancel_running, s7_cancel_waiting, s8_webhook_retry, s9_one_worker_per_job]


# --- main ----------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--keep", action="store_true", help="leave the stack running at the end")
    parser.add_argument("--only", type=int, nargs="+", help="scenario numbers to run (default: all)")
    args = parser.parse_args()
    chosen = [(n, s) for n, s in enumerate(SCENARIOS, 1) if not args.only or n in args.only]

    override = Path(tempfile.mkdtemp()) / "resilience.override.yml"
    override.write_text(OVERRIDE, encoding="utf-8")
    stack = Stack(override)
    print(f"Starting stack '{PROJECT}' (2 workers, fake LLM)...", flush=True)
    stack.compose("down", "-v", "--remove-orphans")
    stack.compose("up", "-d", "--scale", "worker=2")
    results = []
    try:
        wait_for("API healthy", lambda: request("GET", f"{API}/health")[0] == 200, timeout=120)
        wait_for("fake LLM up", lambda: request("GET", f"{FAKE}/health")[0] == 200, timeout=60)
        memo: dict = {}
        for n, scenario in chosen:
            name = scenario.__name__.split("_", 1)[1].replace("_", " ")
            print(f"[{n}] {name} ...", end=" ", flush=True)
            start = time.monotonic()
            try:
                detail, ok = scenario(stack, memo), True
            except Exception as e:  # a failed check or a broken step: report it, go on
                detail, ok = f"{type(e).__name__}: {e}", False
            seconds = time.monotonic() - start
            results.append((n, name, ok, detail))
            print(f"{'PASS' if ok else 'FAIL'} ({seconds:.0f} s) - {detail}", flush=True)
    finally:
        if args.keep:
            print(f"Stack left running: docker compose -p {PROJECT} {' '.join(stack.files)} down -v")
        else:
            stack.compose("down", "-v", "--remove-orphans")

    passed = sum(ok for *_, ok, _ in results)
    print(f"\n{passed} / {len(results)} scenarios passed.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
