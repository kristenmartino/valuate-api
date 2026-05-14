"""End-to-end integration test against a real SEC 10-K (AAPL).

The 23 unit tests cover XBRL parsing math and per-industry valuation
formulas against synthetic data. None of them exercise the full
ingest → track_a → track_b → validate → compose flow against a real
filing. That's a real gap: a regression in section_extractor's Item 8
anchor regex, a change to SEC's submissions URL shape, a Pydantic
validation drift, or a prompt regression would all ship silently.

This test fills that gap with structural invariants rather than pinned
numbers, since AAPL's revenue / net income / share count change each
fiscal year. What we assert holds across every plausible AAPL 10-K:

  - industry classification (SIC 3571 → STANDARD)
  - schema variant (kind == "standard" on all three statements)
  - required fields populated (revenue, op income, net income, shares all > 0)
  - scale sanity (AAPL is a hundreds-of-billions-revenue, hundreds-of-billions-cap business)
  - balance-sheet identity holds within tolerance
  - default assumptions + projection produce a fair value in a plausible band

Skipped by default — requires SEC_USER_AGENT in the env and outbound
network. Run with:

    SEC_USER_AGENT="Your Name your@email.com" pytest tests/ -m network

A weekly Railway cron should run this. Failures signal a real
production regression: SEC API change, section-extractor breakage,
schema validation drift, or an AAPL filing in an unexpected shape.
"""

import asyncio

import pytest


async def _extract_aapl():
    """Run the full graph against the real SEC API. Returns the composed Company."""
    from anthropic import AsyncAnthropic

    from edgar import EdgarClient
    from graph import build_graph

    edgar = EdgarClient()
    # Dummy key — Track B's call to Claude will fail (401) and the pipeline
    # falls through to derivation. AAPL is fully tagged in XBRL, so this is
    # enough. If Track B becomes required for AAPL, this test will fail in
    # a way that points us to the underlying XBRL change.
    anthropic = AsyncAnthropic(api_key="sk-ant-test-dummy-not-real")
    graph = build_graph(edgar, anthropic)

    state = await graph.ainvoke({"ticker": "AAPL"})
    return state["company"]


@pytest.mark.network
def test_end_to_end_aapl_pipeline_health():
    """ticker AAPL → graph → composed Company → default assumptions → projection.

    Sync wrapper around the async pipeline (avoids the pytest-asyncio dep
    for a single test). Track B fails gracefully on the dummy key; for AAPL,
    Track A alone fills every required field, so composition succeeds.
    """
    from dcf import compute_projection, default_assumptions
    from industry import Industry

    company = asyncio.run(_extract_aapl())

    assert company.ticker == "AAPL"
    assert company.cik == "0000320193"  # AAPL's CIK is permanent
    assert "apple" in company.name.lower()

    # --- 1. Industry classification ------------------------------------------
    period = company.periods[0]
    assert period.industry == Industry.STANDARD, (
        f"AAPL should classify as STANDARD (SIC 3571 — Electronic Computers); "
        f"got {period.industry}"
    )

    # --- 2. Schema variant ---------------------------------------------------
    assert period.income_statement.kind == "standard"
    assert period.balance_sheet.kind == "standard"
    assert period.cash_flow_statement.kind == "standard"

    # --- 3. Required fields populated ----------------------------------------
    is_ = period.income_statement
    bs = period.balance_sheet
    cf = period.cash_flow_statement

    assert float(is_.revenue.value) > 0
    assert float(is_.operating_income.value) > 0
    assert float(is_.net_income.value) > 0
    assert float(is_.diluted_shares_outstanding.value) > 0
    assert float(bs.total_assets.value) > 0
    assert float(bs.total_liabilities.value) > 0
    assert float(bs.shareholders_equity.value) > 0
    assert float(cf.cash_from_operations.value) > 0
    assert float(cf.depreciation_amortization.value) > 0
    assert float(cf.capital_expenditures.value) > 0

    # --- 4. Scale sanity -----------------------------------------------------
    # AAPL has been > $300B revenue, > 14B diluted shares since well before
    # this codebase existed. These bands are wide enough to survive every
    # plausible future AAPL fiscal year without retuning, and narrow enough
    # to catch a "we extracted millions instead of billions" unit-scale bug.
    assert 300_000_000_000 < float(is_.revenue.value) < 1_000_000_000_000
    assert 14_000_000_000 < float(is_.diluted_shares_outstanding.value) < 50_000_000_000
    assert 200_000_000_000 < float(bs.total_assets.value) < 2_000_000_000_000

    # AAPL's margins have been > 25% net every year since FY12.
    net_margin = float(is_.net_income.value) / float(is_.revenue.value)
    assert 0.20 < net_margin < 0.40, f"AAPL net margin out of band: {net_margin:.3f}"

    # --- 5. Balance-sheet identity (mirror of the production validator) -----
    assets = float(bs.total_assets.value)
    expected = float(bs.total_liabilities.value) + float(bs.shareholders_equity.value)
    diff = abs(assets - expected)
    tolerance = assets * 0.005  # 50bps
    assert diff <= tolerance, (
        f"Balance sheet identity off by more than 50bps: "
        f"assets {assets} vs L+E {expected} (diff {diff})"
    )

    # --- 6. Default assumptions + projection produce a plausible fair value -
    a = default_assumptions(company)
    # Defaults should land in a plausible band — not a slider-extreme value.
    assert 0.0 < a.wacc < 0.20
    assert 0.0 < a.tax_rate < 0.40
    assert -0.10 < a.revenue_growth < 0.30
    assert 0.20 < a.operating_margin < 0.40  # AAPL's op margin is ~30%

    proj = compute_projection(company, a)
    assert proj.fair_value_per_share > 0
    # AAPL has traded in roughly the $90-$300 band over the past five years;
    # a defensible DCF should land somewhere in that vicinity. We use a wider
    # band (40-600) to leave headroom for slider defaults that come in
    # conservative or aggressive without making the test brittle.
    assert 40 < proj.fair_value_per_share < 600, (
        f"AAPL fair value out of plausible band: ${proj.fair_value_per_share:.2f}"
    )
    # 5-year FCFF projection, terminal value computed via Gordon growth.
    assert len(proj.years) == 5
    assert proj.terminal_value > 0
