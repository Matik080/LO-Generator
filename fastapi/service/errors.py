"""One error format for every response: {"error": {"code", "message", "details"}}.

Covers our own ApiError, FastAPI's request validation, Starlette's HTTP errors (unknown
path, wrong method) and unexpected exceptions (500 without exception details).
"""

import logging
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from lo_pipeline.contract import ErrorBody, ErrorResponse

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str,
                 details: Optional[dict[str, Any]] = None, headers: Optional[dict[str, str]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}


def job_not_found(job_id: str) -> ApiError:
    return ApiError(404, "job_not_found", "Job does not exist or was deleted.", {"job_id": job_id})


def error_list(errors) -> list[dict[str, Any]]:
    """Pydantic errors without the input values (they may contain document text)."""
    return [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
            for e in errors]


def error_response(status_code: int, code: str, message: str,
                   details: Optional[dict[str, Any]] = None, headers: Optional[dict[str, str]] = None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details or {}))
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code, headers=headers)


_CODE_FOR_STATUS = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed",
                    413: "file_too_large", 415: "unsupported_file", 422: "validation_error"}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        return error_response(422, "validation_error", "Request does not match the API.",
                              {"errors": error_list(exc.errors())})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        code = _CODE_FOR_STATUS.get(exc.status_code, "internal_error")
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error."
        return error_response(exc.status_code, code, message, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.error("unexpected error on %s %s: %s", request.method, request.url.path, type(exc).__name__,
                     exc_info=exc)
        return error_response(500, "internal_error", "Internal server error.")
