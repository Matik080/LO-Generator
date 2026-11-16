"""The API contract: what Laravel (ECEH) sends and receives.

Models only, no pipeline logic. The contract is the same as Mlynček A's, so the two
services can be swapped; the implementation is B's own. JSON Schemas: schemas.py.
"""

from lo_pipeline.contract.jobs import (
    FINAL_STATUSES,
    CreateJobResponse,
    ErrorBody,
    ErrorResponse,
    JobError,
    JobLinks,
    JobListItem,
    JobListResponse,
    JobStatus,
    WebhookPayload,
)
from lo_pipeline.contract.options import JobOptions, OptionDefaults, resolve_options
from lo_pipeline.contract.report import QuestionMeta, Quality, Report, ReportWarning, Section, SourceRef, section_id
from lo_pipeline.contract.units import Choice, Hint, Question, Unit, UnitsResult, question_type_problems

__all__ = [
    "FINAL_STATUSES", "CreateJobResponse", "ErrorBody", "ErrorResponse", "JobError", "JobLinks",
    "JobListItem", "JobListResponse", "JobStatus", "WebhookPayload",
    "JobOptions", "OptionDefaults", "resolve_options",
    "QuestionMeta", "Quality", "Report", "ReportWarning", "Section", "SourceRef", "section_id",
    "Choice", "Hint", "Question", "Unit", "UnitsResult", "question_type_problems",
]
