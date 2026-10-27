"""Cross-filing comparability is distinct from matching individual share facts."""

import asyncio
import copy
from datetime import date
from decimal import Decimal
import pytest

from eval.reusable_cold import run_fixture
from reusable_inputs import selection
from share_basis import PATH, SPLIT, reconcile_share_history
from dcf import compute_projection, default_assumptions
from schemas import ExtractionSource


def history(factor="10"):
    # Deliberately unrelated to NVDA: no ticker-specific rule can pass this case.
    def filed(year, month=3):
        return {"accession": f"0000000123-{year % 100:02}-000001",
                "filed": date(year, month, 1)}

    new, old = filed(2026), filed(2025)

    def shares(year, value, filing):
        row = {"concept": "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
               "value": Decimal(value), "unit": "shares", "end": f"{year}-12-31",
               "start": f"{year}-01-01", "accn": filing["accession"],
               "source_url": "https://www.sec.gov/example.htm", "decimals": "INF"}
        return selection(row["value"], [row], "Annual diluted shares", "Filed annual shares", direct=row["concept"])

    sources = [(new, {date(2025, 12, 31): shares(2025, "980", new),
                      date(2024, 12, 31): shares(2024, "900", new)}),
               (old, {date(2024, 12, 31): shares(2024, str(Decimal(900) / Decimal(factor)), old),
                      date(2023, 12, 31): shares(2023, "110", old)})]
    inputs = {pe: {PATH: s, "untouched": object()} for _, ss in sources for pe, s in ss.items()}
    event = {"end": "2025-06-30", "val": Decimal(factor), "accn": "0000000123-25-000002",
             "filed": "2025-08-01", "form": "10-Q", "fp": "Q2"}
    facts = {"cik": 123, "facts": {"us-gaap": {SPLIT: {"units": {"pure": [event]}}}}}
    return inputs, sources, facts


@pytest.mark.parametrize("factor", ["10", "3", "0.1"])
def test_forward_and_reverse_splits_require_same_period_bridge_and_event(factor):
    inputs, sources, facts = history(factor)
    others = {pe: fields["untouched"] for pe, fields in inputs.items()}
    latest = sources[0][1][date(2025, 12, 31)]
    assert reconcile_share_history(inputs, sources, facts)
    assert inputs[date(2023, 12, 31)][PATH].item.value == Decimal(110) * Decimal(factor)
    assert inputs[date(2025, 12, 31)][PATH] is latest
    assert all(inputs[pe]["untouched"] is v for pe, v in others.items())
    adjusted = inputs[date(2023, 12, 31)][PATH]
    assert adjusted.item.source == ExtractionSource.DERIVED
    assert any(r["unit"] == "pure" and r["usage"] == "split conversion ratio" for r in adjusted.rows)


@pytest.mark.parametrize("mutation", ["absent", "wrong_ratio", "future", "before_old_filing", "wrong_unit", "amendment", "invalid_date", "nonfinite", "conflict", "amended_conflict", "nonfinite_duplicate", "annual_context", "no_overlap", "inconsistent_overlaps"])
def test_unknown_or_conflicting_basis_never_silently_becomes_comparable(mutation):
    inputs, sources, facts = history()
    rows = facts["facts"]["us-gaap"][SPLIT]["units"]["pure"]
    if mutation == "absent": rows.clear()
    if mutation == "wrong_ratio": rows[0]["val"] = 5
    if mutation == "future": rows[0]["filed"] = "2027-01-01"
    if mutation == "before_old_filing": rows[0]["end"] = "2023-06-30"
    if mutation == "wrong_unit": facts["facts"]["us-gaap"][SPLIT]["units"] = {"USD": rows}
    if mutation == "amendment": rows[0]["form"] = "10-K/A"
    if mutation == "invalid_date": rows[0]["end"] = "invalid"
    if mutation == "nonfinite": rows[0]["val"] = "NaN"
    if mutation == "conflict": rows.append({**rows[0], "val": 5})
    if mutation == "amended_conflict": rows.append({**rows[0], "form": "10-Q/A", "val": 5})
    if mutation == "nonfinite_duplicate": rows.append({**rows[0], "val": "NaN"})
    if mutation == "annual_context": sources[1][1][date(2024, 12, 31)].rows[0]["start"] = "2024-02-01"
    if mutation == "no_overlap": sources[1][1].pop(date(2024, 12, 31))
    if mutation == "inconsistent_overlaps":
        sources[0][1][date(2023, 12, 31)] = copy.deepcopy(sources[1][1][date(2023, 12, 31)])
        # The extra overlap is on the old basis while the first requires 10x.
        sources[1][1][date(2022, 12, 31)] = copy.deepcopy(sources[1][1][date(2023, 12, 31)])
    with pytest.raises(ValueError, match="share|Share"):
        reconcile_share_history(inputs, sources, facts)


def test_large_year_to_year_growth_is_not_treated_as_a_split():
    inputs, sources, facts = history("1")
    sources[0][1][date(2025, 12, 31)].item.value = Decimal(100000)
    facts["facts"]["us-gaap"].clear()
    assert not reconcile_share_history(inputs, sources, facts)
    assert inputs[date(2023, 12, 31)][PATH].item.value == 110


def test_nvda_cold_history_qualifies_on_one_basis_without_changing_latest_model():
    c, _ = asyncio.run(run_fixture("NVDA"))
    assert c.qualification.status == "qualified", c.qualification.issues
    assert [p.income_statement.diluted_shares_outstanding.value for p in c.periods] == list(map(Decimal, ["24514000000", "24804000000", "24940000000", "25070000000", "25350000000"]))
    assert c.share_count_label == "Diluted shares (split-adjusted history)"
    assert "latest valuation denominator is unchanged" in c.valuation_note
    assert compute_projection(c, default_assumptions(c)).fair_value_per_share == pytest.approx(124.38297259189262)
    for p in c.periods[-2:]:
        item = p.income_statement.diluted_shares_outstanding
        assert item.source == ExtractionSource.DERIVED and "split factor 10" in item.source_quote
    # Reverting an older share amount to its original basis fails independent
    # qualification even if it is relabeled as a saved human correction.
    from reusable_inputs import load_reusable_inputs
    from eval.reusable_cold import ReusableEdgar
    from qualification import qualify
    client = ReusableEdgar("NVDA"); p = c.periods[0]
    loaded = asyncio.run(load_reusable_inputs(client, client.facts, c.cik, p.fiscal_period_end, p.filing_accession, p.industry))
    old = c.model_copy(deep=True)
    old.periods[-1].income_statement.diluted_shares_outstanding.value /= 10
    old.periods[-1].income_statement.diluted_shares_outstanding.source = ExtractionSource.USER_OVERRIDE
    q = qualify(old, client.facts, {"reusable_inputs": loaded}, p.filing_accession)
    assert q.status == "review_required" and any(i.code == "filing_value_unverified" for i in q.issues)


def test_missing_split_evidence_blocks_cold_graph_and_public_valuation(monkeypatch):
    from eval.reusable_cold import ReusableEdgar
    from graph import CompositionError
    from qualification_service import qualify_record
    from fastapi.testclient import TestClient
    from app import main as api
    from repository import InMemoryRepo

    valid, _ = asyncio.run(run_fixture("NVDA"))
    client = ReusableEdgar("NVDA")
    client.facts["facts"]["us-gaap"].pop(SPLIT)
    # run_fixture explicitly forbids AI calls; no alternative may guess a basis.
    with pytest.raises(CompositionError, match="share bases conflict"):
        asyncio.run(run_fixture("NVDA", client))
    withheld, _ = asyncio.run(qualify_record(valid, client, client.facts))
    assert withheld.qualification.status == "review_required"
    repo = InMemoryRepo()
    asyncio.run(repo.set("NVDA", withheld))
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {})
    public = TestClient(api.app)
    assert public.get("/value/NVDA/defaults").status_code == 422
    response = public.post("/value/NVDA", json={
        "assumptions": default_assumptions(valid).model_dump(mode="json")
    })
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "valuation_not_qualified"
