"""The `units` output format: GET /v1/jobs/{id}/result (api-design.md, kap. 6).

Laravel stores exactly these fields in the ECEH database, so the format is the same as
Mlynček A's and no field may be added anywhere (extra="forbid" → additionalProperties: false).
String limits are the ECEH column sizes ("Limit v ECEH", kap. 6); the JSON Schema has them
as maxLength.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

# ECEH column sizes (kap. 6, page "Databáza").
UNIT_TITLE_MAX = 255          # units.title
UNIT_DESCRIPTION_MAX = 4000   # units.description
QUESTION_NAME_MAX = 255       # events.header
QUESTION_DESCRIPTION_MAX = 4000  # events.message
HINT_NAME_MAX = 50            # helps.name
HINT_URL_MAX = 2000           # helps.url (the HTML text of the hint, not a web address)
CHOICE_TEXT_MAX = 4000        # options.answer_data

QuestionType = Literal["choice", "open", "no_answer"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Hint(_Strict):
    name: str = Field(min_length=1, max_length=HINT_NAME_MAX, description="Hint label, e.g. 'Zdroj'.")
    url: str = Field(max_length=HINT_URL_MAX, description="HTML text of the hint, e.g. '<p>…</p>'.")


class Choice(_Strict):
    text: str = Field(min_length=1, max_length=CHOICE_TEXT_MAX)
    is_correct: StrictBool


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def question_type_problems(question_type: str, choices: list[tuple[str, bool]]) -> list[str]:
    """Rules of the question types (kap. 7) for (text, is_correct) pairs; [] when all hold.

    choice:    at least 2 choices, at least 1 correct, no two choices with the same text
    open:      exactly 1 choice (the model answer) with is_correct = true
    no_answer: no choices
    """
    problems = []
    if question_type == "choice":
        if len(choices) < 2:
            problems.append("a choice question needs at least 2 choices")
        if not any(correct for _, correct in choices):
            problems.append("a choice question needs at least 1 correct choice")
        texts = [_normalized(text) for text, _ in choices]
        if len(set(texts)) != len(texts):
            problems.append("a choice question has two choices with the same text")
    elif question_type == "open":
        if len(choices) != 1 or not choices[0][1]:
            problems.append("an open question needs exactly 1 choice with is_correct = true")
    elif question_type == "no_answer":
        if choices:
            problems.append("a no_answer question must have no choices")
    return problems


# The same rules as JSON Schema (if/then per type), so Laravel can check them without
# our Python code. Duplicate texts are not expressible in JSON Schema; only the model checks them.
_TYPE_RULES: list[dict[str, Any]] = [
    {
        "if": {"properties": {"type": {"const": "choice"}}, "required": ["type"]},
        "then": {"properties": {"choices": {
            "minItems": 2,
            "contains": {"properties": {"is_correct": {"const": True}}, "required": ["is_correct"]},
        }}},
    },
    {
        "if": {"properties": {"type": {"const": "open"}}, "required": ["type"]},
        "then": {"properties": {"choices": {
            "minItems": 1,
            "maxItems": 1,
            "items": {"properties": {"is_correct": {"const": True}}},
        }}},
    },
    {
        "if": {"properties": {"type": {"const": "no_answer"}}, "required": ["type"]},
        "then": {"properties": {"choices": {"maxItems": 0}}},
    },
]


class Question(_Strict):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"allOf": _TYPE_RULES})

    name: str = Field(min_length=1, max_length=QUESTION_NAME_MAX, description="Short name of the question.")
    description: str = Field(min_length=1, max_length=QUESTION_DESCRIPTION_MAX, description="Question or task text.")
    type: QuestionType
    time_to_handle: StrictInt = Field(ge=0, description="Seconds a student needs to answer.")
    time_to_explain: StrictInt = Field(ge=0, description="Seconds a teacher needs to explain the answer.")
    hints: list[Hint]
    choices: list[Choice]

    @model_validator(mode="after")
    def _type_rules(self) -> "Question":
        problems = question_type_problems(self.type, [(c.text, c.is_correct) for c in self.choices])
        if problems:
            raise ValueError("; ".join(problems))
        return self


class Unit(_Strict):
    """One lesson (vyučovacia hodina)."""

    title: str = Field(min_length=1, max_length=UNIT_TITLE_MAX)
    description: str = Field(max_length=UNIT_DESCRIPTION_MAX)
    questions: list[Question]


class UnitsResult(_Strict):
    model_config = ConfigDict(extra="forbid", title="UnitsResult")

    units: list[Unit] = Field(min_length=1, description="Lessons; exactly one in split mode 'single'.")
