"""Pinned SEC facts reproduce the three CRM coverage defects and their repair."""
import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dcf import compute_projection, default_assumptions
from graph import track_a, validate_company
from industry import Industry
from overrides import apply_override
from repository import InMemoryRepo
from salesforce import filing_items, mapping_flags, needs_mapping_repair, repair_company
from schemas import Company

FIXTURES = Path(__file__).parent / "fixtures"
FACTS = json.loads((FIXTURES / "salesforce-2026-coverage-facts.json").read_text())


def company():
    return Company.model_validate_json((FIXTURES / "salesforce-reviewed-company.json").read_text())


def test_repairs_all_five_annual_periods_and_preserves_reviewed_sga():
    c = company()
    u = validate_company(repair_company(c, FACTS))
    assert needs_mapping_repair(c) and not needs_mapping_repair(u)
    assert len(mapping_flags(c)) == 2 and u.extraction_flags == []
    assert [p.cash_flow_statement.depreciation_amortization.value for p in u.periods] == [
        3_631_000_000, 3_477_000_000, 3_959_000_000, 3_786_000_000, 3_298_000_000,
    ]
    assert u.periods[0].balance_sheet.long_term_debt.value == 10_439_000_000
    assert u.periods[0].balance_sheet.accounts_payable.value == 8_253_000_000
    assert u.periods[0].balance_sheet.accounts_payable.xbrl_tag == "us-gaap:AccountsPayableAndOtherAccruedLiabilitiesCurrent"
    for before, after in zip(c.periods, u.periods):
        assert before.income_statement.selling_general_administrative == after.income_statement.selling_general_administrative
        for name, changed in [("income_statement", {"depreciation_amortization"}),
                              ("balance_sheet", {"long_term_debt", "accounts_payable"}),
                              ("cash_flow_statement", {"depreciation_amortization"})]:
            x, y = getattr(before, name).model_dump(), getattr(after, name).model_dump()
            assert {k: v for k, v in x.items() if k not in changed} == {k: v for k, v in y.items() if k not in changed}
    assert "0001108524-25-000006" in u.periods[3].cash_flow_statement.depreciation_amortization.source_quote
    assert repair_company(u, FACTS) == u


def test_corrected_valuation_uses_total_da_and_noncurrent_debt_once():
    u = repair_company(company(), FACTS)
    a = default_assumptions(u)
    assert a.da_ratio == pytest.approx(0.10760428456750834)
    assert a.wacc == pytest.approx(0.0840433294143686)
    p = compute_projection(u, a)
    assert p.net_debt == 9_849_000_000
    assert p.fair_value_per_share == pytest.approx(184.28442639481958)


@pytest.mark.parametrize("mutation", ["entity", "unit", "date", "quarter", "start", "accession", "conflict"])
def test_rejects_unsafe_annual_facts(mutation):
    facts = copy.deepcopy(FACTS)
    if mutation == "entity": facts["cik"] = 320193
    for concept, data in facts["facts"]["us-gaap"].items():
        entries = data["units"]["USD"]
        if mutation == "unit": data["units"]["EUR"] = data["units"].pop("USD")
        for e in entries:
            if mutation == "date": e["end"] = "2026-10-31"
            if mutation == "quarter": e["fp"] = "Q3"
            if mutation == "accession": e["accn"] = "0001108524-27-000001"
            if mutation == "start": e["start"] = "2025-11-01"
        if mutation == "conflict": entries.extend([{**e, "val": e["val"] + 1} for e in entries])
    c = company()
    assert repair_company(c, facts) == c
    assert len(validate_company(c).extraction_flags) == 2


def test_repair_never_replaces_reviewer_amount_or_note():
    c = apply_override(company(), "cash_flow_statement.depreciation_amortization", Decimal(123), "reviewed D&A")
    c = apply_override(c, "balance_sheet.long_term_debt", Decimal(456), "reviewed debt")
    u = repair_company(c, FACTS)
    assert u.periods[0].cash_flow_statement.depreciation_amortization == c.periods[0].cash_flow_statement.depreciation_amortization
    assert u.periods[0].balance_sheet.long_term_debt == c.periods[0].balance_sheet.long_term_debt
    other = c.model_copy(update={"cik": "0000320193"})
    assert repair_company(other, FACTS) == other and not needs_mapping_repair(other)


def test_fresh_extraction_selects_the_same_coverage_before_ai():
    c = company();p = c.periods[0]
    result = asyncio.run(track_a({"cik": c.cik, "period_end": p.fiscal_period_end,
        "filing_accession": p.filing_accession, "facts": FACTS, "industry": Industry.STANDARD}))
    items = result["periods_items"][p.fiscal_period_end]
    assert items["long_term_debt"].value == 10_439_000_000
    assert items["depreciation_amortization"].value == 3_631_000_000
    assert items["accounts_payable"].value == 8_253_000_000


def test_cached_read_repairs_once_and_retains_sga_note(monkeypatch):
    from app import main as api
    repo = InMemoryRepo();asyncio.run(repo.set("CRM", company()));calls = []
    class Edgar:
        async def get_company_facts(self, cik):
            calls.append(cik);return FACTS
    monkeypatch.setattr(api, "EdgarClient", Edgar)
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {})
    monkeypatch.setattr(api, "_mapping_repair_attempts", {})
    client = TestClient(api.app)
    r = client.get("/company/CRM")
    assert r.status_code == 200 and r.json()["extraction_flags"] == []
    assert client.get("/company/CRM").json() == r.json() and len(calls) == 1
    assert r.json()["periods"][0]["income_statement"]["selling_general_administrative"] == company().model_dump(mode="json")["periods"][0]["income_statement"]["selling_general_administrative"]


def test_failed_cached_repair_retains_values_and_surfaces_coverage_flags(monkeypatch):
    from app import main as api
    repo = InMemoryRepo();asyncio.run(repo.set("CRM", company()));calls = []
    class Edgar:
        async def get_company_facts(self, cik):
            calls.append(cik);raise RuntimeError("SEC unavailable")
    monkeypatch.setattr(api, "EdgarClient", Edgar)
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {})
    monkeypatch.setattr(api, "_mapping_repair_attempts", {})
    client = TestClient(api.app)
    for _ in range(2):
        assert len(client.get("/company/CRM").json()["extraction_flags"]) == 2
    assert len(calls) == 1 and asyncio.run(repo.get("CRM")) == company()
