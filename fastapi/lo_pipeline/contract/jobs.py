"""Job state, API errors and webhook body (api-design-appendix.md, Príloha A).

Same values as Mlynček A, so Laravel handles both services with one code path.
"""

from typing import Any, Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

JobStatusValue = Literal["queued", "running", "completed", "failed", "cancelled"]
FINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")

# Steps in order (Príloha A). B runs extract, units, learning_objects, lo_evaluation, graph,
# questions, output; "units" and "graph" exist only in B, the others are shared with A.
JobStage = Literal[
    "extract", "language", "units", "learning_objects", "lo_evaluation",
    "graph", "questions", "q_evaluation", "split", "output",
]

# Error codes of a failed job (status "failed", field error.code). Not HTTP responses.
JobErrorCode = Literal[
    "llm_error",          # the LLM provider failed even after retries (also a rejected API key)
    "worker_lost",        # the worker stopped during the job twice (limit of 2 attempts)
    "extraction_failed",  # no text could be extracted from the documents
    "internal_error",     # anything else: invalid output, time limit, configuration, bug
]

# HTTP error codes of the API. not_found / method_not_allowed / internal_error cover an unknown
# path, a wrong method and an unexpected error, so that every error response has the same shape.
ApiErrorCode = Literal[
    "invalid_options", "unauthorized", "job_not_found", "result_not_ready", "job_not_completed",
    "job_not_retryable", "file_too_large", "unsupported_file", "validation_error", "too_many_jobs",
    "not_found", "method_not_allowed", "internal_error",
]

WebhookEvent = Literal["job.stage_changed", "job.completed", "job.failed", "job.cancelled"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobError(_Strict):
    code: JobErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class JobStatus(_Strict):
    """GET /v1/jobs/{id}."""

    job_id: str = Field(min_length=1)
    external_ref: Optional[str] = None
    status: JobStatusValue
    stage: Optional[JobStage] = None
    progress: float = Field(ge=0.0, le=1.0)
    error: Optional[JobError] = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    retryable_until: Optional[AwareDatetime] = None


class JobLinks(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    self_: str = Field(alias="self", description="Path of the job, e.g. '/v1/jobs/3f6b…'.")


class CreateJobResponse(_Strict):
    """POST /v1/jobs: 202 for a new job, 200 and replayed=true when external_ref already exists."""

    job_id: str = Field(min_length=1)
    status: JobStatusValue
    replayed: bool = False
    links: JobLinks


class JobListItem(_Strict):
    job_id: str = Field(min_length=1)
    status: JobStatusValue
    created_at: AwareDatetime


class JobListResponse(_Strict):
    """GET /v1/jobs?external_ref=…, newest first."""

    jobs: list[JobListItem]


class ErrorBody(_Strict):
    code: ApiErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(_Strict):
    """Every API error: {"error": {"code", "message", "details"}}."""

    error: ErrorBody


class WebhookPayload(_Strict):
    """Body of the webhook POST to callback_url (sprint task 7). Only the state, not the result."""

    event: WebhookEvent
    job_id: str = Field(min_length=1)
    external_ref: Optional[str] = None
    status: JobStatusValue
    stage: Optional[JobStage] = None
    progress: float = Field(ge=0.0, le=1.0)
    error: Optional[JobError] = None
    occurred_at: AwareDatetime
