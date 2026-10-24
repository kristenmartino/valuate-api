import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import graph
from berkshire import BERKSHIRE_FIELDS, extract_berkshire_filing
from dcf import compute_projection, default_assumptions, monte_carlo
from industry import Industry
from schemas import ExtractionSource, LineItem

FIXTURES = Path(__file__).parent / "fixtures"
HTML = (FIXTURES / "berkshire-2025-inline.xml").read_text()
PE = date(2025, 12, 31)


def test_filing_facts_are_class_specific_scaled_and_not_double_counted():
    b = extract_berkshire_filing(HTML, " brk.b ", [PE])[PE]
    a = extract_berkshire_filing(HTML, "BRK-A", [PE])[PE]
    assert b["diluted_shares_outstanding"].value == 2_157_335_139
    assert a["diluted_shares_outstanding"].value == 1_438_223
    assert b["premiums_earned"].value == 88_902_000_000
    # Excludes parent-only cash and duplicate contexts shown elsewhere.
    cash = b["cash_and_equivalents"]
    assert cash.value == 47_719_000_000 + 4_158_000_000
    assert cash.source == ExtractionSource.DERIVED
    assert "Retains Treasury bills classified as cash equivalents" in cash.source_quote
    assert "Excludes separately reported restricted cash and short-term investments in Treasury bills" in cash.source_quote
    shares = b["diluted_shares_outstanding"]
    assert shares.source == ExtractionSource.XBRL
    assert "EquivalentClassBMember" in shares.source_quote
    assert "not generic diluted shares" in shares.source_quote


@pytest.mark.parametrize("mutation", ["wrong_class", "quarter", "wrong_unit", "wrong_entity", "extra_dimension", "conflict"])
def test_unsafe_share_contexts_fail_closed(mutation):
    soup = BeautifulSoup(HTML, "xml")
    targets = []
    for fact in soup.find_all("nonFraction"):
        context = soup.find("context", id=fact.get("contextRef"))
        if (fact.get("name") == "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic"
                and context and context.find("endDate").text == "2025-12-31"
                and "brka:EquivalentClassBMember" in context.get_text()):
            targets.append((fact, context))
    assert targets
    for fact, context in targets:
        if mutation == "wrong_class":
            context.find("explicitMember").string = "brka:ActualClassBMember"
        elif mutation == "quarter":
            context.find("startDate").string = "2025-10-01"
        elif mutation == "wrong_unit":
            fact["unitRef"] = "U_USD"
        elif mutation == "wrong_entity":
            context.find("identifier").string = "0000320193"
        elif mutation == "extra_dimension":
            extra = soup.new_tag("xbrldi:explicitMember", attrs={"dimension": "srt:ConsolidatedEntitiesAxis"})
            extra.string = "srt:ParentCompanyMember"
            context.find("segment").append(extra)
        elif mutation == "conflict":
            # Build with the original namespace-bearing document instead.
            copy = soup.new_tag("ix:nonFraction", attrs=dict(fact.attrs))
            copy.string = "999"
            fact.insert_after(copy)
    result = extract_berkshire_filing(str(soup), "BRK-B", [PE])[PE]
    assert "diluted_shares_outstanding" not in result


def test_incomplete_cash_does_not_substitute_parent_or_partial_cash():
    html = HTML.replace("brka:RailroadUtilitiesAndEnergyMember", "brka:UnknownMember")
    result = extract_berkshire_filing(html, "BRK-B", [PE])[PE]
    assert "cash_and_equivalents" not in result
    assert extract_berkshire_filing(HTML, "PRU", [PE]) == {}
    assert extract_berkshire_filing(HTML, "BRK-B", [date(2026, 12, 31)])[date(2026, 12, 31)] == {}


def compose(monkeypatch, ticker="BRK-B", html=HTML):
    facts = json.loads((FIXTURES / "berkshire-2025-facts.json").read_text())
    state = dict(ticker=ticker, cik="0001067983", company_name="BERKSHIRE HATHAWAY INC",
                 period_end=PE, filing_accession="0001193125-26-083899", filing_url="https://example.test/10k",
                 industry=Industry.INSURER, facts=facts)
    state.update(asyncio.run(graph.track_a(state)))

    class Edgar:
        async def get_filing_html(self, url):
            return html

    async def claude(**kwargs):
        assert not BERKSHIRE_FIELDS.intersection(kwargs["fields_to_extract"])
        # Even an unexpected model response must never supply a share basis.
        return {"diluted_shares_outstanding": LineItem(value=1, source=ExtractionSource.LLM_HTML, confidence=0.99)}

    monkeypatch.setattr(graph, "extract_track_b", claude)
    return asyncio.run(graph._make_track_b(Edgar(), None)(state))["company"]


def test_real_filing_pipeline_composes_values_and_simulates_both_classes(monkeypatch):
    b = compose(monkeypatch)
    a = compose(monkeypatch, "BRK-A")
    assert len(b.periods) == 3
    assert "Class B" in b.share_count_label
    assert "not a sum-of-the-parts" in b.valuation_note
    assert "Class A" in a.share_count_label
    assert b.periods[0].income_statement.premiums_earned.value == 88_902_000_000
    assumptions = default_assumptions(b)
    result = compute_projection(b, assumptions)
    # Independent application of the documented P/B formula to filing values.
    expected = (717_419_000_000 / 2_157_335_139) * ((assumptions.operating_margin - 0.03) / (0.09 - 0.03))
    assert result.fair_value_per_share == pytest.approx(expected)
    result_a = compute_projection(a, default_assumptions(a))
    assert result_a.fair_value_per_share / result.fair_value_per_share == pytest.approx(1500, rel=1e-6)
    assert result_a.equity_value == pytest.approx(result.equity_value)
    simulation = monte_carlo(b, assumptions)
    assert simulation.p10 > 0


def test_missing_class_fact_cannot_be_filled_by_llm(monkeypatch):
    html = HTML.replace("brka:EquivalentClassBMember", "brka:ActualClassBMember")
    with pytest.raises(graph.CompositionError, match="diluted_shares_outstanding"):
        compose(monkeypatch, html=html)
