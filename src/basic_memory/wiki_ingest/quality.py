"""Deterministic review aids; findings are never rewritten automatically."""

import re

from pydantic import JsonValue

PATTERNS = {
    "date": r"\b\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})\b|\b20\d{2}-\d{2}-\d{2}\b",
    "time": r"\b\d{1,2}:\d{2}(?::\d{2})?\b",
    "case": r"\b[IVX]{1,4}\s+[A-Z][A-Za-z]{0,4}\s+\d{1,5}/\d{2}\b",
    "amount": r"\b\d[\d\s]*[,.]\d{2}\s?(?:zł|PLN|EUR|€)|\b\d+\s?(?:zł|PLN)\b",
    "page": r"\bk\.\s?\d+(?:\s?[-–]\s?\d+)?",
}


def coverage(source: str, findings: str) -> dict[str, JsonValue]:
    normalized = re.sub(r"\s+", "", findings).lower()
    missing: dict[str, JsonValue] = {}
    for category, pattern in PATTERNS.items():
        values = sorted(set(re.findall(pattern, source)))
        missing[category] = [v for v in values if re.sub(r"\s+", "", v).lower() not in normalized]
    return {
        "missing": missing,
        "meaning": "Review aid only: missing tokens do not prove a substantive omission",
    }
