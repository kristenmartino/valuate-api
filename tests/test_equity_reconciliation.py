"""Prologis filing regression: parent equity excludes noncontrolling interests.

The SEC-sourced instant facts are pinned to accession 0001193125-26-051453.
The old saved-company fixture reproduces the live false warning and tests
that filling a missing component preserves every existing value and result.
"""
import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dcf import compute_projection, default_assumptions, monte_carlo
from equity import (
    PARENT_EQUITY_TAG, TOTAL_EQUITY_TAG, backfill_noncontrolling_interests,
    filing_noncontrolling_interests, needs_equity_backfill, reconciliation_equity,
)
from graph import _balance_sheet_check, _derive_missing_required, validate_company
from industry import Industry
from overrides import apply_override
from repository import InMemoryRepo
from schemas import Company, ExtractionSource, LineItem
from tests.test_overrides import _std_period, _bank_period, _insurer_period, _reit_period

FIXTURES = Path(__file__).parent / "fixtures"
FACTS = json.loads((FIXTURES / "prologis-2025-equity-facts.json").read_text())


def company():
    return Company.model_validate_json((FIXTURES / "prologis-2025-company.json").read_text())


def test_prologis_reconciles_without_changing_existing_amounts_or_valuation():
    before = company()
    assert needs_equity_backfill(before)
    assert _balance_sheet_check(before.periods[0].balance_sheet) is not None
    updated = validate_company(backfill_noncontrolling_interests(before, FACTS))
    assert updated.extraction_flags == []
    bs = updated.periods[0].balance_sheet
    assert bs.noncontrolling_interests.value == 4_560_830_000
    assert bs.shareholders_equity.value == 53_193_178_000
    assert reconciliation_equity(bs) == 57_754_008_000
    for original, enriched in zip(before.periods, updated.periods):
        data = enriched.model_dump()
        data["balance_sheet"].pop("noncontrolling_interests")
        old = original.model_dump()
        old["balance_sheet"].pop("noncontrolling_interests")
        assert data == old
    assert before.periods[0].balance_sheet.noncontrolling_interests is None
    assert not needs_equity_backfill(updated)
    assumptions = default_assumptions(before)
    assert default_assumptions(updated) == assumptions
    assert compute_projection(updated, assumptions) == compute_projection(before, assumptions)
    assert monte_carlo(updated, assumptions, iterations=1000, seed=42) == monte_carlo(before, assumptions, iterations=1000, seed=42)


def test_inclusive_equity_is_not_counted_twice_and_real_mismatch_still_flags():
    bs = backfill_noncontrolling_interests(company(), FACTS).periods[0].balance_sheet
    inclusive = bs.model_copy(update={"shareholders_equity": LineItem(
        value=57_754_008_000, source=ExtractionSource.XBRL,
        confidence=1, xbrl_tag=TOTAL_EQUITY_TAG,
    )})
    assert reconciliation_equity(inclusive) == 57_754_008_000
    assert _balance_sheet_check(inclusive) is None
    wrong = bs.model_copy(update={"total_assets": bs.total_assets.model_copy(update={"value": Decimal(100_724_256_000)})})
    assert _balance_sheet_check(wrong) is not None


@pytest.mark.parametrize("make_period", [_std_period, _bank_period, _insurer_period, _reit_period])
def test_each_statement_variant_reconciles_a_reported_component(make_period):
    bs = make_period().balance_sheet
    nci = bs.total_assets.value * Decimal("0.02")
    bs = bs.model_copy(update={
        "shareholders_equity": bs.shareholders_equity.model_copy(update={"value": bs.shareholders_equity.value - nci, "xbrl_tag": PARENT_EQUITY_TAG}),
        "noncontrolling_interests": LineItem(value=nci, source=ExtractionSource.XBRL, confidence=1, xbrl_tag="us-gaap:MinorityInterest"),
    })
    assert _balance_sheet_check(bs) is None


@pytest.mark.parametrize("mutation", ["entity", "date", "accession", "quarter", "duration", "unit", "conflict"])
def test_backfill_rejects_unsafe_or_conflicting_facts(mutation):
    facts = copy.deepcopy(FACTS)
    entries = facts["facts"]["us-gaap"]["MinorityInterest"]["units"]["USD"]
    if mutation == "entity":
        facts["cik"] = 320193
    elif mutation == "unit":
        facts["facts"]["us-gaap"]["MinorityInterest"]["units"] = {"shares": entries}
    else:
        for entry in entries:
            if mutation == "date": entry["end"] = "2026-12-31"
            if mutation == "accession": entry["accn"] = "0001193125-27-000001"
            if mutation == "quarter": entry["fp"] = "Q3"
            if mutation == "duration": entry["start"] = "2025-01-01"
        if mutation == "conflict":
            for entry in list(entries): entries.append({**entry, "val": entry["val"] + 1})
    c = company()
    assert backfill_noncontrolling_interests(c, facts) == c


def test_duplicate_equal_facts_are_safe_and_unknown_equity_is_not_assumed_parent():
    facts = copy.deepcopy(FACTS)
    entries = facts["facts"]["us-gaap"]["MinorityInterest"]["units"]["USD"]
    entries.extend(copy.deepcopy(entries))
    c = backfill_noncontrolling_interests(company(), facts)
    bs = c.periods[0].balance_sheet
    assert bs.noncontrolling_interests.value == 4_560_830_000
    unknown = bs.model_copy(update={"shareholders_equity": bs.shareholders_equity.model_copy(update={"xbrl_tag": None})})
    assert reconciliation_equity(unknown) == bs.shareholders_equity.value
    assert _balance_sheet_check(unknown) is not None


def test_overrides_preserve_existing_component_and_equity_coverage():
    c = backfill_noncontrolling_interests(company(), FACTS)
    reviewed = apply_override(c, "balance_sheet.noncontrolling_interests", Decimal(4_560_830_000), "Checked filing")
    assert backfill_noncontrolling_interests(reviewed, FACTS) == reviewed
    parent = apply_override(reviewed, "balance_sheet.shareholders_equity", Decimal(53_193_178_000), "Parent equity")
    assert parent.periods[0].balance_sheet.shareholders_equity.xbrl_tag == PARENT_EQUITY_TAG
    assert validate_company(parent).extraction_flags == []
    bs = c.periods[0].balance_sheet
    inclusive_bs = bs.model_copy(update={"shareholders_equity": bs.shareholders_equity.model_copy(update={"value": Decimal(57_754_008_000), "xbrl_tag": TOTAL_EQUITY_TAG})})
    inclusive = c.model_copy(update={"periods": [c.periods[0].model_copy(update={"balance_sheet": inclusive_bs})]})
    inclusive = apply_override(inclusive, "balance_sheet.shareholders_equity", Decimal(57_754_008_000), "Total equity")
    assert validate_company(inclusive).extraction_flags == []


@pytest.mark.parametrize("inclusive", [False, True])
def test_liabilities_derivation_respects_noncontrolling_interests(inclusive):
    bs = backfill_noncontrolling_interests(company(), FACTS).periods[0].balance_sheet
    items = {"total_assets": bs.total_assets, "shareholders_equity": bs.shareholders_equity,
             "noncontrolling_interests": bs.noncontrolling_interests}
    if inclusive:
        items["shareholders_equity"] = bs.shareholders_equity.model_copy(update={"value": Decimal(57_754_008_000), "xbrl_tag": TOTAL_EQUITY_TAG})
    assert _derive_missing_required(items, Industry.REIT)["total_liabilities"].value == 40_970_248_000


def test_cached_company_upgrades_once_preserving_the_saved_record(monkeypatch):
    from app import main as api
    c = company()
    repo = InMemoryRepo()
    asyncio.run(repo.set("PLD", c))
    calls = []
    class Edgar:
        async def get_company_facts(self, cik):
            calls.append(cik)
            return FACTS
    monkeypatch.setattr(api, "EdgarClient", Edgar)
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {})
    monkeypatch.setattr(api, "_equity_backfill_attempts", {})
    client = TestClient(api.app)
    response = client.get("/company/PLD")
    assert response.status_code == 200
    assert response.json()["extraction_flags"] == []
    assert client.get("/company/PLD").json() == response.json()
    assert client.get("/value/PLD/defaults").status_code == 200
    assert len(calls) == 1
    assert asyncio.run(repo.get("PLD")).periods[0].balance_sheet.noncontrolling_interests.value == 4_560_830_000


def test_unavailable_sec_keeps_warning_and_throttles_retries(monkeypatch):
    from app import main as api
    repo = InMemoryRepo()
    asyncio.run(repo.set("PLD", company()))
    calls = []
    class Edgar:
        async def get_company_facts(self, cik):
            calls.append(cik)
            raise RuntimeError("SEC unavailable")
    monkeypatch.setattr(api, "EdgarClient", Edgar)
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {})
    monkeypatch.setattr(api, "_equity_backfill_attempts", {})
    client = TestClient(api.app)
    assert client.get("/company/PLD").json()["extraction_flags"]
    assert client.get("/company/PLD").json()["extraction_flags"]
    assert len(calls) == 1
    assert asyncio.run(repo.get("PLD")) == company()


def test_concurrent_backfill_and_correction_preserve_both_updates(monkeypatch):
    from app import main as api
    async def run():
        repo = InMemoryRepo()
        await repo.set("PLD", company())
        started, proceed = asyncio.Event(), asyncio.Event()
        class Edgar:
            async def get_company_facts(self, cik):
                started.set()
                await proceed.wait()
                return FACTS
        monkeypatch.setattr(api, "EdgarClient", Edgar)
        monkeypatch.setattr(api, "_repo", repo)
        monkeypatch.setattr(api, "_company_locks", {})
        monkeypatch.setattr(api, "_equity_backfill_attempts", {})
        read = asyncio.create_task(api._require_company("PLD"))
        await started.wait()
        review = asyncio.create_task(api.override("PLD", api.OverrideRequest(
            field_path="income_statement.revenue", value=Decimal(123), source_quote="Concurrent correction test",
        ), None))
        proceed.set()
        await asyncio.gather(read, review)
        saved = await repo.get("PLD")
        assert saved.periods[0].balance_sheet.noncontrolling_interests.value == 4_560_830_000
        assert saved.periods[0].income_statement.revenue.value == 123
        assert saved.periods[0].income_statement.revenue.source_quote == "Concurrent correction test"
    asyncio.run(run())


def test_new_extraction_uses_the_same_filing_matched_component(monkeypatch):
    import graph
    c = company()
    p = c.periods[0]
    state = {"cik": c.cik, "period_end": p.fiscal_period_end, "filing_accession": p.filing_accession,
             "facts": FACTS, "industry": Industry.REIT}
    result = asyncio.run(graph.track_a(state))
    item = result["periods_items"][p.fiscal_period_end]["noncontrolling_interests"]
    assert item == filing_noncontrolling_interests(FACTS, c.cik, p.fiscal_period_end, p.filing_accession)
    assert item.value == 4_560_830_000
    assert "noncontrolling_interests" not in graph._missing_fields({}, Industry.REIT)


def test_unrequested_ai_component_cannot_fill_a_missing_tagged_fact(monkeypatch):
    import graph
    c = company()
    periods_items = {}
    for p in c.periods:
        items = {}
        for statement in [p.income_statement, p.balance_sheet, p.cash_flow_statement]:
            items.update({key: getattr(statement, key) for key in type(statement).model_fields if key != "kind"})
        periods_items[p.fiscal_period_end] = items
    class Edgar:
        async def get_filing_html(self, url): return "<html>filing</html>"
    async def claude(**kwargs):
        assert "noncontrolling_interests" not in kwargs["fields_to_extract"]
        return {"noncontrolling_interests": LineItem(value=4_560_830_000, source=ExtractionSource.LLM_HTML, confidence=0.99)}
    monkeypatch.setattr(graph, "extract_track_b", claude)
    monkeypatch.setattr(graph, "extract_financial_statements_section", lambda html: "filing")
    result = asyncio.run(graph._make_track_b(Edgar(), None)({
        "ticker": "PLD", "cik": c.cik, "company_name": c.name, "industry": Industry.REIT,
        "period_ends": list(periods_items), "periods_items": periods_items,
        "filing_accession": c.periods[0].filing_accession, "filing_url": "unused",
    }))
    assert result["company"].periods[0].balance_sheet.noncontrolling_interests is None
