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

# CAPM-style cost-of-equity baseline for the standard FCFF path. Approximates
# rf (~4.5%) + beta (~1.0) × ERP (~5%) for a mid-cap industrial. Used as the
# unlevered cost of equity in the WACC calculation; combined with an observed
# cost of debt (interest_expense / total_debt) and the company's actual
# debt-to-capital ratio to produce a per-company WACC default.
DEFAULT_COST_OF_EQUITY = 0.095
DEFAULT_COST_OF_DEBT = 0.05  # fallback when interest expense or debt is missing
DEFAULT_FALLBACK_WACC = 0.09  # used when even the capital-structure approximation can't be computed
WACC_BOUNDS = (0.07, 0.18)  # cap the computed WACC to defensible analyst range — 7% floor catches debt-heavy filers (AAPL, etc.) whose interest-expense / debt understates cost of capital

# Normalized tax rate clipping. Observed effective tax rate (interest expense
# / IBT) can be wonky for filers with R&D credits, foreign mix, one-time
# items. We use the observed rate but clip to the 15-30% band that captures
# the structural rate for most US C-corps after typical deductions/credits.
NORMALIZED_TAX_BOUNDS = (0.15, 0.30)

# Monte Carlo sigma-from-volatility scaling. We derive σ from the standard
# deviation of historical year-over-year ratios (revenue growth, op margin)
# and clip to plausible ranges. Falls back to the prior hardcoded defaults
# when too few historical periods exist.
MC_REVENUE_GROWTH_SIGMA_BOUNDS = (0.005, 0.10)
MC_OPERATING_MARGIN_SIGMA_BOUNDS = (0.005, 0.05)
MC_TERMINAL_GROWTH_SIGMA = 0.005  # WACC and terminal-growth aren't observed; fixed
MC_WACC_SIGMA = 0.005

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

# Energy E&P — 10-year reserve-life-capped FCFF, no terminal value.
# Most pure-play US E&P companies have a proved-reserve life of 8-15 years
# at current production rates; 10 years is a defensible round number for
# the demo and matches what most sell-side NAV models use as the projection
# horizon. The horizon could be a slider, but the simpler the better for a
# demo — and the choice is documented in the help text on the frontend.
ENERGY_PROJECTION_YEARS = 10
DEFAULT_ENERGY_PRODUCTION_GROWTH = -0.02  # mild annual decline (production curve)


def _line_value(line) -> Optional[float]:
    """Pull a float from a LineItem, or None if absent."""
    if line is None:
        return None
    return float(line.value)


def _industry(company: Company) -> Industry:
    if not company.periods:
        return Industry.STANDARD
    return company.periods[0].industry


def _normalize_tax_rate(observed: float) -> float:
    """Clip an observed effective tax rate to the structural-rate band.

    Filers with R&D credits (NVDA), foreign earnings mix (AAPL, MSFT), or
    one-time settlements (JNJ in some years) report observed ETRs that swing
    well outside the structural rate. Clipping to 15-30% lands the default
    closer to the steady-state rate a forward projection should use, without
    ignoring the historical signal entirely.
    """
    return max(NORMALIZED_TAX_BOUNDS[0], min(NORMALIZED_TAX_BOUNDS[1], observed))


def _operating_lease_liabilities(company: Company) -> float:
    """Pull operating-lease liabilities from the latest period's balance sheet.

    Under ASC 842 (effective FY19+), operating leases sit on the balance
    sheet as right-of-use assets and lease liabilities. Real DCFs add lease
    liabilities to net debt because they're contractually committed cash
    outflows. AAPL's are ~$11B; ignored by the prior _net_debt computation.

    The bs.standardized_measure path doesn't apply here — we extract this
    line via XBRL when the filer tags it. Returns 0.0 when the lease
    liability isn't extracted (legacy filers, or non-extracted Optional
    fields), preserving the prior _net_debt behavior as the floor.
    """
    bs = company.periods[0].balance_sheet
    val = _line_value(getattr(bs, "operating_lease_liabilities", None))
    return val if val is not None else 0.0


def _wacc_from_capital_structure(company: Company) -> float:
    """Compute a CAPM-style WACC default from the company's actual capital
    structure: WACC = (E/V) × Re + (D/V) × Rd × (1 − tax_rate).

    Re (cost of equity) is the CAPM-style baseline (rf + β × ERP) at ~9.5%
    — we don't have per-company beta from XBRL, so the baseline holds for
    most large industrials/tech filers. Rd (cost of debt) is interest_expense
    / total_debt observed from the financials, falling back to 5% when
    either is missing or zero. The result is clipped to [5%, 18%] which
    captures the defensible analyst range.

    For filers where the computation can't run (no balance sheet, zero
    debt and equity), falls back to DEFAULT_FALLBACK_WACC. Specifically
    for the standard-industry path; banks/insurers/REITs/E&P use their
    own per-industry cost-of-equity defaults instead.
    """
    bs = company.periods[0].balance_sheet
    is_ = company.periods[0].income_statement

    long_term = _line_value(getattr(bs, "long_term_debt", None)) or 0.0
    short_term = _line_value(getattr(bs, "short_term_debt", None)) or 0.0
    debt = long_term + short_term
    equity = _line_value(getattr(bs, "shareholders_equity", None)) or 0.0
    capital = debt + equity
    if capital <= 0:
        return DEFAULT_FALLBACK_WACC

    interest_expense = _line_value(getattr(is_, "interest_expense", None))
    if interest_expense is not None and debt > 0:
        cost_of_debt = max(0.02, min(0.12, abs(interest_expense) / debt))
    else:
        cost_of_debt = DEFAULT_COST_OF_DEBT

    ibt = _line_value(getattr(is_, "income_before_tax", None))
    tax = _line_value(getattr(is_, "income_tax_expense", None))
    if ibt and ibt > 0 and tax is not None:
        tax_rate = _normalize_tax_rate(tax / ibt)
    else:
        tax_rate = DEFAULT_TAX_RATE

    weight_e = equity / capital
    weight_d = debt / capital
    after_tax_cost_of_debt = cost_of_debt * (1 - tax_rate)
    wacc = weight_e * DEFAULT_COST_OF_EQUITY + weight_d * after_tax_cost_of_debt
    return max(WACC_BOUNDS[0], min(WACC_BOUNDS[1], wacc))


def _sigma_from_series(values: list[float], bounds: tuple[float, float]) -> Optional[float]:
    """Sample standard deviation of a series of ratios, clipped to bounds.

    Used for Monte Carlo σ calibration on revenue growth and op margin.
    Returns None when there aren't enough data points to compute a sample
    σ (need ≥3, since the year-over-year YoY-growth series has length n-1
    and a sample-σ over a length-2 series is degenerate at 0).
    """
    if len(values) < 3:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    sigma = math.sqrt(variance)
    return max(bounds[0], min(bounds[1], sigma))


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

    def _avg(xs: list[float], fallback: float) -> float:
        return sum(xs) / len(xs) if xs else fallback

    avg_roe = _avg(roes, 0.12)

    # Sustainable-growth-rate (SGR) anchor for dividend growth: g = b × ROE
    # where b is the retention ratio (1 − payout). This is the textbook bank
    # DDM convention and what sell-side analysts actually use, vs. the prior
    # naive dividend CAGR that captured one-off post-COVID dividend hikes
    # at JPM/BAC and lifted defaults to non-sustainable rates. The CAGR
    # path is kept as an upper bound for filers with thin payout history.
    payout_ratio = 0.0
    if div_per_share and roes:
        # Latest-year payout = dividends per share / EPS; EPS ≈ ROE × BVPS.
        # We approximate with the latest year's div/share / (NI/share). For
        # the bank universe this is a reasonable proxy.
        latest_ni = _line_value(periods[0].income_statement.net_income)
        latest_shares = _line_value(periods[0].income_statement.diluted_shares_outstanding)
        if latest_ni and latest_shares and latest_shares > 0 and latest_ni > 0:
            eps = latest_ni / latest_shares
            payout_ratio = min(1.0, max(0.0, div_per_share[0] / eps)) if eps > 0 else 0.0

    sgr = (1.0 - payout_ratio) * avg_roe
    max_g = DEFAULT_BANK_COST_OF_EQUITY - 0.02  # leave 200bps headroom under r
    div_growth = max(-0.02, min(max_g, sgr if sgr > 0 else DEFAULT_BANK_DIVIDEND_GROWTH))

    return Assumptions(
        revenue_growth=0.0,
        operating_margin=avg_roe,
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


def _default_energy_assumptions(periods: list) -> Assumptions:
    """Energy E&P NAV-DCF defaults.

    The Assumptions schema is reused as-is; the conceptual differences from
    standard FCFF are:
    - `revenue_growth` is interpreted as production growth/decline (E&P
      companies' "revenue growth" is dominated by volume × commodity-price
      moves, both of which decline as wells deplete absent reinvestment).
      Defaulted to a small negative number if no historical CAGR is
      available — mild decline is the realistic base case for E&P assets
      held without aggressive drilling reinvestment.
    - `terminal_growth` is unused (compute_energy_projection ignores it
      and sets terminal_value = 0; reserves deplete, so Gordon-growth-to-
      infinity produces nonsense for E&P).
    Other ratios behave like the standard path.
    """
    revenues: list[float] = []
    margins: list[float] = []
    capex_ratios: list[float] = []
    da_ratios: list[float] = []
    tax_rates: list[float] = []

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

    production_growth = DEFAULT_ENERGY_PRODUCTION_GROWTH
    if len(revenues) >= 2 and revenues[-1] > 0:
        cagr = (revenues[0] / revenues[-1]) ** (1 / (len(revenues) - 1)) - 1
        # Cap symmetrically — both runaway growth and unrealistic crashes
        # should land somewhere defensible for a slider starting position.
        production_growth = max(-0.15, min(0.15, cagr))

    def _avg(xs: list[float], fallback: float) -> float:
        return sum(xs) / len(xs) if xs else fallback

    return Assumptions(
        revenue_growth=production_growth,
        operating_margin=_avg(margins, 0.20),
        terminal_growth=0.0,  # ignored by compute_energy_projection
        wacc=0.10,  # E&P sector WACC tends to run a bit above industrials given commodity beta
        tax_rate=_avg(tax_rates, DEFAULT_TAX_RATE),
        capex_ratio=_avg(capex_ratios, 0.20),  # E&P capex/revenue is much higher than industrials
        da_ratio=_avg(da_ratios, 0.20),  # depletion is large
        working_capital_ratio=DEFAULT_WORKING_CAPITAL_RATIO,
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
    if industry == Industry.ENERGY:
        return _default_energy_assumptions(periods)

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

    # Tax rate: average observed ETR across the multi-year window, clipped
    # to the 15-30% structural-rate band so credits/foreign mix don't pull
    # the default outside what a forward projection should plan for.
    avg_observed_tax = _avg(tax_rates, DEFAULT_TAX_RATE)
    tax_rate = _normalize_tax_rate(avg_observed_tax)

    # WACC: per-company CAPM-style computation over the actual capital
    # structure, instead of a flat 8% across all filers. Equity-heavy
    # filers like AAPL/NVDA land near 9-10%; debt-heavy industrials lower.
    wacc = _wacc_from_capital_structure(company)

    return Assumptions(
        revenue_growth=revenue_growth,
        operating_margin=_avg(margins, 0.20),
        terminal_growth=0.025,
        wacc=wacc,
        tax_rate=tax_rate,
        capex_ratio=_avg(capex_ratios, 0.04),
        da_ratio=_avg(da_ratios, 0.04),
        working_capital_ratio=DEFAULT_WORKING_CAPITAL_RATIO,
    )


def _net_debt(company: Company) -> float:
    """Total debt − cash, plus operating-lease liabilities (ASC 842).

    Operating leases are contractually committed cash outflows that real
    DCFs include in the EV → equity bridge; AAPL's operating-lease
    liabilities are ~$11B, material enough that ignoring them was a
    real-analyst credibility flag from the senior review.
    """
    bs = company.periods[0].balance_sheet
    long_term = _line_value(bs.long_term_debt) or 0.0
    short_term = _line_value(bs.short_term_debt) or 0.0
    cash = _line_value(bs.cash_and_equivalents) or 0.0
    return long_term + short_term + _operating_lease_liabilities(company) - cash


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


def compute_energy_projection(company: Company, a: Assumptions) -> Projection:
    """Reserve-life-capped FCFF DCF for E&P (no terminal value).

    Standard FCFF math — NOPAT + D&A − Capex − ΔWC each year — projected
    over a 10-year horizon (~typical proved-reserve life for US E&P) and
    summed at the discount rate. The conceptual difference from the
    industrial DCF: terminal_value = 0. Reserves deplete; you can't
    extrapolate FCFF to infinity for an E&P asset, and Gordon-growth-to-
    infinity produces materially wrong (too high) fair values for any
    company whose primary asset is depleting.

    `revenue_growth` here is the production growth/decline rate (the
    frontend relabels the slider). `terminal_growth` is ignored. Other
    ratios (op margin, capex/revenue, D&A/revenue, tax rate, WC) behave
    like the standard path.

    The horizon could be a slider in a future iteration ("reserve life"),
    but a fixed 10-year window matches what most sell-side NAV models use
    for the projection portion and avoids adding a new Assumptions field.
    """
    period = company.periods[0]
    base_revenue = _line_value(period.income_statement.revenue) or 0.0
    if base_revenue <= 0:
        raise ValueError("Base revenue must be positive")

    years: list[YearProjection] = []
    prev_rev = base_revenue
    for t in range(1, ENERGY_PROJECTION_YEARS + 1):
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

    # No terminal value — reserves deplete. The 10-year window captures the
    # productive life; everything past that is a salvage / abandonment story
    # that's deliberately not modeled.
    terminal_value = 0.0
    pv_fcff = sum(y.free_cash_flow / (1 + a.wacc) ** y.year for y in years)
    enterprise_value = pv_fcff

    net_debt = _net_debt(company)
    equity_value = enterprise_value - net_debt
    shares = _diluted_shares(company)
    fair_value_per_share = equity_value / shares if shares > 0 else 0.0

    # SMOG cross-check: if the filing's standardized-measure disclosure was
    # extracted by Track B, surface it as the per-share equivalent. The
    # SMOG is already a PV-10 of proved reserves net of estimated taxes,
    # so we treat it as a direct NAV alternative — equity value, not EV.
    # Doesn't change the primary fair_value_per_share (which is still the
    # 10-year FCFF anchor); just hands the reviewer a sell-side-style
    # PV-10/share number to compare against.
    smog_per_share: Optional[float] = None
    smog_item = _line_value(getattr(period, "standardized_measure", None))
    if smog_item is not None and shares > 0:
        smog_per_share = smog_item / shares

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
        smog_per_share=smog_per_share,
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

    # AFFO is the more conservative cash-earnings number REIT analysts use:
    # FFO minus recurring capex (and sometimes a straight-line-rent
    # adjustment, which we don't model). When the filer reports capex on
    # the cash flow statement, deduct it directly; otherwise fall back to a
    # convention-based haircut. We surface AFFO/share informationally — the
    # primary fair-value math still anchors on FFO/share so existing fair
    # values don't shift, but a REIT reviewer can compare the two.
    cf = period.cash_flow_statement
    capex = _line_value(getattr(cf, "capital_expenditures", None))
    if capex is not None and capex > 0:
        affo = ffo - capex
    else:
        # Industry-convention haircut: ~80% of FFO is a defensible AFFO floor
        # for industrial / data-center REITs. Crude, but better than nothing.
        affo = ffo * 0.80
    affo_per_share = affo / shares if shares > 0 else 0.0

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
        ffo_per_share=ffo_per_share,
        affo_per_share=affo_per_share,
    )


def compute_projection(company: Company, a: Assumptions) -> Projection:
    """Dispatch to industry-appropriate valuation.

    Standard (industrial / tech): 5-year FCFF DCF (Gordon-growth terminal).
    Bank: Dividend Discount Model (Gordon).
    Insurer: Justified P/B model — book value × (ROE−g)/(r−g).
    REIT: FFO-multiple Gordon model — FFO/share × (1+g)/(r−g).
    Energy E&P: 10-year reserve-life-capped FCFF (no terminal value).
    """
    industry = _industry(company)
    if industry == Industry.BANK:
        return compute_bank_projection(company, a)
    if industry == Industry.INSURER:
        return compute_insurer_projection(company, a)
    if industry == Industry.REIT:
        return compute_reit_projection(company, a)
    if industry == Industry.ENERGY:
        return compute_energy_projection(company, a)

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


def _historical_volatility(company: Company) -> tuple[Optional[float], Optional[float]]:
    """Sample standard deviations of revenue growth (YoY) and operating margin
    over the company's historical FinancialPeriod window. Returns (None, None)
    when too few periods exist to compute a sample σ — caller falls back to
    the prior hardcoded defaults in that case.

    Used to calibrate Monte Carlo σ — the prior hardcoded 2% / 2% / 0.5% / 0.5%
    σ values were arbitrary and the same for AAPL (low volatility) as for NVDA
    (very high volatility). Anchoring on observed history makes the p10-p90
    intervals defensible to a research analyst.
    """
    revenues: list[float] = []  # newest-first
    margins: list[float] = []
    for p in company.periods:
        rev = _line_value(p.income_statement.revenue)
        if rev is None or rev <= 0:
            continue
        revenues.append(rev)
        op = _line_value(p.income_statement.operating_income)
        if op is not None:
            margins.append(op / rev)
    # YoY growth needs n+1 revenue observations to produce n growth ratios
    growth_yoy: list[float] = []
    for i in range(len(revenues) - 1):
        if revenues[i + 1] > 0:
            growth_yoy.append((revenues[i] / revenues[i + 1]) - 1.0)
    rg_sigma = _sigma_from_series(growth_yoy, MC_REVENUE_GROWTH_SIGMA_BOUNDS)
    om_sigma = _sigma_from_series(margins, MC_OPERATING_MARGIN_SIGMA_BOUNDS)
    return rg_sigma, om_sigma


def monte_carlo(
    company: Company,
    base: Assumptions,
    iterations: int = 10_000,
    revenue_growth_std: Optional[float] = None,
    operating_margin_std: Optional[float] = None,
    terminal_growth_std: float = MC_TERMINAL_GROWTH_SIGMA,
    wacc_std: float = MC_WACC_SIGMA,
    seed: Optional[int] = None,
) -> MonteCarloResult:
    """Run `iterations` DCFs with the four key drivers sampled from normals.

    The revenue-growth and operating-margin sigmas default to per-company
    historical sample-σ values (YoY growth volatility and margin volatility
    over the available FinancialPeriod window), instead of the prior
    universal 2% / 2% defaults — an SBC-heavy mature filer like KO and a
    high-growth filer like NVDA shouldn't share the same MC envelope.
    Falls back to 2% when fewer than 3 historical observations are available.

    Terminal growth and WACC σ stay at the small fixed values (0.5%) since
    those aren't observed inputs and the user-set slider position is the
    central estimate.

    WACC is clipped to stay strictly above terminal_growth; a sample that
    would invert the relationship gets WACC pulled up to terminal_growth + 1%
    rather than discarded, keeping the iteration count stable.
    """
    if revenue_growth_std is None or operating_margin_std is None:
        rg_obs, om_obs = _historical_volatility(company)
        if revenue_growth_std is None:
            revenue_growth_std = rg_obs if rg_obs is not None else 0.02
        if operating_margin_std is None:
            operating_margin_std = om_obs if om_obs is not None else 0.02
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
