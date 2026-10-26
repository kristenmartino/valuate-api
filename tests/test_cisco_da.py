"""Pinned inline facts from Cisco's 2026 and 2024 annual cash-flow statements."""
import asyncio
import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import pytest

from debt import repair_debt
from cisco_da import CONCEPT, extract_filing_da, filing_da_history, needs_da_repair, repair_da
from dcf import compute_projection, default_assumptions
from graph import validate_company
from overrides import apply_override
from repository import InMemoryRepo
from schemas import Company

FIXTURES = Path(__file__).parent / "fixtures"
FACTS = json.loads((FIXTURES / "cisco-debt-facts.json").read_text())
HTML = (FIXTURES / "cisco-da-2026.html").read_text()
OLD_HTML = (FIXTURES / "cisco-da-2024.html").read_text()
END = date(2026, 7, 25)
ACCESSION = "0000858877-26-000132"
SUBMISSIONS = {"cik": "0000858877", "filings": {"recent": {
    "accessionNumber": [ACCESSION, "0000858877-24-000017"],
    "form": ["10-K", "10-K"], "reportDate": ["2026-07-25", "2024-07-27"],
    "primaryDocument": ["csco-20260725.htm", "csco-20240727.htm"],
}}}


def company():
    c = Company.model_validate_json((FIXTURES / "cisco-reviewed-company.json").read_text())
    return repair_debt(c, FACTS)


class Edgar:
    def __init__(self): self.urls = []
    async def get_submissions(self, cik): return copy.deepcopy(SUBMISSIONS)
    async def get_filing_html(self, url):
        self.urls.append(url)
        return OLD_HTML if "csco-20240727.htm" in url else HTML


def history(client=None):
    c = company()
    return asyncio.run(filing_da_history(client or Edgar(), c.cik, END, ACCESSION, [p.fiscal_period_end for p in c.periods]))


def test_five_annual_cash_flow_adjustments_preserve_every_other_field_and_review():
    c = company();items = history();u = validate_company(repair_da(c, items))
    assert [p.cash_flow_statement.depreciation_amortization.value for p in u.periods] == [
        2_540_000_000, 2_811_000_000, 2_507_000_000, 1_726_000_000, 1_957_000_000,
    ]
    assert not needs_da_repair(u) and u.extraction_flags == []
    for before, after in zip(c.periods, u.periods):
        x, y = before.model_dump(), after.model_dump()
        for name in ("income_statement", "cash_flow_statement"):
            x[name].pop("depreciation_amortization");y[name].pop("depreciation_amortization")
            item = getattr(after, name).depreciation_amortization
            assert item.xbrl_tag == CONCEPT and "not pure D&A" in item.source_quote
        assert x == y
    assert u.periods[0].balance_sheet == c.periods[0].balance_sheet
    assert "0000858877-26-000132" in u.periods[0].cash_flow_statement.depreciation_amortization.source_quote
    assert "0000858877-24-000017" in u.periods[-1].cash_flow_statement.depreciation_amortization.source_quote
    assert repair_da(u, items) == u


def test_only_da_assumption_changes_and_cash_flow_valuation_matches_calculation():
    c = company();u = repair_da(c, history());a = default_assumptions(c);b = default_assumptions(u)
    assert a.model_dump(exclude={"da_ratio"}) == b.model_dump(exclude={"da_ratio"})
    expected = sum(float(p.cash_flow_statement.depreciation_amortization.value / p.income_statement.revenue.value) for p in u.periods) / 5
    assert b.da_ratio == pytest.approx(expected)
    assert b.da_ratio == pytest.approx(0.040912636262161836)
    old, new = compute_projection(c, a), compute_projection(u, b)
    assert new.net_debt == old.net_debt == 23_967_000_000
    assert new.fair_value_per_share == pytest.approx(76.16811352423511)
    assert new.years[0].depreciation_amortization == pytest.approx(new.years[0].revenue * expected)


@pytest.mark.parametrize("mutation", ["entity", "context_entity", "unit", "start", "end", "form", "document_end", "dimension", "typed", "conflict", "negative", "nonfinite", "scale", "nil", "missing", "ppe_tag"])
def test_rejects_unsafe_or_incomplete_latest_fact(mutation):
    s = BeautifulSoup(HTML, "xml")
    f = s.find("nonFraction", attrs={"name": CONCEPT});c = s.find("context", attrs={"id": f["contextRef"]})
    if mutation == "entity": s.find("nonNumeric", attrs={"name": "dei:EntityCentralIndexKey"}).string = "320193"
    if mutation == "context_entity": c.find("identifier").string = "320193"
    if mutation == "unit": s.find("unit").find("measure").string = "iso4217:EUR"
    if mutation == "start": c.find("startDate").string = "2026-04-26"
    if mutation == "end": c.find("endDate").string = "2026-07-24"
    if mutation == "form": s.find("nonNumeric", attrs={"name": "dei:DocumentType"}).string = "10-Q"
    if mutation == "document_end": s.find("nonNumeric", attrs={"name": "dei:DocumentPeriodEndDate"}).string = "2025-07-26"
    if mutation in {"dimension", "typed"}:
        segment = s.new_tag("segment");segment.append(s.new_tag("explicitMember" if mutation == "dimension" else "typedMember"));c.find("entity").append(segment)
    if mutation == "conflict":
        duplicate = copy.copy(f);duplicate.string = "999";f.insert_after(duplicate)
    if mutation == "negative": f["sign"] = "-"
    if mutation == "nonfinite": f.string = "NaN"
    if mutation == "scale": f["scale"] = "9999999"
    if mutation == "nil": f["xsi:nil"] = "true"
    if mutation == "missing": f.decompose()
    if mutation == "ppe_tag": f["name"] = "us-gaap:DepreciationDepletionAndAmortization"
    assert END not in extract_filing_da(str(s), END, ACCESSION, [END])


def test_repeated_equal_facts_and_exact_annual_comparatives_are_accepted():
    s = BeautifulSoup(OLD_HTML, "xml");f = s.find("nonFraction", attrs={"name": CONCEPT});f.insert_after(copy.copy(f))
    pe = date(2024, 7, 27)
    assert extract_filing_da(str(s), pe, "0000858877-24-000017", [pe])[pe].value == 2_507_000_000
    pe = date(2023, 7, 29)
    assert extract_filing_da(OLD_HTML, date(2024, 7, 27), "0000858877-24-000017", [pe])[pe].value == 1_726_000_000


def test_saved_accession_required_and_anchor_comparatives_take_precedence():
    client = Edgar();items = history(client)
    assert len(client.urls) == 2
    assert ACCESSION in items[date(2024, 7, 27)].source_quote
    class Wrong(Edgar):
        async def get_submissions(self, cik):
            result = copy.deepcopy(SUBMISSIONS);result["filings"]["recent"]["accessionNumber"][0] = "0000858877-26-999999";return result
    assert history(Wrong()) == {}


def test_user_da_override_and_other_company_are_not_replaced():
    c = apply_override(company(), "cash_flow_statement.depreciation_amortization", Decimal(123), "Reviewed add-back")
    assert repair_da(c, history()).periods[0].cash_flow_statement.depreciation_amortization == c.periods[0].cash_flow_statement.depreciation_amortization
    other = c.model_copy(update={"cik": "320193"})
    assert not needs_da_repair(other) and repair_da(other, history()) == other


def test_partial_history_does_not_mix_new_totals_with_old_components():
    c = company();items = history();items.pop(c.periods[-1].fiscal_period_end)
    assert repair_da(c, items) == c
    assert validate_company(c).extraction_flags[0].field_path == "cash_flow_statement.depreciation_amortization"


def test_saved_read_repairs_once_and_failed_fetch_preserves_data_and_warning(monkeypatch):
    from app import main as api
    repo = InMemoryRepo();asyncio.run(repo.set("CSCO", company()))
    calls = [];selected = history()
    async def fetch(*args): calls.append(args);return selected
    monkeypatch.setattr(api, "filing_da_history", fetch)
    monkeypatch.setattr(api, "EdgarClient", Edgar)
    monkeypatch.setattr(api, "_repo", repo);monkeypatch.setattr(api, "_company_locks", {})
    monkeypatch.setattr(api, "_cisco_da_repair_attempts", {})
    client = TestClient(api.app)
    first = client.get("/company/CSCO")
    assert first.status_code == 200 and first.json()["extraction_flags"] == []
    assert client.get("/company/CSCO").json() == first.json() and len(calls) == 1
    async def fail(*args): calls.append(args);raise RuntimeError("SEC unavailable")
    asyncio.run(repo.set("CSCO", company()));monkeypatch.setattr(api, "filing_da_history", fail)
    monkeypatch.setattr(api, "_cisco_da_repair_attempts", {})
    for _ in range(2):
        response = client.get("/company/CSCO")
        assert response.status_code == 200
        assert response.json()["extraction_flags"][0]["field_path"] == "cash_flow_statement.depreciation_amortization"
    assert len(calls) == 2 and asyncio.run(repo.get("CSCO")) == company()


def test_fresh_graph_uses_tagged_adjustment_and_never_ai_da(monkeypatch):
    import graph
    from industry import Industry
    c = company();items = {}
    for p in c.periods:
        row = {}
        for stmt in (p.income_statement, p.balance_sheet, p.cash_flow_statement):
            row.update({key: getattr(stmt, key) for key in type(stmt).model_fields if key != "kind"})
        items[p.fiscal_period_end] = row
    async def fields(**kwargs):
        assert "depreciation_amortization" not in kwargs["fields_to_extract"]
        return {}
    async def segments(**kwargs): return []
    monkeypatch.setattr(graph, "extract_track_b", fields);monkeypatch.setattr(graph, "extract_revenue_segments", segments)
    result = asyncio.run(graph._make_track_b(Edgar(), None)({
        "ticker": "CSCO", "cik": c.cik, "company_name": c.name, "industry": Industry.STANDARD,
        "period_ends": list(items), "periods_items": items, "filing_accession": ACCESSION, "filing_url": "csco-20260725.htm",
    }))["company"]
    assert result.periods[0].cash_flow_statement.depreciation_amortization.value == 2_540_000_000
    assert len(result.periods) == 5


def test_missing_tagged_latest_da_fails_instead_of_accepting_ppe_or_ai(monkeypatch):
    import graph
    from industry import Industry
    c = company();p = c.periods[0];row = {}
    for stmt in (p.income_statement, p.balance_sheet, p.cash_flow_statement):
        row.update({key: getattr(stmt, key) for key in type(stmt).model_fields if key != "kind"})
    class Missing(Edgar):
        async def get_filing_html(self, url): return HTML.replace(CONCEPT, "csco:Unavailable")
    async def fields(**kwargs):
        assert "depreciation_amortization" not in kwargs["fields_to_extract"]
        return {"depreciation_amortization": row["depreciation_amortization"]}
    async def segments(**kwargs): return []
    monkeypatch.setattr(graph, "extract_track_b", fields);monkeypatch.setattr(graph, "extract_revenue_segments", segments)
    with pytest.raises(graph.CompositionError, match="depreciation_amortization"):
        asyncio.run(graph._make_track_b(Missing(), None)({
            "ticker": "CSCO", "cik": c.cik, "company_name": c.name, "industry": Industry.STANDARD,
            "period_ends": [END], "periods_items": {END: row}, "filing_accession": ACCESSION, "filing_url": "csco-20260725.htm",
        }))
