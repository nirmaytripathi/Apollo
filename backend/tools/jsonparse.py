"""Safe JSON parsing for LLM output.

LLMs often wrap JSON in ```json fences or add a sentence before/after it.
parse_json strips fences, tries a direct parse, then falls back to the first
{...} block in the text. It never raises: it returns None on failure so the
caller decides what to do.
"""

import json
import re

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_json(text: str) -> dict | None:
    if not text:
        return None
    cleaned = _FENCE.sub("", text.strip())
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
