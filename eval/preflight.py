"""Cost-free SEC source preflight: no AI, repository writes or publication.

Use this during ticker onboarding to find source/history gaps before a public
search. A complete source checklist still requires independent qualification.
"""

import argparse
import asyncio
import json
from pathlib import Path

from edgar import EdgarClient
from graph import (
    _make_ingest,
    track_a,
    _derive_missing_required,
    _missing_required_for_period,
)
from scoped_inputs import (
    paths as scoped_paths,
    load_scoped_inputs,
    apply_inputs as apply_scoped_inputs,
)
from schemas import Industry
from filing_coverage import filing_items as debt_items
from statement_coverage import specs, load_statement_coverage, apply_items, policy


async def source_preflight(ticker, client):
    state = await _make_ingest(client)({"ticker": ticker})
    if not policy(state["cik"], state["filing_accession"]):
        from reusable_inputs import load_reusable_inputs
        from reusable_qualification import required_paths

        loaded = await load_reusable_inputs(
            client,
            state["facts"],
            state["cik"],
            state["period_end"],
            state["filing_accession"],
            state["industry"],
        )
        periods = [
            {
                "period_end": str(pe),
                "required_fields_missing": sorted(
                    required_paths(state["industry"], pe == state["period_end"], items)
                    - items.keys()
                ),
            }
            for pe, items in loaded["inputs"].items()
        ]
        model_issues = [loaded["model_issue"]] if loaded.get("model_issue") else []
        model_issues.extend(p["reason"] for p in loaded.get("history_check", {}).get("issues", []))
        complete = not model_issues and not any(
            p["required_fields_missing"] for p in periods
        )
        return {
            "ticker": state["ticker"],
            "filing": state["filing_accession"],
            "mode": "SEC sources only; no AI or publication",
            "reviewed_filing_policy": False,
            "source_ruleset": loaded["ruleset"],
            "source_checklist_complete": complete,
            "periods": periods,
            "statement_evidence_missing": model_issues,
            "next_step": "independent qualification required"
            if complete
            else "resolve source or model-scope gaps",
            "qualification_granted": False,
        }
    state.update(await track_a(state))
    configured = specs(state["cik"], state["filing_accession"])
    loaded = {"inputs": {}, "missing": []}
    if configured or state["industry"] in {Industry.STANDARD, Industry.ENERGY}:
        loaded = await load_statement_coverage(
            client,
            state["facts"],
            state["cik"],
            state["period_end"],
            state["filing_accession"],
            state["period_ends"],
            candidate_cash_only=not bool(configured),
        )
        for pe, selected in loaded["inputs"].items():
            apply_items(state["periods_items"][pe], selected)
            state["periods_items"][pe].update(
                debt_items(
                    state["facts"],
                    state["cik"],
                    pe,
                    state["filing_accession"],
                    state["period_end"],
                    zero_current_borrowings=loaded.get("zero_current_borrowings"),
                )
            )
    if scoped_paths(state["cik"], state["filing_accession"]):
        scoped = await load_scoped_inputs(
            client,
            state["facts"],
            state["cik"],
            state["period_end"],
            state["filing_accession"],
            state["period_ends"],
        )
        for pe, selected in scoped["inputs"].items():
            apply_scoped_inputs(state["periods_items"][pe], selected)
        loaded["missing"].extend(scoped["missing"])
    reviewed = policy(state["cik"], state["filing_accession"])
    periods = []
    for pe in state["period_ends"]:
        items = _derive_missing_required(state["periods_items"][pe], state["industry"])
        missing = _missing_required_for_period(items, state["industry"])
        for path in reviewed.get("required_history_context", []) + reviewed.get(
            "required_history_inputs", []
        ):
            if items.get(path.split(".")[1]) is None:
                missing.append(path)
        if pe == state["period_end"] and state["industry"] in {
            Industry.STANDARD,
            Industry.ENERGY,
        }:
            for field in [
                "short_term_debt",
                "long_term_debt",
                "operating_lease_liabilities",
            ]:
                if items.get(field) is None:
                    missing.append("balance_sheet." + field)
        periods.append(
            {"period_end": str(pe), "required_fields_missing": sorted(set(missing))}
        )
    complete = (
        not any(p["required_fields_missing"] for p in periods) and not loaded["missing"]
    )
    return {
        "ticker": state["ticker"],
        "filing": state["filing_accession"],
        "mode": "SEC sources only; no AI or publication",
        "reviewed_filing_policy": bool(reviewed),
        "source_checklist_complete": complete,
        "periods": periods,
        "statement_evidence_missing": loaded["missing"],
        "next_step": "independent qualification required"
        if complete and reviewed
        else "resolve source gaps and review filing scope",
        "qualification_granted": False,
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tickers", required=True, help="Comma-separated onboarding tickers"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    client = EdgarClient()
    results = []
    for ticker in dict.fromkeys(
        t.strip().upper() for t in args.tickers.split(",") if t.strip()
    ):
        try:
            results.append(await source_preflight(ticker, client))
        except Exception as exc:
            results.append(
                {
                    "ticker": ticker,
                    "source_checklist_complete": False,
                    "qualification_granted": False,
                    "error_type": type(exc).__name__,
                }
            )
    report = json.dumps({"source_preflight": results}, indent=2) + "\n"
    if args.output:
        args.output.write_text(report)
    print(report, end="")
    if any(not r.get("source_checklist_complete") for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
