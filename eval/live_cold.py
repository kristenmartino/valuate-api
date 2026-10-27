"""Budget-bounded, authenticated cold runs; candidates never replace active data.

Runs each requested ticker once. No automatic AI retries. Source checks and
publication failures are recorded separately from successful qualification.
"""

import argparse
import concurrent.futures
import json
from pathlib import Path
from ops.migrate_records import request

SAMPLES = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "NVDA",
    "COST",
    "WMT",
    "HD",
    "NKE",
    "JNJ",
    "KO",
    "F",
    "CAT",
    "JPM",
    "PRU",
    "PLD",
    "EOG",
]
REGRESSIONS = ["INTU", "IBM", "BRK-A", "BRK-B", "TXN", "ADBE", "CRM", "CSCO", "PEP"]
HELD_OUT = ["ORCL", "SBUX"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="https://valuate.kristenmartino.ai/api")
    parser.add_argument("--tickers")
    parser.add_argument("--max-extractions", type=int, default=29)
    args = parser.parse_args()
    tickers = list(
        dict.fromkeys(
            args.tickers.split(",")
            if args.tickers
            else SAMPLES + REGRESSIONS + HELD_OUT
        )
    )
    if len(tickers) > args.max_extractions:
        raise RuntimeError("Explicit extraction budget exceeded")
    args.output.mkdir(parents=True, exist_ok=True)

    def run(ticker):
        status, before = request(args.base, "company/" + ticker)
        active = before if status == 200 else None
        status, response = request(args.base, "admin/cold-extract", {"ticker": ticker})
        (args.output / (ticker + ".json")).write_text(
            json.dumps({"http_status": status, "response": response}, indent=2) + "\n"
        )
        if status != 200:
            row = {
                "ticker": ticker,
                "extraction_status": "failed",
                "http_status": status,
                "qualification": "unavailable",
            }
        else:
            candidate = response["company"]
            q = candidate["qualification"]
            row = {
                "ticker": ticker,
                "extraction_status": "composed",
                "qualification": q["status"],
                "candidate_id": response["candidate_id"],
                "issues": q["issues"],
            }
            if q["status"] != "qualified":
                rejected, body = request(
                    args.base, "admin/publish/" + response["candidate_id"], {}
                )
                assert rejected == 422, (
                    ticker,
                    "unqualified publication was not rejected",
                    rejected,
                )
                row["unqualified_publish_rejected"] = True
        after_status, after = request(args.base, "company/" + ticker)
        if active is not None:
            assert after_status == 200 and after == active, (
                ticker,
                "Cold candidate changed the active snapshot",
            )
        else:
            assert after_status == 404, (
                ticker,
                "Cold run unexpectedly published an active record",
            )
        row["active_record_unchanged"] = True
        print(ticker, row["qualification"], flush=True)
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(run, tickers))
    summary = {
        "extraction_budget": args.max_extractions,
        "attempted": len(tickers),
        "composed": sum(r["extraction_status"] == "composed" for r in rows),
        "qualified": sum(r["qualification"] == "qualified" for r in rows),
        "withheld_or_failed": sum(r["qualification"] != "qualified" for r in rows),
        "results": rows,
    }
    (args.output / "report.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}), flush=True)


if __name__ == "__main__":
    main()
