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
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


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
    """Income statement line items needed for DCF modeling."""

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


class BalanceSheet(BaseModel):
    """Balance sheet line items needed for DCF + equity bridge."""

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


class CashFlowStatement(BaseModel):
    """Cash flow line items needed for DCF (FCF and capex)."""

    depreciation_amortization: LineItem  # add-back from CFO; canonical D&A figure
    cash_from_operations: LineItem
    capital_expenditures: LineItem  # positive number; sign handled downstream
    cash_from_investing: Optional[LineItem] = None
    cash_from_financing: Optional[LineItem] = None


class FinancialPeriod(BaseModel):
    """Bundled financial statements for one fiscal period."""

    fiscal_year: int
    fiscal_period_end: date
    filing_accession: str = Field(
        ..., description="SEC accession number, e.g. '0000320193-24-000123'"
    )
    filing_type: FilingType
    income_statement: IncomeStatement
    balance_sheet: BalanceSheet
    cash_flow_statement: CashFlowStatement


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
