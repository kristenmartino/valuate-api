"""Qualification is filing/record-bound, source-backed and enforced by the API."""

import asyncio
import copy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from dcf import default_assumptions
from eval.cold import FixtureEdgar, INDEX, run_fixture
from overrides import apply_override
from qualification import qualify, current_qualification, record_digest, POLICIES
from repository import InMemoryRepo, payload_id, RecordConflict


def saved(ticker="MSFT"):
    return FixtureEdgar(ticker).saved


def checked(ticker="MSFT"):
    c = saved(ticker)
    report = qualify(c, FixtureEdgar(ticker).facts)
    assert report.status == "qualified", report.issues
    return c.model_copy(update={"qualification": report})


def api_client(monkeypatch, c=None):
    from app import main as api

    repo = InMemoryRepo()
    if c:
        asyncio.run(repo.set(c.ticker, c))
    monkeypatch.setattr(api, "_repo", repo)
    monkeypatch.setattr(api, "_company_locks", {})
    monkeypatch.setenv("VALUATE_OVERRIDE_TOKEN", "qualification-unit-test-token")
    return TestClient(api.app), repo


@pytest.mark.parametrize("case", INDEX, ids=lambda case: case["ticker"])
def test_cold_graph_replays_sources_without_any_saved_repository(case):
    c, evidence = asyncio.run(run_fixture(case["ticker"]))
    assert c.qualification.status == case["expected_status"]
    assert c.qualification.evidence_digest
    assert evidence["facts"]
    for p in c.periods:
        for stmt in [p.income_statement, p.balance_sheet, p.cash_flow_statement]:
            assert not any(
                getattr(getattr(stmt, k), "source", None) == "user_override"
                for k in type(stmt).model_fields
            )
    if c.qualification.status != "qualified":
        assert c.qualification.issues


def test_checked_filing_exposes_defaults_and_only_qualified_valuation(monkeypatch):
    c = checked()
    client, _ = api_client(monkeypatch, c)
    a = client.get("/value/MSFT/defaults")
    assert a.status_code == 200
    response = client.post(
        "/value/MSFT",
        json={"record_digest": c.qualification.record_digest, "assumptions": a.json()},
    )
    assert response.status_code == 200
    assert response.json()["source_record_digest"] == record_digest(c)
    assert response.json()["projection"]["fair_value_per_share"] == pytest.approx(
        248.2311000066803
    )
    assert (
        client.post(
            "/value/MSFT", json={"record_digest": "stale", "assumptions": a.json()}
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "mutation",
    ["latest_debt", "older_da", "filing", "tag", "quote", "share_basis", "policy"],
)
def test_saved_qualification_cannot_survive_changed_inputs_or_policy(
    monkeypatch, mutation
):
    c = checked()
    p = c.periods[0]
    if mutation == "latest_debt":
        c = c.model_copy(
            update={
                "periods": [
                    p.model_copy(
                        update={
                            "balance_sheet": p.balance_sheet.model_copy(
                                update={
                                    "long_term_debt": p.balance_sheet.long_term_debt.model_copy(
                                        update={"value": Decimal(1)}
                                    )
                                }
                            )
                        }
                    ),
                    *c.periods[1:],
                ]
            }
        )
    if mutation == "older_da":
        old = c.periods[-1]
        new = old.model_copy(
            update={
                "cash_flow_statement": old.cash_flow_statement.model_copy(
                    update={
                        "depreciation_amortization": old.cash_flow_statement.depreciation_amortization.model_copy(
                            update={"value": Decimal(1)}
                        )
                    }
                )
            }
        )
        c = c.model_copy(update={"periods": [*c.periods[:-1], new]})
    if mutation == "filing":
        c = c.model_copy(
            update={
                "periods": [
                    p.model_copy(update={"filing_accession": "0000789019-27-000001"}),
                    *c.periods[1:],
                ]
            }
        )
    if mutation in ["tag", "quote"]:
        key = "xbrl_tag" if mutation == "tag" else "source_quote"
        c = c.model_copy(
            update={
                "periods": [
                    p.model_copy(
                        update={
                            "income_statement": p.income_statement.model_copy(
                                update={
                                    "revenue": p.income_statement.revenue.model_copy(
                                        update={key: "changed"}
                                    )
                                }
                            )
                        }
                    ),
                    *c.periods[1:],
                ]
            }
        )
    if mutation == "share_basis":
        c = c.model_copy(update={"share_count_label": "different share class"})
    if mutation == "policy":
        c = c.model_copy(
            update={
                "qualification": c.qualification.model_copy(
                    update={"policy_version": "old"}
                )
            }
        )
    assert current_qualification(c).status == "review_required"
    client, _ = api_client(monkeypatch, c)
    assert client.get("/value/MSFT/defaults").status_code == 422
    assert (
        client.post(
            "/value/MSFT",
            json={"assumptions": default_assumptions(saved()).model_dump()},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("ticker", ["ADBE", "CRM", "EOG", "PEP", "TXN"])
def test_unresolved_coverage_still_blocks_caller_supplied_assumptions(
    monkeypatch, ticker
):
    c = saved(ticker)
    # An incorrect current-debt amount must still fail after the five filings
    # gain reviewed scope; saved confidence/notes cannot rescue the mismatch.
    from schemas import LineItem, ExtractionSource
    p = c.periods[0]
    p = p.model_copy(update={"balance_sheet": p.balance_sheet.model_copy(update={"short_term_debt": LineItem(value=Decimal(1), source=ExtractionSource.XBRL, confidence=1)})})
    c = c.model_copy(update={"periods": [p, *c.periods[1:]]})
    q = qualify(c, FixtureEdgar(ticker).facts)
    assert q.status == "review_required"
    c = c.model_copy(update={"qualification": q})
    client, _ = api_client(monkeypatch, c)
    from app import main as api

    def forbidden(*args, **kwargs):
        raise AssertionError("Unqualified input reached calculation")

    monkeypatch.setattr(api, "compute_projection", forbidden)
    assert client.get(f"/value/{ticker}/defaults").status_code == 422
    result = client.post(
        f"/value/{ticker}",
        json={
            "assumptions": default_assumptions(c).model_dump(),
            "monte_carlo": {"iterations": 100},
            "sensitivity": {"revenue_growth_steps": 2, "operating_margin_steps": 2},
        },
    )
    assert (
        result.status_code == 422
        and result.json()["detail"]["code"] == "valuation_not_qualified"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "entity",
        "unit",
        "quarter_duration",
        "quarter_label",
        "form",
        "filed",
        "conflict",
        "missing_history",
    ],
)
def test_annual_provenance_errors_prevent_qualification(mutation):
    c = saved()
    f = copy.deepcopy(FixtureEdgar("MSFT").facts)
    data = f["facts"]["us-gaap"]["Depreciation"]["units"]
    latest = c.periods[0]
    if mutation == "entity":
        f["cik"] = 999999
    if mutation == "unit":
        data["EUR"] = data.pop("USD")
    for row in data.get("USD", []):
        if (
            row.get("end") != str(latest.fiscal_period_end)
            or row.get("accn") != latest.filing_accession
        ):
            continue
        if mutation == "quarter_duration":
            row["start"] = "2026-04-01"
        if mutation == "quarter_label":
            row["fp"] = "Q4"
        if mutation == "form":
            row["form"] = "10-Q"
        if mutation == "filed":
            row["filed"] = "2028-01-01"
    if mutation == "conflict":
        row = next(r for r in data["USD"] if r["end"] == str(latest.fiscal_period_end))
        data["USD"].append({**row, "val": 1})
    if mutation == "missing_history":
        data["USD"] = [
            r for r in data["USD"] if r["end"] != str(c.periods[-1].fiscal_period_end)
        ]
    q = qualify(c, f)
    assert q.status == "review_required" and q.issues


def test_unregistered_filing_is_not_approved_by_matching_numbers_or_confidence(
    monkeypatch,
):
    c = saved()
    facts = FixtureEdgar("MSFT").facts
    monkeypatch.delitem(
        POLICIES, c.cik.lstrip("0") + ":" + c.periods[0].filing_accession
    )
    q = qualify(c, facts)
    assert q.status == "review_required"
    assert any(i.code == "statement_evidence_unavailable" for i in q.issues)


def test_override_does_not_grant_qualification_even_at_confidence_one(monkeypatch):
    c = checked()
    client, repo = api_client(monkeypatch, c)
    result = client.put(
        "/company/MSFT/override",
        headers={"Authorization": "Bearer qualification-unit-test-token"},
        json={
            "field_path": "cash_flow_statement.depreciation_amortization",
            "value": "1",
            "source_quote": "A user-supplied claim does not establish full D&A",
        },
    )
    assert result.status_code == 200
    assert result.json()["qualification"]["status"] == "review_required"
    assert (
        result.json()["periods"][0]["cash_flow_statement"]["depreciation_amortization"][
            "confidence"
        ]
        == 1
    )
    assert client.get("/value/MSFT/defaults").status_code == 422
    amended = asyncio.run(repo.get("MSFT"))
    assert qualify(amended, FixtureEdgar("MSFT").facts).status == "review_required"


@pytest.mark.parametrize(
    "path,method",
    [
        ("/admin/import/MSFT", "post"),
        ("/admin/qualify/MSFT", "post"),
        ("/admin/repair-coverage/ADBE", "post"),
        ("/admin/cold-extract", "post"),
        ("/admin/publish/fake", "post"),
        ("/admin/history/MSFT", "get"),
    ],
)
def test_admin_routes_fail_closed_without_credentials(monkeypatch, path, method):
    client, _ = api_client(monkeypatch)
    monkeypatch.delenv("VALUATE_OVERRIDE_TOKEN")
    kwargs = (
        {
            "json": saved().model_dump(mode="json")
            if "/import/" in path
            else {"ticker": "MSFT"}
        }
        if method == "post"
        else {}
    )
    assert getattr(client, method)(path, **kwargs).status_code == 401


def test_import_strips_claimed_approval_and_cannot_overwrite_review(monkeypatch):
    c = checked()
    client, repo = api_client(monkeypatch)
    headers = {"Authorization": "Bearer qualification-unit-test-token"}
    result = client.post(
        "/admin/import/MSFT", headers=headers, json=c.model_dump(mode="json")
    )
    assert result.status_code == 200
    assert asyncio.run(repo.get("MSFT")).qualification is None
    assert client.get("/value/MSFT/defaults").status_code == 422
    assert (
        client.post(
            "/admin/import/MSFT", headers=headers, json=c.model_dump(mode="json")
        ).status_code
        == 409
    )


def test_cold_candidate_cannot_replace_active_review_or_publish_unqualified(
    monkeypatch,
):
    from app import main as api

    c = apply_override(
        checked(),
        "income_statement.selling_general_administrative",
        Decimal(7157000000),
        "Preserve this exact saved review",
    )
    c = c.model_copy(update={"qualification": qualify(c, FixtureEdgar("MSFT").facts)})
    client, repo = api_client(monkeypatch, c)
    candidate = apply_override(
        saved(),
        "cash_flow_statement.depreciation_amortization",
        Decimal(1),
        "Incomplete candidate",
    )

    async def extract(ticker):
        return candidate.model_copy(
            update={"qualification": qualify(candidate, FixtureEdgar("MSFT").facts)}
        ), {"facts": FixtureEdgar("MSFT").facts}

    monkeypatch.setattr(api, "_extract_candidate", extract)
    headers = {"Authorization": "Bearer qualification-unit-test-token"}
    response = client.post(
        "/admin/cold-extract", headers=headers, json={"ticker": "MSFT"}
    )
    assert (
        response.status_code == 200
        and response.json()["company"]["qualification"]["status"] == "review_required"
    )
    assert asyncio.run(repo.get("MSFT")) == c

    async def qualification(record):
        return record.model_copy(
            update={"qualification": qualify(record, FixtureEdgar("MSFT").facts)}
        ), {}

    monkeypatch.setattr(api, "qualify_record", qualification)
    result = client.post(
        "/admin/publish/" + response.json()["candidate_id"], headers=headers
    )
    assert result.status_code == 422
    assert asyncio.run(repo.get("MSFT")) == c


def test_public_extract_returns_saved_review_without_cold_rerun(monkeypatch):
    from app import main as api
    from app.rate_limit import require_extract_rate_limit

    c = checked()
    client, _ = api_client(monkeypatch, c)

    async def forbidden(ticker):
        raise AssertionError("Public cache lookup reran extraction")

    monkeypatch.setattr(api, "_extract_candidate", forbidden)
    api.app.dependency_overrides[require_extract_rate_limit] = lambda: None
    try:
        response = client.post("/extract", json={"ticker": "MSFT"})
        assert response.status_code == 200 and response.json() == c.model_dump(
            mode="json"
        )
    finally:
        api.app.dependency_overrides.pop(require_extract_rate_limit, None)


def test_stale_repository_write_cannot_replace_a_new_review():
    async def scenario():
        repo = InMemoryRepo()
        c = checked()
        await repo.set("MSFT", c)
        expected = payload_id(c.model_dump(mode="json"))
        reviewed = apply_override(
            c,
            "income_statement.selling_general_administrative",
            Decimal(1),
            "Newer exact note",
        )
        await repo.set("MSFT", reviewed, expected_id=expected)
        with pytest.raises(RecordConflict):
            await repo.set("MSFT", c, expected_id=expected)
        await repo.set("MSFT", c)  # Compatibility repair retains concurrent reviews.
        assert (await repo.get("MSFT")).periods[
            0
        ].income_statement.selling_general_administrative == reviewed.periods[
            0
        ].income_statement.selling_general_administrative
        assert len(await repo.versions("MSFT")) >= 2

    asyncio.run(scenario())


def test_dropping_a_difficult_historical_year_does_not_qualify_a_shorter_history():
    c = saved()
    c = c.model_copy(update={"periods": c.periods[:-1]})
    report = qualify(c, FixtureEdgar("MSFT").facts)
    assert report.status == "review_required"
    assert any(issue.code == "history_coverage_changed" for issue in report.issues)


def test_qualification_and_valuation_responses_cannot_cache_stale_approval(monkeypatch):
    client, _ = api_client(monkeypatch, checked())
    assert client.get("/company/MSFT").headers["cache-control"] == "no-store"
    assert client.get("/value/MSFT/defaults").headers["cache-control"] == "no-store"
