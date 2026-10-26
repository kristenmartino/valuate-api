"""Intuit's complete cash-flow D&A and non-overlapping annual debt facts."""
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from schemas import Company, ExtractionFlag, ExtractionSource, LineItem

CIK = "896878"
PROTECTED_FIELDS = {"depreciation_amortization", "short_term_debt", "long_term_debt"}
CONCEPTS = {
    "depreciation": "Depreciation",
    "amortization": "AdjustmentForAmortization",
    "short_term_debt": "LongTermDebtCurrent",
    "long_term_debt": "LongTermDebtNoncurrent",
}
VALUATION_NOTE = (
    "INTU's D&A mapping combines filed depreciation and the full cash-flow amortization adjustment. "
    "Debt uses noncurrent amounts separately from current maturities. "
    "Capex remains PP&E-only, excluding separately reported capitalized internal-use software; "
    "review this simplification before relying on the estimate."
)


def filing_items(facts: dict[str, Any], cik: str, pe: date, accession: str, latest: date) -> dict[str, LineItem]:
    """Require matching annual USD operands from one filing, bounded by the anchor."""
    if (cik.lstrip("0") != CIK or str(facts.get("cik", "")).lstrip("0") != CIK
            or (pe.month, pe.day) != (7, 31) or (latest.month, latest.day) != (7, 31)
            or pe > latest or not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)):
        return {}
    gaap = facts.get("facts", {}).get("us-gaap", {})
    anchor_dates = set()
    for row in gaap.get("Depreciation", {}).get("units", {}).get("USD", []):
        if (row.get("accn") == accession and row.get("end") == latest.isoformat()
                and row.get("start") == f"{latest.year - 1}-08-01"
                and row.get("form") in {"10-K", "10-K/A"} and row.get("fp") == "FY"):
            try:
                anchor_dates.add(date.fromisoformat(row["filed"]))
            except (KeyError, TypeError, ValueError):
                return {}
    if len(anchor_dates) != 1:
        return {}
    anchor_filed = anchor_dates.pop()
    if anchor_filed < latest:
        return {}
    by_field = {}
    for field, concept in CONCEPTS.items():
        by_accession = {}
        instant = field in {"short_term_debt", "long_term_debt"}
        for row in gaap.get(concept, {}).get("units", {}).get("USD", []):
            accn = row.get("accn", "")
            try:
                filed = date.fromisoformat(row["filed"])
            except (KeyError, TypeError, ValueError):
                continue
            if (row.get("end") != pe.isoformat()
                    or (bool(row.get("start")) if instant else row.get("start") != f"{pe.year - 1}-08-01")
                    or row.get("form") not in {"10-K", "10-K/A"} or row.get("fp") != "FY"
                    or not isinstance(accn, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accn)
                    or filed > anchor_filed or filed < pe or (pe == latest and accn != accession)):
                continue
            by_accession.setdefault(accn, []).append(row)
        by_field[field] = by_accession
    common = set.intersection(*(set(rows) for rows in by_field.values()))
    if not common:
        return {}
    selected = accession if accession in common else max(
        common, key=lambda accn: (max(r["filed"] for r in by_field["depreciation"][accn]), accn),
    )
    if len({r["filed"] for rows in by_field.values() for r in rows[selected]}) != 1:
        return {}
    values = {}
    for field, rows in by_field.items():
        try:
            candidates = {Decimal(str(r["val"])) for r in rows[selected]}
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return {}
        if len(candidates) != 1:
            return {}
        value = candidates.pop()
        if not value.is_finite() or value < 0:
            return {}
        values[field] = value
    output = {}
    for field in ("short_term_debt", "long_term_debt"):
        output[field] = LineItem(
            value=values[field], source=ExtractionSource.XBRL, confidence=1.0,
            xbrl_tag=f"us-gaap:{CONCEPTS[field]}",
            source_quote=(f"Filed {'current maturities' if field == 'short_term_debt' else 'noncurrent debt, excluding current maturities'}: "
                          f"{values[field]} USD; year-end {pe}; SEC accession {selected}."),
        )
    dep, amort = values["depreciation"], values["amortization"]
    output["depreciation_amortization"] = LineItem(
        value=dep + amort, source=ExtractionSource.DERIVED, confidence=0.99,
        source_quote=(f"INTU cash-flow D&A: us-gaap:Depreciation + us-gaap:AdjustmentForAmortization "
                      f"({dep} + {amort} = {dep + amort} USD); annual period {pe.year - 1}-08-01 to {pe}; "
                      f"SEC accession {selected}. The amortization adjustment includes acquired technology and other acquired intangibles; "
                      "AmortizationOfIntangibleAssets alone is only the other-intangibles expense component."),
    )
    return output


def _incomplete_da(item: LineItem | None) -> bool:
    return item is None or (item.source != ExtractionSource.USER_OVERRIDE
                            and item.xbrl_tag == "us-gaap:Depreciation")


def _inclusive_debt(item: LineItem | None) -> bool:
    return item is None or (item.source != ExtractionSource.USER_OVERRIDE
                            and item.xbrl_tag == "us-gaap:LongTermDebt")


def incomplete_inputs(company: Company) -> bool:
    return company.cik.lstrip("0") == CIK and any(
        _incomplete_da(p.income_statement.depreciation_amortization)
        or _incomplete_da(p.cash_flow_statement.depreciation_amortization)
        or _inclusive_debt(p.balance_sheet.long_term_debt)
        or p.balance_sheet.short_term_debt is None
        for p in company.periods
    )


def needs_repair(company: Company) -> bool:
    return company.cik.lstrip("0") == CIK and (incomplete_inputs(company) or company.valuation_note != VALUATION_NOTE)


def repair_company(company: Company, facts: dict[str, Any]) -> Company:
    if not needs_repair(company):
        return company
    latest = company.periods[0]
    selections = {}
    for p in company.periods:
        required = (_incomplete_da(p.income_statement.depreciation_amortization)
                    or _incomplete_da(p.cash_flow_statement.depreciation_amortization)
                    or _inclusive_debt(p.balance_sheet.long_term_debt) or p.balance_sheet.short_term_debt is None)
        if required:
            items = filing_items(facts, company.cik, p.fiscal_period_end, latest.filing_accession, latest.fiscal_period_end)
            if not items:
                return company  # Never mix repaired totals with partial saved history.
            selections[p.fiscal_period_end] = items
    periods = []
    for p in company.periods:
        items = selections.get(p.fiscal_period_end, {})
        updates = {}
        for name in ("income_statement", "cash_flow_statement"):
            stmt = getattr(p, name)
            if _incomplete_da(stmt.depreciation_amortization):
                updates[name] = stmt.model_copy(update={"depreciation_amortization": items["depreciation_amortization"]})
        bs_updates = {}
        if _inclusive_debt(p.balance_sheet.long_term_debt):
            bs_updates["long_term_debt"] = items["long_term_debt"]
        if p.balance_sheet.short_term_debt is None:
            bs_updates["short_term_debt"] = items["short_term_debt"]
        if bs_updates:
            updates["balance_sheet"] = p.balance_sheet.model_copy(update=bs_updates)
        periods.append(p.model_copy(update=updates))
    return company.model_copy(update={"periods": periods, "valuation_note": VALUATION_NOTE})


def coverage_flags(company: Company) -> list[ExtractionFlag]:
    if not incomplete_inputs(company):
        return []
    return [ExtractionFlag(
        field_path="cash_flow_statement.depreciation_amortization",
        current_value=company.periods[0].cash_flow_statement.depreciation_amortization.value,
        reason="INTU annual D&A/current/noncurrent debt coverage is incomplete; valuation is unavailable until matching filing inputs are recovered.",
    )]
