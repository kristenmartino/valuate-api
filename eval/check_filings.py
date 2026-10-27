"""Read-only filing-change monitor: no AI calls and no publication writes."""

import asyncio
import json
from edgar import EdgarClient
from qualification import POLICIES


async def main():
    client = EdgarClient()
    changes = []
    for key in POLICIES:
        cik, accession = key.split(":")
        submissions = await client.get_submissions(cik)
        recent = submissions.get("filings", {}).get("recent", {})
        annual = [
            acc
            for acc, form in zip(
                recent.get("accessionNumber", []), recent.get("form", [])
            )
            if form in {"10-K", "10-K/A"}
        ]
        latest = {"accession_number": annual[0] if annual else None}
        if latest["accession_number"] != accession:
            changes.append(
                {
                    "cik": cik,
                    "scope_policy_filing": accession,
                    "new_filing": latest["accession_number"],
                }
            )
    print(json.dumps({"new_filings_requiring_scope_review": changes}, indent=2))
    if changes:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
