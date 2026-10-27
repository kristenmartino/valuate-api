"""Source-only cold replays for filings with no issuer-specific policy.

Fixtures retain all inline numeric facts/contexts and original relationships,
with surrounding prose removed. Manifests pin original and fixture hashes.
Company-facts fixtures retain annual records from 2020 onward. No saved Company
or AI response is an input, and the production extraction/check paths are used.
"""

import asyncio
import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from edgar import EdgarClient
from graph import build_graph
from qualification_service import qualify_record

ROOT = Path(__file__).resolve().parents[1] / "tests/fixtures/reusable"
INDEX = json.loads((ROOT / "index.json").read_text())


class ReusableEdgar(EdgarClient):
    def __init__(self, ticker):
        super().__init__(user_agent="Valuate offline source replay tests@example.com")
        self.ticker = ticker
        self.root = ROOT / ticker
        self.manifest = json.loads((self.root / "manifest.json").read_text())
        self.facts = json.loads(self.read("facts.json"))
        self.submissions = json.loads(self.read("submissions.json"))
        self._ticker_cik_map = {ticker: str(self.facts["cik"]).zfill(10)}

    def read(self, name):
        spec = self.manifest[name]
        raw = gzip.decompress((self.root / spec["file"]).read_bytes())
        assert hashlib.sha256(raw).hexdigest() == spec["fixture_sha256"], name
        return raw.decode()

    async def get_submissions(self, cik):
        return self.submissions

    async def get_company_facts(self, cik):
        return self.facts

    async def _get_json(self, url):
        return json.loads(self.read(url.rsplit("/", 1)[-1]))

    async def get_filing_html(self, url):
        return self.read(url.rsplit("/", 1)[-1])


async def run_fixture(ticker, client=None):
    client = client or ReusableEdgar(ticker)

    async def forbidden(**kwargs):
        raise AssertionError("Reusable source rules attempted an AI request")

    with (
        patch("graph.extract_track_b", forbidden),
        patch("graph.extract_revenue_segments", forbidden),
    ):
        result = await build_graph(client, None).ainvoke({"ticker": ticker})
    company, evidence = await qualify_record(result["company"], client, result["facts"])
    return company, evidence


async def main():
    rows = []
    for case in INDEX:
        company, _ = await run_fixture(case["ticker"])
        rows.append(
            {
                "ticker": case["ticker"],
                "status": company.qualification.status,
                "issues": [i.model_dump() for i in company.qualification.issues],
            }
        )
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
