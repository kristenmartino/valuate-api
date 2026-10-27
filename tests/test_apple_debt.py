"""Apple's current term debt is already included in its short-term debt slot."""
import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from debt import debt_flags, filing_debt, needs_debt_repair, repair_debt
from dcf import compute_projection, default_assumptions
from graph import track_a, validate_company
from industry import Industry
from overrides import apply_override
from repository import InMemoryRepo
from schemas import Company

FIXTURES = Path(__file__).parent / "fixtures"
FACTS = json.loads((FIXTURES / "apple-debt-facts.json").read_text())


def company():
    return Company.model_validate_json((FIXTURES / "apple-saved-company.json").read_text())


def test_apple_history_repair_changes_only_inclusive_debt_and_preserves_other_fields():
    c = company();u = validate_company(repair_debt(c, FACTS))
    assert needs_debt_repair(c) and not needs_debt_repair(u) and u.extraction_flags == []
    assert [p.balance_sheet.long_term_debt.value for p in u.periods] == [
        78_328_000_000, 85_750_000_000, 95_281_000_000, 98_959_000_000, 109_106_000_000,
    ]
    for before, after in zip(c.periods, u.periods):
        x, y = before.model_dump(), after.model_dump()
        x["balance_sheet"].pop("long_term_debt");y["balance_sheet"].pop("long_term_debt")
        assert x == y
        if before.balance_sheet.long_term_debt.xbrl_tag != "us-gaap:LongTermDebt":
            assert before.balance_sheet.long_term_debt == after.balance_sheet.long_term_debt
    assert "0000320193-25-000079" in u.periods[0].balance_sheet.long_term_debt.source_quote
    assert repair_debt(u, FACTS) == u


def test_apple_default_valuation_removes_12350m_duplicate_without_changing_assumptions():
    c = company();u = repair_debt(c, FACTS);a = default_assumptions(c);b = default_assumptions(u)
    # The WACC calculation is rerun but remains at the existing 7% floor.
    assert a == b and b.wacc == 0.07
    old, new = compute_projection(c, a), compute_projection(u, b)
    assert old.net_debt == 87_563_000_000 and new.net_debt == 75_213_000_000
    assert new.enterprise_value == old.enterprise_value
    assert new.equity_value - old.equity_value == 12_350_000_000
    assert new.fair_value_per_share == pytest.approx(164.00397907554068)


@pytest.mark.parametrize("mutation", ["entity", "unit", "end", "duration", "quarter", "form", "accession", "conflict", "nonfinite"])
def test_apple_unsafe_facts_do_not_replace_saved_latest_debt(mutation):
    facts = copy.deepcopy(FACTS);data = facts["facts"]["us-gaap"]["LongTermDebtNoncurrent"]
    if mutation == "entity": facts["cik"] = 858877  # Supported filer, wrong company.
    if mutation == "unit": data["units"]["EUR"] = data["units"].pop("USD")
    for e in data["units"].get("USD", []):
        if mutation == "end": e["end"] = "2025-09-26"
        if mutation == "duration": e["start"] = "2024-09-29"
        if mutation == "quarter": e["fp"] = "Q3"
        if mutation == "form": e["form"] = "10-Q"
        if mutation == "accession": e["accn"] = "0000320193-26-999999"
        if mutation == "nonfinite": e["val"] = "NaN"
    if mutation == "conflict":
        row = next(e for e in data["units"]["USD"] if e["end"] == "2025-09-27")
        data["units"]["USD"].append({**row, "val": 1})
    c = company();p = c.periods[0]
    assert filing_debt(facts, c.cik, p.fiscal_period_end, p.filing_accession, p.fiscal_period_end) is None
    assert repair_debt(c, facts).periods[0].balance_sheet.long_term_debt == p.balance_sheet.long_term_debt


def test_apple_reviewer_debt_and_sga_notes_are_preserved():
    c = apply_override(company(), "income_statement.selling_general_administrative", Decimal(27_601_000_000), "Reviewed SG&A test note")
    u = repair_debt(c, FACTS)
    assert u.periods[0].income_statement == c.periods[0].income_statement
    c = apply_override(c, "balance_sheet.long_term_debt", Decimal(78_328_000_000), "Reviewed noncurrent debt test note")
    assert repair_debt(c, FACTS).periods[0].balance_sheet.long_term_debt == c.periods[0].balance_sheet.long_term_debt
    assert debt_flags(c) == []


def test_apple_equal_duplicates_are_safe_and_unsupported_companies_unchanged():
    facts = copy.deepcopy(FACTS);rows = facts["facts"]["us-gaap"]["LongTermDebtNoncurrent"]["units"]["USD"]
    rows.extend(copy.deepcopy(rows))
    assert repair_debt(company(), facts) == repair_debt(company(), FACTS)
    other = company().model_copy(update={"cik": "1326801"})
    assert repair_debt(other, FACTS) == other and not needs_debt_repair(other) and debt_flags(other) == []


def test_apple_fresh_extraction_selects_noncurrent_before_ai():
    c = company();p = c.periods[0]
    result = asyncio.run(track_a({"cik": c.cik, "facts": FACTS, "period_end": p.fiscal_period_end,
                                "filing_accession": p.filing_accession, "industry": Industry.STANDARD}))
    item = result["periods_items"][p.fiscal_period_end]["long_term_debt"]
    assert item.value == 78_328_000_000 and item.xbrl_tag == "us-gaap:LongTermDebtNoncurrent"


def test_apple_saved_read_repairs_once_and_value_endpoint_uses_the_repair(monkeypatch):
    from app import main as api
    repo = InMemoryRepo();asyncio.run(repo.set("AAPL", company()));calls = []
    class Edgar:
        async def get_company_facts(self, cik): calls.append(cik);return FACTS
    monkeypatch.setattr(api, "EdgarClient", Edgar);monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {});monkeypatch.setattr(api, "_mapping_repair_attempts", {})
    client = TestClient(api.app);first = client.get("/company/AAPL")
    assert first.status_code == 200 and first.json()["extraction_flags"] == []
    assert client.get("/company/AAPL").json() == first.json() and len(calls) == 1
    assert client.get("/value/AAPL/defaults").status_code == 422
    repaired = Company.model_validate(first.json())
    defaults = default_assumptions(repaired)
    result = client.post("/value/AAPL", json={"assumptions": defaults.model_dump()})
    assert result.status_code == 422 and result.json()["detail"]["code"] == "valuation_not_qualified"
    assert compute_projection(repaired, defaults).fair_value_per_share == pytest.approx(164.00397907554068)


def test_apple_failed_repair_retains_values_and_warning_and_throttles_retry(monkeypatch):
    from app import main as api
    repo = InMemoryRepo();asyncio.run(repo.set("AAPL", company()));calls = []
    class Edgar:
        async def get_company_facts(self, cik): calls.append(cik);raise RuntimeError("SEC unavailable")
    monkeypatch.setattr(api, "EdgarClient", Edgar);monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {});monkeypatch.setattr(api, "_mapping_repair_attempts", {})
    client = TestClient(api.app)
    for _ in range(2):
        result = client.get("/company/AAPL")
        assert result.status_code == 200 and result.json()["extraction_flags"][0]["field_path"] == "balance_sheet.long_term_debt"
    assert len(calls) == 1 and asyncio.run(repo.get("AAPL")) == company()
