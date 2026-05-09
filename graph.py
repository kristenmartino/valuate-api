"""LangGraph state machine for the Valuate extraction flow.

Phase 3.5 graph: ingest -> track_a -> track_b -> validate -> END

- ingest: fetch CIK, latest 10-K metadata, and XBRL company facts
- track_a: extract canonical concepts from XBRL into a flat field dict
  (no Company is constructed yet; missing fields are None)
- track_b: ask Claude to fill remaining gaps (required + optional), then
  compose the Company. Best-effort for the Claude call itself; the
  composition step raises if a required field is still None afterwards.
- validate: balance-sheet identity check + low-confidence flagging

Track A used to construct the Company directly and raise on any missing
required field — but real filers (CAT, NKE, JNJ, KO, GOOGL, ...) often
omit one of the canonical tags, and those gaps are exactly what Track B
is designed to fill. Composing the Company AFTER Track B lets Claude
backstop XBRL.
"""

import sys
from datetime import date
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional, TypedDict

from anthropic import AsyncAnthropic
from langgraph.graph import END, StateGraph

from edgar import EdgarClient, concepts_for, latest_value_per_period
from extract_track_a import extract_track_a
from extract_track_b import extract_revenue_segments, extract_track_b
from industry import Industry, classify_sic
from schemas import (
    BalanceSheet,
    BankBalanceSheet,
    BankCashFlowStatement,
    BankIncomeStatement,
    CashFlowStatement,
    Company,
    ExtractionFlag,
    ExtractionSource,
    FilingType,
    FinancialPeriod,
    IncomeStatement,
    InsuranceBalanceSheet,
    InsuranceCashFlowStatement,
    InsuranceIncomeStatement,
    LineItem,
    REITBalanceSheet,
    REITCashFlowStatement,
    REITIncomeStatement,
    RevenueSegment,
)
from section_extractor import extract_financial_statements_section


CONFIDENCE_FLAG_THRESHOLD = 0.80
BALANCE_SHEET_TOLERANCE = Decimal("0.005")  # 50bps of total assets

# How many fiscal years of history to extract per Company. The latest year is
# always included; we fill backwards from there. XBRL company-facts already
# carries every year the filer has tagged, so the only cost of N>1 is more
# in-process LineItem objects — no extra HTTP fetches.
N_HISTORICAL_PERIODS = 3


# Schema field names per statement, grouped by industry. Each tuple is the
# (income_fields, balance_fields, cashflow_fields, required_income,
# required_balance, required_cashflow) for that industry. Composition uses
# this dispatch table to know which fields to pull out of the items dict
# and which are required to instantiate the variant.

INCOME_STATEMENT_FIELDS = [
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "research_and_development",
    "selling_general_administrative",
    "depreciation_amortization",
    "operating_income",
    "interest_expense",
    "income_before_tax",
    "income_tax_expense",
    "net_income",
    "diluted_shares_outstanding",
]
BALANCE_SHEET_FIELDS = [
    "cash_and_equivalents",
    "short_term_investments",
    "accounts_receivable",
    "inventory",
    "total_current_assets",
    "property_plant_equipment_net",
    "total_assets",
    "accounts_payable",
    "short_term_debt",
    "total_current_liabilities",
    "long_term_debt",
    "total_liabilities",
    "shareholders_equity",
]
CASH_FLOW_FIELDS = [
    "depreciation_amortization",
    "cash_from_operations",
    "capital_expenditures",
    "cash_from_investing",
    "cash_from_financing",
    "dividends_paid",
]

REQUIRED_INCOME = {"revenue", "operating_income", "net_income", "diluted_shares_outstanding"}
REQUIRED_BALANCE = {"cash_and_equivalents", "total_assets", "total_liabilities", "shareholders_equity"}
REQUIRED_CASH_FLOW = {"depreciation_amortization", "cash_from_operations", "capital_expenditures"}

# --- Bank fields ------------------------------------------------------------

BANK_INCOME_FIELDS = [
    "interest_income",
    "interest_expense",
    "net_interest_income",
    "provision_for_credit_losses",
    "non_interest_income",
    "non_interest_expense",
    "income_before_tax",
    "income_tax_expense",
    "net_income",
    "diluted_shares_outstanding",
]
BANK_BALANCE_FIELDS = [
    "cash_and_equivalents",
    "securities",
    "total_loans",
    "allowance_for_loan_losses",
    "total_deposits",
    "long_term_debt",
    "total_assets",
    "total_liabilities",
    "shareholders_equity",
]
BANK_CASH_FLOW_FIELDS = [
    "cash_from_operations",
    "cash_from_investing",
    "cash_from_financing",
    "dividends_paid",
    "depreciation_amortization",
    "capital_expenditures",
]

BANK_REQUIRED_INCOME = {
    "net_interest_income",
    "income_before_tax",
    "income_tax_expense",
    "net_income",
    "diluted_shares_outstanding",
}
BANK_REQUIRED_BALANCE = {
    "cash_and_equivalents",
    "total_loans",
    "total_deposits",
    "total_assets",
    "total_liabilities",
    "shareholders_equity",
}
BANK_REQUIRED_CASH_FLOW = {"cash_from_operations"}

# --- Insurance fields -------------------------------------------------------

INSURANCE_INCOME_FIELDS = [
    "premiums_earned",
    "net_investment_income",
    "benefits_and_claims",
    "operating_expenses",
    "income_before_tax",
    "income_tax_expense",
    "net_income",
    "diluted_shares_outstanding",
]
INSURANCE_BALANCE_FIELDS = [
    "cash_and_equivalents",
    "investments",
    "insurance_reserves",
    "total_assets",
    "total_liabilities",
    "shareholders_equity",
]
INSURANCE_CASH_FLOW_FIELDS = [
    "cash_from_operations",
    "cash_from_investing",
    "cash_from_financing",
    "dividends_paid",
    "depreciation_amortization",
    "capital_expenditures",
]

INSURANCE_REQUIRED_INCOME = {
    "premiums_earned",
    "income_before_tax",
    "income_tax_expense",
    "net_income",
    "diluted_shares_outstanding",
}
INSURANCE_REQUIRED_BALANCE = {
    "cash_and_equivalents",
    "total_assets",
    "total_liabilities",
    "shareholders_equity",
}
INSURANCE_REQUIRED_CASH_FLOW = {"cash_from_operations"}

# --- REIT fields ------------------------------------------------------------

REIT_INCOME_FIELDS = [
    "revenue",
    "property_operating_expense",
    "depreciation_amortization",
    "general_and_administrative",
    "operating_income",
    "interest_expense",
    "income_before_tax",
    "income_tax_expense",
    "net_income",
    "diluted_shares_outstanding",
]
REIT_BALANCE_FIELDS = [
    "cash_and_equivalents",
    "real_estate_at_cost",
    "accumulated_depreciation",
    "real_estate_net",
    "total_assets",
    "long_term_debt",
    "total_liabilities",
    "shareholders_equity",
]
REIT_CASH_FLOW_FIELDS = [
    "cash_from_operations",
    "cash_from_investing",
    "cash_from_financing",
    "dividends_paid",
    "depreciation_amortization",
    "capital_expenditures",
]

# FFO = net_income + D&A (income-statement D&A is the load-bearing input);
# diluted shares anchors the per-share fair value; net_income closes out the
# normal IBT/tax → NI flow that XBRL almost always tags. Dividends_paid lives
# on the cash-flow statement and is informational rather than required —
# REITs by tax code distribute ≥90% of taxable income, which is enough
# context for a finance reviewer without hard-failing extraction on it.
REIT_REQUIRED_INCOME = {
    "revenue",
    "depreciation_amortization",
    "net_income",
    "diluted_shares_outstanding",
}
REIT_REQUIRED_BALANCE = {
    "cash_and_equivalents",
    "total_assets",
    "total_liabilities",
    "shareholders_equity",
}
REIT_REQUIRED_CASH_FLOW = {"cash_from_operations"}


def _industry_fields(industry: Industry) -> tuple[
    list[str], list[str], list[str], set[str], set[str], set[str]
]:
    """Return (income, balance, cashflow, req_income, req_balance, req_cashflow)
    field lists for the given industry."""
    if industry == Industry.BANK:
        return (
            BANK_INCOME_FIELDS,
            BANK_BALANCE_FIELDS,
            BANK_CASH_FLOW_FIELDS,
            BANK_REQUIRED_INCOME,
            BANK_REQUIRED_BALANCE,
            BANK_REQUIRED_CASH_FLOW,
        )
    if industry == Industry.INSURER:
        return (
            INSURANCE_INCOME_FIELDS,
            INSURANCE_BALANCE_FIELDS,
            INSURANCE_CASH_FLOW_FIELDS,
            INSURANCE_REQUIRED_INCOME,
            INSURANCE_REQUIRED_BALANCE,
            INSURANCE_REQUIRED_CASH_FLOW,
        )
    if industry == Industry.REIT:
        return (
            REIT_INCOME_FIELDS,
            REIT_BALANCE_FIELDS,
            REIT_CASH_FLOW_FIELDS,
            REIT_REQUIRED_INCOME,
            REIT_REQUIRED_BALANCE,
            REIT_REQUIRED_CASH_FLOW,
        )
    return (
        INCOME_STATEMENT_FIELDS,
        BALANCE_SHEET_FIELDS,
        CASH_FLOW_FIELDS,
        REQUIRED_INCOME,
        REQUIRED_BALANCE,
        REQUIRED_CASH_FLOW,
    )


def _all_fields_for(industry: Industry) -> list[str]:
    income, balance, cashflow, *_ = _industry_fields(industry)
    return sorted(set(income) | set(balance) | set(cashflow))


# Backward-compat: standard-industry union of all fields. The graph helpers
# below use _all_fields_for(industry) instead, but the module-level constant
# is referenced in some places that haven't been updated.
_ALL_FIELDS = _all_fields_for(Industry.STANDARD)


class CompositionError(Exception):
    """Raised when required fields are still missing after Track A + Track B."""


class GraphState(TypedDict, total=False):
    ticker: str
    cik: str
    company_name: str
    period_end: date  # latest filing's period_of_report; alias for period_ends[0]
    filing_accession: str
    filing_url: str
    facts: dict[str, Any]
    industry: Industry  # classified at ingest from SEC SIC code
    period_ends: list[date]  # most recent first; populated by track_a
    periods_items: dict[date, dict[str, Optional[LineItem]]]  # by track_a/b
    company: Company


def _make_ingest(client: EdgarClient) -> Callable[[GraphState], Awaitable[GraphState]]:
    async def ingest(state: GraphState) -> GraphState:
        ticker = state["ticker"]
        cik = await client.get_cik_from_ticker(ticker)
        submissions = await client.get_submissions(cik)
        name = submissions.get("name", ticker)
        # Classify industry from the SIC code on submissions. Routes the
        # whole rest of the graph (Track A concept map, required fields,
        # composition variant). Defaults to STANDARD on unknown SIC.
        industry = classify_sic(submissions.get("sic"))
        filing_meta = await client.get_latest_10k(cik)
        period_end = date.fromisoformat(filing_meta["period_of_report"])
        facts = await client.get_company_facts(cik)
        return {
            "cik": cik,
            "company_name": name,
            "period_end": period_end,
            "filing_accession": filing_meta["accession_number"],
            "filing_url": filing_meta["primary_doc_url"],
            "facts": facts,
            "industry": industry,
        }

    return ingest


def _recent_period_ends(
    facts: dict[str, Any],
    latest_end: date,
    n: int = N_HISTORICAL_PERIODS,
) -> list[date]:
    """Find up to N most-recent FY period-end dates present in XBRL.

    Walks a small set of high-coverage concepts (net income, total assets,
    revenue under both common tags) and unions their period-ends. The latest
    10-K's period_of_report is always considered — even if XBRL hasn't picked
    up that exact end yet, the caller should still include it as the anchor.
    Returns dates sorted newest-first, capped at `n`.
    """
    candidate_concepts = (
        "NetIncomeLoss",
        "Assets",
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
    )
    ends_iso: set[str] = set()
    for concept in candidate_concepts:
        ends_iso.update(latest_value_per_period(facts, concept).keys())
    ends_iso.add(latest_end.isoformat())
    parsed = sorted(
        (date.fromisoformat(e) for e in ends_iso if e <= latest_end.isoformat()),
        reverse=True,
    )
    return parsed[:n]


async def track_a(state: GraphState) -> GraphState:
    """Extract the N most-recent fiscal years from XBRL into a per-period dict.

    Uses the industry-specific concept map (banks have different XBRL tags
    than industrials). Older periods often have thinner coverage than the
    latest; composition tolerates this by silently dropping older periods
    that miss required fields, while still raising on the latest.
    """
    facts = state["facts"]
    latest = state["period_end"]
    industry = state.get("industry", Industry.STANDARD)
    concepts = concepts_for(industry)

    period_ends = _recent_period_ends(facts, latest)
    if not period_ends:
        period_ends = [latest]

    expected_fields = _all_fields_for(industry)
    periods_items: dict[date, dict[str, Optional[LineItem]]] = {}
    for pe in period_ends:
        items = extract_track_a(pe, facts, concepts=concepts)
        for field in expected_fields:
            items.setdefault(field, None)
        periods_items[pe] = items

    return {"period_ends": period_ends, "periods_items": periods_items}


def _missing_fields(
    items: dict[str, Optional[LineItem]],
    industry: Industry = Industry.STANDARD,
) -> list[str]:
    """Return all industry-relevant fields that are None in items."""
    return [f for f in _all_fields_for(industry) if items.get(f) is None]


def _derive_missing_required(
    items: dict[str, Optional[LineItem]],
    industry: Industry = Industry.STANDARD,
) -> dict[str, Optional[LineItem]]:
    """Last-ditch derivations for required fields neither Track A nor B filled.

    Standard industry:
    - operating_income ≈ income_before_tax + interest_expense (JNJ, NKE)

    Bank industry:
    - net_interest_income ≈ interest_income − interest_expense (filers that
      tag the components but not the net)

    Universal:
    - total_liabilities = total_assets − shareholders_equity (NKE, KO and
      banks that don't tag Liabilities directly)

    Each DERIVED entry preserves provenance with a synthetic source quote
    describing the formula, so the HITL surface can still flag it for review.
    """
    items = dict(items)

    if industry == Industry.STANDARD and items.get("operating_income") is None:
        ibt = items.get("income_before_tax")
        ie = items.get("interest_expense")
        if ibt is not None and ie is not None:
            items["operating_income"] = LineItem(
                value=ibt.value + ie.value,
                source=ExtractionSource.DERIVED,
                confidence=0.65,
                source_quote=(
                    f"Derived: income_before_tax + interest_expense "
                    f"({ibt.value} + {ie.value})"
                ),
                xbrl_tag=None,
            )

    if industry == Industry.BANK and items.get("net_interest_income") is None:
        ii = items.get("interest_income")
        ie = items.get("interest_expense")
        if ii is not None and ie is not None:
            items["net_interest_income"] = LineItem(
                value=ii.value - ie.value,
                source=ExtractionSource.DERIVED,
                confidence=0.85,
                source_quote=(
                    f"Derived: interest_income − interest_expense "
                    f"({ii.value} − {ie.value})"
                ),
                xbrl_tag=None,
            )

    if industry == Industry.REIT and items.get("real_estate_net") is None:
        rec = items.get("real_estate_at_cost")
        ad = items.get("accumulated_depreciation")
        if rec is not None and ad is not None:
            items["real_estate_net"] = LineItem(
                value=rec.value - ad.value,
                source=ExtractionSource.DERIVED,
                confidence=0.95,
                source_quote=(
                    f"Derived: real_estate_at_cost − accumulated_depreciation "
                    f"({rec.value} − {ad.value})"
                ),
                xbrl_tag=None,
            )

    if items.get("total_liabilities") is None:
        ta = items.get("total_assets")
        eq = items.get("shareholders_equity")
        if ta is not None and eq is not None:
            items["total_liabilities"] = LineItem(
                value=ta.value - eq.value,
                source=ExtractionSource.DERIVED,
                confidence=0.99,
                source_quote=(
                    f"Derived: total_assets − shareholders_equity "
                    f"({ta.value} − {eq.value})"
                ),
                xbrl_tag=None,
            )

    return items


def _missing_required_for_period(
    items: dict[str, Optional[LineItem]],
    industry: Industry = Industry.STANDARD,
) -> list[str]:
    """Field paths still None in `items` and required by the industry's schema."""
    _, _, _, req_income, req_balance, req_cashflow = _industry_fields(industry)
    missing: list[str] = []
    for f in req_income:
        if items.get(f) is None:
            missing.append(f"income_statement.{f}")
    for f in req_balance:
        if items.get(f) is None:
            missing.append(f"balance_sheet.{f}")
    for f in req_cashflow:
        if items.get(f) is None:
            missing.append(f"cash_flow_statement.{f}")
    return missing


def _build_financial_period(
    period_end: date,
    filing_accession: str,
    items: dict[str, Optional[LineItem]],
    industry: Industry = Industry.STANDARD,
    revenue_segments: Optional[list[RevenueSegment]] = None,
) -> FinancialPeriod:
    income_fields, balance_fields, cashflow_fields, *_ = _industry_fields(industry)
    income_kwargs: dict[str, Any] = {f: items.get(f) for f in income_fields}
    balance_kwargs: dict[str, Any] = {f: items.get(f) for f in balance_fields}
    cash_flow_kwargs: dict[str, Any] = {f: items.get(f) for f in cashflow_fields}

    if industry == Industry.BANK:
        income_stmt = BankIncomeStatement(**income_kwargs)
        balance_stmt = BankBalanceSheet(**balance_kwargs)
        cash_flow_stmt = BankCashFlowStatement(**cash_flow_kwargs)
    elif industry == Industry.INSURER:
        income_stmt = InsuranceIncomeStatement(**income_kwargs)
        balance_stmt = InsuranceBalanceSheet(**balance_kwargs)
        cash_flow_stmt = InsuranceCashFlowStatement(**cash_flow_kwargs)
    elif industry == Industry.REIT:
        income_stmt = REITIncomeStatement(**income_kwargs)
        balance_stmt = REITBalanceSheet(**balance_kwargs)
        cash_flow_stmt = REITCashFlowStatement(**cash_flow_kwargs)
    else:
        if revenue_segments is not None:
            income_kwargs["revenue_segments"] = revenue_segments
        income_stmt = IncomeStatement(**income_kwargs)
        balance_stmt = BalanceSheet(**balance_kwargs)
        cash_flow_stmt = CashFlowStatement(**cash_flow_kwargs)

    return FinancialPeriod(
        fiscal_year=period_end.year,
        fiscal_period_end=period_end,
        filing_accession=filing_accession,
        filing_type=FilingType.FORM_10K,
        industry=industry,
        income_statement=income_stmt,
        balance_sheet=balance_stmt,
        cash_flow_statement=cash_flow_stmt,
    )


def _compose_company(
    ticker: str,
    cik: str,
    company_name: str,
    period_ends: list[date],  # newest-first
    filing_accession: str,
    periods_items: dict[date, dict[str, Optional[LineItem]]],
    industry: Industry = Industry.STANDARD,
    revenue_segments: Optional[list[RevenueSegment]] = None,
) -> Company:
    """Build a Company from per-period field dicts, dispatching by industry.

    The latest period (period_ends[0]) is required: if any of its required
    fields are still None after Track A + Track B + derivation, raise
    CompositionError. Older periods with thinner coverage are silently
    dropped from `Company.periods` rather than failing the whole request.

    `revenue_segments` only applies to standard-industry filers (banks
    don't report segment revenue in the same way) and is attached to the
    latest period's IncomeStatement only.
    """
    if not period_ends:
        raise CompositionError(f"No fiscal periods to compose for {ticker}")

    latest = period_ends[0]
    latest_missing = _missing_required_for_period(
        periods_items.get(latest, {}), industry
    )
    if latest_missing:
        raise CompositionError(
            f"Required fields still missing after Track A + Track B for {ticker}: {latest_missing}"
        )

    fp_list: list[FinancialPeriod] = []
    for pe in period_ends:
        items = periods_items.get(pe, {})
        if pe != latest and _missing_required_for_period(items, industry):
            # Older period with thin coverage — drop rather than fail.
            continue
        segments_for_period = (
            revenue_segments
            if pe == latest and industry in (Industry.STANDARD, Industry.ENERGY)
            else None
        )
        fp_list.append(
            _build_financial_period(
                pe,
                filing_accession,
                items,
                industry=industry,
                revenue_segments=segments_for_period,
            )
        )

    return Company(
        ticker=ticker.upper(),
        cik=cik,
        name=company_name,
        fiscal_year_end_month=latest.month,
        periods=fp_list,
    )


def _make_track_b(
    edgar_client: EdgarClient,
    anthropic_client: AsyncAnthropic,
) -> Callable[[GraphState], Awaitable[GraphState]]:
    async def track_b(state: GraphState) -> GraphState:
        period_ends = state["period_ends"]
        periods_items = {pe: dict(items) for pe, items in state["periods_items"].items()}
        industry = state.get("industry", Industry.STANDARD)
        latest = period_ends[0]
        latest_items = periods_items[latest]

        # Two distinct things we want from the latest 10-K's text section:
        #  (a) backfill any required/optional line items XBRL didn't tag
        #  (b) extract revenue-by-segment (standard industry only — banks
        #      don't report revenue segments in a comparable way)
        # Both share the same HTML fetch + section slice, and the same cached
        # system prompt on the Anthropic side.
        missing = _missing_fields(latest_items, industry)
        revenue_segments: list[RevenueSegment] = []

        try:
            html = await edgar_client.get_filing_html(state["filing_url"])
            section_text = extract_financial_statements_section(html)

            if missing:
                claude_items = await extract_track_b(
                    client=anthropic_client,
                    ticker=state["ticker"],
                    company_name=state["company_name"],
                    period_end=latest,
                    accession_number=state["filing_accession"],
                    filing_section_text=section_text,
                    fields_to_extract=missing,
                )
                for field, line_item in claude_items.items():
                    if latest_items.get(field) is None and line_item is not None:
                        latest_items[field] = line_item

            if industry in (Industry.STANDARD, Industry.ENERGY):
                # Energy filers share the standard schema, and E&P companies
                # often report meaningful basin / geography segments worth
                # surfacing. Banks / insurers / REITs report segments
                # differently (geographies, line of business) and the
                # extraction prompt isn't tuned for those — skip there.
                revenue_segments = await extract_revenue_segments(
                    client=anthropic_client,
                    ticker=state["ticker"],
                    company_name=state["company_name"],
                    period_end=latest,
                    accession_number=state["filing_accession"],
                    filing_section_text=section_text,
                )
        except Exception as e:
            # Best-effort: log and let _compose_company decide if the
            # remaining gaps are tolerable (i.e. all-optional).
            print(f"Track B failed for {state['ticker']}: {e}", file=sys.stderr)

        # Derivation runs for every period — older years can also be missing
        # operating_income / net_interest_income / total_liabilities depending
        # on the filer.
        for pe in period_ends:
            periods_items[pe] = _derive_missing_required(periods_items[pe], industry)

        company = _compose_company(
            ticker=state["ticker"],
            cik=state["cik"],
            company_name=state["company_name"],
            period_ends=period_ends,
            filing_accession=state["filing_accession"],
            periods_items=periods_items,
            industry=industry,
            revenue_segments=revenue_segments,
        )
        return {"company": company}

    return track_b


def _walk_line_items(period: FinancialPeriod):
    for stmt_name, stmt in (
        ("income_statement", period.income_statement),
        ("balance_sheet", period.balance_sheet),
        ("cash_flow_statement", period.cash_flow_statement),
    ):
        for field_name in stmt.model_fields:
            value = getattr(stmt, field_name)
            if isinstance(value, LineItem):
                yield f"{stmt_name}.{field_name}", value


def _balance_sheet_check(bs: BalanceSheet) -> Optional[ExtractionFlag]:
    expected = bs.total_liabilities.value + bs.shareholders_equity.value
    diff = abs(bs.total_assets.value - expected)
    tolerance = abs(bs.total_assets.value) * BALANCE_SHEET_TOLERANCE
    if diff <= tolerance:
        return None
    return ExtractionFlag(
        field_path="balance_sheet",
        reason=(
            f"Balance sheet identity off by {diff}: "
            f"assets {bs.total_assets.value} vs liabilities+equity {expected}"
        ),
        current_value=bs.total_assets.value,
    )


def validate_company(company: Company) -> Company:
    """Compute ExtractionFlags for a Company and return a new copy with them attached."""
    period = company.periods[0]
    flags: list[ExtractionFlag] = []

    for field_path, item in _walk_line_items(period):
        if item.confidence < CONFIDENCE_FLAG_THRESHOLD:
            flags.append(
                ExtractionFlag(
                    field_path=field_path,
                    reason=f"Low confidence: {item.confidence:.2f}",
                    current_value=item.value,
                )
            )

    bs_flag = _balance_sheet_check(period.balance_sheet)
    if bs_flag is not None:
        flags.append(bs_flag)

    return company.model_copy(update={"extraction_flags": flags})


async def validate(state: GraphState) -> GraphState:
    return {"company": validate_company(state["company"])}


def build_graph(edgar_client: EdgarClient, anthropic_client: AsyncAnthropic):
    """Compile the extraction graph bound to shared SDK clients."""
    sg: StateGraph = StateGraph(GraphState)
    sg.add_node("ingest", _make_ingest(edgar_client))
    sg.add_node("track_a", track_a)
    sg.add_node("track_b", _make_track_b(edgar_client, anthropic_client))
    sg.add_node("validate", validate)
    sg.set_entry_point("ingest")
    sg.add_edge("ingest", "track_a")
    sg.add_edge("track_a", "track_b")
    sg.add_edge("track_b", "validate")
    sg.add_edge("validate", END)
    return sg.compile()
