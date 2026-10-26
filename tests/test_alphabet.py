"""Alphabet carrying-value debt, PPE proxy provenance, and safe repair."""
import asyncio
import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from alphabet import CONCEPTS, VALUATION_NOTE, filing_items, incomplete_inputs, repair_company
from dcf import compute_projection, default_assumptions
from graph import CompositionError, _make_track_b, track_a, validate_company
from industry import Industry
from overrides import apply_override
from repository import InMemoryRepo
from schemas import Company, ExtractionSource

FIXTURES = Path(__file__).parent / "fixtures"
FACTS = json.loads((FIXTURES / "googl-annual-facts.json").read_text())
SAVED = Company.model_validate_json((FIXTURES / "googl-before-input-repair.json").read_text())
LATEST = date(2025, 12, 31)
ACCESSION = "0001652044-26-000018"


def state(facts):
    return {"ticker": "GOOGL", "cik": "0001652044", "company_name": "Alphabet Inc.",
            "facts": facts, "period_end": LATEST, "filing_accession": ACCESSION,
            "filing_url": "https://example.test/googl.htm", "industry": Industry.STANDARD}


def test_latest_carrying_amount_reconciles_and_proxy_never_invents_amortization():
    items = filing_items(FACTS, "1652044", LATEST, ACCESSION, LATEST)
    assert items["long_term_debt"].value == 46_547_000_000
    assert items["short_term_debt"].value == 1_996_000_000
    assert "49085000000" in items["short_term_debt"].source_quote
    assert "-542000000" in items["long_term_debt"].source_quote
    assert items["depreciation_amortization"].value == 21_136_000_000
    assert items["depreciation_amortization"].xbrl_tag == "us-gaap:Depreciation"
    assert "full amortization coverage is unverified" in items["depreciation_amortization"].source_quote


@pytest.mark.parametrize("mutation", ["entity", "unit", "end", "quarter", "form", "accession", "filed", "mixed_filing", "conflict", "nonfinite", "negative", "missing", "duration", "reconciliation"])
def test_unsafe_debt_facts_are_rejected_without_guesses(mutation):
    facts = copy.deepcopy(FACTS)
    if mutation == "entity": facts["cik"] = 51143
    data = facts["facts"]["us-gaap"]["LongTermDebtNoncurrent"]["units"]
    if mutation == "unit": data["EUR"] = data.pop("USD")
    for row in data.get("USD", []):
        if row.get("end") != LATEST.isoformat() or row.get("accn") != ACCESSION: continue
        if mutation == "end": row["end"] = "2025-12-30"
        if mutation == "quarter": row["fp"] = "Q4"
        if mutation == "form": row["form"] = "10-Q"
        if mutation == "accession": row["accn"] = "bad"
        if mutation == "filed": row["filed"] = "2026-03-01"
        if mutation == "mixed_filing": row["accn"] = "0001652044-25-000014"
        if mutation == "nonfinite": row["val"] = "NaN"
        if mutation == "negative": row["val"] = -1
        if mutation == "duration": row["start"] = "2025-01-01"
        if mutation == "reconciliation": row["val"] += 1
    if mutation == "missing": data["USD"] = []
    if mutation == "conflict":
        row = next(r for r in data["USD"] if r["end"] == LATEST.isoformat())
        data["USD"].append({**row, "val": 1})
    assert filing_items(facts, "1652044", LATEST, ACCESSION, LATEST) == {}
    assert repair_company(SAVED, facts) == SAVED


def test_history_uses_same_filing_carrying_basis_and_explicit_legacy_lease_scope():
    repaired = repair_company(SAVED, FACTS)
    assert not incomplete_inputs(repaired)
    assert [p.balance_sheet.long_term_debt.value for p in repaired.periods] == [46_547_000_000, 10_883_000_000, 11_870_000_000, 14_701_000_000]
    assert [p.balance_sheet.short_term_debt.value for p in repaired.periods] == [1_996_000_000, 3_299_000_000, 1_000_000_000, 298_000_000]
    assert "commercial paper 2300000000" in repaired.periods[1].balance_sheet.short_term_debt.source_quote
    assert "0001652044-25-000014" in repaired.periods[2].balance_sheet.long_term_debt.source_quote
    assert "including finance leases" in repaired.periods[3].balance_sheet.long_term_debt.source_quote
    assert "0001652044-24-000022" in repaired.periods[3].balance_sheet.long_term_debt.source_quote
    assert repaired.valuation_note == VALUATION_NOTE
    assert repair_company(repaired, FACTS) == repaired
    for old, new in zip(SAVED.periods, repaired.periods):
        for name in ("income_statement", "cash_flow_statement"):
            assert getattr(old, name).depreciation_amortization.value == getattr(new, name).depreciation_amortization.value
        assert old.balance_sheet.cash_and_equivalents == new.balance_sheet.cash_and_equivalents
        assert old.income_statement.gross_profit == new.income_statement.gross_profit
    assert validate_company(repaired).extraction_flags == SAVED.extraction_flags


def test_future_facts_equal_duplicates_and_unrelated_filers():
    expected = repair_company(SAVED, FACTS)
    facts = copy.deepcopy(FACTS)
    for data in facts["facts"]["us-gaap"].values():
        for rows in data["units"].values():
            rows.extend([{**r, "filed": "2027-02-01", "accn": "0001652044-27-999999", "val": 1} for r in rows.copy()])
            rows *= 2
    assert repair_company(SAVED, facts) == expected
    other = SAVED.model_copy(update={"cik": "789019"})
    assert repair_company(other, FACTS) == other


def test_missing_history_is_atomic_and_bad_depreciation_context_is_rejected():
    facts = copy.deepcopy(FACTS)
    data = facts["facts"]["us-gaap"]["Depreciation"]["units"]
    data["USD"] = [r for r in data["USD"] if r.get("end") != "2022-12-31"]
    assert repair_company(SAVED, facts) == SAVED
    facts = copy.deepcopy(FACTS)
    for row in facts["facts"]["us-gaap"]["Depreciation"]["units"]["USD"]:
        if row.get("end") == LATEST.isoformat(): row["start"] = "2025-10-01"
    assert filing_items(facts, "1652044", LATEST, ACCESSION, LATEST) == {}


@pytest.mark.parametrize("field", ["short_term_debt", "long_term_debt"])
def test_reviewed_debt_pair_and_other_corrections_are_preserved(field):
    c = apply_override(SAVED, "balance_sheet." + field, Decimal(1_000_000_000), "Keep this exact debt review")
    c = apply_override(c, "income_statement.selling_general_administrative", Decimal(40_000_000_000), "Keep SG&A review")
    c = apply_override(c, "cash_flow_statement.depreciation_amortization", Decimal(21_136_000_000), "Keep add-back review")
    updated = repair_company(c, FACTS)
    assert updated.periods[0].balance_sheet == c.periods[0].balance_sheet
    assert updated.periods[0].income_statement.selling_general_administrative == c.periods[0].income_statement.selling_general_administrative
    assert updated.periods[0].cash_flow_statement.depreciation_amortization == c.periods[0].cash_flow_statement.depreciation_amortization
    assert incomplete_inputs(updated)
    assert all(flag.current_value is not None for flag in validate_company(updated).extraction_flags)


def test_model_changes_only_debt_wacc_not_proxy_or_other_assumptions():
    repaired = repair_company(SAVED, FACTS)
    a, b = default_assumptions(SAVED), default_assumptions(repaired)
    assert {k for k in a.model_dump() if a.model_dump()[k] != b.model_dump()[k]} == {"wacc"}
    before, after = compute_projection(SAVED, a), compute_projection(repaired, b)
    assert before.net_debt == 34_331_000_000
    assert after.net_debt == 33_789_000_000
    assert b.da_ratio == a.da_ratio
    assert after.fair_value_per_share == pytest.approx(106.75291078533097)


@pytest.mark.parametrize("missing_component", list(CONCEPTS))
def test_fresh_pipeline_protects_filed_inputs_from_ai_guesses(monkeypatch, missing_component):
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
    assert len(c.periods) == 4
    assert c.periods[0].balance_sheet.long_term_debt.value == 46_547_000_000
    assert c.periods[0].balance_sheet.short_term_debt.value == 1_996_000_000
    assert c.periods[0].cash_flow_statement.depreciation_amortization.value == 21_136_000_000
    assert not {"depreciation_amortization", "long_term_debt", "short_term_debt"}.intersection(seen)
    facts = copy.deepcopy(FACTS);facts["facts"]["us-gaap"].pop(CONCEPTS[missing_component])
    s = state(facts);s.update(asyncio.run(track_a(s)))
    with pytest.raises(CompositionError, match="GOOGL annual"):
        asyncio.run(_make_track_b(Edgar(), None)(s))


@pytest.mark.parametrize("available", [True, False])
def test_cached_api_repair_is_idempotent_and_failed_repair_blocks_valuation(monkeypatch, available):
    from app import main as api
    monkeypatch.setenv("SEC_USER_AGENT", "Valuate tests test@example.com")
    calls = []
    async def facts(self, cik):
        calls.append(cik)
        if not available: raise RuntimeError("SEC unavailable")
        return FACTS
    monkeypatch.setattr(api.EdgarClient, "get_company_facts", facts)
    repo = InMemoryRepo();asyncio.run(repo.set("GOOGL", SAVED))
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_googl_repair_attempts", {})
    client = TestClient(api.app)
    c = Company.model_validate(client.get("/company/GOOGL").json())
    if available:
        assert not incomplete_inputs(c)
        a = client.get("/value/GOOGL/defaults");assert a.status_code == 200
        assert client.post("/value/GOOGL", json={"assumptions": a.json()}).status_code == 200
    else:
        assert c.periods == SAVED.periods
        assert any("coverage is incomplete" in f.reason for f in c.extraction_flags)
        assert client.get("/value/GOOGL/defaults").status_code == 422
        assert client.post("/value/GOOGL", json={"assumptions": default_assumptions(SAVED).model_dump()}).status_code == 422
    client.get("/company/GOOGL")
    assert len(calls) == 1
