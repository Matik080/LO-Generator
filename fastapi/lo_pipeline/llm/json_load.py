import json
import re
from typing import Any
import logging

logger = logging.getLogger(__name__)


def parse_json(text: str) -> Any:
    """Attempts to parse JSON string into Python object"""
    try:
        # Strip markdown fences if model ignored instructions
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text).strip()

        return json.loads(text)
    except json.JSONDecodeError:
        # Second attempt: try to fix invalid escape sequences
        # Valid JSON escapes are: \" \\ \/ \b \f \n \r \t \uXXXX
        cleaned = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as error:
            # Third attempt: text around the JSON ("Here you go: ```json [...] ```"): from the first
            # '[' or '{' to the last matching bracket
            starts = [i for i in (cleaned.find("["), cleaned.find("{")) if i >= 0]
            if starts:
                start = min(starts)
                end = cleaned.rfind("]" if cleaned[start] == "[" else "}")
                if end > start:
                    try:
                        return json.loads(cleaned[start:end + 1])
                    except json.JSONDecodeError:
                        pass
            # Debug only: the output contains document text
            logger.debug("Model output that is not valid JSON:\n=== RAW MODEL OUTPUT START ===\n%s\n=== RAW MODEL OUTPUT END ===", text)
            raise error
