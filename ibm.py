"""IBM's explicitly labeled EBIT proxy from same-filing annual GAAP facts.

IBM incorporates its statements in Exhibit 13 and does not report the
standard OperatingIncomeLoss subtotal. Do not substitute its non-GAAP
operating earnings or ask an LLM to invent the missing subtotal.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from schemas import ExtractionSource, LineItem

CIK = "51143"
PROTECTED_FIELDS = {"operating_income", "income_before_tax", "interest_expense", "income_tax_expense"}
CONCEPTS = {
    "income_before_tax": "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "interest_expense": "InterestExpense",
    "income_tax_expense": "IncomeTaxExpenseBenefit",
}
PROXY_LABEL = "EBIT proxy"
VALUATION_NOTE = (
    "IBM uses an EBIT proxy: GAAP pre-tax income from continuing operations plus separately reported interest expense. "
    "This is a calculated model input, not reported operating income or IBM's operating (non-GAAP) earnings. "
    "Other income/expense remains included, and financing interest in cost of revenue remains deducted. "
    "The historical proxy margin drives the cash-flow projection; review its suitability before relying on the estimate."
)


def filing_items(facts: dict[str, Any], cik: str, pe: date, accession: str, latest: date) -> dict[str, LineItem]:
    if (cik.lstrip("0") != CIK or str(facts.get("cik", "")).lstrip("0") != CIK
            or (pe.month, pe.day) != (12, 31) or (latest.month, latest.day) != (12, 31)
            or pe > latest or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)):
        return {}
    # SEC filing agents can submit under a different accession prefix.
    # Bound history by filed date rather than comparing those prefixes.
    anchor_dates = set()
    for field in ("income_before_tax", "interest_expense"):
        for e in facts.get("facts", {}).get("us-gaap", {}).get(CONCEPTS[field], {}).get("units", {}).get("USD", []):
            if (e.get("accn") == accession and e.get("start") == f"{latest.year}-01-01"
                    and e.get("end") == latest.isoformat() and e.get("form") in {"10-K", "10-K/A"}
                    and e.get("fp") == "FY"):
                try:
                    anchor_dates.add(date.fromisoformat(e["filed"]))
                except (KeyError, ValueError, TypeError):
                    return {}
    if len(anchor_dates) != 1:
        return {}
    anchor_filed = anchor_dates.pop()
    if anchor_filed < latest:
        return {}
    start = date(pe.year, 1, 1)
    by_field = {}
    for field, concept in CONCEPTS.items():
        by_accession = {}
        for e in facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {}).get("USD", []):
            accn = e.get("accn", "")
            try:
                filed = date.fromisoformat(e["filed"])
            except (KeyError, ValueError, TypeError):
                continue
            if (e.get("start") != start.isoformat() or e.get("end") != pe.isoformat()
                    or e.get("form") not in {"10-K", "10-K/A"} or e.get("fp") != "FY"
                    or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accn)
                    or filed > anchor_filed or filed < pe or (pe == latest and accn != accession)):
                continue
            by_accession.setdefault(accn, []).append(e)
        by_field[field] = by_accession
    # Both operands must be in one filing. A newer conflicting observation
    # is not hidden by falling back to an older, convenient value.
    required = ("income_before_tax", "interest_expense")
    common = set(by_field[required[0]]) & set(by_field[required[1]])
    if not common:
        return {}
    selected = accession if accession in common else max(
        common, key=lambda accn: (max(e["filed"] for e in by_field[required[0]][accn]), accn),
    )
    if len({e["filed"] for field in required for e in by_field[field][selected]}) != 1:
        return {}
    output = {}
    for field, concept in CONCEPTS.items():
        rows = by_field[field].get(selected, [])
        try:
            values = {Decimal(str(e["val"])) for e in rows}
        except (KeyError, InvalidOperation, ValueError):
            values = set()
        if len(values) != 1:
            if field in required:
                return {}
            continue
        value = values.pop()
        if not value.is_finite() or (field == "interest_expense" and value < 0):
            if field in required:
                return {}
            continue
        output[field] = LineItem(
            value=value, source=ExtractionSource.XBRL, confidence=1.0, xbrl_tag=f"us-gaap:{concept}",
            source_quote=f"Filed {field}: {value} USD; annual period {start} to {pe}; SEC accession {selected}.",
        )
    ibt, interest = (output[field].value for field in required)
    output["operating_income"] = LineItem(
        value=ibt + interest, source=ExtractionSource.DERIVED, confidence=0.65,
        source_quote=(
            f"IBM EBIT proxy: GAAP pre-tax income from continuing operations + separately reported interest expense "
            f"({ibt} + {interest} = {ibt + interest} USD); annual period {start} to {pe}; SEC accession {selected}. "
            "Calculated model input, not reported operating income or IBM operating (non-GAAP) earnings. "
            "Retains other income/expense; financing interest in cost of revenue is not added back. Review required."
        ),
    )
    return output
