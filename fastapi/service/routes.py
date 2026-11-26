"""The /v1 endpoints (Príloha A). Every route except /health needs X-API-Key when LOGEN_API_KEYS is set."""

import hmac
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from lo_pipeline.contract import (
    CreateJobResponse,
    ErrorResponse,
    JobLinks,
    JobListResponse,
    JobStatus,
    Report,
    UnitsResult,
)
from lo_pipeline.contract.schemas import SCHEMA_NAMES, build_schema
from service.backend import JobBackend
from service.errors import ApiError
from service.settings import ServiceSettings
from service.uploads import FILE_TYPES, check_syllabus_documents, parse_options, read_uploads

SERVICE_NAME = "lo-generator"
VERSION = "0.1.0"

_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (400, 401, 404, 409, 413, 415, 422, 429)
}


def settings_of(request: Request) -> ServiceSettings:
    return request.app.state.settings


def backend_of(request: Request) -> JobBackend:
    return request.app.state.backend


def require_api_key(request: Request, x_api_key: Annotated[Optional[str], Header()] = None) -> None:
    keys = settings_of(request).api_keys
    if not keys:
        return
    if x_api_key is None or not any(hmac.compare_digest(x_api_key, key) for key in keys):
        raise ApiError(401, "unauthorized", "Missing or invalid X-API-Key.")


Backend = Annotated[JobBackend, Depends(backend_of)]
Settings = Annotated[ServiceSettings, Depends(settings_of)]

public = APIRouter()
v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)], responses=_ERRORS)


@public.get("/health", tags=["service"], summary="State of the service (no API key).")
def health(backend: Backend) -> dict[str, Any]:
    components = backend.health()
    ok = all(state in ("ok", "mock") for name, state in components.items() if name != "backend")
    body = {"status": "ok" if ok else "error", "components": components}
    return JSONResponse(body, status_code=200 if ok else 503)


@v1.get("/info", tags=["service"], summary="Version, models and limits.")
def info(backend: Backend, settings: Settings) -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "version": VERSION,
        "backend": backend.name,
        "models": None,  # filled by the real backend (task 3)
        "limits": {
            "max_files": settings.max_files,
            "max_upload_mb": settings.max_upload_mb,
            "max_active_jobs": settings.max_active_jobs,
            "file_types": list(FILE_TYPES),
        },
        "ocr_available": False,
    }


def _links(job_id: str) -> JobLinks:
    return JobLinks(self_=f"/v1/jobs/{job_id}")


@v1.post("/jobs", tags=["jobs"], status_code=202, response_model=CreateJobResponse,
         summary="Create a job: files (PDF, PPTX, HTML) + options (JSON text).",
         responses={200: {"model": CreateJobResponse, "description": "external_ref already exists (replayed)"}})
async def create_job(
    backend: Backend,
    settings: Settings,
    # format "binary" makes the /docs page show a file picker for each item (FastAPI's
    # default "contentMediaType" shows only a text box for a list of files)
    files: Annotated[list[UploadFile], File(
        description="Documents to process.",
        json_schema_extra={"items": {"type": "string", "format": "binary"}},
    )] = [],  # noqa: B006
    options: Annotated[Optional[str], Form(description="JSON object with the job options.")] = None,
):
    job_options = parse_options(options, settings)
    uploads = await read_uploads(list(files), settings)
    check_syllabus_documents(job_options, len(uploads))
    created = backend.create(job_options, uploads)
    body = CreateJobResponse(job_id=created.status.job_id, status=created.status.status,
                             replayed=created.replayed, links=_links(created.status.job_id))
    return JSONResponse(body.model_dump(mode="json"), status_code=200 if created.replayed else 202)


@v1.get("/jobs", tags=["jobs"], response_model=JobListResponse, summary="Jobs with an external_ref, newest first.")
def list_jobs(backend: Backend, external_ref: Annotated[str, Query(min_length=1)]) -> JobListResponse:
    return JobListResponse(jobs=backend.find(external_ref))


@v1.get("/jobs/{job_id}", tags=["jobs"], response_model=JobStatus, summary="State, stage, progress and error.")
def get_job(job_id: str, backend: Backend) -> JobStatus:
    return backend.get(job_id)


@v1.get("/jobs/{job_id}/result", tags=["jobs"], response_model=UnitsResult, summary="Result in the units format.")
def get_result(job_id: str, backend: Backend) -> UnitsResult:
    return backend.result(job_id)


@v1.get("/jobs/{job_id}/report", tags=["jobs"], response_model=Report, summary="Report (diagnostics).")
def get_report(job_id: str, backend: Backend) -> Report:
    return backend.report(job_id)


@v1.delete("/jobs/{job_id}", tags=["jobs"], summary="Cancel a running job (200) or delete a finished one (204).",
           responses={200: {"model": JobStatus}, 204: {"description": "Deleted"}})
def delete_job(job_id: str, backend: Backend):
    status = backend.cancel_or_delete(job_id)
    if status is None:
        return Response(status_code=204)
    return JSONResponse(status.model_dump(mode="json"))


@v1.post("/jobs/{job_id}/retry", tags=["jobs"], response_model=JobStatus, summary="Run a failed job again.")
def retry_job(job_id: str, backend: Backend) -> JobStatus:
    return backend.retry(job_id)


@v1.get("/schema/{name}", tags=["schema"], summary="JSON Schema: lesson (units), report or options.")
def get_schema(name: str) -> dict[str, Any]:
    if name not in SCHEMA_NAMES:
        raise ApiError(404, "not_found", f"Unknown schema '{name}'.", {"schemas": list(SCHEMA_NAMES)})
    return build_schema(name)
