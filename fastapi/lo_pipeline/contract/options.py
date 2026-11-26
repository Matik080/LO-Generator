"""Job parameters `options` (POST /v1/jobs, api-design-appendix.md – Vstupné parametre).

The same parameters as Mlynček A, so Laravel sends one request format to both. Every
parameter is optional; `resolve_options` fills the missing ones from the configuration
and the API stores the complete options with the job (a retry then runs with the same
settings even if the configuration changed meanwhile).

What B does with them in the prototype (sprint task 5):
- split: only "single" is implemented; other modes are accepted and fall back to one
  lesson with a warning in the report,
- questions.types: B generates choice and open; no_answer is skipped with a warning.
Checks that need the configuration (allowed webhook hosts, OCR installed) are in the API.
"""

import re
from dataclasses import dataclass
from typing import Annotated, Literal, Optional, Union

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from lo_pipeline.contract.units import UNIT_DESCRIPTION_MAX, UNIT_TITLE_MAX, QuestionType

Language = Literal["auto", "sk", "en"]

# "D1:1-10" or "D1:1-10, D2:3": document number (order of the uploaded files) and pages.
PAGES_PATTERN = r"^D\d+:\d+(-\d+)?(\s*,\s*D\d+:\d+(-\d+)?)*$"
_PAGE_RANGE = re.compile(r"D(\d+):(\d+)(?:-(\d+))?")

LessonMinutes = Annotated[int, Field(ge=5, le=240, description="Lesson length in minutes.")]
LessonCount = Annotated[int, Field(ge=1, le=100, description="Number of lessons.")]


def page_ranges(text: str) -> list[tuple[int, int, int]]:
    """'D1:1-10, D2:3' → [(1, 1, 10), (2, 3, 3)] as (document, first page, last page)."""
    return [(int(doc), int(first), int(last or first)) for doc, first, last in _PAGE_RANGE.findall(text)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractionOptions(_Strict):
    ocr: Optional[bool] = Field(default=None, description="OCR of scanned PDF pages and of pictures in PPTX; "
                                                          "only when /v1/info reports ocr_available.")


class QuestionOptions(_Strict):
    types: Optional[list[QuestionType]] = Field(default=None, min_length=1, description="Question types.")
    per_section: Optional[int] = Field(default=None, ge=1, le=10, description="Questions per LO (minimum).")
    choice_options: Optional[int] = Field(default=None, ge=2, le=10, description="Options of a choice question.")
    hints: Optional[int] = Field(default=None, ge=0, le=2, description="0 = no hint; 1–2 = a source hint.")

    @model_validator(mode="after")
    def _no_duplicate_types(self) -> "QuestionOptions":
        if self.types is not None and len(set(self.types)) != len(self.types):
            raise ValueError("questions.types has duplicates")
        return self


# split: one model per mode, chosen by `mode`. Each mode accepts the fields of the other
# modes only as null, so the full example of Príloha A ("lesson_count": null, …) is valid,
# but a value in a field the mode does not use is an error.

class SingleSplit(_Strict):
    mode: Literal["single"] = "single"
    lesson_count: None = None
    lesson_minutes: Optional[LessonMinutes] = None
    syllabus: None = None
    syllabus_text: None = None


class LessonsSplit(_Strict):
    mode: Literal["lessons"]
    lesson_count: LessonCount
    lesson_minutes: Optional[LessonMinutes] = None
    syllabus: None = None
    syllabus_text: None = None


class TopicsSplit(_Strict):
    mode: Literal["topics"]
    lesson_count: None = None
    lesson_minutes: Optional[LessonMinutes] = None
    syllabus: None = None
    syllabus_text: None = None


class SyllabusLesson(_Strict):
    title: str = Field(min_length=1, max_length=UNIT_TITLE_MAX)
    description: Optional[str] = Field(default=None, max_length=UNIT_DESCRIPTION_MAX)
    pages: Optional[str] = Field(default=None, pattern=PAGES_PATTERN, description="e.g. 'D1:1-10, D2:3'.")

    @model_validator(mode="after")
    def _valid_ranges(self) -> "SyllabusLesson":
        for doc, first, last in page_ranges(self.pages or ""):
            if doc < 1 or first < 1:
                raise ValueError(f"pages: documents and pages start at 1 (D{doc}:{first})")
            if last < first:
                raise ValueError(f"pages: range ends before it starts (D{doc}:{first}-{last})")
        return self


class SyllabusSplit(_Strict):
    mode: Literal["syllabus"]
    lesson_count: None = None
    lesson_minutes: Optional[LessonMinutes] = None
    syllabus: Optional[list[SyllabusLesson]] = Field(default=None, min_length=1, max_length=100)
    syllabus_text: Optional[str] = Field(default=None, max_length=UNIT_DESCRIPTION_MAX)

    @model_validator(mode="after")
    def _list_or_text(self) -> "SyllabusSplit":
        if self.syllabus_text is not None and not self.syllabus_text.strip():
            raise ValueError("syllabus_text is empty")
        if (self.syllabus is None) == (self.syllabus_text is None):
            raise ValueError("mode 'syllabus' needs exactly one of 'syllabus' and 'syllabus_text'")
        return self


SplitOptions = Annotated[
    Union[SingleSplit, LessonsSplit, TopicsSplit, SyllabusSplit],
    Field(discriminator="mode"),
]


class JobOptions(_Strict):
    model_config = ConfigDict(extra="forbid", title="JobOptions")

    external_ref: Optional[str] = Field(
        default=None, min_length=1, max_length=255,
        description="Caller's own id (ECEH: generations.external_ref); unique per API key.",
    )
    callback_url: Optional[AnyHttpUrl] = Field(default=None, description="Webhook address.")
    language: Optional[Language] = None
    extraction: Optional[ExtractionOptions] = None
    questions: Optional[QuestionOptions] = None
    split: SplitOptions = Field(default_factory=SingleSplit)


@dataclass(frozen=True)
class OptionDefaults:
    """Values for options the caller did not send (documented defaults, Príloha A)."""

    language: Language = "auto"
    ocr: bool = False
    question_types: tuple[str, ...] = ("choice", "open", "no_answer")
    per_section: int = 3
    choice_options: int = 4
    hints: int = 2


def resolve_options(options: JobOptions, defaults: OptionDefaults = OptionDefaults()) -> JobOptions:
    """Complete options: every missing value taken from `defaults`.

    external_ref and callback_url stay as sent (None = not used), split stays as sent.
    """
    extraction = options.extraction or ExtractionOptions()
    questions = options.questions or QuestionOptions()

    def pick(value, default):
        return default if value is None else value

    complete = {
        "external_ref": options.external_ref,
        "callback_url": options.callback_url,
        "language": pick(options.language, defaults.language),
        "extraction": {"ocr": pick(extraction.ocr, defaults.ocr)},
        "questions": {
            "types": list(pick(questions.types, defaults.question_types)),
            "per_section": pick(questions.per_section, defaults.per_section),
            "choice_options": pick(questions.choice_options, defaults.choice_options),
            "hints": pick(questions.hints, defaults.hints),
        },
        "split": options.split.model_dump(),
    }
    return JobOptions.model_validate(complete)  # validated again, so bad defaults fail here
