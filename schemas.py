"""Pydantic schemas for financial data extracted from 10-K filings.

Design notes:
- Every line item carries provenance (source, confidence, source_quote/xbrl_tag)
  so the HITL review UI can show users *why* we have this number.
- Values are stored as Decimal in actual USD (not millions). Filing-reported
  scaling is normalized at extraction time.
- Optional fields are line items we might not always extract (e.g. R&D for
  non-tech companies). Required fields are the minimum needed to produce a DCF.
"""

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from industry import Industry


class FilingType(str, Enum):
    FORM_10K = "10-K"
    FORM_10Q = "10-Q"


class ExtractionSource(str, Enum):
    """Where a line item value came from."""

    XBRL = "xbrl"  # Track A — structured XBRL company facts
    LLM_HTML = "llm_html"  # Track B — Claude extraction from 10-K HTML
    USER_OVERRIDE = "user_override"  # HITL correction
    DERIVED = "derived"  # Calculated from other line items


class LineItem(BaseModel):
    """A single financial line item with provenance."""

    model_config = ConfigDict(json_encoders={Decimal: str})

    value: Decimal = Field(..., description="Value in USD (not millions/thousands)")
    source: ExtractionSource
    confidence: float = Field(..., ge=0.0, le=1.0)
    source_quote: Optional[str] = Field(
        None,
        description="Verbatim 5-30 word quote from filing supporting this value (Track B)",
    )
    xbrl_tag: Optional[str] = Field(
        None,
        description="Canonical XBRL concept tag, e.g. 'us-gaap:Revenues' (Track A)",
    )


class RevenueSegment(BaseModel):
    """One row of a filer's segment-revenue breakdown.

    `name` mirrors the filer's exact label ("iPhone", "Services", "Data
    Center", etc.); `revenue` is a full LineItem so segment values carry
    the same provenance machinery as consolidated line items.
    """

    name: str = Field(..., description="Segment / product / geography name as reported")
    revenue: LineItem


class IncomeStatement(BaseModel):
    """Industrial / tech income statement — the default shape.

    `kind` is the discriminator that lets the response carry one of several
    industry-specific income statement variants (banks have a totally
    different shape — see BankIncomeStatement). Frontend dispatches on this.
    """

    kind: Literal["standard"] = "standard"
    revenue: LineItem
    cost_of_revenue: Optional[LineItem] = None
    gross_profit: Optional[LineItem] = None
    research_and_development: Optional[LineItem] = None
    selling_general_administrative: Optional[LineItem] = None
    depreciation_amortization: Optional[LineItem] = None
    operating_income: LineItem
    interest_expense: Optional[LineItem] = None
    income_before_tax: Optional[LineItem] = None
    income_tax_expense: Optional[LineItem] = None
    net_income: LineItem
    diluted_shares_outstanding: LineItem
    revenue_segments: Optional[list[RevenueSegment]] = Field(
        default=None,
        description="Revenue broken out by segment if the filer reports it. "
        "None means we didn't extract; [] means we asked and the filer is single-segment.",
    )


class BankIncomeStatement(BaseModel):
    """Bank income statement.

    Banks don't have COGS or operating margin in the traditional sense. The
    primary economic story is interest spread (interest income − interest
    expense, net of credit losses) plus fee-and-trading income. Operating
    expense is "non-interest expense" — branch ops, comp, tech, regulatory.

    Required fields here track what's needed for a bank DCF flavor (DDM
    or residual income): pre-tax income, tax expense, net income, diluted
    shares. Net interest income is the load-bearing top-line metric.
    """

    kind: Literal["bank"] = "bank"
    interest_income: Optional[LineItem] = None
    interest_expense: Optional[LineItem] = None
    net_interest_income: LineItem
    provision_for_credit_losses: Optional[LineItem] = None
    non_interest_income: Optional[LineItem] = None
    non_interest_expense: Optional[LineItem] = None
    income_before_tax: LineItem
    income_tax_expense: LineItem
    net_income: LineItem
    diluted_shares_outstanding: LineItem


class InsuranceIncomeStatement(BaseModel):
    """Insurance income statement.

    Insurers' top line is `premiums_earned` plus `net_investment_income`
    (investment income on the float / general account is a primary economic
    driver, not a non-operating line item). The biggest expense is
    `benefits_and_claims_incurred` — what was paid out plus reserve changes
    for claims / future policy benefits. Operating expenses are run-the-shop
    costs (commissions, underwriting, G&A).
    """

    kind: Literal["insurer"] = "insurer"
    premiums_earned: LineItem
    net_investment_income: Optional[LineItem] = None
    benefits_and_claims: Optional[LineItem] = None
    operating_expenses: Optional[LineItem] = None
    income_before_tax: LineItem
    income_tax_expense: LineItem
    net_income: LineItem
    diluted_shares_outstanding: LineItem


class REITIncomeStatement(BaseModel):
    """REIT income statement.

    REITs report rental / property revenue and own real-estate-heavy
    balance sheets that depreciate aggressively under GAAP. Because the
    accounting depreciation typically overstates economic depreciation
    (well-maintained properties don't lose value at the GAAP rate), GAAP
    net income understates the cash a REIT actually distributes — which
    is why REIT analysts use FFO (net income + D&A) and AFFO as their
    primary earnings measure.
    """

    kind: Literal["reit"] = "reit"
    revenue: LineItem  # rental + service income
    property_operating_expense: Optional[LineItem] = None
    depreciation_amortization: LineItem  # load-bearing for the FFO calc
    general_and_administrative: Optional[LineItem] = None
    operating_income: Optional[LineItem] = None
    interest_expense: Optional[LineItem] = None
    income_before_tax: Optional[LineItem] = None
    income_tax_expense: Optional[LineItem] = None
    net_income: LineItem
    diluted_shares_outstanding: LineItem


# Discriminated union: Pydantic v2 uses the `kind` field to decide which
# variant to validate against. Frontend TS gets the same narrowing semantics.
AnyIncomeStatement = Annotated[
    Union[
        IncomeStatement,
        BankIncomeStatement,
        InsuranceIncomeStatement,
        REITIncomeStatement,
    ],
    Field(discriminator="kind"),
]


class BalanceSheet(BaseModel):
    """Industrial / tech balance sheet — current/non-current breakdown."""

    kind: Literal["standard"] = "standard"
    cash_and_equivalents: LineItem
    short_term_investments: Optional[LineItem] = None
    accounts_receivable: Optional[LineItem] = None
    inventory: Optional[LineItem] = None
    total_current_assets: Optional[LineItem] = None
    property_plant_equipment_net: Optional[LineItem] = None
    total_assets: LineItem
    accounts_payable: Optional[LineItem] = None
    short_term_debt: Optional[LineItem] = None
    total_current_liabilities: Optional[LineItem] = None
    long_term_debt: Optional[LineItem] = None
    total_liabilities: LineItem
    shareholders_equity: LineItem


class BankBalanceSheet(BaseModel):
    """Bank balance sheet — loans + deposits dominate, current/non-current split is meaningless.

    Banks don't classify their balance sheet into current vs non-current the
    way an industrial does. The economically interesting items are the loan
    book (and its allowance) and the deposit base. Capital ratios — driven
    by `shareholders_equity / total_assets` — drive regulatory headroom.
    """

    kind: Literal["bank"] = "bank"
    cash_and_equivalents: LineItem
    securities: Optional[LineItem] = None  # AFS + HTM securities portfolio
    total_loans: LineItem  # net of allowance, typically
    allowance_for_loan_losses: Optional[LineItem] = None
    total_deposits: LineItem
    long_term_debt: Optional[LineItem] = None
    total_assets: LineItem
    total_liabilities: LineItem
    shareholders_equity: LineItem


class InsuranceBalanceSheet(BaseModel):
    """Insurance balance sheet — investments + reserves are the main lines.

    Insurance balance sheets are dominated by the general-account investment
    portfolio (debt + equity securities, alternatives) on the asset side and
    the reserve liability for future policy benefits / unpaid claims on the
    liability side. The reserve is the load-bearing line; book value is the
    residual after subtracting it from invested assets.
    """

    kind: Literal["insurer"] = "insurer"
    cash_and_equivalents: LineItem
    investments: Optional[LineItem] = None
    insurance_reserves: Optional[LineItem] = None  # future policy benefits + unpaid claims
    total_assets: LineItem
    total_liabilities: LineItem
    shareholders_equity: LineItem


class REITBalanceSheet(BaseModel):
    """REIT balance sheet — real estate dominates the asset side.

    A REIT's asset base is overwhelmingly investment property: land +
    buildings at cost, less accumulated depreciation, equals net real
    estate. We carry both the gross (`real_estate_at_cost`) and net
    (`real_estate_net`) figures because the gap between them tells you
    something — a REIT with $90B at cost and $80B net is much earlier
    in its book's depreciation schedule than one at $90B / $40B, even
    if both report identical NOI today. Long-term debt is the other
    load-bearing line; REITs run high leverage by industrials' standards
    and roll their secured/unsecured stack continuously.
    """

    kind: Literal["reit"] = "reit"
    cash_and_equivalents: LineItem
    real_estate_at_cost: Optional[LineItem] = None  # gross PPE in real estate
    accumulated_depreciation: Optional[LineItem] = None  # contra-asset
    real_estate_net: Optional[LineItem] = None  # at_cost − accumulated_depreciation
    total_assets: LineItem
    long_term_debt: Optional[LineItem] = None
    total_liabilities: LineItem
    shareholders_equity: LineItem


AnyBalanceSheet = Annotated[
    Union[BalanceSheet, BankBalanceSheet, InsuranceBalanceSheet, REITBalanceSheet],
    Field(discriminator="kind"),
]


class CashFlowStatement(BaseModel):
    """Industrial / tech cash flow — DCF needs D&A, CFO, capex."""

    kind: Literal["standard"] = "standard"
    depreciation_amortization: LineItem  # add-back from CFO; canonical D&A figure
    cash_from_operations: LineItem
    capital_expenditures: LineItem  # positive number; sign handled downstream
    cash_from_investing: Optional[LineItem] = None
    cash_from_financing: Optional[LineItem] = None
    dividends_paid: Optional[LineItem] = None  # positive; needed for DDM


class BankCashFlowStatement(BaseModel):
    """Bank cash flow — DCF doesn't apply, DDM does.

    Banks don't have meaningful "free cash flow" the way industrials do —
    capex is rounding error compared to loan-book changes, and the cash
    flow statement is dominated by deposit/loan flows. What we actually
    need for a bank DDM is `dividends_paid` — the rest of the cash flow
    is informational only.
    """

    kind: Literal["bank"] = "bank"
    cash_from_operations: LineItem
    cash_from_investing: Optional[LineItem] = None
    cash_from_financing: Optional[LineItem] = None
    dividends_paid: Optional[LineItem] = None  # required for DDM in practice
    depreciation_amortization: Optional[LineItem] = None
    capital_expenditures: Optional[LineItem] = None


class InsuranceCashFlowStatement(BaseModel):
    """Insurance cash flow — same shape as bank's, for the same reasons.

    The insurance valuation flavor is justified P/B (BVPS × (ROE−g)/(r−g)),
    which doesn't even consume cash flow data — but we still extract CFO/CFI/
    CFF for transparency, and `dividends_paid` is useful for sanity-checking
    the implied payout vs ROE−g.
    """

    kind: Literal["insurer"] = "insurer"
    cash_from_operations: LineItem
    cash_from_investing: Optional[LineItem] = None
    cash_from_financing: Optional[LineItem] = None
    dividends_paid: Optional[LineItem] = None
    depreciation_amortization: Optional[LineItem] = None
    capital_expenditures: Optional[LineItem] = None


class REITCashFlowStatement(BaseModel):
    """REIT cash flow — D&A and dividends carry most of the signal.

    The FFO valuation flavor reads net income and D&A off the income
    statement, so the cash flow statement is informational here — but
    `cash_from_operations` is the standard sanity check (REIT FFO and
    operating cash flow track loosely; AFFO closes most of the gap by
    subtracting recurring capex). `dividends_paid` and `capital_expenditures`
    matter to a finance reviewer — REITs are required by tax code to
    distribute ≥90% of taxable income, so dividends are large and
    structurally meaningful.
    """

    kind: Literal["reit"] = "reit"
    cash_from_operations: LineItem
    cash_from_investing: Optional[LineItem] = None
    cash_from_financing: Optional[LineItem] = None
    dividends_paid: Optional[LineItem] = None
    depreciation_amortization: Optional[LineItem] = None
    capital_expenditures: Optional[LineItem] = None


AnyCashFlowStatement = Annotated[
    Union[
        CashFlowStatement,
        BankCashFlowStatement,
        InsuranceCashFlowStatement,
        REITCashFlowStatement,
    ],
    Field(discriminator="kind"),
]


class FinancialPeriod(BaseModel):
    """Bundled financial statements for one fiscal period.

    `industry` mirrors the `kind` discriminator on each statement and is
    redundant with them — but it's much more convenient for the frontend
    (one check at the period level) than four parallel checks per render.
    """

    fiscal_year: int
    fiscal_period_end: date
    filing_accession: str = Field(
        ..., description="SEC accession number, e.g. '0000320193-24-000123'"
    )
    filing_type: FilingType
    industry: Industry = Industry.STANDARD
    income_statement: AnyIncomeStatement
    balance_sheet: AnyBalanceSheet
    cash_flow_statement: AnyCashFlowStatement


class ExtractionFlag(BaseModel):
    """A flagged extraction surfaced to the HITL review UI."""

    field_path: str = Field(
        ..., description="Dot-notation path, e.g. 'income_statement.revenue'"
    )
    reason: str
    current_value: Decimal
    suggested_value: Optional[Decimal] = None


class Company(BaseModel):
    """Top-level company financial data."""

    ticker: str
    cik: str  # zero-padded to 10 digits
    name: str
    fiscal_year_end_month: int = Field(..., ge=1, le=12)
    periods: list[FinancialPeriod]
    extraction_flags: list[ExtractionFlag] = Field(default_factory=list)


# --- DCF / valuation models ---------------------------------------------------


class Assumptions(BaseModel):
    """Forward-looking inputs to the DCF.

    All ratios and rates are expressed as decimals (0.05 = 5%, not 5.0).
    """

    revenue_growth: float = Field(..., description="Annual revenue growth rate")
    operating_margin: float = Field(..., description="Operating income / revenue")
    terminal_growth: float = Field(..., description="Perpetual growth past year 5")
    wacc: float = Field(..., gt=0.0, description="Weighted average cost of capital")
    tax_rate: float = Field(..., ge=0.0, lt=1.0)
    capex_ratio: float = Field(..., description="Capex / revenue")
    da_ratio: float = Field(..., description="D&A / revenue")
    working_capital_ratio: float = Field(
        ..., description="Change in WC / change in revenue"
    )


class YearProjection(BaseModel):
    """One projected year of the three-statement model."""

    year: int  # offset from base year (1..5)
    revenue: float
    operating_income: float
    nopat: float
    depreciation_amortization: float
    capital_expenditures: float
    change_in_working_capital: float
    free_cash_flow: float


class Projection(BaseModel):
    """Output of a single DCF run."""

    assumptions: Assumptions
    base_year: int
    base_revenue: float
    years: list[YearProjection]
    terminal_value: float
    enterprise_value: float
    net_debt: float
    equity_value: float
    diluted_shares: float
    fair_value_per_share: float


class MonteCarloResult(BaseModel):
    """Summary of a Monte Carlo run over per-share fair value."""

    iterations_completed: int
    mean: float
    median: float
    std_dev: float
    p10: float
    p25: float
    p75: float
    p90: float
    histogram: list[tuple[float, int]] = Field(
        ..., description="(bin_left_edge, count) pairs over per-share fair value"
    )


class SensitivityGrid(BaseModel):
    """2-D fair-value grid over (revenue_growth × operating_margin)."""

    revenue_growth_axis: list[float]
    operating_margin_axis: list[float]
    values: list[list[Optional[float]]] = Field(
        ..., description="values[i][j] = fair value at (rev_growth[i], op_margin[j])"
    )


# --- Trading-comps cross-check ----------------------------------------------


class PeerMultiples(BaseModel):
    """One row in a trading-comps table — a peer's current market multiples."""

    ticker: str
    name: str
    market_cap: Optional[float] = None
    enterprise_value: Optional[float] = None
    revenue: Optional[float] = None  # LTM
    ebitda: Optional[float] = None  # LTM
    pe_ratio: Optional[float] = None  # trailing P/E
    ev_revenue: Optional[float] = None
    ev_ebitda: Optional[float] = None


class CompsResponse(BaseModel):
    """Output of the /comps/{ticker} endpoint.

    `target_market` is the target ticker's own current market multiples (so
    the user can see the spread vs DCF fair value). `peers` are the
    hand-picked comparables. Median multiples are computed across `peers`,
    excluding nulls and non-positive denominators.

    `dcf_implied_ev_revenue` and `dcf_implied_ev_ebitda` aren't computed
    here — the frontend has the latest DCF result already and computes
    them client-side from `valuation.projection.enterprise_value` /
    company.periods[0].income_statement.revenue.
    """

    target_ticker: str
    target_market: Optional[PeerMultiples] = None
    peers: list[PeerMultiples] = Field(default_factory=list)
    median_pe: Optional[float] = None
    median_ev_revenue: Optional[float] = None
    median_ev_ebitda: Optional[float] = None
