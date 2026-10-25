"""Filing-backed coverage repair for Salesforce's debt, D&A and payables.

The generic tag order selected fixed-asset D&A and debt including current
maturities. Prefer the reported cash-flow total and noncurrent debt. Older
comparatives absent from the anchor filing use an earlier annual filing,
identified in the source note; reviewer corrections are never replaced.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from schemas import Company, ExtractionFlag, ExtractionSource, LineItem

CIK = "1108524"
CONCEPTS = {
    "long_term_debt": ("LongTermDebtNoncurrent", "Noncurrent debt; excludes current maturities"),
    "depreciation_amortization": ("DepreciationAndAmortization", "Cash-flow depreciation and amortization; not fixed assets alone"),
    "accounts_payable": ("AccountsPayableAndOtherAccruedLiabilitiesCurrent", "Accounts payable, accrued expenses and other liabilities"),
}


def filing_items(facts: dict[str, Any], cik: str, pe: date, accession: str, latest: date) -> dict[str, LineItem]:
    if cik.lstrip("0") != CIK or str(facts.get("cik", "")).lstrip("0") != CIK or (pe.month, pe.day) != (1, 31):
        return {}
    output = {}
    for field, (concept, label) in CONCEPTS.items():
        entries = []
        for e in facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {}).get("USD", []):
            if e.get("end") != pe.isoformat() or e.get("form") not in {"10-K", "10-K/A"} or e.get("fp") != "FY":
                continue
            accn = e.get("accn", "")
            # Latest values must come from the saved anchor filing. Older
            # periods can use its comparatives, then earlier annual filings.
            if not accn or accn > accession or (pe == latest and accn != accession):
                continue
            if field == "depreciation_amortization":
                if e.get("start") != f"{pe.year - 1}-02-01":
                    continue
            elif e.get("start"):
                continue
            entries.append(e)
        if not entries:
            continue
        selected = max(e["accn"] for e in entries)
        try:
            values = {Decimal(str(e["val"])) for e in entries if e["accn"] == selected}
        except (KeyError, InvalidOperation):
            continue
        if len(values) != 1:
            continue
        value = values.pop()
        if not value.is_finite() or value < 0:
            continue
        output[field] = LineItem(
            value=value, source=ExtractionSource.XBRL, confidence=1.0,
            xbrl_tag=f"us-gaap:{concept}",
            source_quote=f"{label}: {value} USD; annual period ending {pe}; SEC accession {selected}.",
        )
    return output


def needs_mapping_repair(company: Company) -> bool:
    if company.cik.lstrip("0") != CIK:
        return False
    for p in company.periods:
        for stmt, field, old_tag in [
            (p.balance_sheet, "long_term_debt", "us-gaap:LongTermDebt"),
            (p.cash_flow_statement, "depreciation_amortization", "us-gaap:DepreciationDepletionAndAmortization"),
            (p.income_statement, "depreciation_amortization", "us-gaap:DepreciationDepletionAndAmortization"),
        ]:
            item = getattr(stmt, field, None)
            if item and item.source != ExtractionSource.USER_OVERRIDE and item.xbrl_tag == old_tag:
                return True
        ap = p.balance_sheet.accounts_payable
        if ap and ap.source == ExtractionSource.LLM_HTML and "accrued" in (ap.source_quote or "").lower():
            return True
    return False


def repair_company(company: Company, facts: dict[str, Any]) -> Company:
    if company.cik.lstrip("0") != CIK:
        return company
    periods = []
    for p in company.periods:
        items = filing_items(facts, company.cik, p.fiscal_period_end, p.filing_accession, company.periods[0].fiscal_period_end)
        statements = {}
        for name, fields in {
            "balance_sheet": ("long_term_debt", "accounts_payable"),
            "income_statement": ("depreciation_amortization",),
            "cash_flow_statement": ("depreciation_amortization",),
        }.items():
            stmt = getattr(p, name)
            changes = {f: items[f] for f in fields if f in items
                       and (getattr(stmt, f) is None or getattr(stmt, f).source != ExtractionSource.USER_OVERRIDE)}
            statements[name] = stmt.model_copy(update=changes)
        periods.append(p.model_copy(update=statements))
    return company.model_copy(update={"periods": periods})


def mapping_flags(company: Company) -> list[ExtractionFlag]:
    """Known stale mappings stay visible if SEC repair is unavailable."""
    if company.cik.lstrip("0") != CIK:
        return []
    p = company.periods[0]
    flags = []
    for path, item, tag, reason in [
        ("balance_sheet.long_term_debt", p.balance_sheet.long_term_debt, "us-gaap:LongTermDebt", "Debt includes current maturities; verify noncurrent debt before valuation."),
        ("cash_flow_statement.depreciation_amortization", p.cash_flow_statement.depreciation_amortization, "us-gaap:DepreciationDepletionAndAmortization", "Fixed-asset D&A component; verify the cash-flow D&A total before valuation."),
    ]:
        if item and item.source != ExtractionSource.USER_OVERRIDE and item.xbrl_tag == tag:
            flags.append(ExtractionFlag(field_path=path, reason=reason, current_value=item.value))
    return flags
