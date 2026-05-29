"""Standard FCFF DCF + Monte Carlo + sensitivity tests.

The alternative valuation flavors (bank DDM, insurer P/B, REIT FFO, energy
reserve-life) already have closed-form algebraic tests in test_extraction.py.
The *default* path that most tickers take — the standard 5-year FCFF DCF, its
Gordon terminal value, the Monte Carlo, and the sensitivity grid — had none.
This file closes that gap with deterministic, hand-derivable assertions so the
"valuation math verified correct" claim is regression-guarded.
"""

from datetime import date
from decimal import Decimal

import pytest

from dcf import PROJECTION_YEARS, compute_projection, monte_carlo, sensitivity_grid
from industry import Industry
from schemas import (
    Assumptions,
    BalanceSheet,
    CashFlowStatement,
    Company,
    ExtractionSource,
    FilingType,
    FinancialPeriod,
    IncomeStatement,
    LineItem,
)


def _line(value: float, source: ExtractionSource = ExtractionSource.XBRL) -> LineItem:
    return LineItem(value=Decimal(str(value)), source=source, confidence=1.0)


def _std_company(
    revenue: int = 1000,
    shares: int = 100,
    cash: int = 50,
    long_term_debt: int = 200,
    short_term_debt: int = 50,
) -> Company:
    """A clean standard-industry company with round numbers so the whole DCF is
    hand-derivable. net_debt = LT(200) + ST(50) + leases(0) - cash(50) = 200."""
    period = FinancialPeriod(
        fiscal_year=2025,
        fiscal_period_end=date(2025, 12, 31),
        filing_accession="0000000-25-000001",
        filing_type=FilingType.FORM_10K,
        industry=Industry.STANDARD,
        income_statement=IncomeStatement(
            revenue=_line(revenue),
            operating_income=_line(revenue * 0.2),
            net_income=_line(revenue * 0.15),
            diluted_shares_outstanding=_line(shares),
        ),
        balance_sheet=BalanceSheet(
            cash_and_equivalents=_line(cash),
            total_assets=_line(revenue * 2),
            total_liabilities=_line(revenue),
            shareholders_equity=_line(revenue),
            long_term_debt=_line(long_term_debt),
            short_term_debt=_line(short_term_debt),
        ),
        cash_flow_statement=CashFlowStatement(
            depreciation_amortization=_line(revenue * 0.04),
            cash_from_operations=_line(revenue * 0.2),
            capital_expenditures=_line(revenue * 0.05),
        ),
    )
    return Company(
        ticker="TEST",
        cik="0000000001",
        name="Test Co",
        fiscal_year_end_month=12,
        periods=[period],
    )


def _base_assumptions() -> Assumptions:
    return Assumptions(
        revenue_growth=0.10,
        operating_margin=0.20,
        terminal_growth=0.02,
        wacc=0.10,
        tax_rate=0.25,
        capex_ratio=0.05,
        da_ratio=0.04,
        working_capital_ratio=0.10,
    )


# --- standard FCFF DCF: hand-derived end to end ------------------------------


def test_standard_fcff_projection_matches_hand_derivation():
    company = _std_company()
    a = _base_assumptions()
    proj = compute_projection(company, a)

    assert len(proj.years) == PROJECTION_YEARS == 5

    # Year 1, computed entirely by hand from $1,000 base revenue:
    #   rev   = 1000 * 1.10        = 1100
    #   op    = 1100 * 0.20        = 220
    #   nopat = 220  * (1 - 0.25)  = 165
    #   da    = 1100 * 0.04        = 44
    #   capex = 1100 * 0.05        = 55
    #   dwc   = (1100 - 1000)*0.10 = 10
    #   fcff  = 165 + 44 - 55 - 10 = 144
    y1 = proj.years[0]
    assert y1.revenue == pytest.approx(1100.0)
    assert y1.operating_income == pytest.approx(220.0)
    assert y1.nopat == pytest.approx(165.0)
    assert y1.depreciation_amortization == pytest.approx(44.0)
    assert y1.capital_expenditures == pytest.approx(55.0)
    assert y1.change_in_working_capital == pytest.approx(10.0)
    assert y1.free_cash_flow == pytest.approx(144.0)

    # Independent re-derivation of every year, plus per-year internal identities.
    prev = 1000.0
    expected_fcff = []
    for t in range(1, PROJECTION_YEARS + 1):
        rev = prev * 1.10
        op = rev * 0.20
        nopat = op * (1 - 0.25)
        da = rev * 0.04
        capex = rev * 0.05
        dwc = (rev - prev) * 0.10
        fcff = nopat + da - capex - dwc
        expected_fcff.append(fcff)

        py = proj.years[t - 1]
        assert py.year == t
        assert py.revenue == pytest.approx(rev)
        assert py.operating_income == pytest.approx(op)
        assert py.nopat == pytest.approx(nopat)
        assert py.free_cash_flow == pytest.approx(fcff)
        # FCFF identity holds on the returned object itself.
        assert py.free_cash_flow == pytest.approx(
            py.nopat
            + py.depreciation_amortization
            - py.capital_expenditures
            - py.change_in_working_capital
        )
        prev = rev

    # Gordon terminal value off the final-year FCFF.
    expected_tv = expected_fcff[-1] * (1 + 0.02) / (0.10 - 0.02)
    assert proj.terminal_value == pytest.approx(expected_tv)

    # Enterprise value = PV(FCFF) + PV(terminal), end-of-year discounting.
    expected_pv_fcff = sum(f / (1.10 ** t) for t, f in enumerate(expected_fcff, start=1))
    expected_pv_tv = expected_tv / (1.10 ** PROJECTION_YEARS)
    expected_ev = expected_pv_fcff + expected_pv_tv
    assert proj.enterprise_value == pytest.approx(expected_ev)

    # EV -> equity -> per-share bridge.
    assert proj.net_debt == pytest.approx(200.0)  # 200 + 50 + 0 - 50
    assert proj.equity_value == pytest.approx(expected_ev - 200.0)
    assert proj.diluted_shares == pytest.approx(100.0)
    assert proj.fair_value_per_share == pytest.approx((expected_ev - 200.0) / 100.0)


def test_standard_projection_rejects_wacc_below_terminal_growth():
    """The Gordon r > g guard: a non-convergent perpetuity must not be priced."""
    company = _std_company()
    bad = _base_assumptions().model_copy(
        update={"wacc": 0.04, "terminal_growth": 0.06}
    )
    with pytest.raises(ValueError, match="must exceed terminal_growth"):
        compute_projection(company, bad)


def test_standard_projection_rejects_nonpositive_revenue():
    company = _std_company(revenue=0)
    with pytest.raises(ValueError, match="revenue must be positive"):
        compute_projection(company, _base_assumptions())


# --- Monte Carlo smoke + determinism -----------------------------------------


def test_monte_carlo_is_ordered_and_deterministic():
    company = _std_company()
    a = _base_assumptions()

    r1 = monte_carlo(company, a, iterations=2000, seed=42)
    r2 = monte_carlo(company, a, iterations=2000, seed=42)

    # Completed at least most draws (the wacc<=tg clip keeps the count stable).
    assert r1.iterations_completed > 0
    # Percentiles are monotonically ordered.
    assert r1.p10 <= r1.p25 <= r1.median <= r1.p75 <= r1.p90
    assert r1.std_dev >= 0.0
    assert len(r1.histogram) > 0
    # The deterministic point estimate falls inside the 10-90 envelope.
    point = compute_projection(company, a).fair_value_per_share
    assert r1.p10 <= point <= r1.p90

    # Same seed -> byte-for-byte identical summary.
    assert r1.iterations_completed == r2.iterations_completed
    assert r1.mean == pytest.approx(r2.mean)
    assert r1.median == pytest.approx(r2.median)
    assert r1.p10 == pytest.approx(r2.p10)
    assert r1.p90 == pytest.approx(r2.p90)
    assert r1.histogram == r2.histogram


def test_monte_carlo_different_seeds_diverge():
    company = _std_company()
    a = _base_assumptions()
    a_run = monte_carlo(company, a, iterations=2000, seed=1)
    b_run = monte_carlo(company, a, iterations=2000, seed=2)
    # Different RNG streams should not produce an identical mean.
    assert a_run.mean != b_run.mean


# --- sensitivity grid --------------------------------------------------------


def test_sensitivity_grid_shape_center_and_monotonicity():
    company = _std_company()
    a = _base_assumptions()
    grid = sensitivity_grid(
        company, a, revenue_growth_steps=7, operating_margin_steps=7
    )

    # Requested dimensions.
    assert len(grid.revenue_growth_axis) == 7
    assert len(grid.operating_margin_axis) == 7
    assert len(grid.values) == 7
    assert all(len(row) == 7 for row in grid.values)

    # The standard path never raises over the rev-growth x op-margin axes, so
    # every cell is a real number.
    assert all(cell is not None for row in grid.values for cell in row)

    # Default axes are base +/- 5pp over 7 steps, so the middle index is the
    # base assumption and the center cell equals the base point valuation.
    base_fvps = compute_projection(company, a).fair_value_per_share
    assert grid.revenue_growth_axis[3] == pytest.approx(a.revenue_growth)
    assert grid.operating_margin_axis[3] == pytest.approx(a.operating_margin)
    assert grid.values[3][3] == pytest.approx(base_fvps)

    # Fair value rises strictly with operating margin (center row, columns).
    center_row = grid.values[3]
    assert all(center_row[j] < center_row[j + 1] for j in range(6))

    # Fair value rises with revenue growth (center column, rows).
    center_col = [grid.values[i][3] for i in range(7)]
    assert all(center_col[i] < center_col[i + 1] for i in range(6))


def test_sensitivity_grid_returns_none_cells_without_crashing():
    """When the base assumptions are non-convergent (wacc <= terminal_growth),
    every cell's projection raises and is caught as None — the grid must come
    back fully populated with None rather than blowing up."""
    company = _std_company()
    bad = _base_assumptions().model_copy(
        update={"wacc": 0.03, "terminal_growth": 0.05}
    )
    grid = sensitivity_grid(
        company, bad, revenue_growth_steps=3, operating_margin_steps=3
    )
    assert len(grid.values) == 3
    assert all(len(row) == 3 for row in grid.values)
    assert all(cell is None for row in grid.values for cell in row)
