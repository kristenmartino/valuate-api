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
from schemas import ExtractionSource, LineItem, RevenueSegment


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


async def extract_revenue_segments(
    client: AsyncAnthropic,
    ticker: str,
    company_name: str,
    period_end: date,
    accession_number: str,
    filing_section_text: str,
) -> list[RevenueSegment]:
    """Ask Claude for the most-recent FY revenue breakdown by segment.

    Returns an empty list if the filer is single-segment / doesn't break out
    revenue, or if extraction fails for any reason. The list contains
    RevenueSegment objects whose `revenue` LineItem carries the same
    provenance fields (source quote, confidence) as consolidated line items.

    Reuses the cached EXTRACTION_SYSTEM_PROMPT for cost amortization across
    the segment call and the main field-fill call on the same filing.
    """
    user_prompt = (
        f"Company: {company_name} ({ticker})\n"
        f"Filing: 10-K for fiscal year ended {period_end.isoformat()}\n"
        f"Accession: {accession_number}\n\n"
        "Locate the segment reporting note in the filing excerpt below "
        '(typically titled "Segment Information", "Operating Segments", '
        '"Disaggregation of Revenue", "Net Sales by Reportable Segment", '
        "or similar). Return revenue broken out by segment for the most "
        "recent fiscal year only.\n\n"
        "Schema:\n"
        "{\n"
        '  "segments": [\n'
        '    {"name": "<exact label as reported>", '
        '"value": <number in actual USD>, '
        '"source_quote": "<5-30 word verbatim quote>", '
        '"confidence": <float 0-1>},\n'
        "    ...\n"
        "  ]\n"
        "}\n\n"
        'If the company does not report revenue by segment, return {"segments": []}.\n'
        "Use the filer's segment names verbatim — do not regroup, rename, or "
        "paraphrase. Return value in actual USD (multiply through if the "
        "filing reports in millions or thousands).\n\n"
        "FILING EXCERPT (financial statements section):\n"
        "---\n"
        f"{filing_section_text}\n"
        "---\n\n"
        "Return only the JSON object. No prose, no markdown."
    )

    response = await client.messages.create(
        model=MODEL,
        max_tokens=4096,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        system=[
            {
                "type": "text",
                "text": EXTRACTION_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = next((b.text for b in response.content if b.type == "text"), "")
    data = _parse_response_text(text)
    raw = data.get("segments", [])
    if not isinstance(raw, list):
        return []

    segments: list[RevenueSegment] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        value = entry.get("value")
        if not isinstance(name, str) or not name.strip() or value is None:
            continue
        try:
            confidence = float(entry.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        confidence = max(0.0, min(1.0, confidence))
        try:
            line = LineItem(
                value=Decimal(str(value)),
                source=ExtractionSource.LLM_HTML,
                confidence=confidence,
                source_quote=entry.get("source_quote"),
            )
        except (ValueError, TypeError):
            continue
        segments.append(RevenueSegment(name=name.strip(), revenue=line))
    return segments
