"""What the routes need from a job backend.

The routes check the request (API key, options, files) and then call a backend. The mock
backend (mock.py) keeps jobs in memory; the real one (task 4: job folder + Redis + worker)
implements the same methods, so the routes and Laravel see no difference.
Methods raise service.errors.ApiError for 404 / 409 / 429.
"""

from dataclasses import dataclass
from typing import Optional, Protocol

from lo_pipeline.contract import JobListItem, JobOptions, JobStatus, Report, UnitsResult
from service.uploads import Upload


@dataclass(frozen=True)
class Created:
    status: JobStatus
    replayed: bool  # True: a job with this external_ref already existed, nothing new was created


class JobBackend(Protocol):
    name: str

    def health(self) -> dict[str, str]: ...

    def create(self, options: JobOptions, files: list[Upload]) -> Created: ...

    def get(self, job_id: str) -> JobStatus: ...

    def find(self, external_ref: str) -> list[JobListItem]: ...

    def result(self, job_id: str) -> UnitsResult: ...

    def report(self, job_id: str) -> Report: ...

    def cancel_or_delete(self, job_id: str) -> Optional[JobStatus]:
        """Running or queued → cancelled (returns the status); finished → deleted (returns None)."""

    def retry(self, job_id: str) -> JobStatus: ...
