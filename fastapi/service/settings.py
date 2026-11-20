"""Service settings from environment variables (LOGEN_*).

Temporary: sprint task 3 moves all configuration (service and pipeline) to pydantic-settings.
"""

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional


def _list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ServiceSettings:
    backend: str = "mock"                    # LOGEN_BACKEND: "mock" or "redis" (real jobs, task 4)
    redis_url: str = "redis://localhost:6379/0"  # LOGEN_REDIS_URL: job state and queue
    data_dir: str = "/data"                  # LOGEN_DATA_DIR: job folders (<data_dir>/jobs/{id}/)
    job_timeout_s: int = 6 * 60 * 60         # LOGEN_JOB_TIMEOUT_S: a job running longer fails (internal_error)
    api_keys: tuple[str, ...] = ()           # LOGEN_API_KEYS (comma-separated); empty = no key needed
    max_files: int = 10                      # LOGEN_MAX_FILES
    max_upload_mb: int = 50                  # LOGEN_MAX_UPLOAD_MB, per file
    max_active_jobs: int = 10                # LOGEN_MAX_ACTIVE_JOBS
    docs_enabled: bool = True                # LOGEN_DOCS_ENABLED: /docs and /openapi.json
    webhook_allowed_hosts: tuple[str, ...] = field(default=())  # LOGEN_WEBHOOK_ALLOWED_HOSTS
    mock_duration_s: float = 30.0            # LOGEN_MOCK_DURATION_S: how long a mock job runs

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ServiceSettings":
        env = os.environ if env is None else env
        defaults = cls()
        return cls(
            backend=env.get("LOGEN_BACKEND", defaults.backend),
            redis_url=env.get("LOGEN_REDIS_URL", defaults.redis_url),
            data_dir=env.get("LOGEN_DATA_DIR", defaults.data_dir),
            job_timeout_s=int(env.get("LOGEN_JOB_TIMEOUT_S", defaults.job_timeout_s)),
            api_keys=_list(env.get("LOGEN_API_KEYS", "")),
            max_files=int(env.get("LOGEN_MAX_FILES", defaults.max_files)),
            max_upload_mb=int(env.get("LOGEN_MAX_UPLOAD_MB", defaults.max_upload_mb)),
            max_active_jobs=int(env.get("LOGEN_MAX_ACTIVE_JOBS", defaults.max_active_jobs)),
            docs_enabled=_bool(env.get("LOGEN_DOCS_ENABLED", "true")),
            webhook_allowed_hosts=_list(env.get("LOGEN_WEBHOOK_ALLOWED_HOSTS", "")),
            mock_duration_s=float(env.get("LOGEN_MOCK_DURATION_S", defaults.mock_duration_s)),
        )
