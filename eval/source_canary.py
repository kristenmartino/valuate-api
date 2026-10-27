"""Scheduled SEC-only cold qualification. No AI, saved record or publication.

Keep discovery separate from approval: a failure produces diagnostics, never
guessed financials or an automatic edit of a consumer's active record.
"""

import argparse
import asyncio
import json
from pathlib import Path

from edgar import EdgarClient
from graph import _make_ingest, validate_company
from qualification import POLICIES
from qualification_service import qualify_record
from reusable_inputs import load_reusable_inputs, compose_reusable

DEFAULT_TICKERS = ["META", "NVDA", "COST", "WMT", "CAT", "BAC", "O", "HD", "KO"]


async def inspect_source(ticker, client):
    state = await _make_ingest(client)({"ticker": ticker})
    if state["cik"].lstrip("0") + ":" + state["filing_accession"] in POLICIES:
        raise ValueError("Use reviewed cold regression coverage for this filing; the SEC-only canary never invokes AI backfill")
    loaded = await load_reusable_inputs(client, state["facts"], state["cik"], state["period_end"], state["filing_accession"], state["industry"])
    company = validate_company(compose_reusable(state, loaded))
    checked, _ = await qualify_record(company, client, state["facts"])
    q = checked.qualification
    return {
        "ticker": state["ticker"], "filing": state["filing_accession"],
        "status": q.status, "issues": [i.model_dump() for i in q.issues],
        "period_count": len(checked.periods), "checked_inputs": len(q.inputs),
        "history_revisions": loaded["history_check"]["revisions"],
        "ai_calls": 0, "publication_writes": 0,
    }


async def run_canary(tickers, client_factory=None, timeout=240):
    client_factory = client_factory or (lambda ticker: EdgarClient())
    rows = []
    for ticker in dict.fromkeys(tickers):
        try:
            row = await asyncio.wait_for(inspect_source(ticker, client_factory(ticker)), timeout)
        except Exception as exc:
            # The public SEC error/context is diagnostic data; no service
            # credentials or application record contents are included.
            row = {"ticker": ticker, "status": "source_failed", "error_type": type(exc).__name__, "reason": str(exc), "ai_calls": 0, "publication_writes": 0}
        rows.append(row)
        print(ticker, row["status"], flush=True)
    return {"mode": "SEC-only cold qualification; no saved records, AI or publication", "results": rows,
            "qualified": sum(r["status"] == "qualified" for r in rows), "attempted": len(rows)}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not 1 <= len(tickers) <= 12:
        raise ValueError("The SEC-only canary is bounded to 1–12 tickers")
    report = await run_canary(tickers, lambda ticker: EdgarClient(), timeout=120)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["qualified"] != report["attempted"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
