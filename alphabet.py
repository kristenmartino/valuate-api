"""Alphabet's carrying-value debt split and explicitly scoped PPE proxy."""
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from schemas import Company, ExtractionFlag, ExtractionSource, LineItem

CIK = "1652044"
PROTECTED_FIELDS = {"depreciation_amortization", "short_term_debt", "long_term_debt"}
CONCEPTS = {
    "depreciation": "Depreciation",
    "current_maturities": "LongTermDebtCurrent",
    "long_term_debt": "LongTermDebtNoncurrent",
    "face_debt": "LongTermDebt",
    "commercial_paper": "CommercialPaper",
    "discount": "DebtInstrumentUnamortizedDiscountPremiumAndDebtIssuanceCostsNet",
}
LEGACY_CONCEPTS = {
    "depreciation": "Depreciation",
    "long_term_debt": "LongTermDebtAndCapitalLeaseObligations",
    "gross_debt": "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
    "commercial_paper": "CommercialPaper",
    "discount": "DebtInstrumentUnamortizedDiscountPremiumAndDebtIssuanceCostsNet",
}
VALUATION_NOTE = (
    "GOOGL uses filed property-and-equipment depreciation as a cash-flow add-back proxy, not verified full D&A. "
    "The model projects its historical revenue ratio without adding unverified amortization. "
    "DCF-implied EV/EBITDA is unavailable on this basis. Debt uses carrying amounts: noncurrent debt plus "
    "current maturities and commercial paper, counted once. FY2022 debt includes finance leases; "
    "later debt-note figures exclude them. The net-debt bridge separately adds reported operating-lease liabilities."
)


def filing_items(facts: dict[str, Any], cik: str, pe: date, accession: str, latest: date) -> dict[str, LineItem]:
    """Use one bounded annual filing; reconcile notes before accepting the pair."""
    if (cik.lstrip("0") != CIK or str(facts.get("cik", "")).lstrip("0") != CIK
            or (pe.month, pe.day) != (12, 31) or (latest.month, latest.day) != (12, 31)
            or pe > latest or pe.year < 2022
            or not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)):
        return {}
    gaap = facts.get("facts", {}).get("us-gaap", {})
    anchors = set()
    for row in gaap.get("Depreciation", {}).get("units", {}).get("USD", []):
        if (row.get("accn") == accession and row.get("end") == latest.isoformat()
                and row.get("start") == f"{latest.year}-01-01"
                and row.get("form") in {"10-K", "10-K/A"} and row.get("fp") == "FY"):
            try:
                anchors.add(date.fromisoformat(row["filed"]))
            except (KeyError, TypeError, ValueError):
                return {}
    if len(anchors) != 1:
        return {}
    anchor = anchors.pop()
    if anchor < latest:
        return {}
    legacy = pe.year == 2022
    concepts = LEGACY_CONCEPTS if legacy else CONCEPTS
    by_field = {}
    for field, concept in concepts.items():
        by_accession = {}
        for row in gaap.get(concept, {}).get("units", {}).get("USD", []):
            accn = row.get("accn", "")
            try:
                filed = date.fromisoformat(row["filed"])
            except (KeyError, TypeError, ValueError):
                continue
            if (row.get("end") != pe.isoformat()
                    or (row.get("start") != f"{pe.year}-01-01" if field == "depreciation" else bool(row.get("start")))
                    or row.get("form") not in {"10-K", "10-K/A"} or row.get("fp") != "FY"
                    or not isinstance(accn, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accn)
                    or filed > anchor or filed < pe or (pe == latest and accn != accession)):
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
        if not value.is_finite() or (field != "discount" and value < 0):
            return {}
        values[field] = value
    if legacy:
        # FY2022's table reports a positive discount magnitude and includes
        # finance leases. Its future principal table is a different basis.
        if values["discount"] < 0:
            return {}
        maturities = values["gross_debt"] - values["discount"] - values["long_term_debt"]
        if maturities < 0:
            return {}
        debt_quote = (f"FY2022 debt including finance leases: gross table total {values['gross_debt']} "
                      f"- discount/issuance-cost magnitude {values['discount']} - noncurrent {values['long_term_debt']} "
                      f"= current component {maturities} USD. This legacy scope differs from later notes-only debt.")
    else:
        maturities = values["current_maturities"]
        if values["face_debt"] + values["discount"] != maturities + values["long_term_debt"]:
            return {}
        debt_quote = (f"Notes carrying amount: face value {values['face_debt']} + signed discount/issuance costs "
                      f"({values['discount']}) = {maturities + values['long_term_debt']} USD, split into "
                      f"current maturities {maturities} and noncurrent debt {values['long_term_debt']}. "
                      "Finance leases are outside this debt-note scope.")
    context = f" Year-end {pe}; SEC accession {selected}."
    return {
        "long_term_debt": LineItem(
            value=values["long_term_debt"], source=ExtractionSource.XBRL, confidence=1.0,
            xbrl_tag=f"us-gaap:{concepts['long_term_debt']}", source_quote=debt_quote + context,
        ),
        "short_term_debt": LineItem(
            value=maturities + values["commercial_paper"], source=ExtractionSource.DERIVED, confidence=0.99,
            source_quote=(debt_quote + f" Current debt = current component {maturities} + filed commercial paper "
                          f"{values['commercial_paper']} = {maturities + values['commercial_paper']} USD." + context),
        ),
        "depreciation_amortization": LineItem(
            value=values["depreciation"], source=ExtractionSource.XBRL, confidence=0.95,
            xbrl_tag="us-gaap:Depreciation",
            source_quote=(f"Depreciation of property and equipment: {values['depreciation']} USD; annual period "
                          f"{pe.year}-01-01 to {pe}; SEC accession {selected}. Used as a PPE-depreciation proxy "
                          "in the model add-back; full amortization coverage is unverified. No unverified amortization is added."),
        ),
    }


def _debt_incomplete(period) -> bool:
    return (period.balance_sheet.short_term_debt is None or period.balance_sheet.long_term_debt is None
            or period.balance_sheet.long_term_debt.xbrl_tag not in {
                "us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligations"})


def incomplete_inputs(company: Company) -> bool:
    return company.cik.lstrip("0") == CIK and any(_debt_incomplete(p) for p in company.periods)


def needs_repair(company: Company) -> bool:
    return company.cik.lstrip("0") == CIK and (incomplete_inputs(company) or company.valuation_note != VALUATION_NOTE)


def repair_company(company: Company, facts: dict[str, Any]) -> Company:
    if not needs_repair(company):
        return company
    latest = company.periods[0]
    selected = {p.fiscal_period_end: filing_items(facts, company.cik, p.fiscal_period_end,
                                               latest.filing_accession, latest.fiscal_period_end) for p in company.periods}
    if not all(selected.values()):
        return company
    periods = []
    for p in company.periods:
        items = selected[p.fiscal_period_end]
        updates = {}
        for name in ("income_statement", "cash_flow_statement"):
            stmt = getattr(p, name)
            item = stmt.depreciation_amortization
            # Disclose the existing proxy without rewriting a reviewed amount.
            if item is not None and item.source != ExtractionSource.USER_OVERRIDE and item.xbrl_tag == "us-gaap:Depreciation":
                if item.value != items["depreciation_amortization"].value:
                    return company
                updates[name] = stmt.model_copy(update={"depreciation_amortization": items["depreciation_amortization"]})
        reviewed = any(item is not None and item.source == ExtractionSource.USER_OVERRIDE
                       for item in (p.balance_sheet.long_term_debt, p.balance_sheet.short_term_debt))
        if not reviewed:
            updates["balance_sheet"] = p.balance_sheet.model_copy(update={field: items[field] for field in ("long_term_debt", "short_term_debt")})
        periods.append(p.model_copy(update=updates))
    return company.model_copy(update={"periods": periods, "valuation_note": VALUATION_NOTE})


def coverage_flags(company: Company) -> list[ExtractionFlag]:
    if not incomplete_inputs(company):
        return []
    return [ExtractionFlag(
        field_path="cash_flow_statement.depreciation_amortization",
        current_value=company.periods[0].cash_flow_statement.depreciation_amortization.value,
        reason="GOOGL annual carrying-value debt coverage is incomplete; valuation is unavailable until matching filing inputs are recovered.",
    )]
