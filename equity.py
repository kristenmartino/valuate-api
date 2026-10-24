"""Keep parent equity and noncontrolling interests separate for reconciliation."""

from decimal import Decimal
from datetime import date
from typing import Any

from schemas import AnyBalanceSheet, Company, ExtractionSource, LineItem

PARENT_EQUITY_TAG = "us-gaap:StockholdersEquity"
TOTAL_EQUITY_TAG = "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
NCI_CONCEPT = "MinorityInterest"


def reconciliation_equity(bs: AnyBalanceSheet) -> Decimal:
    """Add NCI to parent equity; a consolidated equity tag already includes it.

    Corrections retain the original equity concept as their field basis.
    Unknown/AI equity coverage is not assumed to exclude NCI.
    """
    equity = bs.shareholders_equity
    nci = bs.noncontrolling_interests
    if nci is not None and equity.xbrl_tag == PARENT_EQUITY_TAG:
        return equity.value + nci.value
    return equity.value


def needs_equity_backfill(company: Company) -> bool:
    """Only fetch missing NCI for a material parent-equity reconciliation gap."""
    bs = company.periods[0].balance_sheet
    return (
        bs.shareholders_equity.xbrl_tag == PARENT_EQUITY_TAG
        and bs.noncontrolling_interests is None
        and abs(bs.total_assets.value - bs.total_liabilities.value - bs.shareholders_equity.value)
        > abs(bs.total_assets.value) * Decimal("0.005")
    )


def filing_noncontrolling_interests(
    facts: dict[str, Any], cik: str, period_end: date, accession: str,
) -> LineItem | None:
    if str(facts.get("cik", "")).lstrip("0") != cik.lstrip("0"):
        return None
    entries = facts.get("facts", {}).get("us-gaap", {}).get(NCI_CONCEPT, {}).get("units", {}).get("USD", [])
    values = {
        Decimal(str(entry["val"]))
        for entry in entries
        if entry.get("end") == period_end.isoformat()
        and entry.get("accn") == accession
        and entry.get("form") in {"10-K", "10-K/A"}
        and entry.get("fp") == "FY"
        and not entry.get("start")
    }
    if len(values) != 1:
        return None
    return LineItem(
        value=values.pop(), source=ExtractionSource.XBRL, confidence=1.0,
        xbrl_tag=f"us-gaap:{NCI_CONCEPT}",
        source_quote=(f"SEC XBRL noncontrolling interests, instant {period_end}, "
                      f"accession {accession}; separate from parent stockholders' equity."),
    )


def backfill_noncontrolling_interests(company: Company, facts: dict[str, Any]) -> Company:
    """Add only missing NCI from each saved period's exact filing and date.

    No refresh of existing financial values, no AI fallback, no newer-filing
    substitution. Conflicting repeats, wrong units, durations or entities
    leave the missing component and its warning untouched.
    """
    periods = []
    for period in company.periods:
        bs = period.balance_sheet
        if bs.noncontrolling_interests is not None or bs.shareholders_equity.xbrl_tag != PARENT_EQUITY_TAG:
            periods.append(period)
            continue
        item = filing_noncontrolling_interests(facts, company.cik, period.fiscal_period_end, period.filing_accession)
        if item is None:
            periods.append(period)
            continue
        periods.append(period.model_copy(update={"balance_sheet": bs.model_copy(update={"noncontrolling_interests": item})}))
    return company.model_copy(update={"periods": periods})
