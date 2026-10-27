"""Filing coverage, immutable repair, model effects and API safety for INTU."""
import asyncio
import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dcf import compute_projection, default_assumptions
from graph import CompositionError, _make_track_b, track_a, validate_company
from industry import Industry
from intuit import CONCEPTS, VALUATION_NOTE, filing_items, incomplete_inputs, repair_company
from overrides import apply_override
from repository import InMemoryRepo
from schemas import Company, ExtractionSource

FIXTURES = Path(__file__).parent / "fixtures"
FACTS = json.loads((FIXTURES / "intu-annual-facts.json").read_text())
SAVED = Company.model_validate_json((FIXTURES / "intu-before-input-repair.json").read_text())
LATEST = date(2026, 7, 31)
ACCESSION = "0000896878-26-000037"


def state(facts):
    return {"ticker": "INTU", "cik": "0000896878", "company_name": "Intuit",
            "facts": facts, "period_end": LATEST, "filing_accession": ACCESSION,
            "filing_url": "https://example.test/intu.htm", "industry": Industry.STANDARD}


def test_cashflow_total_uses_full_adjustment_not_other_intangibles_expense():
    items = filing_items(FACTS, "896878", LATEST, ACCESSION, LATEST)
    assert items["depreciation_amortization"].value == 846_000_000
    assert items["long_term_debt"].value == 6_420_000_000
    assert items["short_term_debt"].value == 1_249_000_000
    quote = items["depreciation_amortization"].source_quote
    assert "AdjustmentForAmortization" in quote and "187000000 + 659000000" in quote
    assert "other-intangibles expense component" in quote
    assert items["depreciation_amortization"].source == ExtractionSource.DERIVED


@pytest.mark.parametrize("mutation", ["entity", "unit", "start", "end", "quarter", "form", "accession", "filed", "mixed_filing", "conflict", "nonfinite", "negative", "missing", "debt_duration"])
def test_unsafe_components_never_fall_back_to_partial_or_guessed_inputs(mutation):
    facts = copy.deepcopy(FACTS)
    if mutation == "entity": facts["cik"] = 51143
    field = "LongTermDebtNoncurrent" if mutation == "debt_duration" else CONCEPTS["amortization"]
    data = facts["facts"]["us-gaap"][field]["units"]
    if mutation == "unit": data["EUR"] = data.pop("USD")
    for row in data.get("USD", []):
        if row.get("end") != LATEST.isoformat() or row.get("accn") != ACCESSION: continue
        if mutation == "start": row["start"] = "2026-05-01"
        if mutation == "end": row["end"] = "2026-07-30"
        if mutation == "quarter": row["fp"] = "Q4"
        if mutation == "form": row["form"] = "10-Q"
        if mutation == "accession": row["accn"] = "bad"
        if mutation == "filed": row["filed"] = "2026-10-01"
        if mutation == "mixed_filing": row["accn"] = "0000896878-25-000035"
        if mutation == "nonfinite": row["val"] = "NaN"
        if mutation == "negative": row["val"] = -1
        if mutation == "debt_duration": row["start"] = "2025-08-01"
    if mutation == "missing": data["USD"] = []
    if mutation == "conflict":
        row = next(r for r in data["USD"] if r["end"] == LATEST.isoformat())
        data["USD"].append({**row, "val": 1})
    assert filing_items(facts, "896878", LATEST, ACCESSION, LATEST) == {}


def test_history_is_complete_same_filing_and_not_contaminated_by_future_facts():
    repaired = repair_company(SAVED, FACTS)
    assert not incomplete_inputs(repaired)
    assert [p.cash_flow_statement.depreciation_amortization.value for p in repaired.periods] == [846_000_000, 809_000_000, 789_000_000, 806_000_000, 746_000_000]
    assert [p.balance_sheet.long_term_debt.value for p in repaired.periods] == [6_420_000_000, 5_973_000_000, 5_539_000_000, 6_120_000_000, 6_415_000_000]
    assert repaired.periods[0].balance_sheet.short_term_debt == SAVED.periods[0].balance_sheet.short_term_debt
    for old, new in zip(SAVED.periods, repaired.periods):
        assert new.cash_flow_statement.depreciation_amortization == new.income_statement.depreciation_amortization
        assert new.income_statement.gross_profit == old.income_statement.gross_profit
        assert new.income_statement.selling_general_administrative == old.income_statement.selling_general_administrative
        assert new.balance_sheet.cash_and_equivalents == old.balance_sheet.cash_and_equivalents
        assert new.cash_flow_statement.capital_expenditures == old.cash_flow_statement.capital_expenditures
    assert validate_company(repaired).extraction_flags == SAVED.extraction_flags
    assert repaired.valuation_note == VALUATION_NOTE
    assert repair_company(repaired, FACTS) == repaired
    assert SAVED.periods[0].cash_flow_statement.depreciation_amortization.value == 187_000_000
    facts = copy.deepcopy(FACTS)
    for data in facts["facts"]["us-gaap"].values():
        for rows in data["units"].values():
            rows.extend([{**r, "filed": "2027-09-01", "accn": "0000896878-27-999999", "val": 1} for r in rows.copy()])
    assert repair_company(SAVED, facts) == repaired


def test_equal_duplicates_safe_other_filers_and_reviewed_inputs_untouched():
    facts = copy.deepcopy(FACTS)
    for data in facts["facts"]["us-gaap"].values():
        for rows in data["units"].values(): rows *= 2
    assert repair_company(SAVED, facts) == repair_company(SAVED, FACTS)
    other = SAVED.model_copy(update={"cik": "51143"})
    assert repair_company(other, FACTS) == other
    c = apply_override(SAVED, "cash_flow_statement.depreciation_amortization", Decimal(900_000_000), "Preserved reviewer D&A note")
    c = apply_override(c, "balance_sheet.long_term_debt", Decimal(6_400_000_000), "Preserved reviewer debt note")
    c = apply_override(c, "income_statement.selling_general_administrative", Decimal(7_157_000_000), "Preserved reviewer SG&A note")
    updated = repair_company(c, FACTS)
    for stmt, field in [("cash_flow_statement", "depreciation_amortization"), ("balance_sheet", "long_term_debt"), ("income_statement", "selling_general_administrative")]:
        assert getattr(getattr(updated.periods[0], stmt), field) == getattr(getattr(c.periods[0], stmt), field)


def test_missing_older_component_keeps_entire_saved_history_atomic():
    facts = copy.deepcopy(FACTS)
    data = facts["facts"]["us-gaap"][CONCEPTS["amortization"]]["units"]
    data["USD"] = [r for r in data["USD"] if r.get("end") != "2022-07-31"]
    assert repair_company(SAVED, facts) == SAVED


def test_repaired_inputs_change_only_expected_default_ratios_and_debt_bridge():
    repaired = repair_company(SAVED, FACTS)
    a, b = default_assumptions(SAVED), default_assumptions(repaired)
    assert {k for k in a.model_dump() if a.model_dump()[k] != b.model_dump()[k]} == {"da_ratio", "wacc"}
    before, after = compute_projection(SAVED, a), compute_projection(repaired, b)
    assert before.net_debt == 4_965_000_000
    assert after.net_debt == 3_716_000_000
    assert b.da_ratio == pytest.approx(sum(float(p.cash_flow_statement.depreciation_amortization.value / p.income_statement.revenue.value) for p in repaired.periods) / 5)
    assert b.wacc == pytest.approx(0.0785874874844343)
    assert after.fair_value_per_share == pytest.approx(496.00174556893404)


@pytest.mark.parametrize("missing_component", ["amortization", "short_term_debt", "long_term_debt"])
def test_fresh_pipeline_uses_tagged_inputs_even_if_ai_offers_partial_values(monkeypatch, missing_component):
    import graph
    class Edgar:
        async def get_filing_html(self, url): return "<html>Annual statements</html>"
    seen = []
    async def backfill(**kwargs):
        seen.extend(kwargs["fields_to_extract"])
        return {"depreciation_amortization": SAVED.periods[0].cash_flow_statement.depreciation_amortization,
                "long_term_debt": SAVED.periods[0].balance_sheet.long_term_debt}
    async def segments(**kwargs): return []
    monkeypatch.setattr(graph, "extract_track_b", backfill)
    monkeypatch.setattr(graph, "extract_revenue_segments", segments)
    s = state(FACTS);s.update(asyncio.run(track_a(s)))
    c = asyncio.run(_make_track_b(Edgar(), None)(s))["company"]
    assert len(c.periods) == 5
    assert c.periods[0].cash_flow_statement.depreciation_amortization.value == 846_000_000
    assert c.periods[0].balance_sheet.long_term_debt.value == 6_420_000_000
    assert not {"depreciation_amortization", "long_term_debt", "short_term_debt"}.intersection(seen)
    facts = copy.deepcopy(FACTS);facts["facts"]["us-gaap"].pop(CONCEPTS[missing_component])
    s = state(facts);s.update(asyncio.run(track_a(s)))
    with pytest.raises(CompositionError, match="INTU annual"):
        asyncio.run(_make_track_b(Edgar(), None)(s))


@pytest.mark.parametrize("available", [True, False])
def test_cached_api_repair_is_idempotent_and_fails_closed_without_facts(monkeypatch, available):
    from app import main as api
    monkeypatch.setenv("SEC_USER_AGENT", "Valuate tests test@example.com")
    calls = []
    async def facts(self, cik):
        calls.append(cik)
        if not available: raise RuntimeError("SEC unavailable")
        return FACTS
    monkeypatch.setattr(api.EdgarClient, "get_company_facts", facts)
    repo = InMemoryRepo();asyncio.run(repo.set("INTU", SAVED))
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_intu_repair_attempts", {})
    client = TestClient(api.app)
    c = Company.model_validate(client.get("/company/INTU").json())
    if available:
        assert not incomplete_inputs(c)
        assert c.periods[0].income_statement.gross_profit == SAVED.periods[0].income_statement.gross_profit
        a = client.get("/value/INTU/defaults");assert a.status_code == 422
        assert a.json()["detail"]["code"] == "valuation_not_qualified"
        assert client.post("/value/INTU", json={"assumptions": default_assumptions(c).model_dump()}).status_code == 422
    else:
        assert c.periods == SAVED.periods
        assert any("coverage is incomplete" in f.reason for f in c.extraction_flags)
        assert client.get("/value/INTU/defaults").status_code == 422
        assert client.post("/value/INTU", json={"assumptions": default_assumptions(SAVED).model_dump()}).status_code == 422
    client.get("/company/INTU")
    assert len(calls) == 1
