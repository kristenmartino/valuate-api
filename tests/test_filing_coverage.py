import asyncio
import copy
from decimal import Decimal

import pytest

from eval.cold import FixtureEdgar, run_fixture
from filing_coverage import repair_company, selections
from overrides import apply_override
from qualification import qualify
from qualification_service import qualify_record
from tests.test_qualification import api_client

TICKERS = ["ADBE", "CRM", "EOG", "PEP", "TXN"]
EXPECTED = {
    "ADBE": (6210000000, 0),
    "CRM": (10439000000, 4000000000),
    "EOG": (7909000000, 27000000),
    "PEP": (42321000000, 6861000000),
    "TXN": (13548000000, 500000000),
}


@pytest.mark.parametrize("ticker", TICKERS)
def test_repaired_and_cold_inputs_share_one_filing_contract(ticker):
    fixture = FixtureEdgar(ticker)
    repaired = repair_company(fixture.saved, fixture.facts)
    p = repaired.periods[0]
    assert (p.balance_sheet.long_term_debt.value, p.balance_sheet.short_term_debt.value) == EXPECTED[ticker]
    q = qualify(repaired, fixture.facts)
    assert q.status == "qualified", q.issues
    assert all(i.operands for i in q.inputs if i.field_path.endswith("debt"))
    cold, _ = asyncio.run(run_fixture(ticker))
    assert cold.qualification.status == "qualified"
    assert len(cold.periods) == 5
    assert [p.cash_flow_statement.depreciation_amortization.value for p in cold.periods] == [p.cash_flow_statement.depreciation_amortization.value for p in repaired.periods]
    # All original non-target fields and saved review objects are exact.
    old = fixture.saved.model_dump(mode="json", exclude={"qualification", "valuation_note"})
    new = repaired.model_dump(mode="json", exclude={"qualification", "valuation_note"})
    for obj in [old, new]:
        for p in obj["periods"]:
            for field in ["short_term_debt", "long_term_debt"]:
                p["balance_sheet"].pop(field)
            if ticker in ["ADBE", "PEP", "TXN"]:
                p["income_statement"].pop("depreciation_amortization")
                p["cash_flow_statement"].pop("depreciation_amortization")
    assert old == new


def test_complete_txn_addbacks_and_pepsico_cashflow_scope():
    tx = FixtureEdgar("TXN")
    c = repair_company(tx.saved, tx.facts)
    assert [p.cash_flow_statement.depreciation_amortization.value for p in c.periods] == [1999000000, 1580000000, 1238000000, 979000000, 954000000]
    last = selections(tx.facts, c.cik, c.periods[-1].fiscal_period_end, c.periods[0].filing_accession, c.periods[0].fiscal_period_end)["depreciation_amortization"]
    assert len(last.rows) == 3 and {r["value"] for r in last.rows} == {Decimal(755000000), Decimal(57000000), Decimal(142000000)}
    pep = FixtureEdgar("PEP")
    c = repair_company(pep.saved, pep.facts)
    assert [p.cash_flow_statement.depreciation_amortization.value for p in c.periods] == [p.cash_flow_statement.depreciation_amortization.value for p in pep.saved.periods]
    assert "excluding separately shown intangible" in c.periods[-1].cash_flow_statement.depreciation_amortization.source_quote


@pytest.mark.parametrize("ticker,tag", [("ADBE", "DebtCurrent"), ("CRM", "LongTermDebtCurrent"), ("EOG", "LongTermDebtCurrent"), ("PEP", "DepreciationDepletionAndAmortization"), ("TXN", "CapitalizedComputerSoftwareAmortization1")])
@pytest.mark.parametrize("mutation", ["missing", "conflict", "unit", "quarter", "future", "entity", "duration", "negative", "nonfinite", "accession"])
def test_missing_or_unsafe_scope_evidence_never_qualifies(ticker, tag, mutation):
    f = FixtureEdgar(ticker)
    c = repair_company(f.saved, f.facts)
    facts = copy.deepcopy(f.facts)
    latest = c.periods[0]
    data = facts["facts"]["us-gaap"][tag]["units"]
    rows = data["USD"]
    target = [r for r in rows if r["end"] == str(latest.fiscal_period_end) and r["accn"] == latest.filing_accession]
    assert target
    if mutation == "missing":
        data["USD"] = [r for r in rows if r not in target]
    elif mutation == "unit":
        data["EUR"] = data.pop("USD")
    elif mutation == "conflict":
        rows.append({**target[0], "val": 1})
    elif mutation == "entity":
        facts["cik"] = 999999
    else:
        key, value = {"quarter": ("fp", "Q4"), "future": ("filed", "2030-01-01"), "duration": ("start", str(latest.fiscal_period_end)), "negative": ("val", -1), "nonfinite": ("val", "NaN"), "accession": ("accn", "0000000001-26-000001")}[mutation]
        for r in target:
            r[key] = value
    q = qualify(c, facts)
    assert q.status == "review_required" and q.issues
    with pytest.raises(ValueError):
        repair_company(f.saved, facts)


def test_missing_historical_txn_acquisition_amortization_is_not_zero_or_dropped():
    f = FixtureEdgar("TXN")
    facts = copy.deepcopy(f.facts)
    facts["facts"]["us-gaap"]["AmortizationOfIntangibleAssets"]["units"]["USD"] = []
    before = f.saved.model_dump_json()
    with pytest.raises(ValueError):
        repair_company(f.saved, facts)
    assert f.saved.model_dump_json() == before
    c = repair_company(f.saved, f.facts)
    assert qualify(c, facts).status == "review_required"


@pytest.mark.parametrize("ticker", ["CRM", "TXN"])
def test_incoherent_debt_total_is_rejected(ticker):
    f = FixtureEdgar(ticker)
    facts = copy.deepcopy(f.facts)
    p = f.saved.periods[0]
    for r in facts["facts"]["us-gaap"]["LongTermDebt"]["units"]["USD"]:
        if r["accn"] == p.filing_accession and r["end"] == str(p.fiscal_period_end):
            r["val"] += 1
    with pytest.raises(ValueError):
        repair_company(f.saved, facts)


def test_reviewed_debt_pair_is_preserved_but_a_conflicting_amount_stays_withheld():
    f = FixtureEdgar("CRM")
    reviewed = apply_override(f.saved, "balance_sheet.long_term_debt", Decimal(1), "Do not overwrite this review")
    c = repair_company(reviewed, f.facts)
    assert c.periods[0].balance_sheet.long_term_debt == reviewed.periods[0].balance_sheet.long_term_debt
    assert c.periods[0].balance_sheet.short_term_debt == reviewed.periods[0].balance_sheet.short_term_debt
    assert qualify(c, f.facts).status == "review_required"
    assert repair_company(c, f.facts).valuation_note == c.valuation_note


@pytest.mark.parametrize("broken", [False, True])
def test_admin_repair_is_source_only_qualified_and_atomic(monkeypatch, broken):
    from app import main as api
    f = FixtureEdgar("TXN")
    original = f.saved
    client, repo = api_client(monkeypatch, original)
    if broken:
        f.facts["facts"]["us-gaap"]["CapitalizedComputerSoftwareAmortization1"]["units"]["USD"] = []
    monkeypatch.setattr(api, "EdgarClient", lambda: f)
    async def checked(company, facts):
        return await qualify_record(company, client=f, facts=facts)
    monkeypatch.setattr(api, "qualify_record", checked)
    response = client.post("/admin/repair-coverage/TXN", headers={"Authorization": "Bearer qualification-unit-test-token"}, json={})
    assert response.status_code == (422 if broken else 200)
    active = asyncio.run(repo.get("TXN"))
    if broken:
        assert active == original
    else:
        assert active.qualification.status == "qualified"
        assert client.get("/value/TXN/defaults").status_code == 200


def test_cold_graph_does_not_ask_ai_to_replace_missing_txn_software(monkeypatch):
    import eval.cold as cold
    from graph import CompositionError
    f = FixtureEdgar("TXN")
    f.facts["facts"]["us-gaap"]["CapitalizedComputerSoftwareAmortization1"]["units"]["USD"] = []
    monkeypatch.setattr(cold, "FixtureEdgar", lambda ticker: f)
    with pytest.raises(CompositionError, match="AI fallback"):
        asyncio.run(cold.run_fixture("TXN"))
