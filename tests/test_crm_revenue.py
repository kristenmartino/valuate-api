"""SEC inline facts pin the six exhaustive Salesforce revenue categories."""
import asyncio
import copy
from datetime import date
from decimal import Decimal
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import pytest

from crm_revenue import extract_crm_revenue, needs_revenue_repair, repair_revenue
from dcf import compute_projection, default_assumptions, monte_carlo
from repository import InMemoryRepo
from schemas import Company, ExtractionSource

FIXTURES = Path(__file__).parent / "fixtures"
HTML = (FIXTURES / "salesforce-2026-revenue-inline.xml").read_text()
END = date(2026, 1, 31)
ACCESSION = "0001108524-26-000060"
TOTAL = Decimal(41_525_000_000)


def company():
    return Company.model_validate_json((FIXTURES / "salesforce-partial-revenue-company.json").read_text())


def test_six_categories_reconcile_without_counting_subscription_subtotal_twice():
    rows = extract_crm_revenue(HTML, END, ACCESSION, TOTAL)
    assert [s.revenue.value for s in rows] == [9_028_000_000, 9_818_000_000, 8_882_000_000, 5_428_000_000, 6_232_000_000, 2_137_000_000]
    assert sum(s.revenue.value for s in rows[:5]) == 39_388_000_000
    assert sum(s.revenue.value for s in rows) == TOTAL
    assert len({s.name for s in rows}) == 6
    assert all(s.revenue.source == ExtractionSource.XBRL and ACCESSION in s.revenue.source_quote for s in rows)
    assert all("Revenue category, not a separate operating segment" in s.revenue.source_quote for s in rows)
    assert "Subscription and support" not in [s.name for s in rows]


@pytest.mark.parametrize("mutation", ["entity", "context_entity", "period", "quarter", "unit", "dimensions", "missing", "conflict", "subtotal", "document_type", "document_period"])
def test_rejects_unsafe_or_incomplete_breakdowns(mutation):
    s = BeautifulSoup(HTML, "xml")
    f = s.find("nonFraction", attrs={"contextRef": "c-9"})  # professional services
    c = s.find("context", attrs={"id": "c-9"})
    if mutation == "entity": s.find("nonNumeric", attrs={"name": "dei:EntityCentralIndexKey"}).string = "0000320193"
    if mutation == "context_entity": c.find("identifier").string = "0000320193"
    if mutation == "period": c.find("endDate").string = "2025-01-31"
    if mutation == "quarter": c.find("startDate").string = "2025-11-01"
    if mutation == "unit": f["unitRef"] = "shares"
    if mutation == "dimensions": c.find("explicitMember")["dimension"] = "srt:StatementGeographicalAxis"
    if mutation == "missing":
        for fact in list(s.find_all("nonFraction", attrs={"contextRef": "c-9"})): fact.decompose()
    if mutation == "conflict":
        duplicate = copy.copy(f);duplicate.string = "2,138";f.insert_after(duplicate)
    if mutation == "subtotal":
        for fact in s.find_all("nonFraction", attrs={"contextRef": "c-6"}): fact.string = "39,000"
    if mutation == "document_type": s.find("nonNumeric", attrs={"name": "dei:DocumentType"}).string = "10-Q"
    if mutation == "document_period": s.find("nonNumeric", attrs={"name": "dei:DocumentPeriodEndDate"}).string = "January 31, 2025"
    assert extract_crm_revenue(str(s), END, ACCESSION, TOTAL) == []


def test_rejects_saved_revenue_mismatch_instead_of_inventing_residual():
    assert extract_crm_revenue(HTML, END, ACCESSION, TOTAL + 1) == []


def test_equal_repeated_facts_are_deduplicated():
    s = BeautifulSoup(HTML, "xml")
    f = s.find("nonFraction", attrs={"contextRef": "c-9"});f.insert_after(copy.copy(f))
    assert extract_crm_revenue(str(s), END, ACCESSION, TOTAL) == extract_crm_revenue(HTML, END, ACCESSION, TOTAL)


def test_repair_changes_only_breakdown_preserving_all_figures_reviews_and_valuation():
    c = company();u = repair_revenue(c, HTML)
    assert needs_revenue_repair(c) and not needs_revenue_repair(u)
    old = c.model_dump();new = u.model_dump()
    old["periods"][0]["income_statement"].pop("revenue_segments")
    new["periods"][0]["income_statement"].pop("revenue_segments")
    assert old == new
    a = default_assumptions(c)
    assert default_assumptions(u) == a
    assert compute_projection(u, a) == compute_projection(c, a)
    assert monte_carlo(u, a, iterations=1000, seed=42) == monte_carlo(c, a, iterations=1000, seed=42)
    assert repair_revenue(u, HTML) == u
    other = c.model_copy(update={"cik": "0000320193"})
    assert repair_revenue(other, HTML) == other


def test_user_reviewed_breakdown_is_not_replaced():
    c = company();p = c.periods[0];rows = p.income_statement.revenue_segments
    reviewed = rows[0].model_copy(update={"revenue": rows[0].revenue.model_copy(update={"source": ExtractionSource.USER_OVERRIDE, "source_quote": "reviewed category"})})
    c = c.model_copy(update={"periods": [p.model_copy(update={"income_statement": p.income_statement.model_copy(update={"revenue_segments": [reviewed, *rows[1:]]})}), *c.periods[1:]]})
    assert repair_revenue(c, HTML) == c


def test_cached_read_fetches_saved_accession_once_preserving_review(monkeypatch):
    from app import main as api
    repo = InMemoryRepo();asyncio.run(repo.set("CRM", company()));urls = []
    class Edgar:
        async def get_submissions(self, cik):
            return {"filings": {"recent": {"accessionNumber": ["0001108524-27-000001", ACCESSION], "form": ["10-K", "10-K"], "primaryDocument": ["newer.htm", "crm-20260131.htm"]}}}
        async def get_filing_html(self, url): urls.append(url);return HTML
    monkeypatch.setattr(api, "EdgarClient", Edgar)
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {})
    monkeypatch.setattr(api, "_revenue_repair_attempts", {})
    client = TestClient(api.app);r = client.get("/company/CRM")
    assert r.status_code == 200
    assert len(r.json()["periods"][0]["income_statement"]["revenue_segments"]) == 6
    assert client.get("/company/CRM").json() == r.json()
    assert urls == ["https://www.sec.gov/Archives/edgar/data/1108524/000110852426000060/crm-20260131.htm"]


def test_unavailable_sec_leaves_partial_breakdown_and_throttles_retry(monkeypatch):
    from app import main as api
    repo = InMemoryRepo();asyncio.run(repo.set("CRM", company()));calls = []
    class Edgar:
        async def get_submissions(self, cik): calls.append(cik);raise RuntimeError("SEC unavailable")
    monkeypatch.setattr(api, "EdgarClient", Edgar)
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {})
    monkeypatch.setattr(api, "_revenue_repair_attempts", {})
    client = TestClient(api.app)
    for _ in range(2):assert len(client.get("/company/CRM").json()["periods"][0]["income_statement"]["revenue_segments"]) == 5
    assert len(calls) == 1 and asyncio.run(repo.get("CRM")) == company()


def test_fresh_graph_uses_complete_tagged_categories_instead_of_ai_subset(monkeypatch):
    import graph
    from industry import Industry
    c = company();items = {}
    for p in c.periods:
        row = {}
        for stmt in [p.income_statement, p.balance_sheet, p.cash_flow_statement]:
            row.update({key: getattr(stmt, key) for key in type(stmt).model_fields if key != "kind"})
        items[p.fiscal_period_end] = row
    class Edgar:
        async def get_filing_html(self, url): return HTML
    async def fields(**kwargs): return {}
    ai_calls = []
    async def ai_segments(**kwargs):
        ai_calls.append(kwargs)
        raise AssertionError("CRM breakdown must not use AI")
    monkeypatch.setattr(graph, "extract_track_b", fields)
    monkeypatch.setattr(graph, "extract_revenue_segments", ai_segments)
    result = asyncio.run(graph._make_track_b(Edgar(), None)({
        "ticker": "CRM", "cik": c.cik, "company_name": c.name, "industry": Industry.STANDARD,
        "period_ends": list(items), "periods_items": items, "filing_accession": ACCESSION, "filing_url": "unused",
    }))
    rows = result["company"].periods[0].income_statement.revenue_segments
    assert len(rows) == 6 and sum(s.revenue.value for s in rows) == TOTAL
    assert ai_calls == []
