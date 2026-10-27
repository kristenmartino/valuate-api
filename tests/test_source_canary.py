import asyncio
from unittest.mock import patch
from eval.source_canary import run_canary
from eval.reusable_cold import ReusableEdgar


def test_canary_distinguishes_qualified_source_failed_and_review_required_without_writes():
    def factory(ticker):
        if ticker == "BROKEN": raise ValueError("Source unavailable")
        return ReusableEdgar(ticker)
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
