"""Where jobs live (sprint task 4a): files in a job folder, live state in Redis.

Job folder  <data_dir>/jobs/{id}/
    input/          the uploaded files
    job.json        job id, complete options (resolve_options) and file names; written once
    checkpoint/     FileCheckpoint of run_pipeline (resume after a crash or retry)
    result.json     the `units` result   } written when the job completes
    report.json     the report           }
    pipeline_result.json   everything run_pipeline returned (diagnostics, not in the API)

Redis (state that changes while the job runs; RQ only carries the job id):
    logen:job:{id}         hash: status, stage, progress, error, attempts, cancel_requested,
                           external_ref, created_at, updated_at, retryable_until
    logen:jobs             sorted set of all job ids (score = creation time)
    logen:ref:{ref}        job id of the job with this external_ref (SET NX: one job per ref)

The API writes the folder first and the Redis state last, so a job that the API reports as
created always has its files on disk.
"""

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from lo_pipeline.contract import JobError, JobOptions, JobStatus
from service.uploads import Upload

PREFIX = "logen"
RETRYABLE_FOR = timedelta(hours=24)  # task 7 deletes inputs after this; until then retry works
ACTIVE = ("queued", "running")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StoredJob:
    """job.json: what the worker needs to run the job."""

    job_id: str
    options: JobOptions
    files: list[str]  # names in input/, in upload order

    def to_json(self) -> dict:
        return {"job_id": self.job_id, "options": self.options.model_dump(mode="json"), "files": self.files}

    @classmethod
    def from_json(cls, data: dict) -> "StoredJob":
        return cls(job_id=data["job_id"], options=JobOptions.model_validate(data["options"]), files=data["files"])


def _unique_names(names: list[str]) -> list[str]:
    """'a.pdf', 'a.pdf' -> 'a.pdf', 'a-2.pdf' (two uploads must not overwrite each other)."""
    seen, out = set(), []
    for name in names:
        candidate, n = name, 1
        while candidate.lower() in seen:
            n += 1
            stem, ext = os.path.splitext(name)
            candidate = f"{stem}-{n}{ext}"
        seen.add(candidate.lower())
        out.append(candidate)
    return out


def _write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class JobStore:
    def __init__(self, redis, data_dir: str, clock: Callable[[], datetime] = utc_now):
        """`redis`: a redis.Redis client created with decode_responses=True (or fakeredis in tests)."""
        self.redis = redis
        self.jobs_dir = Path(data_dir) / "jobs"
        self.clock = clock

    # --- paths ------------------------------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def input_paths(self, job: StoredJob) -> list[Path]:
        return [self.job_dir(job.job_id) / "input" / name for name in job.files]

    def checkpoint_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "checkpoint"

    @staticmethod
    def _key(job_id: str) -> str:
        return f"{PREFIX}:job:{job_id}"

    @staticmethod
    def _ref_key(external_ref: str) -> str:
        return f"{PREFIX}:ref:{external_ref}"

    # --- create -----------------------------------------------------------------------------

    def create(self, options: JobOptions, uploads: list[Upload]) -> tuple[str, bool]:
        """Saves a new job as `queued`. Returns (job_id, created).

        created=False: a job with the same external_ref exists; its id is returned, nothing is saved.
        """
        job_id = str(uuid.uuid4())
        ref = options.external_ref
        if ref and not self.redis.set(self._ref_key(ref), job_id, nx=True):
            existing = self.redis.get(self._ref_key(ref))
            if existing and self.exists(existing):
                return existing, False
            self.redis.set(self._ref_key(ref), job_id)  # left over from a job that no longer exists
        try:
            self._write_folder(job_id, options, uploads)
            now = self.clock().isoformat()
            self.redis.hset(self._key(job_id), mapping={
                "status": "queued", "stage": "", "progress": 0.0, "error": "", "attempts": 0,
                "cancel_requested": 0, "external_ref": ref or "",
                "created_at": now, "updated_at": now, "retryable_until": "",
            })
            self.redis.zadd(f"{PREFIX}:jobs", {job_id: self.clock().timestamp()})
        except Exception:
            shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
            if ref:
                self.redis.delete(self._ref_key(ref))
            raise
        return job_id, True

    def _write_folder(self, job_id: str, options: JobOptions, uploads: list[Upload]) -> None:
        names = _unique_names([u.filename for u in uploads])
        input_dir = self.job_dir(job_id) / "input"
        input_dir.mkdir(parents=True)
        for name, upload in zip(names, uploads):
            (input_dir / name).write_bytes(upload.content)
        _write_json_atomic(self.job_dir(job_id) / "job.json",
                           StoredJob(job_id=job_id, options=options, files=names).to_json())

    # --- read -------------------------------------------------------------------------------

    def exists(self, job_id: str) -> bool:
        return bool(self.redis.exists(self._key(job_id)))

    def load(self, job_id: str) -> StoredJob:
        data = json.loads((self.job_dir(job_id) / "job.json").read_text(encoding="utf-8"))
        return StoredJob.from_json(data)

    def fields(self, job_id: str) -> Optional[dict]:
        """The raw Redis hash, or None when the job does not exist."""
        data = self.redis.hgetall(self._key(job_id))
        return data or None

    def status(self, job_id: str) -> Optional[JobStatus]:
        f = self.fields(job_id)
        if f is None:
            return None
        return JobStatus(
            job_id=job_id,
            external_ref=f["external_ref"] or None,
            status=f["status"],
            stage=f["stage"] or None,
            progress=float(f["progress"]),
            error=JobError.model_validate_json(f["error"]) if f["error"] else None,
            created_at=datetime.fromisoformat(f["created_at"]),
            updated_at=datetime.fromisoformat(f["updated_at"]),
            retryable_until=datetime.fromisoformat(f["retryable_until"]) if f["retryable_until"] else None,
        )

    def find(self, external_ref: str) -> Optional[str]:
        job_id = self.redis.get(self._ref_key(external_ref))
        return job_id if job_id and self.exists(job_id) else None

    def all_ids(self) -> list[str]:
        return list(self.redis.zrange(f"{PREFIX}:jobs", 0, -1))

    def active_count(self) -> int:
        return sum(1 for job_id in self.all_ids() if (self.fields(job_id) or {}).get("status") in ACTIVE)

    # --- change -----------------------------------------------------------------------------

    def update(self, job_id: str, **fields) -> None:
        """Sets the given fields and updated_at. `error` is a JobError or None.

        Does nothing when the job was deleted meanwhile (DELETE while the worker still runs it),
        so the worker never brings back a half-empty job.
        """
        if not self.exists(job_id):
            return
        values = {"updated_at": self.clock().isoformat()}
        for name, value in fields.items():
            if name == "error":
                value = value.model_dump_json() if value is not None else ""
            elif name == "retryable_until":
                value = value.isoformat() if value is not None else ""
            elif value is None:
                value = ""
            values[name] = value
        self.redis.hset(self._key(job_id), mapping=values)

    def mark_failed(self, job_id: str, error: JobError) -> None:
        self.update(job_id, status="failed", error=error, retryable_until=self.clock() + RETRYABLE_FOR)

    def increment_attempts(self, job_id: str) -> int:
        return int(self.redis.hincrby(self._key(job_id), "attempts", 1))

    def request_cancel(self, job_id: str) -> None:
        self.redis.hset(self._key(job_id), "cancel_requested", 1)

    def cancel_requested(self, job_id: str) -> bool:
        """True after DELETE: the job was cancelled, or deleted altogether."""
        return self.redis.hget(self._key(job_id), "cancel_requested") in ("1", None)

    # --- result -----------------------------------------------------------------------------

    def save_output(self, job_id: str, units: dict, report: dict) -> None:
        _write_json_atomic(self.job_dir(job_id) / "result.json", units)
        _write_json_atomic(self.job_dir(job_id) / "report.json", report)

    def save_pipeline_result(self, job_id: str, result: dict) -> None:
        """pipeline_result.json: the whole run_pipeline result (same as the CLI's output file)."""
        _write_json_atomic(self.job_dir(job_id) / "pipeline_result.json", result)

    def read_output(self, job_id: str, name: str) -> dict:
        """name: 'result' or 'report'."""
        return json.loads((self.job_dir(job_id) / f"{name}.json").read_text(encoding="utf-8"))

    # --- delete -----------------------------------------------------------------------------

    def delete(self, job_id: str) -> None:
        ref = (self.fields(job_id) or {}).get("external_ref")
        if ref and self.redis.get(self._ref_key(ref)) == job_id:
            self.redis.delete(self._ref_key(ref))
        self.redis.delete(self._key(job_id))
        self.redis.zrem(f"{PREFIX}:jobs", job_id)
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
