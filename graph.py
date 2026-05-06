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

from edgar import EdgarClient
from extract_track_a import extract_track_a
from extract_track_b import extract_track_b
from schemas import (
    BalanceSheet,
    CashFlowStatement,
    Company,
    ExtractionFlag,
    FilingType,
    FinancialPeriod,
    IncomeStatement,
    LineItem,
)
from section_extractor import extract_financial_statements_section


CONFIDENCE_FLAG_THRESHOLD = 0.80
BALANCE_SHEET_TOLERANCE = Decimal("0.005")  # 50bps of total assets


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
    period_end: date
    filing_accession: str
    filing_url: str
    facts: dict[str, Any]
    items: dict[str, Optional[LineItem]]
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


async def track_a(state: GraphState) -> GraphState:
    items = extract_track_a(state["period_end"], state["facts"])
    # Pad with the schema-only fields Track A doesn't even attempt.
    for field in _ALL_FIELDS:
        items.setdefault(field, None)
    return {"items": items}


def _missing_fields(items: dict[str, Optional[LineItem]]) -> list[str]:
    """Return all fields in _ALL_FIELDS that are None in items."""
    return [f for f in _ALL_FIELDS if items.get(f) is None]


def _compose_company(
    ticker: str,
    cik: str,
    company_name: str,
    period_end: date,
    filing_accession: str,
    items: dict[str, Optional[LineItem]],
) -> Company:
    """Build a Company from the merged items dict.

    Raises CompositionError listing every required field that is still None.
    """
    income_kwargs = {f: items.get(f) for f in INCOME_STATEMENT_FIELDS}
    balance_kwargs = {f: items.get(f) for f in BALANCE_SHEET_FIELDS}
    cash_flow_kwargs = {f: items.get(f) for f in CASH_FLOW_FIELDS}

    missing: list[str] = []
    for f in REQUIRED_INCOME:
        if income_kwargs.get(f) is None:
            missing.append(f"income_statement.{f}")
    for f in REQUIRED_BALANCE:
        if balance_kwargs.get(f) is None:
            missing.append(f"balance_sheet.{f}")
    for f in REQUIRED_CASH_FLOW:
        if cash_flow_kwargs.get(f) is None:
            missing.append(f"cash_flow_statement.{f}")
    if missing:
        raise CompositionError(
            f"Required fields still missing after Track A + Track B for {ticker}: {missing}"
        )

    period = FinancialPeriod(
        fiscal_year=period_end.year,
        fiscal_period_end=period_end,
        filing_accession=filing_accession,
        filing_type=FilingType.FORM_10K,
        income_statement=IncomeStatement(**income_kwargs),
        balance_sheet=BalanceSheet(**balance_kwargs),
        cash_flow_statement=CashFlowStatement(**cash_flow_kwargs),
    )
    return Company(
        ticker=ticker.upper(),
        cik=cik,
        name=company_name,
        fiscal_year_end_month=period_end.month,
        periods=[period],
    )


def _make_track_b(
    edgar_client: EdgarClient,
    anthropic_client: AsyncAnthropic,
) -> Callable[[GraphState], Awaitable[GraphState]]:
    async def track_b(state: GraphState) -> GraphState:
        items = dict(state["items"])  # shallow copy; we'll mutate locally
        missing = _missing_fields(items)
        if missing:
            try:
                html = await edgar_client.get_filing_html(state["filing_url"])
                section_text = extract_financial_statements_section(html)
                claude_items = await extract_track_b(
                    client=anthropic_client,
                    ticker=state["ticker"],
                    company_name=state["company_name"],
                    period_end=state["period_end"],
                    accession_number=state["filing_accession"],
                    filing_section_text=section_text,
                    fields_to_extract=missing,
                )
                for field, line_item in claude_items.items():
                    if items.get(field) is None and line_item is not None:
                        items[field] = line_item
            except Exception as e:
                # Best-effort: log and let _compose_company decide if the
                # remaining gaps are tolerable (i.e. all-optional).
                print(f"Track B failed for {state['ticker']}: {e}", file=sys.stderr)

        company = _compose_company(
            ticker=state["ticker"],
            cik=state["cik"],
            company_name=state["company_name"],
            period_end=state["period_end"],
            filing_accession=state["filing_accession"],
            items=items,
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
