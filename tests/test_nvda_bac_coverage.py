"""Paid common DPS and historical asset amortization have distinct contracts."""

import asyncio
import copy
from decimal import Decimal
from functools import lru_cache
import pytest
from eval.reusable_cold import ReusableEdgar, run_fixture
from filing_document import FilingDocument
from filing_sources import annual_documents, linked_documents
from input_contracts import anchor_date
from reusable_inputs import carrying_debt, load_reusable_inputs
from qualification import qualify
from dcf import default_assumptions, compute_projection
from schemas import ExtractionSource, LineItem


@lru_cache
def checked(ticker):
    return asyncio.run(run_fixture(ticker))[0]


@lru_cache
def latest_doc(ticker):
    client = ReusableEdgar(ticker)
    company = checked(ticker)
    p = company.periods[0]

    async def read():
        bound = anchor_date(client.facts, p.filing_accession)
        fs, _ = await annual_documents(
            client, company.cik, p.fiscal_period_end, p.filing_accession, bound
        )
        filing = fs[0]
        html = await client.get_filing_html(filing["url"])
        doc = FilingDocument(
            html,
            company.cik,
            p.fiscal_period_end,
            await linked_documents(client, filing, html),
        )
        return client.facts, filing, doc, bound

    return asyncio.run(read())


def test_nvda_reported_notes_reconcile_without_turning_asset_amortization_into_debt():
    c = checked("NVDA")
    q = c.qualification
    p = c.periods[0]
    assert q.status == "qualified" and p.balance_sheet.short_term_debt.value == Decimal(
        "999000000"
    )
    assert p.balance_sheet.long_term_debt.value == Decimal("7469000000")
    rows = [
        i
        for i in q.inputs
        if i.period_end == str(p.fiscal_period_end) and i.field_path.endswith("_debt")
    ]
    for i in rows:
        assert {
            "us-gaap:LongTermDebt",
            "us-gaap:LongTermDebtCurrent",
            "us-gaap:LongTermDebtNoncurrent",
            "us-gaap:FinanceLeaseRightOfUseAssetAccumulatedAmortization",
        } <= set(i.concepts)
        assert all(
            o["concept"] != "us-gaap:FinanceLeaseRightOfUseAssetAccumulatedAmortization"
            or o["usage"] == "scope reconciliation"
            for o in i.operands
        )
    assert any(
        "No finance-lease liability is inferred as zero" in s for s in q.disclosures
    )


@pytest.mark.parametrize(
    "tag",
    [
        "us-gaap:FinanceLeaseLiabilityNoncurrent",
        "us-gaap:FinanceLeaseRightOfUseAsset",
        "us-gaap:FinanceLeasePrincipalPayments",
        "test:FinancingObligationNoncurrent",
    ],
)
def test_recognized_finance_lease_or_new_activity_still_requires_complete_scope(tag):
    facts, filing, doc, bound = copy.deepcopy(latest_doc("NVDA"))
    pe = filing["end"]
    original = next(
        r
        for r in doc.rows
        if r["concept"] == "us-gaap:FinanceLeaseRightOfUseAssetAccumulatedAmortization"
        and r["end"] == str(pe)
    )
    doc.rows.append({**original, "concept": tag, "value": Decimal(100)})
    assert carrying_debt(doc, facts, filing, bound, pe) == {}


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_total",
        "conflicting_total",
        "wrong_unit",
        "mismatch_sum",
        "missing_api",
    ],
)
def test_asset_only_signal_never_bypasses_debt_note_reconciliation(mutation):
    facts, filing, doc, bound = copy.deepcopy(latest_doc("NVDA"))
    pe = filing["end"]
    tag = "us-gaap:LongTermDebt"
    key = (tag, str(pe), "USD", True)
    row = doc.index[key][0]
    if mutation in {"missing_total", "wrong_unit"}:
        doc.index[key] = []
    if mutation == "conflicting_total":
        doc.index[key].append({**row, "value": row["value"] + 1})
    if mutation == "mismatch_sum":
        doc.index[key] = [{**row, "value": row["value"] + 1}]
    if mutation == "missing_api":
        facts["facts"]["us-gaap"]["LongTermDebt"]["units"]["USD"] = []
    assert carrying_debt(doc, facts, filing, bound, pe) == {}


def test_bac_paid_common_dps_is_not_an_invented_aggregate_or_declared_dividend():
    c = checked("BAC")
    q = c.qualification
    assert q.status == "qualified"
    for p, value in zip(c.periods, ["1.08", "1.00", "0.92", "0.86", "0.78"]):
        item = p.cash_flow_statement.common_dividends_per_share
        assert (
            item.value == Decimal(value)
            and item.xbrl_tag == "us-gaap:CommonStockDividendsPerShareCashPaid"
        )
        assert p.cash_flow_statement.dividends_paid is None
        evidence = next(
            i
            for i in q.inputs
            if i.field_path == "cash_flow_statement.common_dividends_per_share"
            and i.period_end == str(p.fiscal_period_end)
        )
        assert evidence.unit == "USD/shares" and "not declared" in evidence.basis
    a = default_assumptions(c)
    p = compute_projection(c, a)
    assert p.fair_value_per_share == pytest.approx(
        1.08 * (1 + a.terminal_growth) / (a.wacc - a.terminal_growth)
    )
    assert p.fair_value_per_share == pytest.approx(43.128256094354654)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "declared_only",
        "preferred_only",
        "wrong_unit",
        "quarter",
        "conflict",
        "negative",
    ],
)
def test_bac_per_share_gap_stays_blocked_without_matching_annual_common_cash_paid(
    mutation,
):
    c = checked("BAC")
    client = ReusableEdgar("BAC")
    facts = copy.deepcopy(client.facts)
    p = c.periods[0]
    data = facts["facts"]["us-gaap"]["CommonStockDividendsPerShareCashPaid"]["units"]
    rows = data["USD/shares"]
    if mutation in {"missing", "declared_only", "preferred_only"}:
        data["USD/shares"] = []
    if mutation == "wrong_unit":
        data["USD"] = data.pop("USD/shares")
    for row in data.get("USD/shares", []):
        if row["end"] == str(p.fiscal_period_end) and row["accn"] == p.filing_accession:
            if mutation == "quarter":
                row["start"] = "2025-10-01"
            if mutation == "negative":
                row["val"] = -1
    if mutation == "conflict":
        row = next(
            r
            for r in rows
            if r["end"] == str(p.fiscal_period_end) and r["accn"] == p.filing_accession
        )
        rows.append({**row, "val": 2})
    loaded = asyncio.run(
        load_reusable_inputs(
            client, facts, c.cik, p.fiscal_period_end, p.filing_accession, p.industry
        )
    )
    report = qualify(c, facts, {"reusable_inputs": loaded}, p.filing_accession)
    assert report.status == "review_required"
    assert any(
        i.field_path
        in {
            "cash_flow_statement.dividends_paid",
            "cash_flow_statement.common_dividends_per_share",
        }
        for i in report.issues
    )


def test_new_paid_dps_input_is_also_checked_on_existing_reviewed_bank_policy():
    from eval.cold import FixtureEdgar

    client = FixtureEdgar("JPM")
    c = client.saved.model_copy(deep=True)
    c.periods[0].cash_flow_statement.common_dividends_per_share = LineItem(
        value=Decimal("999"),
        source=ExtractionSource.USER_OVERRIDE,
        confidence=1,
        source_quote="This cannot approve an unsupported DPS",
    )
    report = qualify(c, client.facts)
    assert report.status == "review_required" and any(
        i.field_path == "cash_flow_statement.common_dividends_per_share"
        for i in report.issues
    )


def test_explicit_zero_per_share_input_cannot_fall_back_to_an_aggregate_payment():
    c = checked("BAC").model_copy(deep=True)
    c.periods[0].cash_flow_statement.common_dividends_per_share.value = Decimal(0)
    c.periods[0].cash_flow_statement.dividends_paid = LineItem(
        value=Decimal("1000000000"), source=ExtractionSource.USER_OVERRIDE, confidence=1
    )
    with pytest.raises(ValueError, match="positive dividends"):
        compute_projection(c, default_assumptions(c))


def test_additive_paid_dps_field_preserves_old_bank_serialization_and_revision_identity():
    import json
    from pathlib import Path
    from schemas import Company
    from repository import payload_id

    raw = json.loads(
        (Path(__file__).parent / "fixtures/cold/JPM/backfill.json").read_text()
    )
    assert "common_dividends_per_share" not in raw["periods"][0]["cash_flow_statement"]
    parsed = Company.model_validate(raw)
    financial = {k: v for k, v in raw.items() if k != "qualification"}
    assert parsed.model_dump(mode="json", exclude={"qualification"}) == financial
    assert payload_id(
        parsed.model_dump(mode="json", exclude={"qualification"})
    ) == payload_id(financial)
