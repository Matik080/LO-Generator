"""FastAPI application of the /v1 service.

    uvicorn service.main:app --host 0.0.0.0 --port 8000

The old development UI (fastapi/main.py, pipeline_ui.html) is separate and not part of the contract.
"""

import logging
from typing import Optional

from fastapi import FastAPI

from service.backend import JobBackend
from service.errors import install_error_handlers
from service.mock import MockBackend
from service.routes import VERSION, public, v1
from service.settings import ServiceSettings

logger = logging.getLogger(__name__)


def make_backend(settings: ServiceSettings) -> JobBackend:
    if settings.backend == "mock":
        return MockBackend(duration_s=settings.mock_duration_s, max_active_jobs=settings.max_active_jobs)
    raise ValueError(f"LOGEN_BACKEND={settings.backend!r} is not available yet (only 'mock')")


def create_app(settings: Optional[ServiceSettings] = None, backend: Optional[JobBackend] = None) -> FastAPI:
    settings = settings or ServiceSettings.from_env()
    docs = settings.docs_enabled
    app = FastAPI(
        title="LO-Generator (Mlynček B)",
        version=VERSION,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.state.backend = backend or make_backend(settings)
    install_error_handlers(app)
    app.include_router(public)
    app.include_router(v1)
    if not settings.api_keys:
        logger.warning("LOGEN_API_KEYS is empty: the API accepts requests without a key")
    return app


app = create_app()
