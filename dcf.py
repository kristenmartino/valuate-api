"""DCF math: 5-year projection, Monte Carlo, and sensitivity grid.

Pure Python — 10K Monte Carlo iterations on the 5-year model fits well
within a typical request budget without numpy. Inputs come in as Decimals
(from the extracted Company); we work in floats internally and emit floats
on the response, since DCF outputs are estimates, not exact accounting.

Notation:
    NOPAT = Operating Income × (1 − tax_rate)
    FCFF  = NOPAT + D&A − Capex − ΔWC
    TV    = FCFF[5] × (1 + g) / (WACC − g)        # Gordon growth
    EV    = Σ PV(FCFF[t]) + PV(TV)
    Equity = EV − Net Debt; per-share = Equity / diluted_shares
"""

import math
import random
from typing import Optional

from schemas import (
    Assumptions,
    Company,
    MonteCarloResult,
    Projection,
    SensitivityGrid,
    YearProjection,
)


PROJECTION_YEARS = 5
DEFAULT_TAX_RATE = 0.21  # US federal corporate rate
DEFAULT_WORKING_CAPITAL_RATIO = 0.05


def _line_value(line) -> Optional[float]:
    """Pull a float from a LineItem, or None if absent."""
    if line is None:
        return None
    return float(line.value)


def default_assumptions(company: Company) -> Assumptions:
    """Derive a starting-point Assumptions object from the latest filing.

    Historical ratios (operating_margin, capex/D&A ratios, tax rate) come
    from the actuals. Forward-looking inputs (revenue_growth, terminal
    growth, WACC) get neutral defaults that the user is expected to tune.
    """
    period = company.periods[0]
    revenue = _line_value(period.income_statement.revenue) or 1.0
    op_income = _line_value(period.income_statement.operating_income) or 0.0
    da = _line_value(period.cash_flow_statement.depreciation_amortization) or 0.0
    capex = _line_value(period.cash_flow_statement.capital_expenditures) or 0.0
    ibt = _line_value(period.income_statement.income_before_tax)
    tax = _line_value(period.income_statement.income_tax_expense)

    if ibt and ibt > 0 and tax is not None:
        tax_rate = max(0.0, min(0.5, tax / ibt))
    else:
        tax_rate = DEFAULT_TAX_RATE

    return Assumptions(
        revenue_growth=0.05,
        operating_margin=op_income / revenue if revenue > 0 else 0.20,
        terminal_growth=0.025,
        wacc=0.08,
        tax_rate=tax_rate,
        capex_ratio=capex / revenue if revenue > 0 else 0.04,
        da_ratio=da / revenue if revenue > 0 else 0.04,
        working_capital_ratio=DEFAULT_WORKING_CAPITAL_RATIO,
    )


def _net_debt(company: Company) -> float:
    bs = company.periods[0].balance_sheet
    long_term = _line_value(bs.long_term_debt) or 0.0
    short_term = _line_value(bs.short_term_debt) or 0.0
    cash = _line_value(bs.cash_and_equivalents) or 0.0
    return long_term + short_term - cash


def _diluted_shares(company: Company) -> float:
    return _line_value(company.periods[0].income_statement.diluted_shares_outstanding) or 0.0


def compute_projection(company: Company, a: Assumptions) -> Projection:
    """Build a 5-year FCFF projection and discount to a per-share fair value.

    Raises ValueError if WACC <= terminal_growth (Gordon growth requires
    WACC strictly greater than g for a finite, non-negative terminal value).
    """
    if a.wacc <= a.terminal_growth:
        raise ValueError(
            f"WACC ({a.wacc}) must exceed terminal_growth ({a.terminal_growth})"
        )

    period = company.periods[0]
    base_revenue = _line_value(period.income_statement.revenue) or 0.0
    if base_revenue <= 0:
        raise ValueError("Base revenue must be positive")

    years: list[YearProjection] = []
    prev_rev = base_revenue
    for t in range(1, PROJECTION_YEARS + 1):
        rev = prev_rev * (1 + a.revenue_growth)
        op_income = rev * a.operating_margin
        nopat = op_income * (1 - a.tax_rate)
        da = rev * a.da_ratio
        capex = rev * a.capex_ratio
        delta_wc = (rev - prev_rev) * a.working_capital_ratio
        fcff = nopat + da - capex - delta_wc
        years.append(
            YearProjection(
                year=t,
                revenue=rev,
                operating_income=op_income,
                nopat=nopat,
                depreciation_amortization=da,
                capital_expenditures=capex,
                change_in_working_capital=delta_wc,
                free_cash_flow=fcff,
            )
        )
        prev_rev = rev

    last_fcff = years[-1].free_cash_flow
    terminal_value = last_fcff * (1 + a.terminal_growth) / (a.wacc - a.terminal_growth)

    pv_fcff = sum(y.free_cash_flow / (1 + a.wacc) ** y.year for y in years)
    pv_terminal = terminal_value / (1 + a.wacc) ** PROJECTION_YEARS
    enterprise_value = pv_fcff + pv_terminal

    net_debt = _net_debt(company)
    equity_value = enterprise_value - net_debt
    shares = _diluted_shares(company)
    fair_value_per_share = equity_value / shares if shares > 0 else 0.0

    return Projection(
        assumptions=a,
        base_year=period.fiscal_year,
        base_revenue=base_revenue,
        years=years,
        terminal_value=terminal_value,
        enterprise_value=enterprise_value,
        net_debt=net_debt,
        equity_value=equity_value,
        diluted_shares=shares,
        fair_value_per_share=fair_value_per_share,
    )


def _histogram(values: list[float], bins: int = 50) -> list[tuple[float, int]]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if lo == hi:
        return [(lo, len(values))]
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        idx = min(int((v - lo) / width), bins - 1)
        counts[idx] += 1
    return [(lo + i * width, c) for i, c in enumerate(counts)]


def monte_carlo(
    company: Company,
    base: Assumptions,
    iterations: int = 10_000,
    revenue_growth_std: float = 0.02,
    operating_margin_std: float = 0.02,
    terminal_growth_std: float = 0.005,
    wacc_std: float = 0.005,
    seed: Optional[int] = None,
) -> MonteCarloResult:
    """Run `iterations` DCFs with the four key drivers sampled from normals.

    WACC is clipped to stay strictly above terminal_growth; a sample that
    would invert the relationship gets WACC pulled up to terminal_growth + 1%
    rather than discarded, keeping the iteration count stable.
    """
    rng = random.Random(seed)
    results: list[float] = []
    for _ in range(iterations):
        rg = rng.gauss(base.revenue_growth, revenue_growth_std)
        om = rng.gauss(base.operating_margin, operating_margin_std)
        tg = rng.gauss(base.terminal_growth, terminal_growth_std)
        wacc = rng.gauss(base.wacc, wacc_std)
        if wacc <= tg:
            wacc = tg + 0.01
        a = base.model_copy(
            update={
                "revenue_growth": rg,
                "operating_margin": om,
                "terminal_growth": tg,
                "wacc": wacc,
            }
        )
        try:
            proj = compute_projection(company, a)
        except (ValueError, ZeroDivisionError):
            continue
        results.append(proj.fair_value_per_share)

    n = len(results)
    if n == 0:
        return MonteCarloResult(
            iterations_completed=0,
            mean=0.0,
            median=0.0,
            std_dev=0.0,
            p10=0.0,
            p25=0.0,
            p75=0.0,
            p90=0.0,
            histogram=[],
        )

    results.sort()
    mean = sum(results) / n
    variance = sum((v - mean) ** 2 for v in results) / n
    std_dev = math.sqrt(variance)

    def pct(p: float) -> float:
        idx = max(0, min(n - 1, int(p * n)))
        return results[idx]

    return MonteCarloResult(
        iterations_completed=n,
        mean=mean,
        median=pct(0.50),
        std_dev=std_dev,
        p10=pct(0.10),
        p25=pct(0.25),
        p75=pct(0.75),
        p90=pct(0.90),
        histogram=_histogram(results, bins=50),
    )


def _linspace(low: float, high: float, steps: int) -> list[float]:
    if steps <= 1:
        return [low]
    return [low + (high - low) * i / (steps - 1) for i in range(steps)]


def sensitivity_grid(
    company: Company,
    base: Assumptions,
    revenue_growth_min: Optional[float] = None,
    revenue_growth_max: Optional[float] = None,
    revenue_growth_steps: int = 7,
    operating_margin_min: Optional[float] = None,
    operating_margin_max: Optional[float] = None,
    operating_margin_steps: int = 7,
) -> SensitivityGrid:
    """2-D fair-value grid; defaults to base ± 5pp on each axis."""
    rg_min = revenue_growth_min if revenue_growth_min is not None else base.revenue_growth - 0.05
    rg_max = revenue_growth_max if revenue_growth_max is not None else base.revenue_growth + 0.05
    om_min = operating_margin_min if operating_margin_min is not None else base.operating_margin - 0.05
    om_max = operating_margin_max if operating_margin_max is not None else base.operating_margin + 0.05

    rg_axis = _linspace(rg_min, rg_max, revenue_growth_steps)
    om_axis = _linspace(om_min, om_max, operating_margin_steps)

    values: list[list[Optional[float]]] = []
    for rg in rg_axis:
        row: list[Optional[float]] = []
        for om in om_axis:
            a = base.model_copy(update={"revenue_growth": rg, "operating_margin": om})
            try:
                row.append(compute_projection(company, a).fair_value_per_share)
            except (ValueError, ZeroDivisionError):
                row.append(None)
        values.append(row)

    return SensitivityGrid(
        revenue_growth_axis=rg_axis,
        operating_margin_axis=om_axis,
        values=values,
    )
