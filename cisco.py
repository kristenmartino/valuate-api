"""Select Cisco's noncurrent debt without counting current maturities twice."""
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from schemas import Company, ExtractionFlag, ExtractionSource, LineItem

CIK = "858877"
INCLUSIVE_TAG = "us-gaap:LongTermDebt"
NONCURRENT_TAG = "us-gaap:LongTermDebtNoncurrent"


def filing_debt(facts: dict[str, Any], cik: str, pe: date, accession: str, latest: date) -> LineItem | None:
    if cik.lstrip("0") != CIK or str(facts.get("cik", "")).lstrip("0") != CIK:
        return None
    entries = []
    for e in facts.get("facts", {}).get("us-gaap", {}).get("LongTermDebtNoncurrent", {}).get("units", {}).get("USD", []):
        accn = e.get("accn", "")
        if (e.get("end") != pe.isoformat() or e.get("start")
                or e.get("form") not in {"10-K", "10-K/A"} or e.get("fp") != "FY"
                or not accn or accn > accession or (pe == latest and accn != accession)):
            continue
        entries.append(e)
    if not entries:
        return None
    selected = max(e["accn"] for e in entries)
    try:
        values = {Decimal(str(e["val"])) for e in entries if e["accn"] == selected}
    except (KeyError, InvalidOperation):
        return None
    if len(values) != 1:
        return None
    value = values.pop()
    if not value.is_finite() or value < 0:
        return None
    return LineItem(
        value=value, source=ExtractionSource.XBRL, confidence=1.0,
        xbrl_tag=NONCURRENT_TAG,
        source_quote=f"Noncurrent debt, excluding current maturities: {value} USD; annual period ending {pe}; SEC accession {selected}.",
    )


def needs_debt_repair(company: Company) -> bool:
    return company.cik.lstrip("0") == CIK and any(
        p.balance_sheet.long_term_debt is not None
        and p.balance_sheet.long_term_debt.source != ExtractionSource.USER_OVERRIDE
        and p.balance_sheet.long_term_debt.xbrl_tag == INCLUSIVE_TAG
        for p in company.periods
    )


def repair_debt(company: Company, facts: dict[str, Any]) -> Company:
    if not needs_debt_repair(company):
        return company
    periods = []
    for p in company.periods:
        old = p.balance_sheet.long_term_debt
        item = filing_debt(facts, company.cik, p.fiscal_period_end, p.filing_accession, company.periods[0].fiscal_period_end)
        if item is not None and old is not None and old.source != ExtractionSource.USER_OVERRIDE and old.xbrl_tag == INCLUSIVE_TAG:
            p = p.model_copy(update={"balance_sheet": p.balance_sheet.model_copy(update={"long_term_debt": item})})
        periods.append(p)
    return company.model_copy(update={"periods": periods})


def debt_flags(company: Company) -> list[ExtractionFlag]:
    if company.cik.lstrip("0") != CIK:
        return []
    item = company.periods[0].balance_sheet.long_term_debt
    if item is not None and item.source != ExtractionSource.USER_OVERRIDE and item.xbrl_tag == INCLUSIVE_TAG:
        return [ExtractionFlag(
            field_path="balance_sheet.long_term_debt",
            reason="Debt includes current maturities already covered by short-term debt; verify noncurrent debt before valuation.",
            current_value=item.value,
        )]
    return []
