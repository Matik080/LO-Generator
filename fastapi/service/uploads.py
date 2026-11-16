"""Checks of POST /v1/jobs: options JSON and uploaded files (Príloha A – Chybové odpovede).

B accepts PDF, PPTX and HTML (A only PDF). A file is checked by its extension and by its
content, so a renamed file is refused with 415 before a job exists.
"""

import json
from dataclasses import dataclass
from pathlib import PurePath
from typing import Optional
from urllib.parse import urlparse

from fastapi import UploadFile
from pydantic import ValidationError

from lo_pipeline.contract import JobOptions
from service.errors import ApiError, error_list
from service.settings import ServiceSettings

FILE_TYPES = (".pdf", ".pptx", ".html", ".htm")


@dataclass(frozen=True)
class Upload:
    filename: str
    content: bytes


def parse_options(text: Optional[str], settings: ServiceSettings) -> JobOptions:
    try:
        raw = json.loads(text) if text and text.strip() else {}
    except json.JSONDecodeError as exc:
        raise ApiError(400, "invalid_options", "options is not valid JSON.", {"error": exc.msg}) from None
    if not isinstance(raw, dict):
        raise ApiError(400, "invalid_options", "options must be a JSON object.")
    try:
        options = JobOptions.model_validate(raw)
    except ValidationError as exc:
        raise ApiError(400, "invalid_options", "Invalid options.", {"errors": error_list(exc.errors())}) from None
    if options.callback_url is not None:
        host = urlparse(str(options.callback_url)).hostname or ""
        if host not in settings.webhook_allowed_hosts:
            raise ApiError(400, "invalid_options", f"callback_url host '{host}' is not allowed.",
                           {"allowed_hosts": list(settings.webhook_allowed_hosts)})
    if options.extraction is not None and options.extraction.ocr:
        raise ApiError(400, "invalid_options", "OCR is not available in this service.", {"ocr_available": False})
    return options


def _content_matches(extension: str, content: bytes) -> bool:
    if extension == ".pdf":
        return content.startswith(b"%PDF")
    if extension == ".pptx":
        return content.startswith(b"PK\x03\x04")  # a zip package
    try:
        content.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


async def read_uploads(files: list[UploadFile], settings: ServiceSettings) -> list[Upload]:
    if not files:
        raise ApiError(422, "validation_error", "At least one file is required (form field 'files').")
    if len(files) > settings.max_files:
        raise ApiError(413, "file_too_large", f"Too many files (limit {settings.max_files}).",
                       {"max_files": settings.max_files})
    limit = settings.max_upload_mb * 1024 * 1024
    uploads = []
    for index, file in enumerate(files):
        name = PurePath(file.filename or f"file-{index + 1}").name  # no directories from the client
        extension = PurePath(name).suffix.lower()
        if extension not in FILE_TYPES:
            raise ApiError(415, "unsupported_file", f"'{name}' is not a PDF, PPTX or HTML file.",
                           {"file": name, "file_types": list(FILE_TYPES)})
        content = await file.read(limit + 1)
        if len(content) > limit:
            raise ApiError(413, "file_too_large", f"'{name}' is larger than {settings.max_upload_mb} MB.",
                           {"file": name, "max_upload_mb": settings.max_upload_mb})
        if not _content_matches(extension, content):
            raise ApiError(415, "unsupported_file", f"'{name}' cannot be read as {extension[1:].upper()}.",
                           {"file": name})
        uploads.append(Upload(filename=name, content=content))
    return uploads
