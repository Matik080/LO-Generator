"""Mock backend: jobs in memory that pass through B's stages in LOGEN_MOCK_DURATION_S seconds.

Lets Laravel integrate before the real processing exists (sprint task 1). The state of a job
is computed from the time since it started, so no background thread is needed:
    first 5 % of the time  queued
    then                    running, stage and progress follow service.progress
    at the end              completed; result and report are the examples from schemas/examples/
An external_ref starting with "mock-fail" makes the job fail with llm_error halfway; a retry
of such a job then succeeds. Nothing is stored on disk; a restart forgets all jobs.
"""

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from lo_pipeline.contract import JobError, JobListItem, JobOptions, JobStatus, Report, UnitsResult
from lo_pipeline.contract.schemas import load_example
from service.backend import Created
from service.errors import ApiError, job_not_found
from service.progress import stage_at
from service.uploads import Upload

QUEUED_SHARE = 0.05
FAIL_AT = 0.5
RETRYABLE_FOR = timedelta(hours=24)
RETRY_AFTER_S = 60


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _MockJob:
    job_id: str
    options: JobOptions
    file_names: list[str]
    created_at: datetime
    started_at: datetime            # creation, or the last retry
    fails: bool
    cancelled_at: Optional[datetime] = None
    cancelled_state: Optional[tuple[str, float]] = None  # stage and progress when cancelled


class MockBackend:
    name = "mock"

    def __init__(self, duration_s: float = 30.0, max_active_jobs: int = 10,
                 clock: Callable[[], datetime] = utc_now):
        self.duration = timedelta(seconds=duration_s)
        self.max_active_jobs = max_active_jobs
        self.clock = clock
        self._jobs: dict[str, _MockJob] = {}
        self._lock = threading.Lock()
        self._units = UnitsResult.model_validate(load_example("units_example.json"))
        self._report = Report.model_validate(load_example("report_example.json"))

    # --- state ----------------------------------------------------------------------------

    def _status(self, job: _MockJob) -> JobStatus:
        now = self.clock()
        share = (now - job.started_at) / self.duration if self.duration else 1.0
        common = dict(job_id=job.job_id, external_ref=job.options.external_ref, created_at=job.created_at)

        if job.cancelled_at is not None:
            stage, progress = job.cancelled_state or (None, 0.0)
            return JobStatus(**common, status="cancelled", stage=stage, progress=progress, updated_at=job.cancelled_at)
        if job.fails and share >= FAIL_AT:
            failed_at = job.started_at + self.duration * FAIL_AT
            stage, _ = stage_at(FAIL_AT)
            error = JobError(code="llm_error", message="Mock failure: the LLM provider did not answer.",
                             details={"stage": stage, "mock": True})
            return JobStatus(**common, status="failed", stage=stage, progress=FAIL_AT, error=error,
                             updated_at=failed_at, retryable_until=failed_at + RETRYABLE_FOR)
        if share >= 1.0:
            return JobStatus(**common, status="completed", stage="output", progress=1.0,
                             updated_at=job.started_at + self.duration)
        if share < QUEUED_SHARE:
            return JobStatus(**common, status="queued", stage=None, progress=0.0, updated_at=job.started_at)
        progress = round((share - QUEUED_SHARE) / (1 - QUEUED_SHARE), 4)
        stage, _ = stage_at(progress)
        return JobStatus(**common, status="running", stage=stage, progress=min(progress, 0.99), updated_at=now)

    def _job(self, job_id: str) -> _MockJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise job_not_found(job_id)
        return job

    def _active_count(self) -> int:
        return sum(1 for job in self._jobs.values() if self._status(job).status in ("queued", "running"))

    def _check_capacity(self) -> None:
        if self._active_count() >= self.max_active_jobs:
            raise ApiError(429, "too_many_jobs", f"Too many active jobs (limit {self.max_active_jobs}).",
                           {"max_active_jobs": self.max_active_jobs}, headers={"Retry-After": str(RETRY_AFTER_S)})

    # --- JobBackend -------------------------------------------------------------------------

    def health(self) -> dict[str, str]:
        return {"api": "ok", "backend": "mock"}

    def create(self, options: JobOptions, files: list[Upload]) -> Created:
        with self._lock:
            ref = options.external_ref
            if ref:
                existing = self.find(ref)
                if existing:
                    return Created(status=self._status(self._jobs[existing[0].job_id]), replayed=True)
            self._check_capacity()
            now = self.clock()
            job = _MockJob(job_id=str(uuid.uuid4()), options=options, file_names=[f.filename for f in files],
                           created_at=now, started_at=now, fails=bool(ref and ref.startswith("mock-fail")))
            self._jobs[job.job_id] = job
            return Created(status=self._status(job), replayed=False)

    def get(self, job_id: str) -> JobStatus:
        return self._status(self._job(job_id))

    def find(self, external_ref: str) -> list[JobListItem]:
        jobs = [j for j in self._jobs.values() if j.options.external_ref == external_ref]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return [JobListItem(job_id=j.job_id, status=self._status(j).status, created_at=j.created_at) for j in jobs]

    def _require_completed(self, job_id: str) -> None:
        status = self.get(job_id).status
        if status in ("queued", "running"):
            raise ApiError(409, "result_not_ready", f"Job is {status}; the result is not ready yet.")
        if status != "completed":
            raise ApiError(409, "job_not_completed", f"Job is {status}; it has no result.")

    def result(self, job_id: str) -> UnitsResult:
        self._require_completed(job_id)
        return self._units

    def report(self, job_id: str) -> Report:
        self._require_completed(job_id)
        return self._report

    def cancel_or_delete(self, job_id: str) -> Optional[JobStatus]:
        with self._lock:
            job = self._job(job_id)
            status = self._status(job)
            if status.status in ("queued", "running"):
                job.cancelled_state = (status.stage, status.progress)
                job.cancelled_at = self.clock()
                return self._status(job)
            del self._jobs[job_id]
            return None

    def retry(self, job_id: str) -> JobStatus:
        with self._lock:
            job = self._job(job_id)
            status = self._status(job)
            if status.status != "failed":
                raise ApiError(409, "job_not_retryable", f"Job is {status.status}; only a failed job can be retried.")
            if self.clock() >= status.retryable_until:
                raise ApiError(409, "job_not_retryable", "The input was already deleted; create a new job.")
            self._check_capacity()
            job.started_at = self.clock()
            job.fails = False
            return self._status(job)
