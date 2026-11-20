"""Service settings (/v1 API, job queue, worker) from LOGEN_* variables and fastapi/.env.

The pipeline's own settings (LLM provider, models, thresholds) are in lo_pipeline/config.py.
"""

from typing import Annotated, Literal, Mapping, Optional

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from lo_pipeline.config import ENV_FILE

CommaList = Annotated[tuple[str, ...], NoDecode]  # "a, b" in the environment -> ("a", "b")


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOGEN_", env_file=ENV_FILE, env_file_encoding="utf-8",
                                      extra="ignore", frozen=True)

    backend: Literal["mock", "redis"] = "mock"     # mock: example answers; redis: real jobs (worker)
    redis_url: str = "redis://localhost:6379/0"   # job state and queue
    data_dir: str = "/data"                       # job folders (<data_dir>/jobs/{id}/)
    job_timeout_s: int = Field(6 * 60 * 60, ge=1)  # a job running longer fails (internal_error)
    api_keys: CommaList = ()                      # accepted X-API-Key values; empty = no key needed
    max_files: int = Field(10, ge=1)              # files per job
    max_upload_mb: int = Field(50, ge=1)          # per file
    max_active_jobs: int = Field(10, ge=1)        # queued + running; more -> 429
    docs_enabled: bool = True                     # /docs and /openapi.json
    webhook_allowed_hosts: CommaList = ()         # hosts allowed in callback_url; empty = webhooks off
    webhook_secret: Optional[SecretStr] = None    # HMAC key of the webhook signature (Laravel has the same)
    webhook_timeout_s: float = Field(5.0, gt=0)   # one delivery attempt
    result_ttl_days: float = Field(7, gt=0)        # finished jobs (result, report, state) are deleted after this
    failed_input_ttl_hours: float = Field(24, gt=0)  # inputs of a failed job stay this long (retry)
    cleanup_interval_s: int = Field(600, ge=10)   # how often the scheduler deletes old inputs and jobs
    mock_duration_s: float = Field(30.0, ge=0)    # how long a mock job runs

    @field_validator("api_keys", "webhook_allowed_hosts", mode="before")
    @classmethod
    def _comma_list(cls, value):
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @model_validator(mode="after")
    def _webhooks_need_a_secret(self) -> "ServiceSettings":
        if self.webhook_allowed_hosts and not self.secret():
            raise ValueError("LOGEN_WEBHOOK_ALLOWED_HOSTS is set, so LOGEN_WEBHOOK_SECRET is required "
                             "(Laravel checks the signature of every webhook)")
        return self

    def secret(self) -> str:
        return self.webhook_secret.get_secret_value().strip() if self.webhook_secret else ""

    @property
    def webhooks_enabled(self) -> bool:
        return bool(self.webhook_allowed_hosts)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ServiceSettings":
        """From the environment and fastapi/.env, or only from `env` (a mapping, for tests)."""
        if env is None:
            return cls()
        values = {name: env[f"LOGEN_{name.upper()}"] for name in cls.model_fields if f"LOGEN_{name.upper()}" in env}
        return cls(_env_file=None, **values)
