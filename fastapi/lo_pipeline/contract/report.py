"""Report: GET /v1/jobs/{id}/report – data that is not in the `units` format.

Same shape as Mlynček A's report, so Laravel reads both the same way:
- sections:      B's learning objects (id "S-<n>", statement, LO type, prerequisites from the
                 prerequisite graph); A calls its groups of questions "sections" (tematické celky)
- question_meta: one entry per question in `units`, pointing to its section (= B's LO) and source pages
- quality:       averages on a 0–1 scale, null when B does not measure it
- warnings, timings

The only difference from A: bloom_level may be null, B does not determine Bloom levels
(api-design-appendix.md, Príloha A, "Poznámky k B").
Laravel does not need the report to store a result; it is diagnostics.
"""

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SECTION_ID_PATTERN = r"^S-\d+$"

SectionId = Annotated[str, Field(pattern=SECTION_ID_PATTERN, description="Section id, e.g. 'S-12'.")]
Score01 = Annotated[float, Field(ge=0.0, le=1.0)]
BloomLevel = Literal["remember", "understand", "apply", "analyze", "evaluate", "create"]


def section_id(index: int) -> str:
    """0-based position of the LO in the result → 'S-1', 'S-2', …"""
    return f"S-{index + 1}"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Section(_Strict):
    id: SectionId
    title: str
    statement: str
    lo_type: str = Field(description="Type of the LO, e.g. 'definition', 'rule', 'algorithm_step'.")
    prerequisites: list[SectionId] = Field(description="Sections this one builds on.")
    topic: Optional[str] = None


class SourceRef(_Strict):
    file: str = Field(min_length=1, description="Name of the uploaded file, e.g. 'modul1.pdf'.")
    pages: list[Annotated[int, Field(ge=1)]] = Field(description="Cited pages, 1-based.")


class QuestionMeta(_Strict):
    """Metadata of the question units[unit].questions[question]."""

    unit: int = Field(ge=0, description="Index in units.")
    question: int = Field(ge=0, description="Index in units[unit].questions.")
    section_id: SectionId
    bloom_level: Optional[BloomLevel] = Field(description="Bloom level; B always sends null.")
    keywords: list[str]
    source: list[SourceRef]
    topic: Optional[str] = None


class Quality(_Strict):
    """Averages on a 0–1 scale (a 1–5 score x becomes (x − 1) / 4); null = not measured."""

    section_faithfulness_avg: Optional[Score01] = None
    question_faithfulness_avg: Optional[Score01] = None
    answerability_avg: Optional[Score01] = None
    topic_coverage: Optional[Score01] = None


class ReportWarning(_Strict):
    code: str = Field(min_length=1, description="Machine-readable code, e.g. 'field_too_long'.")
    message: str


class Report(_Strict):
    model_config = ConfigDict(extra="forbid", title="Report")

    sections: list[Section]
    question_meta: list[QuestionMeta]
    quality: Quality
    warnings: list[ReportWarning]
    timings: dict[str, Annotated[float, Field(ge=0.0)]] = Field(description="Seconds per stage.")
