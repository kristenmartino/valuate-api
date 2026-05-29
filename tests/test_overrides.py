"""HITL override tests — the human-in-the-loop correction path.

These cover the industry-aware override bug fix:
- apply_override must resolve a field against the ACTUAL statement variant
  (standard / bank / insurer / REIT), not a hardcoded standard schema, so an
  override of a bank's net_interest_income or a REIT's depreciation_amortization
  succeeds instead of 400-ing.
- apply_override must preserve all historical periods, not collapse the company
  to a single (overridden) period.
"""

from datetime import date
from decimal import Decimal

import pytest

from industry import Industry
from overrides import apply_override, parse_field_path
from schemas import (
    BalanceSheet,
    BankBalanceSheet,
    BankCashFlowStatement,
    BankIncomeStatement,
    CashFlowStatement,
    Company,
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
)


def _line(value: int, source: ExtractionSource = ExtractionSource.XBRL) -> LineItem:
    return LineItem(value=Decimal(str(value)), source=source, confidence=1.0)


def _company(period: FinancialPeriod, *more: FinancialPeriod, ticker: str = "TEST") -> Company:
    return Company(
        ticker=ticker,
        cik="0000000001",
        name=f"{ticker} Co",
        fiscal_year_end_month=12,
        periods=[period, *more],
    )


def _std_period(year: int = 2025, revenue: int = 1000) -> FinancialPeriod:
    return FinancialPeriod(
        fiscal_year=year,
        fiscal_period_end=date(year, 12, 31),
        filing_accession="0000000-25-000001",
        filing_type=FilingType.FORM_10K,
        industry=Industry.STANDARD,
        income_statement=IncomeStatement(
            revenue=_line(revenue),
            operating_income=_line(revenue // 5),
            net_income=_line(revenue // 7),
            diluted_shares_outstanding=_line(100),
        ),
        balance_sheet=BalanceSheet(
            cash_and_equivalents=_line(50),
            total_assets=_line(revenue * 2),
            total_liabilities=_line(revenue),
            shareholders_equity=_line(revenue),
        ),
        cash_flow_statement=CashFlowStatement(
            depreciation_amortization=_line(revenue // 25),
            cash_from_operations=_line(revenue // 5),
            capital_expenditures=_line(revenue // 20),
        ),
    )


def _bank_period() -> FinancialPeriod:
    return FinancialPeriod(
        fiscal_year=2025,
        fiscal_period_end=date(2025, 12, 31),
        filing_accession="0000000-25-000001",
        filing_type=FilingType.FORM_10K,
        industry=Industry.BANK,
        income_statement=BankIncomeStatement(
            net_interest_income=_line(95_000_000_000),
            income_before_tax=_line(70_000_000_000),
            income_tax_expense=_line(15_000_000_000),
            net_income=_line(55_000_000_000),
            diluted_shares_outstanding=_line(2_500_000_000),
        ),
        balance_sheet=BankBalanceSheet(
            cash_and_equivalents=_line(400_000_000_000),
            total_loans=_line(1_400_000_000_000),
            total_deposits=_line(2_400_000_000_000),
            total_assets=_line(4_200_000_000_000),
            total_liabilities=_line(3_900_000_000_000),
            shareholders_equity=_line(300_000_000_000),
        ),
        cash_flow_statement=BankCashFlowStatement(
            cash_from_operations=_line(80_000_000_000),
            dividends_paid=_line(15_000_000_000),
        ),
    )


def _insurer_period() -> FinancialPeriod:
    return FinancialPeriod(
        fiscal_year=2025,
        fiscal_period_end=date(2025, 12, 31),
        filing_accession="0000000-25-000001",
        filing_type=FilingType.FORM_10K,
        industry=Industry.INSURER,
        income_statement=InsuranceIncomeStatement(
            premiums_earned=_line(40_000_000_000),
            income_before_tax=_line(4_000_000_000),
            income_tax_expense=_line(800_000_000),
            net_income=_line(3_000_000_000),
            diluted_shares_outstanding=_line(400_000_000),
        ),
        balance_sheet=InsuranceBalanceSheet(
            cash_and_equivalents=_line(20_000_000_000),
            total_assets=_line(750_000_000_000),
            total_liabilities=_line(720_000_000_000),
            shareholders_equity=_line(30_000_000_000),
        ),
        cash_flow_statement=InsuranceCashFlowStatement(
            cash_from_operations=_line(10_000_000_000),
        ),
    )


def _reit_period() -> FinancialPeriod:
    return FinancialPeriod(
        fiscal_year=2025,
        fiscal_period_end=date(2025, 12, 31),
        filing_accession="0000000-25-000001",
        filing_type=FilingType.FORM_10K,
        industry=Industry.REIT,
        income_statement=REITIncomeStatement(
            revenue=_line(8_000_000_000),
            depreciation_amortization=_line(2_000_000_000),
            net_income=_line(3_000_000_000),
            diluted_shares_outstanding=_line(1_000_000_000),
        ),
        balance_sheet=REITBalanceSheet(
            cash_and_equivalents=_line(1_000_000_000),
            total_assets=_line(95_000_000_000),
            total_liabilities=_line(40_000_000_000),
            shareholders_equity=_line(55_000_000_000),
        ),
        cash_flow_statement=REITCashFlowStatement(
            cash_from_operations=_line(5_000_000_000),
        ),
    )


# --- happy path across every industry variant --------------------------------


def test_override_standard_revenue_succeeds():
    company = _company(_std_period())
    updated = apply_override(company, "income_statement.revenue", Decimal("1234"))
    item = updated.periods[0].income_statement.revenue
    assert item.value == Decimal("1234")
    assert item.source == ExtractionSource.USER_OVERRIDE
    assert item.confidence == 1.0


def test_override_bank_net_interest_income_succeeds():
    """The regression: a bank field doesn't exist on the standard schema, so the
    old code raised 'Field not found' for every JPM override."""
    company = _company(_bank_period(), ticker="JPM")
    updated = apply_override(
        company, "income_statement.net_interest_income", Decimal("99000000000")
    )
    item = updated.periods[0].income_statement.net_interest_income
    assert item.value == Decimal("99000000000")
    assert item.source == ExtractionSource.USER_OVERRIDE


def test_override_insurer_premiums_earned_succeeds():
    company = _company(_insurer_period(), ticker="PRU")
    updated = apply_override(
        company, "income_statement.premiums_earned", Decimal("41000000000")
    )
    item = updated.periods[0].income_statement.premiums_earned
    assert item.value == Decimal("41000000000")
    assert item.source == ExtractionSource.USER_OVERRIDE


def test_override_reit_depreciation_amortization_succeeds():
    company = _company(_reit_period(), ticker="PLD")
    updated = apply_override(
        company, "income_statement.depreciation_amortization", Decimal("2100000000")
    )
    item = updated.periods[0].income_statement.depreciation_amortization
    assert item.value == Decimal("2100000000")
    assert item.source == ExtractionSource.USER_OVERRIDE


def test_override_balance_sheet_field_succeeds():
    """Overrides target statements other than the income statement too."""
    company = _company(_std_period())
    updated = apply_override(
        company, "balance_sheet.total_assets", Decimal("3000"), source_quote="per filing"
    )
    item = updated.periods[0].balance_sheet.total_assets
    assert item.value == Decimal("3000")
    assert item.source_quote == "per filing"


# --- error paths -------------------------------------------------------------


def test_override_unknown_field_returns_value_error():
    company = _company(_std_period())
    with pytest.raises(ValueError, match="not found"):
        apply_override(company, "income_statement.fake_field", Decimal("1"))


def test_override_unknown_statement_returns_value_error():
    company = _company(_std_period())
    with pytest.raises(ValueError, match="Unknown statement"):
        apply_override(company, "bogus_statement.revenue", Decimal("1"))


def test_override_malformed_path_returns_value_error():
    with pytest.raises(ValueError, match="field_path must be"):
        parse_field_path("revenue")  # missing the '<statement>.' prefix


def test_override_empty_periods_returns_value_error():
    company = Company(
        ticker="TEST",
        cik="0000000001",
        name="Test Co",
        fiscal_year_end_month=12,
        periods=[],
    )
    with pytest.raises(ValueError, match="no periods"):
        apply_override(company, "income_statement.revenue", Decimal("1"))


# --- the second bug: history must survive an override ------------------------


def test_override_preserves_multi_period_history():
    """The old code rebuilt the company with [new_period] only, collapsing a
    multi-year company to a single period on every override."""
    company = _company(_std_period(2025, 1000), _std_period(2024, 900), _std_period(2023, 800))
    assert len(company.periods) == 3

    updated = apply_override(company, "income_statement.revenue", Decimal("1234"))

    assert len(updated.periods) == 3  # history preserved
    # latest period is overridden
    assert updated.periods[0].fiscal_year == 2025
    assert updated.periods[0].income_statement.revenue.value == Decimal("1234")
    # earlier periods are untouched
    assert updated.periods[1].fiscal_year == 2024
    assert updated.periods[1].income_statement.revenue.value == Decimal("900")
    assert updated.periods[2].fiscal_year == 2023
    assert updated.periods[2].income_statement.revenue.value == Decimal("800")
