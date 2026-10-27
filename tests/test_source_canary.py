import asyncio
from unittest.mock import patch
from eval.source_canary import run_canary
from eval.reusable_cold import ReusableEdgar


def test_canary_distinguishes_qualified_source_failed_and_review_required_without_writes():
    class MissingParent(ReusableEdgar):
        async def get_filing_html(self, url):
            return (await super().get_filing_html(url)).replace("us-gaap:ParentMember", "test:UnestablishedOwnershipMember")
    def factory(ticker):
        if ticker == "BROKEN": raise ValueError("Source unavailable")
        return MissingParent(ticker) if ticker == "MET" else ReusableEdgar(ticker)
    def forbidden(*args, **kwargs): raise AssertionError("Canary attempted a repository write or AI call")
    with patch("repository.InMemoryRepo.set", forbidden), patch("graph.extract_track_b", forbidden):
        report = asyncio.run(run_canary(["NVDA", "MET", "BROKEN", "NVDA"], factory))
    assert [r["status"] for r in report["results"]] == ["qualified", "review_required", "source_failed"]
    assert report["attempted"] == 3 and report["qualified"] == 1
    assert all(r["ai_calls"] == r["publication_writes"] == 0 for r in report["results"])


def test_canary_has_a_per_ticker_timeout():
    class Slow:
        async def get_cik_from_ticker(self, ticker): await asyncio.sleep(1)
    report = asyncio.run(run_canary(["SLOW"], lambda ticker: Slow(), timeout=0.001))
    assert report["results"][0]["status"] == "source_failed"
    assert report["results"][0]["error_type"] == "TimeoutError"


def test_scheduled_and_manual_workflow_use_the_current_cold_cohort():
    from pathlib import Path
    from eval.source_canary import DEFAULT_TICKERS
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/source-canary.yml").read_text()
    cohort = ",".join(DEFAULT_TICKERS)
    assert f"default: {cohort}" in workflow
    assert f"inputs.tickers || '{cohort}'" in workflow
    assert "MET" in DEFAULT_TICKERS
