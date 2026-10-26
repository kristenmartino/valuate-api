"""Filing coverage, immutable repair, model effects and API safety for MSFT."""
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
from microsoft import CONCEPTS, VALUATION_NOTE, filing_items, incomplete_inputs, repair_company
from overrides import apply_override
from repository import InMemoryRepo
from schemas import Company, ExtractionSource

FIXTURES = Path(__file__).parent / "fixtures"
FACTS = json.loads((FIXTURES / "msft-annual-facts.json").read_text())
SAVED = Company.model_validate_json((FIXTURES / "msft-before-input-repair.json").read_text())
LATEST = date(2026, 6, 30)
ACCESSION = "0001193125-26-323660"


def state(facts):
    return {"ticker": "MSFT", "cik": "0000789019", "company_name": "Microsoft",
            "facts": facts, "period_end": LATEST, "filing_accession": ACCESSION,
            "filing_url": "https://example.test/msft.htm", "industry": Industry.STANDARD}


def test_pure_da_sums_non_overlapping_expenses_not_combined_cashflow_row():
    items = filing_items(FACTS, "789019", LATEST, ACCESSION, LATEST)
    assert items["depreciation_amortization"].value == 39_000_000_000
    assert items["long_term_debt"].value == 31_067_000_000
    assert items["short_term_debt"].value == 9_227_000_000
    quote = items["depreciation_amortization"].source_quote
    assert "AmortizationOfIntangibleAssets" in quote and "34300000000 + 4700000000" in quote
    assert "not substituted for this pure D&A sum" in quote
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
        if mutation == "end": row["end"] = "2026-06-29"
        if mutation == "quarter": row["fp"] = "Q4"
        if mutation == "form": row["form"] = "10-Q"
        if mutation == "accession": row["accn"] = "bad"
        if mutation == "filed": row["filed"] = "2026-10-01"
        if mutation == "mixed_filing": row["accn"] = "0000950170-25-100235"
        if mutation == "nonfinite": row["val"] = "NaN"
        if mutation == "negative": row["val"] = -1
        if mutation == "debt_duration": row["start"] = "2025-07-01"
    if mutation == "missing": data["USD"] = []
    if mutation == "conflict":
        row = next(r for r in data["USD"] if r["end"] == LATEST.isoformat())
        data["USD"].append({**row, "val": 1})
    assert filing_items(facts, "789019", LATEST, ACCESSION, LATEST) == {}


def test_history_is_complete_same_filing_and_not_contaminated_by_future_facts():
    repaired = repair_company(SAVED, FACTS)
    assert not incomplete_inputs(repaired)
    assert [p.cash_flow_statement.depreciation_amortization.value for p in repaired.periods] == [39_000_000_000, 28_000_000_000, 20_000_000_000, 13_500_000_000, 14_600_000_000]
    assert [p.balance_sheet.long_term_debt.value for p in repaired.periods] == [31_067_000_000, 40_152_000_000, 42_688_000_000, 41_990_000_000, 47_032_000_000]
    assert [p.balance_sheet.short_term_debt.value for p in repaired.periods] == [9_227_000_000, 2_999_000_000, 2_249_000_000, 5_247_000_000, 2_749_000_000]
    for old, new in zip(SAVED.periods, repaired.periods):
        assert new.cash_flow_statement.depreciation_amortization == new.income_statement.depreciation_amortization
        assert new.income_statement.gross_profit == old.income_statement.gross_profit
        assert new.income_statement.selling_general_administrative == old.income_statement.selling_general_administrative
        assert new.balance_sheet.cash_and_equivalents == old.balance_sheet.cash_and_equivalents
        assert new.cash_flow_statement.capital_expenditures == old.cash_flow_statement.capital_expenditures
    assert validate_company(repaired).extraction_flags == SAVED.extraction_flags
    assert repaired.valuation_note == VALUATION_NOTE
    assert repair_company(repaired, FACTS) == repaired
    assert SAVED.periods[0].cash_flow_statement.depreciation_amortization.value == 34_300_000_000
    facts = copy.deepcopy(FACTS)
    for data in facts["facts"]["us-gaap"].values():
        for rows in data["units"].values():
            rows.extend([{**r, "filed": "2027-09-01", "accn": "0000789019-27-999999", "val": 1} for r in rows.copy()])
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
    data["USD"] = [r for r in data["USD"] if r.get("end") != "2022-06-30"]
    assert repair_company(SAVED, facts) == SAVED


def test_repaired_inputs_change_only_expected_default_ratios_and_debt_bridge():
    repaired = repair_company(SAVED, FACTS)
    a, b = default_assumptions(SAVED), default_assumptions(repaired)
    assert {k for k in a.model_dump() if a.model_dump()[k] != b.model_dump()[k]} == {"da_ratio"}
    before, after = compute_projection(SAVED, a), compute_projection(repaired, b)
    assert before.net_debt == 41_284_000_000
    assert after.net_debt == 41_284_000_000
    assert b.da_ratio == pytest.approx(sum(float(p.cash_flow_statement.depreciation_amortization.value / p.income_statement.revenue.value) for p in repaired.periods) / 5)
    assert b.wacc == a.wacc
    assert b.da_ratio == pytest.approx(0.08716973941789623)
    assert after.fair_value_per_share == pytest.approx(248.2311000066803)


@pytest.mark.parametrize("missing_component", ["depreciation", "amortization", "short_term_debt", "long_term_debt", "total_debt"])
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
    assert c.periods[0].cash_flow_statement.depreciation_amortization.value == 39_000_000_000
    assert c.periods[0].balance_sheet.long_term_debt.value == 31_067_000_000
    assert not {"depreciation_amortization", "long_term_debt", "short_term_debt"}.intersection(seen)
    facts = copy.deepcopy(FACTS);facts["facts"]["us-gaap"].pop(CONCEPTS[missing_component])
    s = state(facts);s.update(asyncio.run(track_a(s)))
    with pytest.raises(CompositionError, match="MSFT annual"):
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
    repo = InMemoryRepo();asyncio.run(repo.set("MSFT", SAVED))
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_msft_repair_attempts", {})
    client = TestClient(api.app)
    c = Company.model_validate(client.get("/company/MSFT").json())
    if available:
        assert not incomplete_inputs(c)
        assert c.periods[0].income_statement.gross_profit == SAVED.periods[0].income_statement.gross_profit
        a = client.get("/value/MSFT/defaults");assert a.status_code == 200
        assert client.post("/value/MSFT", json={"assumptions": a.json()}).status_code == 200
    else:
        assert c.periods == SAVED.periods
        assert any("coverage is incomplete" in f.reason for f in c.extraction_flags)
        assert client.get("/value/MSFT/defaults").status_code == 422
        assert client.post("/value/MSFT", json={"assumptions": default_assumptions(SAVED).model_dump()}).status_code == 422
    client.get("/company/MSFT")
    assert len(calls) == 1


def test_incoherent_debt_split_rejects_all_inputs_and_saved_repair():
    facts = copy.deepcopy(FACTS)
    for row in facts["facts"]["us-gaap"][CONCEPTS["total_debt"]]["units"]["USD"]:
        if row.get("end") == LATEST.isoformat() and row.get("accn") == ACCESSION:
            row["val"] += 1
    assert filing_items(facts, "789019", LATEST, ACCESSION, LATEST) == {}
    assert repair_company(SAVED, facts) == SAVED


@pytest.mark.parametrize("field", ["short_term_debt", "long_term_debt"])
def test_reviewed_debt_pair_is_never_partially_changed_or_double_counted(field):
    c = apply_override(SAVED, "balance_sheet." + field, Decimal(1_000_000_000), "Retain this exact reviewer note")
    updated = repair_company(c, FACTS)
    assert updated.periods[0].balance_sheet == c.periods[0].balance_sheet
    assert incomplete_inputs(updated)  # Requires an explicit review; never fill the paired slot automatically.
    flags = validate_company(updated).extraction_flags
    assert any("coverage is incomplete" in flag.reason for flag in flags)
    assert all(flag.current_value is not None for flag in flags)


def test_already_correct_noncurrent_field_keeps_its_source_object():
    selected = filing_items(FACTS, "789019", LATEST, ACCESSION, LATEST)["long_term_debt"]
    selected = selected.model_copy(update={"source_quote": "Retain an already-correct source note"})
    p = SAVED.periods[0]
    c = SAVED.model_copy(update={"periods": [p.model_copy(update={"balance_sheet": p.balance_sheet.model_copy(update={"long_term_debt": selected})}), *SAVED.periods[1:]]})
    result = repair_company(c, FACTS).periods[0].balance_sheet
    assert result.long_term_debt == selected
    assert result.short_term_debt.value == 9_227_000_000
