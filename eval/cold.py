"""Cold graph replay: real ingestion/normalization/qualification, recorded AI.

No repository is consulted. Selected SEC facts and inline fixtures are pinned;
latest-period AI responses are replayed to keep CI deterministic and cost-free.
Live AI/external retrieval are exercised separately with the protected runner.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from edgar import EdgarClient
from graph import build_graph
from qualification_service import qualify_record
from schemas import Company, ExtractionSource, LineItem

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures"
COLD = FIXTURES / "cold"
INDEX = json.loads((COLD / "index.json").read_text())


class FixtureEdgar(EdgarClient):
    def __init__(self, ticker):
        super().__init__(user_agent="Valuate offline fixture tests@example.com")
        self.ticker = ticker
        self.saved = Company.model_validate_json(
            (COLD / ticker / "backfill.json").read_text()
        )
        self.facts = json.loads((COLD / ticker / "facts.json").read_text())
        self.submissions = json.loads((COLD / ticker / "submissions.json").read_text())
        self._ticker_cik_map = {ticker: self.saved.cik}

    async def get_company_facts(self, cik):
        return self.facts

    async def get_submissions(self, cik):
        return self.submissions

    async def get_filing_html(self, url):
        if self.ticker.startswith("BRK-"):
            return (FIXTURES / "berkshire-2025-inline.xml").read_text()
        if self.ticker == "CSCO":
            return (
                FIXTURES
                / (
                    "cisco-da-2024.html"
                    if "000085887724" in url
                    else "cisco-da-2026.html"
                )
            ).read_text()
        if self.ticker == "SBUX":
            if "sbux-20231001" in url:
                return (FIXTURES / "starbucks-2023-statements.xml").read_text()
            if "sbux-20250928" in url:
                return (FIXTURES / "starbucks-2025-statements.xml").read_text()
            return "<html>No pinned statement evidence for this accession.</html>"
        if self.ticker == "CRM":
            return (FIXTURES / "salesforce-2026-revenue-inline.xml").read_text()
        return "<html><body>Offline replay: the recorded latest-period backfill response supplies fields absent from tagged facts.</body></html>"


async def run_fixture(ticker):
    client = FixtureEdgar(ticker)
    recorded = {}
    for name in ["income_statement", "balance_sheet", "cash_flow_statement"]:
        stmt = getattr(client.saved.periods[0], name)
        for field in type(stmt).model_fields:
            item = getattr(stmt, field)
            if isinstance(item, LineItem):
                # A cold candidate must never inherit a saved human review label.
                recorded[field] = item.model_copy(
                    update={"source": ExtractionSource.LLM_HTML, "xbrl_tag": None}
                )

    async def backfill(**kwargs):
        return {field: recorded.get(field) for field in kwargs["fields_to_extract"]}

    async def segments(**kwargs):
        return (
            getattr(client.saved.periods[0].income_statement, "revenue_segments", None)
            or []
        )

    with (
        patch("graph.extract_track_b", backfill),
        patch("graph.extract_revenue_segments", segments),
    ):
        result = await build_graph(client, None).ainvoke({"ticker": ticker})
    checked, evidence = await qualify_record(
        result["company"], client=client, facts=result["facts"]
    )
    return checked, evidence


async def main():
    rows = []
    for case in INDEX:
        checked, _ = await run_fixture(case["ticker"])
        q = checked.qualification
        assert q.status == case["expected_status"], (
            case["ticker"],
            q.status,
            [(i.code, i.field_path, i.period_end) for i in q.issues],
        )
        rows.append(
            {
                "ticker": case["ticker"],
                "status": q.status,
                "input_count": len(q.inputs),
                "issues": [i.model_dump() for i in q.issues],
            }
        )
    print(
        json.dumps(
            {"mode": "offline cold graph with recorded AI responses", "results": rows},
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
