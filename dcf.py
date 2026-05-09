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

from industry import Industry
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

# Bank DDM defaults — used when historicals are too thin to derive.
DEFAULT_BANK_COST_OF_EQUITY = 0.10
DEFAULT_BANK_DIVIDEND_GROWTH = 0.04

# Insurer justified-P/B defaults.
DEFAULT_INSURER_COST_OF_EQUITY = 0.09
DEFAULT_INSURER_GROWTH = 0.03
DEFAULT_INSURER_ROE = 0.10

# REIT FFO-multiple defaults — REITs trade at higher multiples than industrials
# because their cash distributions are large and tax-advantaged. r is set
# below industrial WACC because REIT dividends are pass-through and
# investors price them more like long-duration bond proxies.
DEFAULT_REIT_COST_OF_EQUITY = 0.08
DEFAULT_REIT_FFO_GROWTH = 0.03


def _line_value(line) -> Optional[float]:
    """Pull a float from a LineItem, or None if absent."""
    if line is None:
        return None
    return float(line.value)


def _industry(company: Company) -> Industry:
    if not company.periods:
        return Industry.STANDARD
    return company.periods[0].industry


def _default_bank_assumptions(periods: list) -> Assumptions:
    """Bank DDM-flavored defaults.

    For banks the existing Assumptions fields are repurposed:
    - operating_margin → return on equity (ROE)
    - terminal_growth → long-term dividend growth (g)
    - wacc → cost of equity / required return (r)
    The other ratios (capex, D&A, working capital) aren't used by DDM and
    stay at zero. Tax rate is preserved for informational consistency.

    The frontend dispatches on `period.industry` to relabel the sliders
    accordingly (so users see "Cost of equity" instead of "WACC" etc.).
    """
    roes: list[float] = []
    tax_rates: list[float] = []
    div_per_share: list[float] = []  # newest-first

    for p in periods:
        is_, bs, cf = p.income_statement, p.balance_sheet, p.cash_flow_statement
        ni = _line_value(is_.net_income)
        eq = _line_value(bs.shareholders_equity)
        if ni is not None and eq and eq > 0:
            roes.append(ni / eq)

        ibt = _line_value(is_.income_before_tax)
        tax = _line_value(is_.income_tax_expense)
        if ibt is not None and ibt > 0 and tax is not None:
            tax_rates.append(max(0.0, min(0.5, tax / ibt)))

        div_item = getattr(cf, "dividends_paid", None)
        shares = _line_value(is_.diluted_shares_outstanding)
        if div_item is not None and shares and shares > 0:
            div_value = _line_value(div_item)
            if div_value is not None:
                div_per_share.append(div_value / shares)

    # Dividend CAGR derived from history, but capped well below the default
    # cost of equity so the Gordon constraint (r > g) holds out of the box.
    # Recent post-COVID dividend hikes at major banks hit double digits — not
    # sustainable as terminal growth.
    div_growth = DEFAULT_BANK_DIVIDEND_GROWTH
    if len(div_per_share) >= 2 and div_per_share[-1] > 0:
        cagr = (
            div_per_share[0] / div_per_share[-1]
        ) ** (1 / (len(div_per_share) - 1)) - 1
        max_g = DEFAULT_BANK_COST_OF_EQUITY - 0.02  # leave 200bps headroom
        div_growth = max(-0.02, min(max_g, cagr))

    def _avg(xs: list[float], fallback: float) -> float:
        return sum(xs) / len(xs) if xs else fallback

    return Assumptions(
        revenue_growth=0.0,
        operating_margin=_avg(roes, 0.12),
        terminal_growth=div_growth,
        wacc=DEFAULT_BANK_COST_OF_EQUITY,
        tax_rate=_avg(tax_rates, DEFAULT_TAX_RATE),
        capex_ratio=0.0,
        da_ratio=0.0,
        working_capital_ratio=0.0,
    )


def _default_insurer_assumptions(periods: list) -> Assumptions:
    """Insurer justified-P/B-flavored defaults.

    Same Assumptions schema repurposed slightly differently than banks:
    - operating_margin → return on equity (ROE)
    - terminal_growth → long-term sustainable growth (g)
    - wacc → cost of equity (r)
    No dividend-growth path here — the justified P/B formula uses growth
    of book value, not of dividends.
    """
    roes: list[float] = []
    tax_rates: list[float] = []

    for p in periods:
        is_, bs = p.income_statement, p.balance_sheet
        ni = _line_value(is_.net_income)
        eq = _line_value(bs.shareholders_equity)
        if ni is not None and eq and eq > 0:
            roes.append(ni / eq)

        ibt = _line_value(is_.income_before_tax)
        tax = _line_value(is_.income_tax_expense)
        if ibt is not None and ibt > 0 and tax is not None:
            tax_rates.append(max(0.0, min(0.5, tax / ibt)))

    def _avg(xs: list[float], fallback: float) -> float:
        return sum(xs) / len(xs) if xs else fallback

    return Assumptions(
        revenue_growth=0.0,
        operating_margin=_avg(roes, DEFAULT_INSURER_ROE),
        terminal_growth=DEFAULT_INSURER_GROWTH,
        wacc=DEFAULT_INSURER_COST_OF_EQUITY,
        tax_rate=_avg(tax_rates, DEFAULT_TAX_RATE),
        capex_ratio=0.0,
        da_ratio=0.0,
        working_capital_ratio=0.0,
    )


def _default_reit_assumptions(periods: list) -> Assumptions:
    """REIT FFO-multiple-flavored defaults.

    The Assumptions schema is repurposed in the same pattern as banks /
    insurers:
    - terminal_growth → long-term FFO growth (g)
    - wacc → cost of equity / required return on equity (r)
    operating_margin and the cash-flow ratios aren't consumed by the
    FFO-multiple formula and stay at zero / neutral.

    Long-term FFO growth is anchored on observed period-over-period growth
    (revenue is a reasonable proxy for FFO growth pace at REITs that grow
    primarily through acquisitions + same-store rent inflation), capped
    well below the default cost of equity so the Gordon constraint holds
    out of the box.
    """
    revenues: list[float] = []  # newest-first
    tax_rates: list[float] = []

    for p in periods:
        is_ = p.income_statement
        rev = _line_value(is_.revenue)
        if rev is not None and rev > 0:
            revenues.append(rev)

        ibt = _line_value(getattr(is_, "income_before_tax", None))
        tax = _line_value(getattr(is_, "income_tax_expense", None))
        if ibt is not None and ibt > 0 and tax is not None:
            tax_rates.append(max(0.0, min(0.5, tax / ibt)))

    ffo_growth = DEFAULT_REIT_FFO_GROWTH
    if len(revenues) >= 2 and revenues[-1] > 0:
        cagr = (revenues[0] / revenues[-1]) ** (1 / (len(revenues) - 1)) - 1
        max_g = DEFAULT_REIT_COST_OF_EQUITY - 0.02  # leave 200bps headroom
        ffo_growth = max(-0.02, min(max_g, cagr))

    def _avg(xs: list[float], fallback: float) -> float:
        return sum(xs) / len(xs) if xs else fallback

    return Assumptions(
        revenue_growth=0.0,
        operating_margin=0.0,
        terminal_growth=ffo_growth,
        wacc=DEFAULT_REIT_COST_OF_EQUITY,
        tax_rate=_avg(tax_rates, DEFAULT_TAX_RATE),
        capex_ratio=0.0,
        da_ratio=0.0,
        working_capital_ratio=0.0,
    )


def default_assumptions(company: Company) -> Assumptions:
    """Derive a starting-point Assumptions object from historical actuals.

    Ratios (operating_margin, capex/D&A ratios, tax rate) average over every
    available historical FinancialPeriod with equal weights. Multi-year averages
    are more stable than a single-year snapshot — Apple's FY2025 op margin was
    ~32% but FY2023's was ~30%; a 3-year average is closer to the right point
    for a forward projection. Falls back gracefully when only one period exists.

    Also estimates a starting `revenue_growth` from observed year-over-year
    growth (geometric mean / CAGR) when at least two periods are available, so
    the user opens the workspace with a slider position derived from history
    rather than a hardcoded 5%.

    Forward-looking-only inputs (terminal_growth, WACC) keep neutral defaults.
    """
    periods = company.periods
    if not periods:
        raise ValueError("Company has no FinancialPeriod entries")

    industry = _industry(company)
    if industry == Industry.BANK:
        return _default_bank_assumptions(periods)
    if industry == Industry.INSURER:
        return _default_insurer_assumptions(periods)
    if industry == Industry.REIT:
        return _default_reit_assumptions(periods)

    margins: list[float] = []
    capex_ratios: list[float] = []
    da_ratios: list[float] = []
    tax_rates: list[float] = []
    revenues: list[float] = []  # newest-first, for the CAGR estimate

    for period in periods:
        rev = _line_value(period.income_statement.revenue) or 0.0
        if rev <= 0:
            continue
        revenues.append(rev)

        op = _line_value(period.income_statement.operating_income)
        if op is not None:
            margins.append(op / rev)

        da = _line_value(period.cash_flow_statement.depreciation_amortization)
        if da is not None:
            da_ratios.append(da / rev)

        capex = _line_value(period.cash_flow_statement.capital_expenditures)
        if capex is not None:
            capex_ratios.append(capex / rev)

        ibt = _line_value(period.income_statement.income_before_tax)
        tax = _line_value(period.income_statement.income_tax_expense)
        if ibt and ibt > 0 and tax is not None:
            tax_rates.append(max(0.0, min(0.5, tax / ibt)))

    def _avg(xs: list[float], fallback: float) -> float:
        return sum(xs) / len(xs) if xs else fallback

    # Geometric mean (CAGR) of YoY growth across the window. revenues is
    # newest-first, so revenues[0] is the latest year and revenues[-1] is the
    # oldest. Cap the result so a one-off 50% spike doesn't lock the
    # projection into runaway growth on first render.
    revenue_growth = 0.05
    if len(revenues) >= 2 and revenues[-1] > 0:
        cagr = (revenues[0] / revenues[-1]) ** (1 / (len(revenues) - 1)) - 1
        revenue_growth = max(-0.10, min(0.25, cagr))

    return Assumptions(
        revenue_growth=revenue_growth,
        operating_margin=_avg(margins, 0.20),
        terminal_growth=0.025,
        wacc=0.08,
        tax_rate=_avg(tax_rates, DEFAULT_TAX_RATE),
        capex_ratio=_avg(capex_ratios, 0.04),
        da_ratio=_avg(da_ratios, 0.04),
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


def compute_bank_projection(company: Company, a: Assumptions) -> Projection:
    """Gordon dividend-discount model for banks.

    Uses the same Assumptions schema, with two fields repurposed:
    - `wacc` is interpreted as the cost of equity / required return (r)
    - `terminal_growth` is the long-term dividend growth rate (g)

    Formula: P = D0 × (1 + g) / (r − g), where D0 is current dividends per
    share. Equity value = fair_value_per_share × diluted_shares.

    No 5-year FCFF projection makes sense here, so `years` is empty and the
    response packs the DDM equity value into both `equity_value` and
    `terminal_value`. Frontend detects industry=bank to render appropriately.
    """
    if a.wacc <= a.terminal_growth:
        raise ValueError(
            f"Cost of equity ({a.wacc}) must exceed dividend growth ({a.terminal_growth})"
        )

    period = company.periods[0]
    is_ = period.income_statement  # BankIncomeStatement
    cf = period.cash_flow_statement  # BankCashFlowStatement

    shares = _line_value(is_.diluted_shares_outstanding) or 0.0
    if shares <= 0:
        raise ValueError("Diluted shares must be positive")

    div_paid = _line_value(getattr(cf, "dividends_paid", None)) or 0.0
    div_per_share_now = div_paid / shares

    next_year_div = div_per_share_now * (1 + a.terminal_growth)
    fair_value_per_share = next_year_div / (a.wacc - a.terminal_growth)
    equity_value = fair_value_per_share * shares

    return Projection(
        assumptions=a,
        base_year=period.fiscal_year,
        base_revenue=0.0,  # not applicable for banks
        years=[],  # DDM doesn't project FCFF year by year
        terminal_value=equity_value,
        enterprise_value=equity_value,  # for banks, EV ≈ equity
        net_debt=0.0,
        equity_value=equity_value,
        diluted_shares=shares,
        fair_value_per_share=fair_value_per_share,
    )


def compute_insurer_projection(company: Company, a: Assumptions) -> Projection:
    """Justified-P/B model for insurers.

    Justified P/B = (ROE − g) / (r − g)
    Fair value per share = book_value_per_share × Justified P/B

    where book_value_per_share = shareholders_equity / diluted_shares,
    and Assumptions fields are repurposed as for bank DDM:
    - operating_margin → ROE
    - terminal_growth → long-term growth (g)
    - wacc → cost of equity (r)

    Returns the same Projection shape as the FCFF and DDM paths so the
    frontend's dispatch is purely visual; `years` stays empty (no per-year
    projection in this model).
    """
    if a.wacc <= a.terminal_growth:
        raise ValueError(
            f"Cost of equity ({a.wacc}) must exceed growth rate ({a.terminal_growth})"
        )

    period = company.periods[0]
    is_ = period.income_statement
    bs = period.balance_sheet

    shares = _line_value(is_.diluted_shares_outstanding) or 0.0
    if shares <= 0:
        raise ValueError("Diluted shares must be positive")
    equity = _line_value(bs.shareholders_equity) or 0.0
    if equity <= 0:
        raise ValueError("Shareholders' equity must be positive")

    book_value_per_share = equity / shares
    roe = a.operating_margin
    justified_pb = (roe - a.terminal_growth) / (a.wacc - a.terminal_growth)
    fair_value_per_share = book_value_per_share * justified_pb
    equity_value = fair_value_per_share * shares

    return Projection(
        assumptions=a,
        base_year=period.fiscal_year,
        base_revenue=0.0,
        years=[],
        terminal_value=equity_value,
        enterprise_value=equity_value,
        net_debt=0.0,
        equity_value=equity_value,
        diluted_shares=shares,
        fair_value_per_share=fair_value_per_share,
    )


def compute_reit_projection(company: Company, a: Assumptions) -> Projection:
    """FFO-multiple Gordon-growth model for REITs.

    FFO = Net Income + D&A (the standard REIT cash-earnings adjustment;
    GAAP depreciation overstates economic depreciation for well-maintained
    real estate, so FFO is the conventional pre-distribution earnings
    measure REIT investors anchor on).

    Fair value per share = FFO_per_share × (1 + g) / (r − g)

    where Assumptions fields are repurposed:
    - terminal_growth → long-term FFO growth (g)
    - wacc → cost of equity (r)
    operating_margin and cash-flow ratios aren't used by this formula.

    Returns the same Projection shape as the FCFF / DDM / P/B paths so the
    frontend's dispatch is purely visual; `years` stays empty (no per-year
    projection in this model, mirroring the bank / insurer flavors).
    """
    if a.wacc <= a.terminal_growth:
        raise ValueError(
            f"Cost of equity ({a.wacc}) must exceed FFO growth ({a.terminal_growth})"
        )

    period = company.periods[0]
    is_ = period.income_statement  # REITIncomeStatement

    shares = _line_value(is_.diluted_shares_outstanding) or 0.0
    if shares <= 0:
        raise ValueError("Diluted shares must be positive")

    net_income = _line_value(is_.net_income) or 0.0
    da = _line_value(getattr(is_, "depreciation_amortization", None)) or 0.0
    ffo = net_income + da
    if ffo <= 0:
        raise ValueError("FFO (net income + D&A) must be positive for valuation")

    ffo_per_share = ffo / shares
    fair_value_per_share = ffo_per_share * (1 + a.terminal_growth) / (a.wacc - a.terminal_growth)
    equity_value = fair_value_per_share * shares

    return Projection(
        assumptions=a,
        base_year=period.fiscal_year,
        base_revenue=0.0,  # not applicable for REITs
        years=[],  # FFO multiple doesn't project FCFF year by year
        terminal_value=equity_value,
        enterprise_value=equity_value,  # for REITs we report on equity terms
        net_debt=0.0,
        equity_value=equity_value,
        diluted_shares=shares,
        fair_value_per_share=fair_value_per_share,
    )


def compute_projection(company: Company, a: Assumptions) -> Projection:
    """Dispatch to industry-appropriate valuation.

    Standard (industrial / tech): 5-year FCFF DCF (Gordon-growth terminal).
    Bank: Dividend Discount Model (Gordon).
    Insurer: Justified P/B model — book value × (ROE−g)/(r−g).
    REIT: FFO-multiple Gordon model — FFO/share × (1+g)/(r−g).
    Energy E&P: not yet implemented; falls through to the standard path.
    """
    industry = _industry(company)
    if industry == Industry.BANK:
        return compute_bank_projection(company, a)
    if industry == Industry.INSURER:
        return compute_insurer_projection(company, a)
    if industry == Industry.REIT:
        return compute_reit_projection(company, a)

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
