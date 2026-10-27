"""Cold, previously unlisted sources and explicit unsafe-evidence refusals."""

import asyncio
import copy
from decimal import Decimal
from functools import lru_cache
import pytest
from eval.reusable_cold import ReusableEdgar, INDEX, run_fixture
from filing_document import FilingDocument
from filing_sources import annual_documents, linked_documents
from input_contracts import anchor_date
from qualification import POLICIES, qualify, current_qualification
from reusable_inputs import (
    load_reusable_inputs,
    corroborates,
    model_scope_issue,
    carrying_debt,
)
from schemas import Industry, ExtractionSource


@lru_cache
def cold(ticker):
    return asyncio.run(run_fixture(ticker))


@pytest.mark.parametrize("case", INDEX, ids=lambda c: c["ticker"])
def test_cold_unlisted_filings_use_real_graph_and_independent_rules_without_ai(case):
    company, evidence = cold(case["ticker"])
    q = company.qualification
    assert (
        company.cik.lstrip("0") + ":" + company.periods[0].filing_accession
        not in POLICIES
    )
    assert (
        q.method == "reusable_statement_rules"
        and q.ruleset == "annual-statement-evidence-v1"
    )
    assert q.evidence_digest and evidence["documents"]
    assert len(company.periods) == 5
    assert q.status == case["expected_status"], q.issues
    if case["ticker"] == "BAC":
        assert {i.field_path for i in q.issues} == {
            "cash_flow_statement.dividends_paid"
        }
        assert all("Common cash-dividend" in i.message for i in q.issues)
    if case["ticker"] == "NVDA":
        assert {i.field_path for i in q.issues} == {
            "balance_sheet.long_term_debt",
            "balance_sheet.short_term_debt",
        }
        assert all("finance-lease" in i.message for i in q.issues)
    for p in company.periods:
        for g in ["income_statement", "balance_sheet", "cash_flow_statement"]:
            assert not any(
                getattr(getattr(getattr(p, g), f), "source", None)
                in {ExtractionSource.USER_OVERRIDE, ExtractionSource.LLM_HTML}
                for f in type(getattr(p, g)).model_fields
            )
        if p.industry == Industry.STANDARD:
            assert p.income_statement.depreciation_amortization is None


@pytest.mark.parametrize(
    "ticker,current,noncurrent,da",
    [
        ("META", "308000000", "59620000000", "18616000000"),
        ("COST", "2342000000", "5233000000", "2674000000"),
        ("WMT", "10994000000", "40529000000", "14203000000"),
        ("CAT", "12634000000", "30696000000", "2262000000"),
    ],
)
def test_reconciled_carrying_components_and_cashflow_scope(
    ticker, current, noncurrent, da
):
    c, _ = cold(ticker)
    p = c.periods[0]
    assert p.balance_sheet.short_term_debt.value == Decimal(current)
    assert p.balance_sheet.long_term_debt.value == Decimal(noncurrent)
    assert p.cash_flow_statement.depreciation_amortization.value == Decimal(da)
    # The finance-lease current/noncurrent amounts, group debt and rounded
    # narrative repetitions are counted once, not as inclusive extra totals.
    evidence = next(
        i
        for i in c.qualification.inputs
        if i.field_path == "balance_sheet.short_term_debt"
    )
    assert evidence.display_label and evidence.operands
    if ticker == "CAT":
        assert any(
            "ProductOrServiceAxis" in o.get("dimensions", "") for o in evidence.operands
        )
    if ticker == "WMT":
        assert next(
            i
            for i in c.qualification.inputs
            if i.field_path == "cash_flow_statement.depreciation_amortization"
        ).is_proxy
        assert p.income_statement.revenue.value == Decimal(
            "713163000000"
        )  # Total, not net sales.


@lru_cache
def profile():
    c, _ = cold("META")
    client = ReusableEdgar("META")
    p = c.periods[0]
    loaded = asyncio.run(
        load_reusable_inputs(
            client,
            client.facts,
            c.cik,
            p.fiscal_period_end,
            p.filing_accession,
            p.industry,
        )
    )
    return c, loaded, client.facts


@pytest.mark.parametrize(
    "change",
    ["value", "older_da", "short_history", "model", "optional", "source_absent"],
)
def test_saved_confidence_quotes_reviews_or_shortened_history_cannot_bypass_checks(
    change,
):
    company, loaded, facts = copy.deepcopy(profile())
    p = company.periods[0]
    if change == "value":
        p.balance_sheet.long_term_debt.value += 1
    if change == "optional":
        p.balance_sheet.total_current_liabilities.value += 1
    if change == "older_da":
        company.periods[-1].cash_flow_statement.depreciation_amortization.value += 1
    if change == "short_history":
        company.periods = company.periods[:-1]
    if change == "model":
        loaded["model_issue"] = "Unmodeled financial industry"
    if change == "source_absent":
        loaded = None
    p.balance_sheet.long_term_debt.source = ExtractionSource.USER_OVERRIDE
    p.balance_sheet.long_term_debt.confidence = 1
    p.balance_sheet.long_term_debt.source_quote = "Claimed reviewed and correct"
    report = qualify(company, facts, {"reusable_inputs": loaded}, p.filing_accession)
    assert report.status == "review_required" and report.issues
    company.qualification = report
    assert current_qualification(company).status == "review_required"


@lru_cache
def source_doc():
    client = ReusableEdgar("META")
    company, _, _ = profile()
    p = company.periods[0]

    async def read():
        filings, _ = await annual_documents(
            client,
            company.cik,
            p.fiscal_period_end,
            p.filing_accession,
            anchor_date(client.facts, p.filing_accession),
        )
        f = filings[0]
        html = await client.get_filing_html(f["url"])
        linked = await linked_documents(client, f, html)
        return client, f, html, linked

    return asyncio.run(read())


def test_rounded_inline_duplicates_resolve_but_equally_precise_conflicts_do_not():
    client = ReusableEdgar("WMT")
    c, _ = cold("WMT")
    p = c.periods[0]

    async def read():
        fs, _ = await annual_documents(
            client,
            c.cik,
            p.fiscal_period_end,
            p.filing_accession,
            anchor_date(client.facts, p.filing_accession),
        )
        f = fs[0]
        h = await client.get_filing_html(f["url"])
        return FilingDocument(
            h, c.cik, p.fiscal_period_end, await linked_documents(client, f, h)
        )

    d = asyncio.run(read())
    tag = "us-gaap:DepreciationAmortizationAndAccretionNet"
    row = d.fact(tag, p.fiscal_period_end)
    assert row["value"] == Decimal("14203000000") and Decimal("14200000000") in {
        Decimal(v) for v in row["compatible_values"]
    }
    d.index[(tag, str(p.fiscal_period_end), "USD", False)].append(
        {**row, "value": Decimal(1)}
    )
    assert d.fact(tag, p.fiscal_period_end) is None


@pytest.mark.parametrize(
    "mutation", ["entity", "date", "unit", "quarter", "dimension", "conflict", "hidden"]
)
def test_numeric_source_context_changes_are_not_recovered_by_confidence_or_api(
    mutation,
):
    client, f, html, linked = source_doc()
    c, _, _ = profile()
    pe = c.periods[0].fiscal_period_end
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "xml")
    matches = [
        x
        for x in soup.find_all("nonFraction")
        if x.get("name") == "us-gaap:DepreciationDepletionAndAmortization"
    ]
    assert matches
    if mutation in {"entity", "date"}:
        key = (
            "dei:EntityCentralIndexKey"
            if mutation == "entity"
            else "dei:DocumentPeriodEndDate"
        )
        soup.find("nonNumeric", attrs={"name": key}).string = (
            "999999" if mutation == "entity" else "2024-12-31"
        )
        with pytest.raises(ValueError):
            FilingDocument(str(soup), c.cik, pe, linked)
        return
    for x in matches:
        if mutation == "unit":
            x["unitRef"] = "nonexistent-eur-unit"
        if mutation == "hidden":
            x.wrap(soup.new_tag("ix:hidden"))
        if mutation in {"quarter", "dimension"}:
            ctx = soup.find("context", attrs={"id": x["contextRef"]})
            if mutation == "quarter":
                ctx.find("startDate").string = str(pe.year) + "-10-01"
            else:
                member = soup.new_tag("xbrldi:explicitMember")
                member["dimension"] = "test:BusinessAxis"
                member.string = "test:SegmentMember"
                ctx.append(member)
        if mutation == "conflict":
            duplicate = copy.copy(x)
            duplicate.string = "1"
            x.insert_after(duplicate)
    doc = FilingDocument(str(soup), c.cik, pe, linked)
    assert (
        doc.fact(
            "us-gaap:DepreciationDepletionAndAmortization",
            pe,
            kind="cash_flow_statement",
        )
        is None
    )


def test_sec_api_conflicting_value_is_not_ignored_even_when_one_matches():
    company, loaded, facts = copy.deepcopy(profile())
    p = company.periods[0]
    client, f, _, _ = source_doc()
    row = loaded["inputs"][p.fiscal_period_end][
        "cash_flow_statement.depreciation_amortization"
    ].rows[0]
    tag = row["concept"].split(":")[1]
    rows = facts["facts"]["us-gaap"][tag]["units"]["USD"]
    found = next(
        r
        for r in rows
        if r["accn"] == f["accession"]
        and r["end"] == str(p.fiscal_period_end)
        and r.get("start") == row["start"]
    )
    rows.append({**found, "val": 1})
    assert not corroborates(row, facts, f, f["filed"])


def test_missing_relationship_or_debt_operand_never_turns_into_zero():
    client, f, html, linked = source_doc()
    c, _, facts = profile()
    pe = c.periods[0].fiscal_period_end
    doc = FilingDocument(html, c.cik, pe, linked)
    tag = "us-gaap:LongTermDebtNoncurrent"
    doc.index[(tag, str(pe), "USD", True)] = []
    assert carrying_debt(doc, facts, f, f["filed"], pe) == {}


@pytest.mark.parametrize(
    "sic,industry",
    [
        ("6200", Industry.STANDARD),
        ("2911", Industry.ENERGY),
        ("6411", Industry.INSURER),
        ("", Industry.STANDARD),
    ],
)
def test_unsupported_industry_is_not_an_automatic_industrial_valuation(sic, industry):
    _, f, html, linked = source_doc()
    c, _, _ = profile()
    doc = FilingDocument(html, c.cik, c.periods[0].fiscal_period_end, linked)
    assert model_scope_issue(
        {"sic": sic}, industry, doc, c.periods[0].fiscal_period_end
    )


@pytest.mark.parametrize("ticker", ["NVDA", "BAC"])
def test_automatic_record_with_unqualified_debt_cannot_get_public_defaults_or_value(
    monkeypatch,
    ticker,
):
    from fastapi.testclient import TestClient
    from app import main as api
    from repository import InMemoryRepo

    c, _ = cold(ticker)
    repo = InMemoryRepo()
    asyncio.run(repo.set(ticker, c))
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {})
    client = TestClient(api.app)
    assert client.get(f"/value/{ticker}/defaults").status_code == 422
    response = client.post(
        f"/value/{ticker}",
        json={
            "assumptions": {
                "revenue_growth": 0.05,
                "operating_margin": 0.2,
                "terminal_growth": 0.02,
                "wacc": 0.1,
                "tax_rate": 0.21,
                "capex_ratio": 0.05,
                "da_ratio": 0.05,
                "working_capital_ratio": 0.05,
            }
        },
    )
    assert (
        response.status_code == 422
        and response.json()["detail"]["code"] == "valuation_not_qualified"
    )


@pytest.mark.parametrize(
    "ticker,complete", [("META", True), ("NVDA", False), ("BAC", False)]
)
def test_unlisted_source_preflight_is_useful_without_granting_qualification(
    ticker, complete
):
    from eval.preflight import source_preflight

    result = asyncio.run(source_preflight(ticker, ReusableEdgar(ticker)))
    assert result["source_checklist_complete"] is complete
    assert (
        result["qualification_granted"] is False
        and result["reviewed_filing_policy"] is False
    )
    assert result["source_ruleset"] == "annual-statement-evidence-v1"
