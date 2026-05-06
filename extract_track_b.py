"""Track B extraction: ask Claude to fill line items XBRL didn't cover.

Reuses the EXTRACTION_SYSTEM_PROMPT from extraction_prompt.py — that prompt
encodes the confidence calibration ladder, units handling, and edge cases
the model needs. We mark it cache_control=ephemeral so subsequent /extract
calls hit the cache instead of paying for the prefix again.

The user message is built from extraction_prompt.build_extraction_messages
with a dynamically-generated schema listing only the fields we still need.

Track B is best-effort: parsing failures, network errors, or bad JSON return
an empty result so Track A's partial Company is preserved.
"""

import json
from datetime import date
from decimal import Decimal
from typing import Any

from anthropic import AsyncAnthropic

from extraction_prompt import EXTRACTION_SYSTEM_PROMPT, build_extraction_messages
from schemas import ExtractionSource, LineItem


MODEL = "claude-sonnet-4-6"


def _build_schema_json(field_list: list[str]) -> str:
    """Render a JSON-shape description for the response, scoped to field_list.

    Kept as a string (not a real JSON Schema) because the existing system
    prompt is tuned for human-readable schema specs in the user message.
    """
    field_lines = []
    for f in field_list:
        field_lines.append(
            f'    "{f}": {{"value": <number in actual USD>, '
            f'"source_quote": "<5-30 word verbatim quote>", '
            f'"confidence": <float 0.0-1.0>}}'
        )
    fields_block = ",\n".join(field_lines)
    return (
        "{\n"
        '  "fields": {\n'
        f"{fields_block}\n"
        "  }\n"
        "}\n\n"
        "Use the field keys exactly as listed above. Omit any field you cannot find with confidence.\n"
        "Return value in actual USD (multiply through if the filing reports in millions or thousands)."
    )


def _parse_response_text(text: str) -> dict[str, Any]:
    """Parse Claude's JSON response, recovering from minor formatting issues."""
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Recover from leading/trailing prose by slicing to the outer braces.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}


async def extract_track_b(
    client: AsyncAnthropic,
    ticker: str,
    company_name: str,
    period_end: date,
    accession_number: str,
    filing_section_text: str,
    fields_to_extract: list[str],
) -> dict[str, LineItem]:
    """Ask Claude for the listed fields. Returns {field_name: LineItem}.

    Fields Claude couldn't find (or that returned malformed values) are
    omitted, not included with None values.
    """
    if not fields_to_extract:
        return {}

    schema_json = _build_schema_json(fields_to_extract)
    messages = build_extraction_messages(
        company_name=company_name,
        ticker=ticker,
        period_end=period_end.isoformat(),
        accession_number=accession_number,
        schema_json=schema_json,
        filing_text=filing_section_text,
    )

    response = await client.messages.create(
        model=MODEL,
        max_tokens=8192,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        system=[
            {
                "type": "text",
                "text": EXTRACTION_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
    )

    text = next((b.text for b in response.content if b.type == "text"), "")
    data = _parse_response_text(text)
    fields = data.get("fields", {})
    if not isinstance(fields, dict):
        return {}

    result: dict[str, LineItem] = {}
    for field_name, item in fields.items():
        if field_name not in fields_to_extract:
            continue
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if value is None:
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        try:
            line_item = LineItem(
                value=Decimal(str(value)),
                source=ExtractionSource.LLM_HTML,
                confidence=confidence,
                source_quote=item.get("source_quote"),
            )
        except (ValueError, TypeError):
            continue
        result[field_name] = line_item
    return result
