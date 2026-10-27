"""History-wide source contracts, not ticker-specific repairs."""

import asyncio
import copy
from datetime import date
from decimal import Decimal
import pytest

from history_consistency import validate_history
from reusable_inputs import selection
from schemas import Industry
from share_basis import PATH, reconcile_share_history
from tests.test_share_basis import history


def field(value, tag="Revenues", unit="USD", start="2024-01-01", accn="0000000123-25-000001", proxy=False):
    return selection(Decimal(value), [{"concept": "us-gaap:" + tag, "value": Decimal(value),
        "unit": unit, "start": start, "end": "2024-12-31", "accn": accn,
        "source_url": "https://www.sec.gov/example.htm", "decimals": "INF"}],
        "Consolidated annual source", "Filed primary statement context", proxy=proxy, direct="us-gaap:" + tag)


def monetary(new=100, old=100, **kwargs):
    pe, earlier = date(2024, 12, 31), date(2023, 12, 31)
    path = "income_statement.revenue"
    sources = [({"accession": "0000000123-26-000001"}, {pe: {path: field(new)}}),
               ({"accession": "0000000123-25-000001"}, {pe: {path: field(old, **kwargs)}, earlier: {path: field(80)}})]
    return sources


def test_same_concept_comparative_revision_keeps_newest_without_inventing_a_scale():
    sources = monetary(101, 100)
    report = validate_history(sources, Industry.STANDARD)
    assert not report["issues"] and len(report["revisions"]) == 1
    assert report["revisions"][0]["new_value"] == "101"
    assert sources[1][1][date(2023, 12, 31)]["income_statement.revenue"].item.value == 80


@pytest.mark.parametrize("change", ["scope", "proxy", "unit", "period", "bridge"])
def test_model_history_cannot_cross_an_unreconciled_basis(change):
    sources = monetary(110, 100)
    old = sources[1][1][date(2024, 12, 31)]["income_statement.revenue"]
    if change == "scope": old.rows[0]["concept"] = "test:RevenueComponent"
    if change == "proxy": old.is_proxy = True
    if change == "unit": old.rows[0]["unit"] = "shares"
    if change == "period": old.rows[0]["start"] = "2024-02-01"
    if change == "bridge": sources[1][1].pop(date(2024, 12, 31))
    assert validate_history(sources, Industry.STANDARD)["issues"]


def test_equivalent_primary_subtotal_tags_need_a_matching_overlap():
    sources = monetary(tag="RevenueFromContractWithCustomerExcludingAssessedTax")
    assert not validate_history(sources, Industry.STANDARD)["issues"]


def test_declared_rounding_is_allowed_without_an_arbitrary_percent_threshold():
    sources = monetary(1000000, 1000001)
    for _, periods in sources:
        for items in periods.values():
            for source in items.values(): source.rows[0]["decimals"] = "-3"
    assert validate_history(sources, Industry.STANDARD) == {"issues": [], "revisions": []}


@pytest.mark.parametrize("industry", [Industry.BANK, Industry.INSURER])
def test_parent_roe_cannot_use_consolidated_equity_or_noncontrolling_earnings(industry):
    pe = date(2024, 12, 31)
    items = {"balance_sheet.shareholders_equity": field(100, "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
             "income_statement.net_income": field(10, "ProfitLoss")}
    result = validate_history([({"accession": "test"}, {pe: items})], industry)
    assert {i["path"] for i in result["issues"]} == set(items)


@pytest.mark.parametrize("factor", ["10", "3", "0.1"])
def test_direct_paid_common_dps_uses_inverse_split_basis_with_its_own_bridge(factor):
    inputs, shares, facts = history(factor)
    new, old = shares
    path = "cash_flow_statement.common_dividends_per_share"
    source_periods = []
    for filing, values in shares:
        periods = {pe: {PATH: source} for pe, source in values.items()}
        if filing is new[0]:
            for pe, value in [(date(2025, 12, 31), Decimal("0.4")), (date(2024, 12, 31), Decimal("0.3"))]:
                periods[pe][path] = field(value, "CommonStockDividendsPerShareCashPaid", "USD/shares")
        else:
            for pe, value in [(date(2024, 12, 31), Decimal("0.3") * Decimal(factor)), (date(2023, 12, 31), Decimal("0.2") * Decimal(factor))]:
                periods[pe][path] = field(value, "CommonStockDividendsPerShareCashPaid", "USD/shares")
        source_periods.append((filing, periods))
        for pe, items in periods.items():
            for p, v in items.items(): inputs[pe].setdefault(p, v)
    latest = inputs[date(2025, 12, 31)][path]
    assert reconcile_share_history(inputs, shares, facts, source_periods)
    assert inputs[date(2023, 12, 31)][path].item.value == Decimal("0.2")
    assert inputs[date(2025, 12, 31)][path] is latest
    assert "divided by verified split factor" in inputs[date(2023, 12, 31)][path].item.source_quote


def test_dps_is_not_adjusted_from_share_evidence_without_a_dps_bridge():
    inputs, shares, facts = history()
    path = "cash_flow_statement.common_dividends_per_share"
    periods = [(f, {pe: {PATH: x} for pe, x in s.items()}) for f, s in shares]
    for index, year, value in [(0, 2024, "0.3"), (1, 2023, "2")]:
        pe = date(year, 12, 31)
        periods[index][1][pe][path] = field(value, "CommonStockDividendsPerShareCashPaid", "USD/shares")
        inputs[pe][path] = periods[index][1][pe][path]
    with pytest.raises(ValueError, match="dividend history does not reconcile"):
        reconcile_share_history(inputs, shares, facts, periods)


def test_unpublished_insurer_scope_gap_is_caught_before_any_public_estimate(monkeypatch):
    from eval.reusable_cold import run_fixture
    from fastapi.testclient import TestClient
    from app import main as api
    from repository import InMemoryRepo
    from dcf import default_assumptions
    c, _ = asyncio.run(run_fixture("MET"))
    assert c.qualification.status == "review_required"
    assert any(i.code == "history_basis_unverified" and i.field_path.endswith("shareholders_equity") for i in c.qualification.issues)
    repo = InMemoryRepo(); asyncio.run(repo.set("MET", c))
    monkeypatch.setattr(api, "_repo", repo); monkeypatch.setattr(api, "_company_locks", {})
    def forbidden(*args, **kwargs): raise AssertionError("Unqualified history reached calculation")
    assumptions = default_assumptions(c).model_dump(mode="json")
    monkeypatch.setattr(api, "compute_projection", forbidden)
    client = TestClient(api.app)
    assert client.get("/value/MET/defaults").status_code == 422
    assert client.post("/value/MET", json={"assumptions": assumptions}).status_code == 422


def test_bank_growth_provenance_is_sustainable_growth_not_historical_dividend_cagr():
    from eval.reusable_cold import run_fixture
    from dcf import default_assumptions
    c, _ = asyncio.run(run_fixture("BAC"))
    actual = default_assumptions(c)
    p = c.periods[0]
    roe = sum(float(x.income_statement.net_income.value / x.balance_sheet.shareholders_equity.value) for x in c.periods) / len(c.periods)
    eps = float(p.income_statement.net_income.value / p.income_statement.diluted_shares_outstanding.value)
    payout = min(1, max(0, float(p.cash_flow_statement.common_dividends_per_share.value) / eps))
    sustainable = (1-payout) * roe
    expected = max(-0.02, min(0.08, sustainable if sustainable > 0 else 0.04))
    assert actual.terminal_growth == pytest.approx(expected)
    different_history = c.model_copy(deep=True)
    for old in different_history.periods[1:]: old.cash_flow_statement.common_dividends_per_share.value *= 100
    assert default_assumptions(different_history).terminal_growth == actual.terminal_growth
