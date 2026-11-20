"""The real job backend (sprint task 4b): jobs in the job folder + Redis, processed by the worker.

Same methods and answers as the mock (service/mock.py), so the routes and Laravel see no
difference. Turned on with LOGEN_BACKEND=redis; the worker runs separately
(python -m service.worker).

    POST    -> files + job.json saved, state `queued` in Redis, job id put into the RQ queue
    DELETE  -> queued/running: `cancelled` at once (the worker stops after its current LLM batch)
               finished: job folder and state deleted
    retry   -> a failed job is queued again and continues from its checkpoint
"""

import logging
from typing import Callable, Optional

from lo_pipeline.contract import JobError, JobListItem, JobOptions, JobStatus, Report, UnitsResult
from lo_pipeline.contract.options import resolve_options
from service.backend import Created
from service.errors import ApiError, job_not_found
from service.store import ACTIVE, JobStore
from service.uploads import Upload

logger = logging.getLogger(__name__)

RETRY_AFTER_S = 60


class RedisBackend:
    name = "redis"

    def __init__(self, store: JobStore, enqueue: Callable[[str], None], max_active_jobs: int = 10):
        """`enqueue(job_id)` puts a job into the worker queue (service.jobqueue.enqueue_job)."""
        self.store = store
        self.enqueue = enqueue
        self.max_active_jobs = max_active_jobs

    # --- helpers ----------------------------------------------------------------------------

    def _status(self, job_id: str) -> JobStatus:
        status = self.store.status(job_id)
        if status is None:
            raise job_not_found(job_id)
        return status

    def _check_capacity(self) -> None:
        if self.store.active_count() >= self.max_active_jobs:
            raise ApiError(429, "too_many_jobs", f"Too many active jobs (limit {self.max_active_jobs}).",
                           {"max_active_jobs": self.max_active_jobs}, headers={"Retry-After": str(RETRY_AFTER_S)})

    def _queue(self, job_id: str) -> None:
        """Put the job into the queue; if that fails the job is removed again (the caller gets 500)."""
        try:
            self.enqueue(job_id)
        except Exception:
            logger.exception(f"[API] Could not queue job {job_id}; removed it.")
            self.store.delete(job_id)
            raise

    def _require_completed(self, job_id: str) -> None:
        status = self._status(job_id).status
        if status in ACTIVE:
            raise ApiError(409, "result_not_ready", f"Job is {status}; the result is not ready yet.")
        if status != "completed":
            raise ApiError(409, "job_not_completed", f"Job is {status}; it has no result.")

    # --- JobBackend -------------------------------------------------------------------------

    def health(self) -> dict[str, str]:
        try:
            self.store.redis.ping()
            redis_state = "ok"
        except Exception as exc:  # health must answer, not fail
            logger.warning(f"[API] Redis is not reachable: {type(exc).__name__}")
            redis_state = "error"
        return {"api": "ok", "redis": redis_state, "backend": self.name}

    def create(self, options: JobOptions, files: list[Upload]) -> Created:
        ref = options.external_ref
        existing = self.store.find(ref) if ref else None
        if existing:
            return Created(status=self._status(existing), replayed=True)
        self._check_capacity()
        # complete options are stored with the job: a retry runs with the same settings
        job_id, created = self.store.create(resolve_options(options), files)
        if created:
            self._queue(job_id)
        return Created(status=self._status(job_id), replayed=not created)

    def get(self, job_id: str) -> JobStatus:
        return self._status(job_id)

    def find(self, external_ref: str) -> list[JobListItem]:
        job_id = self.store.find(external_ref)
        if job_id is None:
            return []
        status = self._status(job_id)
        return [JobListItem(job_id=job_id, status=status.status, created_at=status.created_at)]

    def result(self, job_id: str) -> UnitsResult:
        self._require_completed(job_id)
        return UnitsResult.model_validate(self.store.read_output(job_id, "result"))

    def report(self, job_id: str) -> Report:
        self._require_completed(job_id)
        return Report.model_validate(self.store.read_output(job_id, "report"))

    def cancel_or_delete(self, job_id: str) -> Optional[JobStatus]:
        status = self._status(job_id)
        if status.status in ACTIVE:
            self.store.request_cancel(job_id)
            self.store.update(job_id, status="cancelled")
            logger.info(f"[API] Job {job_id} cancelled (was {status.status}).")
            return self._status(job_id)
        self.store.delete(job_id)
        logger.info(f"[API] Job {job_id} deleted.")
        return None

    def retry(self, job_id: str) -> JobStatus:
        status = self._status(job_id)
        if status.status != "failed":
            raise ApiError(409, "job_not_retryable", f"Job is {status.status}; only a failed job can be retried.")
        if status.retryable_until is not None and self.store.clock() >= status.retryable_until:
            raise ApiError(409, "job_not_retryable", "The input was already deleted; create a new job.")
        self._check_capacity()
        # a manual retry starts a new series of attempts (worker_lost counts only lost workers)
        self.store.update(job_id, status="queued", stage=None, progress=0.0, error=None,
                          retryable_until=None, attempts=0)
        self._queue_retry(job_id)
        return self._status(job_id)

    def _queue_retry(self, job_id: str) -> None:
        try:
            self.enqueue(job_id)
        except Exception:
            # keep the job (its checkpoint is valuable) but show it as failed again
            logger.exception(f"[API] Could not queue retry of job {job_id}.")
            self.store.mark_failed(job_id, JobError(code="internal_error", message="Could not queue the retry.",
                                                    details={"reason": "queue_unavailable"}))
            raise
