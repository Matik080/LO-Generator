import json
import re
from typing import Any


def parse_json(text: str) -> Any:
    """Attempts to parse JSON string into Python object"""
    try:
        # Strip markdown fences if model ignored instructions
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text).strip()

        return json.loads(text)
    except json.JSONDecodeError:
        # Second attempt: try to fix invalid escape sequences
        try:
            # Replace invalid backslash escapes that aren't valid JSON escapes
            # Valid JSON escapes are: \" \\ \/ \b \f \n \r \t \uXXXX
            cleaned = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)
            return json.loads(cleaned)
        except json.JSONDecodeError:
            print("\n=== RAW MODEL OUTPUT START ===")
            print(text)
            print("=== RAW MODEL OUTPUT END ===\n")
            raise
