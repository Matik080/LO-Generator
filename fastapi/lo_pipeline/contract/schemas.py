"""JSON Schemas of the contract, generated from the Pydantic models.

The files in fastapi/schemas/ are what we give to Laravel and Mlynček A, and what
GET /v1/schema/{name} returns. They are generated, never edited by hand:

    python -m lo_pipeline.contract.schemas           # rewrite schemas/*.json
    python -m lo_pipeline.contract.schemas --check   # exit 1 if a file is out of date

A test fails when a model changes and the files were not regenerated.
"""

import json
import sys
from pathlib import Path

from lo_pipeline.contract.options import JobOptions
from lo_pipeline.contract.report import Report
from lo_pipeline.contract.units import UnitsResult

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"
EXAMPLES_DIR = SCHEMA_DIR / "examples"  # examples that pass the schemas (tests, mock API, Laravel)

_MODELS = {
    "lesson": UnitsResult,  # name used by the API: /v1/schema/lesson
    "report": Report,
    "options": JobOptions,
}
SCHEMA_NAMES: tuple[str, ...] = tuple(_MODELS)


def build_schema(name: str) -> dict:
    schema = _MODELS[name].model_json_schema()
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def schema_path(name: str) -> Path:
    return SCHEMA_DIR / f"{name}.schema.json"


def schema_text(name: str) -> str:
    return json.dumps(build_schema(name), indent=2, ensure_ascii=False) + "\n"


def load_schema(name: str) -> dict:
    return json.loads(schema_path(name).read_text(encoding="utf-8"))


def load_example(file_name: str) -> dict:
    return json.loads((EXAMPLES_DIR / file_name).read_text(encoding="utf-8"))


def outdated_schemas() -> list[str]:
    return [
        name for name in SCHEMA_NAMES
        if not schema_path(name).exists() or schema_path(name).read_text(encoding="utf-8") != schema_text(name)
    ]


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        outdated = outdated_schemas()
        print("out of date: " + ", ".join(outdated) if outdated else "schemas are up to date")
        sys.exit(1 if outdated else 0)
    SCHEMA_DIR.mkdir(exist_ok=True)
    for name in SCHEMA_NAMES:
        schema_path(name).write_text(schema_text(name), encoding="utf-8", newline="\n")
        print(f"wrote {schema_path(name)}")
