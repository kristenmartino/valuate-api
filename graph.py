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

from edgar import EdgarClient, latest_value_per_period
from extract_track_a import extract_track_a
from extract_track_b import extract_track_b
from schemas import (
    BalanceSheet,
    CashFlowStatement,
    Company,
    ExtractionFlag,
    ExtractionSource,
    FilingType,
    FinancialPeriod,
    IncomeStatement,
    LineItem,
)
from section_extractor import extract_financial_statements_section


CONFIDENCE_FLAG_THRESHOLD = 0.80
BALANCE_SHEET_TOLERANCE = Decimal("0.005")  # 50bps of total assets

# How many fiscal years of history to extract per Company. The latest year is
# always included; we fill backwards from there. XBRL company-facts already
# carries every year the filer has tagged, so the only cost of N>1 is more
# in-process LineItem objects — no extra HTTP fetches.
N_HISTORICAL_PERIODS = 3


# Schema field names per statement. Used both to know what to ask Track B
# for and to map the merged dict back into Pydantic models when composing.
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
]

# Required schema fields per statement (must be present to compose Company).
REQUIRED_INCOME = {"revenue", "operating_income", "net_income", "diluted_shares_outstanding"}
REQUIRED_BALANCE = {"cash_and_equivalents", "total_assets", "total_liabilities", "shareholders_equity"}
REQUIRED_CASH_FLOW = {"depreciation_amortization", "cash_from_operations", "capital_expenditures"}

# Union of all distinct field names across the three statements (D&A is on
# both IS and CF; we store one entry and reuse it).
_ALL_FIELDS = sorted(
    set(INCOME_STATEMENT_FIELDS) | set(BALANCE_SHEET_FIELDS) | set(CASH_FLOW_FIELDS)
)


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
    period_ends: list[date]  # most recent first; populated by track_a
    periods_items: dict[date, dict[str, Optional[LineItem]]]  # by track_a/b
    company: Company


def _make_ingest(client: EdgarClient) -> Callable[[GraphState], Awaitable[GraphState]]:
    async def ingest(state: GraphState) -> GraphState:
        ticker = state["ticker"]
        cik = await client.get_cik_from_ticker(ticker)
        submissions = await client.get_submissions(cik)
        name = submissions.get("name", ticker)
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

    Older periods often have less coverage than the latest (a filer may not
    have tagged some concepts in earlier years). Composition tolerates this
    by skipping older periods that miss required fields, while raising on
    the latest period.
    """
    facts = state["facts"]
    latest = state["period_end"]
    period_ends = _recent_period_ends(facts, latest)
    if not period_ends:
        period_ends = [latest]

    periods_items: dict[date, dict[str, Optional[LineItem]]] = {}
    for pe in period_ends:
        items = extract_track_a(pe, facts)
        for field in _ALL_FIELDS:
            items.setdefault(field, None)
        periods_items[pe] = items

    return {"period_ends": period_ends, "periods_items": periods_items}


def _missing_fields(items: dict[str, Optional[LineItem]]) -> list[str]:
    """Return all fields in _ALL_FIELDS that are None in items."""
    return [f for f in _ALL_FIELDS if items.get(f) is None]


def _derive_missing_required(
    items: dict[str, Optional[LineItem]],
) -> dict[str, Optional[LineItem]]:
    """Last-ditch derivations for required fields neither Track A nor B filled.

    Some filers (JNJ, NKE) don't tag operating income at all and Claude is
    non-deterministic about whether to use a proxy. Some (NKE, KO) don't tag
    total liabilities. Both can be derived from other extracted fields with
    high enough confidence to keep the pipeline from failing — and the
    DERIVED source is preserved so the HITL surface can flag them for
    review.
    """
    items = dict(items)

    # operating_income ≈ income_before_tax + interest_expense
    # (op income = pre-tax earnings + financing costs, ignoring small
    #  non-operating income/expense items — close enough for a first-pass
    #  DCF; user can override).
    if items.get("operating_income") is None:
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

    # total_liabilities = total_assets - shareholders_equity (accounting identity)
    if items.get("total_liabilities") is None:
        ta = items.get("total_assets")
        eq = items.get("shareholders_equity")
        if ta is not None and eq is not None:
            items["total_liabilities"] = LineItem(
                value=ta.value - eq.value,
                source=ExtractionSource.DERIVED,
                confidence=0.99,  # accounting identity, near-certain
                source_quote=(
                    f"Derived: total_assets - shareholders_equity "
                    f"({ta.value} - {eq.value})"
                ),
                xbrl_tag=None,
            )

    return items


def _missing_required_for_period(
    items: dict[str, Optional[LineItem]],
) -> list[str]:
    """Field paths that are still None in `items` and required by the schema."""
    missing: list[str] = []
    for f in REQUIRED_INCOME:
        if items.get(f) is None:
            missing.append(f"income_statement.{f}")
    for f in REQUIRED_BALANCE:
        if items.get(f) is None:
            missing.append(f"balance_sheet.{f}")
    for f in REQUIRED_CASH_FLOW:
        if items.get(f) is None:
            missing.append(f"cash_flow_statement.{f}")
    return missing


def _build_financial_period(
    period_end: date,
    filing_accession: str,
    items: dict[str, Optional[LineItem]],
) -> FinancialPeriod:
    income_kwargs = {f: items.get(f) for f in INCOME_STATEMENT_FIELDS}
    balance_kwargs = {f: items.get(f) for f in BALANCE_SHEET_FIELDS}
    cash_flow_kwargs = {f: items.get(f) for f in CASH_FLOW_FIELDS}
    return FinancialPeriod(
        fiscal_year=period_end.year,
        fiscal_period_end=period_end,
        filing_accession=filing_accession,
        filing_type=FilingType.FORM_10K,
        income_statement=IncomeStatement(**income_kwargs),
        balance_sheet=BalanceSheet(**balance_kwargs),
        cash_flow_statement=CashFlowStatement(**cash_flow_kwargs),
    )


def _compose_company(
    ticker: str,
    cik: str,
    company_name: str,
    period_ends: list[date],  # newest-first
    filing_accession: str,
    periods_items: dict[date, dict[str, Optional[LineItem]]],
) -> Company:
    """Build a Company from per-period field dicts.

    The latest period (period_ends[0]) is required: if any of its required
    fields are still None after Track A + Track B + derivation, raise
    CompositionError. Older periods often have thinner XBRL coverage; if
    any of their required fields are missing they're silently dropped from
    `Company.periods`. The latest period is the demo's anchor; older
    periods are nice-to-have historical context.
    """
    if not period_ends:
        raise CompositionError(f"No fiscal periods to compose for {ticker}")

    latest = period_ends[0]
    latest_missing = _missing_required_for_period(periods_items.get(latest, {}))
    if latest_missing:
        raise CompositionError(
            f"Required fields still missing after Track A + Track B for {ticker}: {latest_missing}"
        )

    fp_list: list[FinancialPeriod] = []
    for pe in period_ends:
        items = periods_items.get(pe, {})
        if pe != latest and _missing_required_for_period(items):
            # Older period with thin coverage — drop rather than fail.
            continue
        fp_list.append(_build_financial_period(pe, filing_accession, items))

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
        latest = period_ends[0]

        # Track B (Claude) runs only for the latest period — it's the demo
        # anchor and the most expensive call. Older periods stand on
        # whatever Track A + derivation produced.
        latest_items = periods_items[latest]
        missing = _missing_fields(latest_items)
        if missing:
            try:
                html = await edgar_client.get_filing_html(state["filing_url"])
                section_text = extract_financial_statements_section(html)
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
            except Exception as e:
                # Best-effort: log and let _compose_company decide if the
                # remaining gaps are tolerable (i.e. all-optional).
                print(f"Track B failed for {state['ticker']}: {e}", file=sys.stderr)

        # Derivation runs for every period — older years can also be missing
        # operating_income or total_liabilities depending on the filer.
        for pe in period_ends:
            periods_items[pe] = _derive_missing_required(periods_items[pe])

        company = _compose_company(
            ticker=state["ticker"],
            cik=state["cik"],
            company_name=state["company_name"],
            period_ends=period_ends,
            filing_accession=state["filing_accession"],
            periods_items=periods_items,
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
