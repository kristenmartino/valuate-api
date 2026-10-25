"""Cisco's current maturities are already included in short-term debt."""
import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cisco import debt_flags, filing_debt, needs_debt_repair, repair_debt
from dcf import compute_projection, default_assumptions
from graph import track_a, validate_company
from industry import Industry
from overrides import apply_override
from repository import InMemoryRepo
from schemas import Company

FIXTURES = Path(__file__).parent / "fixtures"
FACTS = json.loads((FIXTURES / "cisco-debt-facts.json").read_text())


def company():
    return Company.model_validate_json((FIXTURES / "cisco-reviewed-company.json").read_text())


def test_repairs_noncurrent_debt_across_history_and_preserves_all_other_fields():
    c = company()
    updated = validate_company(repair_debt(c, FACTS))
    assert needs_debt_repair(c) and not needs_debt_repair(updated)
    assert len(debt_flags(c)) == 1 and updated.extraction_flags == []
    assert [p.balance_sheet.long_term_debt.value for p in updated.periods] == [
        19_372_000_000, 22_861_000_000, 19_621_000_000, 6_658_000_000, 8_416_000_000,
    ]
    for before, after in zip(c.periods, updated.periods):
        x, y = before.model_dump(), after.model_dump()
        x["balance_sheet"].pop("long_term_debt")
        y["balance_sheet"].pop("long_term_debt")
        assert x == y
    assert updated.periods[0].income_statement.selling_general_administrative == c.periods[0].income_statement.selling_general_administrative
    assert "0000858877-26-000132" in updated.periods[0].balance_sheet.long_term_debt.source_quote
    assert repair_debt(updated, FACTS) == updated


def test_corrected_debt_changes_bridge_and_default_wacc_without_changing_other_assumptions():
    c = company()
    u = repair_debt(c, FACTS)
    old_a, new_a = default_assumptions(c), default_assumptions(u)
    assert old_a.model_dump(exclude={"wacc"}) == new_a.model_dump(exclude={"wacc"})
    assert new_a.wacc == pytest.approx(0.07518111670271994)
    old_p = compute_projection(c, old_a)
    fixed_a = compute_projection(u, old_a)
    assert old_p.net_debt - fixed_a.net_debt == 3_500_000_000
    assert old_p.enterprise_value == fixed_a.enterprise_value
    assert fixed_a.equity_value - old_p.equity_value == 3_500_000_000
    new_p = compute_projection(u, new_a)
    assert new_p.net_debt == 23_967_000_000
    assert new_p.fair_value_per_share == pytest.approx(65.87992596364647)


@pytest.mark.parametrize("mutation", ["entity", "unit", "end", "duration", "quarter", "form", "accession", "conflict", "nonfinite"])
def test_rejects_unsafe_latest_filing_facts(mutation):
    facts = copy.deepcopy(FACTS)
    data = facts["facts"]["us-gaap"]["LongTermDebtNoncurrent"]
    if mutation == "entity": facts["cik"] = 320193
    if mutation == "unit": data["units"]["EUR"] = data["units"].pop("USD")
    for e in data["units"].get("USD", []):
        if mutation == "end": e["end"] = "2026-07-24"
        if mutation == "duration": e["start"] = "2025-07-27"
        if mutation == "quarter": e["fp"] = "Q3"
        if mutation == "form": e["form"] = "10-Q"
        if mutation == "accession": e["accn"] = "0000858877-27-000999"
        if mutation == "nonfinite": e["val"] = "NaN"
    if mutation == "conflict":
        e = next(e for e in data["units"]["USD"] if e["end"] == "2026-07-25")
        data["units"]["USD"].append({**e, "val": 123})
    c = company();p = c.periods[0]
    assert filing_debt(facts, c.cik, p.fiscal_period_end, p.filing_accession, p.fiscal_period_end) is None
    assert repair_debt(c, facts).periods[0].balance_sheet.long_term_debt == p.balance_sheet.long_term_debt


def test_equal_duplicate_facts_are_deduplicated():
    facts = copy.deepcopy(FACTS)
    rows = facts["facts"]["us-gaap"]["LongTermDebtNoncurrent"]["units"]["USD"]
    rows.extend(copy.deepcopy(rows))
    assert repair_debt(company(), facts) == repair_debt(company(), FACTS)


def test_reviewer_debt_and_other_filers_are_never_replaced():
    c = apply_override(company(), "balance_sheet.long_term_debt", Decimal(123), "Reviewed debt")
    assert repair_debt(c, FACTS).periods[0].balance_sheet.long_term_debt == c.periods[0].balance_sheet.long_term_debt
    assert debt_flags(c) == []
    other = c.model_copy(update={"cik": "0000320193"})
    assert repair_debt(other, FACTS) == other and not needs_debt_repair(other)


def test_new_extraction_selects_noncurrent_debt_before_fallback():
    c = company();p = c.periods[0]
    result = asyncio.run(track_a({"cik": c.cik, "facts": FACTS, "period_end": p.fiscal_period_end,
                                "filing_accession": p.filing_accession, "industry": Industry.STANDARD}))
    item = result["periods_items"][p.fiscal_period_end]["long_term_debt"]
    assert item.value == 19_372_000_000
    assert item.xbrl_tag == "us-gaap:LongTermDebtNoncurrent"


def test_saved_read_repairs_once_and_preserves_reviewed_sga(monkeypatch):
    from app import main as api
    repo = InMemoryRepo();asyncio.run(repo.set("CSCO", company()));calls = []
    class Edgar:
        async def get_company_facts(self, cik):
            calls.append(cik);return FACTS
    monkeypatch.setattr(api, "EdgarClient", Edgar)
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {})
    monkeypatch.setattr(api, "_mapping_repair_attempts", {})
    client = TestClient(api.app)
    r = client.get("/company/CSCO")
    assert r.status_code == 200 and r.json()["extraction_flags"] == []
    assert client.get("/company/CSCO").json() == r.json() and len(calls) == 1
    assert r.json()["periods"][0]["income_statement"]["selling_general_administrative"] == company().model_dump(mode="json")["periods"][0]["income_statement"]["selling_general_administrative"]


def test_failed_saved_repair_preserves_record_exposes_warning_and_throttles_retry(monkeypatch):
    from app import main as api
    repo = InMemoryRepo();asyncio.run(repo.set("CSCO", company()));calls = []
    class Edgar:
        async def get_company_facts(self, cik):
            calls.append(cik);raise RuntimeError("SEC unavailable")
    monkeypatch.setattr(api, "EdgarClient", Edgar)
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {})
    monkeypatch.setattr(api, "_mapping_repair_attempts", {})
    client = TestClient(api.app)
    for _ in range(2):
        r = client.get("/company/CSCO")
        assert r.status_code == 200 and r.json()["extraction_flags"][0]["field_path"] == "balance_sheet.long_term_debt"
    assert len(calls) == 1 and asyncio.run(repo.get("CSCO")) == company()
