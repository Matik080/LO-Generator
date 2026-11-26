"""The /v1 API with the mock backend: request checks, job states, errors (sprint task 1).

The mock gets a fake clock, so a job goes through its states without waiting.
"""

import json
from datetime import datetime, timedelta, timezone

import jsonschema
import pytest
from fastapi.testclient import TestClient

from lo_pipeline.contract import CreateJobResponse, ErrorResponse, JobStatus
from lo_pipeline.contract.schemas import load_schema
from service.main import create_app
from service.mock import MockBackend
from service.settings import ServiceSettings

PDF = b"%PDF-1.4 test document"
DURATION = 100  # seconds


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


def make_client(clock, **settings):
    settings = ServiceSettings(**{"mock_duration_s": DURATION, **settings})
    backend = MockBackend(duration_s=DURATION, max_active_jobs=settings.max_active_jobs, clock=clock)
    return TestClient(create_app(settings, backend))


@pytest.fixture
def client(clock):
    return make_client(clock)


def post_job(client, options=None, files=None, headers=None):
    files = files if files is not None else [("files", ("modul1.pdf", PDF, "application/pdf"))]
    data = {"options": json.dumps(options)} if options is not None else {}
    return client.post("/v1/jobs", files=files, data=data, headers=headers or {})


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    body = ErrorResponse.model_validate(response.json())  # every error has the uniform shape
    assert body.error.code == code
    return body.error


# --- job lifecycle ------------------------------------------------------------------------

def test_job_goes_from_queued_to_completed(client, clock):
    created = post_job(client, {"external_ref": "gen-1"})
    assert created.status_code == 202
    body = CreateJobResponse.model_validate(created.json())
    assert body.status == "queued" and body.replayed is False
    assert created.json()["links"] == {"self": f"/v1/jobs/{body.job_id}"}

    seen = []
    for _ in range(12):
        status = JobStatus.model_validate(client.get(f"/v1/jobs/{body.job_id}").json())
        seen.append((status.status, status.stage, status.progress))
        clock.advance(DURATION / 10)
    states = [s for s, _, _ in seen]
    assert states[0] == "queued" and states[-1] == "completed" and "running" in states
    progress = [p for _, _, p in seen]
    assert progress == sorted(progress), "progress never goes back"
    assert {stage for s, stage, _ in seen if s == "running"} >= {"units", "learning_objects", "lo_evaluation"}


def test_result_and_report_pass_the_schemas(client, clock):
    job_id = post_job(client).json()["job_id"]
    assert_error(client.get(f"/v1/jobs/{job_id}/result"), 409, "result_not_ready")
    assert_error(client.get(f"/v1/jobs/{job_id}/report"), 409, "result_not_ready")
    clock.advance(DURATION)
    result = client.get(f"/v1/jobs/{job_id}/result")
    report = client.get(f"/v1/jobs/{job_id}/report")
    assert result.status_code == report.status_code == 200
    jsonschema.validate(result.json(), load_schema("lesson"))
    jsonschema.validate(report.json(), load_schema("report"))


def test_same_external_ref_returns_the_existing_job(client, clock):
    first = post_job(client, {"external_ref": "gen-7"}).json()
    clock.advance(DURATION / 2)
    again = post_job(client, {"external_ref": "gen-7"})
    assert again.status_code == 200
    assert again.json()["job_id"] == first["job_id"]
    assert again.json()["replayed"] is True and again.json()["status"] == "running"

    listed = client.get("/v1/jobs", params={"external_ref": "gen-7"}).json()["jobs"]
    assert [job["job_id"] for job in listed] == [first["job_id"]]
    assert client.get("/v1/jobs", params={"external_ref": "unknown"}).json() == {"jobs": []}


def test_cancel_running_job_then_delete_it(client, clock):
    job_id = post_job(client).json()["job_id"]
    clock.advance(DURATION / 2)
    cancelled = client.delete(f"/v1/jobs/{job_id}")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    clock.advance(DURATION)
    assert client.get(f"/v1/jobs/{job_id}").json()["status"] == "cancelled"  # stays cancelled
    assert_error(client.get(f"/v1/jobs/{job_id}/result"), 409, "job_not_completed")
    assert client.delete(f"/v1/jobs/{job_id}").status_code == 204
    assert_error(client.get(f"/v1/jobs/{job_id}"), 404, "job_not_found")


def test_failed_job_can_be_retried_once_and_then_completes(client, clock):
    job_id = post_job(client, {"external_ref": "mock-fail-1"}).json()["job_id"]
    assert_error(client.post(f"/v1/jobs/{job_id}/retry"), 409, "job_not_retryable")  # still queued
    clock.advance(DURATION * 0.6)
    failed = JobStatus.model_validate(client.get(f"/v1/jobs/{job_id}").json())
    assert failed.status == "failed" and failed.error.code == "llm_error"
    assert failed.retryable_until is not None

    retried = client.post(f"/v1/jobs/{job_id}/retry")
    assert retried.status_code == 200 and retried.json()["status"] == "queued"
    assert retried.json()["job_id"] == job_id
    clock.advance(DURATION)
    assert client.get(f"/v1/jobs/{job_id}").json()["status"] == "completed"


def test_retry_after_24_hours_is_refused(client, clock):
    job_id = post_job(client, {"external_ref": "mock-fail-2"}).json()["job_id"]
    clock.advance(DURATION + 24 * 3600)
    assert_error(client.post(f"/v1/jobs/{job_id}/retry"), 409, "job_not_retryable")


def test_too_many_active_jobs(clock):
    client = make_client(clock, max_active_jobs=2)
    assert post_job(client).status_code == 202
    assert post_job(client).status_code == 202
    response = post_job(client)
    assert_error(response, 429, "too_many_jobs")
    assert response.headers["Retry-After"]
    clock.advance(DURATION)  # both finished: room again
    assert post_job(client).status_code == 202


# --- request checks -------------------------------------------------------------------------

def test_api_key(clock):
    client = make_client(clock, api_keys=("secret-1", "secret-2"))
    assert_error(client.get("/v1/info"), 401, "unauthorized")
    assert_error(client.get("/v1/info", headers={"X-API-Key": "wrong"}), 401, "unauthorized")
    assert client.get("/v1/info", headers={"X-API-Key": "secret-2"}).status_code == 200
    assert_error(post_job(client), 401, "unauthorized")
    assert client.get("/health").status_code == 200  # no key needed


@pytest.mark.parametrize("options", [
    "not json", "[1, 2]", {"split": {"mode": "lessons"}}, {"unknown": True},
    {"callback_url": "http://evil.example/hook"}, {"extraction": {"ocr": True}},
])
def test_invalid_options(client, options):
    data = {"options": options if isinstance(options, str) else json.dumps(options)}
    response = client.post("/v1/jobs", files=[("files", ("a.pdf", PDF, "application/pdf"))], data=data)
    assert_error(response, 400, "invalid_options")


def test_callback_url_on_allowed_host(clock):
    client = make_client(clock, webhook_allowed_hosts=("eceh-backend",), webhook_secret="s3cret")
    response = post_job(client, {"callback_url": "http://eceh-backend/api/internal/qgen/webhook"})
    assert response.status_code == 202


@pytest.mark.parametrize("name, content", [
    ("notes.docx", b"PK\x03\x04..."),       # type B does not accept
    ("fake.pdf", b"<html>not a pdf</html>"),  # content does not match the extension
    ("slides.pptx", b"%PDF-1.4"),
    ("page.html", b"\xff\xfe\x00 not utf-8 \xff"),
])
def test_unsupported_files(client, name, content):
    assert_error(post_job(client, files=[("files", (name, content, "application/octet-stream"))]),
                 415, "unsupported_file")


def test_syllabus_pages_must_name_uploaded_documents(client):
    two_files = [("files", ("a.pdf", PDF, "application/pdf")), ("files", ("b.pdf", PDF, "application/pdf"))]

    def syllabus(pages):
        return {"split": {"mode": "syllabus", "syllabus": [{"title": "Úvod", "pages": "D1:1-3"},
                                                           {"title": "Ďalej", "pages": pages}]}}

    assert post_job(client, syllabus("D2:1-5"), files=two_files).status_code == 202
    error = assert_error(post_job(client, syllabus("D2:1, D3:4"), files=two_files), 400, "invalid_options")
    assert error.details == {"lesson": 1, "document": 3, "files": 2}


def test_accepts_pptx_and_html(client):
    files = [("files", ("slides.pptx", b"PK\x03\x04 zip", "application/zip")),
             ("files", ("page.html", "<p>Ahoj</p>".encode("utf-8"), "text/html"))]
    assert post_job(client, files=files).status_code == 202


def test_docs_form_has_one_file_field(client):
    """The /docs page builds its form from this; a second file field made Swagger send 422."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    body = next(schema for name, schema in schemas.items() if name.startswith("Body_create_job"))
    assert set(body["properties"]) == {"files", "options"}
    assert body["properties"]["files"]["items"]["format"] == "binary"  # file picker, not a text box


def test_file_limits(clock):
    client = make_client(clock, max_files=2, max_upload_mb=1)
    three = [("files", (f"{i}.pdf", PDF, "application/pdf")) for i in range(3)]
    assert_error(post_job(client, files=three), 413, "file_too_large")
    big = [("files", ("big.pdf", PDF + b"0" * (1024 * 1024), "application/pdf"))]
    assert_error(post_job(client, files=big), 413, "file_too_large")


def test_missing_files(client):
    assert_error(client.post("/v1/jobs", data={"options": "{}"}), 422, "validation_error")


def test_unknown_job_path_and_method(client):
    assert_error(client.get("/v1/jobs/does-not-exist"), 404, "job_not_found")
    assert_error(client.get("/v1/nothing-here"), 404, "not_found")
    assert_error(client.put("/v1/jobs/x"), 405, "method_not_allowed")
    assert_error(client.get("/v1/jobs"), 422, "validation_error")  # external_ref is required


# --- service endpoints ----------------------------------------------------------------------

def test_health_info_and_schemas(client):
    assert client.get("/health").json()["status"] == "ok"
    info = client.get("/v1/info").json()
    assert info["service"] == "lo-generator" and ".pptx" in info["limits"]["file_types"]
    for name in ("lesson", "report", "options"):
        assert client.get(f"/v1/schema/{name}").json() == load_schema(name)
    assert_error(client.get("/v1/schema/other"), 404, "not_found")


def test_docs_can_be_disabled(clock):
    assert make_client(clock).get("/docs").status_code == 200
    closed = make_client(clock, docs_enabled=False)
    assert closed.get("/docs").status_code == 404
    assert closed.get("/openapi.json").status_code == 404


def test_settings_from_env():
    settings = ServiceSettings.from_env({"LOGEN_API_KEYS": " a, b ,", "LOGEN_MAX_FILES": "3",
                                         "LOGEN_DOCS_ENABLED": "false", "LOGEN_WEBHOOK_ALLOWED_HOSTS": "eceh",
                                         "LOGEN_WEBHOOK_SECRET": "s3cret"})
    assert settings.api_keys == ("a", "b")
    assert settings.max_files == 3 and settings.docs_enabled is False
    assert settings.webhook_allowed_hosts == ("eceh",)


def test_settings_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LOGEN_MAX_FILES", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("LOGEN_API_KEYS=k1,k2\nLOGEN_MAX_FILES=4\nGROQ_API_KEY=not-a-service-setting\n", encoding="utf-8")
    settings = ServiceSettings(_env_file=env_file)
    assert settings.api_keys == ("k1", "k2") and settings.max_files == 4
    monkeypatch.setenv("LOGEN_MAX_FILES", "6")  # the environment wins over the file
    assert ServiceSettings(_env_file=env_file).max_files == 6


@pytest.mark.parametrize("values", [{"LOGEN_BACKEND": "postgres"}, {"LOGEN_MAX_FILES": "0"}, {"LOGEN_MAX_FILES": "x"}])
def test_wrong_service_settings_are_errors(values):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ServiceSettings.from_env(values)
