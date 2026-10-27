import asyncio
from datetime import date
import pytest
from edgar import EdgarClient


YEAR = date.today().year


def rows(year=YEAR-1):
    return {"form": ["10-K"], "accessionNumber": [f"0000000123-{year%100:02}-000001"],
            "filingDate": [f"{year}-12-31"], "reportDate": [f"{year}-09-30"], "primaryDocument": ["annual.htm"]}


class ArchiveClient(EdgarClient):
    def __init__(self):
        super().__init__(user_agent="Valuate archive unit tests@example.com")
        self.requested = []
        self.recent = {"form": ["10-Q"]}
        self.files = [{"name": "CIK0000000123-submissions-001.json", "filingFrom": f"{YEAR-1}-01-01", "filingTo": f"{YEAR-1}-12-31"}]
        self.archived = rows()
        self.cik = 123

    async def get_submissions(self, cik):
        return {"cik": self.cik, "filings": {"recent": self.recent, "files": self.files}}

    async def _get_json(self, url):
        self.requested.append(url)
        return self.archived


def test_latest_annual_is_found_when_frequent_other_filings_push_it_into_archive():
    c = ArchiveClient(); found = asyncio.run(c.get_latest_10k("0000000123"))
    assert found["period_of_report"] == f"{YEAR-1}-09-30"
    assert len(c.requested) == 1 and c.requested[0].startswith("https://data.sec.gov/submissions/CIK0000000123-")
    assert found["primary_doc_url"].startswith("https://www.sec.gov/Archives/edgar/data/123/")


def test_current_recent_annual_needs_no_redundant_archive_request():
    c = ArchiveClient(); c.recent = rows()
    found = asyncio.run(c.get_latest_10k("0000000123"))
    assert found["period_of_report"] == f"{YEAR-1}-09-30" and not c.requested


@pytest.mark.parametrize("change", ["entity", "archive_name", "unsafe_primary", "future", "no_annual"])
def test_unverified_archive_metadata_cannot_be_used_for_public_extraction(change):
    c = ArchiveClient()
    if change == "entity": c.cik = 999
    if change == "archive_name": c.files[0]["name"] = "https://example.com/foreign.json"
    if change == "unsafe_primary": c.archived["primaryDocument"] = ["../foreign.htm"]
    if change == "future": c.archived["filingDate"] = [f"{YEAR+1}-01-01"]
    if change == "no_annual": c.archived["form"] = ["20-F"]
    with pytest.raises(ValueError): asyncio.run(c.get_latest_10k("0000000123"))
    if change in {"entity", "archive_name"}: assert not c.requested


def test_archive_discovery_is_bounded_when_no_eligible_annual_exists():
    c = ArchiveClient(); c.archived = {"form": []}
    c.files = [{**c.files[0], "name": f"CIK0000000123-submissions-{i:03}.json"} for i in range(25)]
    with pytest.raises(ValueError, match="bounded SEC archive"):
        asyncio.run(c.get_latest_10k("0000000123"))
    assert len(c.requested) == 24
