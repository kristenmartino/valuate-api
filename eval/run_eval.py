"""Track B extraction-quality eval runner.

Scores Claude's Track B extractions against the hand-pinned ground-truth
values in eval/ground_truth.py. Reports per-ticker, per-field accuracy
plus an aggregate score. Useful for catching prompt-drift / model-version
regressions before users see them.

Usage:

    SEC_USER_AGENT="Your Name your@email.com" \\
    ANTHROPIC_API_KEY="sk-ant-..." \\
    python -m eval.run_eval

Optional flags:
    --tickers AAPL,MSFT     run only these (default: all in ground truth)
    --json                  emit machine-readable JSON instead of the table

The eval is intentionally a script, not a pytest test: it's slow (one
Anthropic call per ticker), occasionally needs human review (when a
filing legitimately changes a value year-over-year), and shouldn't gate
PRs. Run it (a) before merging changes to extraction_prompt.py, (b) on
a schedule to catch model-version regressions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Optional

from anthropic import AsyncAnthropic

from edgar import EdgarClient
from extract_track_b import extract_track_b
from extraction_prompt import PROMPT_HASH
from section_extractor import extract_financial_statements_section

from .ground_truth import GROUND_TRUTH, EVAL_LAST_REFRESHED, is_within_tolerance


async def _eval_ticker(
    ticker: str,
    expected_fields: dict[str, float],
    edgar: EdgarClient,
    anthropic: AsyncAnthropic,
) -> dict[str, Any]:
    """Run Track B against one ticker and score against ground truth.

    Returns a per-field map of {field: {extracted, expected, within_tolerance}}.
    """
    cik = await edgar.get_cik_from_ticker(ticker)
    submissions = await edgar.get_submissions(cik)
    name = submissions.get("name", ticker)
    filing_meta = await edgar.get_latest_10k(cik)
    html = await edgar.get_filing_html(filing_meta["primary_doc_url"])
    section_text = extract_financial_statements_section(html)

    from datetime import date

    period_end = date.fromisoformat(filing_meta["period_of_report"])
    extracted = await extract_track_b(
        client=anthropic,
        ticker=ticker,
        company_name=name,
        period_end=period_end,
        accession_number=filing_meta["accession_number"],
        filing_section_text=section_text,
        fields_to_extract=list(expected_fields.keys()),
    )

    results: dict[str, Any] = {}
    for field, expected in expected_fields.items():
        line_item = extracted.get(field)
        if line_item is None:
            results[field] = {
                "extracted": None,
                "expected": expected,
                "within_tolerance": False,
                "miss_reason": "not_extracted",
            }
            continue
        ext_value = float(line_item.value)
        within = is_within_tolerance(ext_value, expected)
        results[field] = {
            "extracted": ext_value,
            "expected": expected,
            "within_tolerance": within,
            "confidence": line_item.confidence,
        }
    return results


def _format_table(scores: dict[str, dict[str, Any]]) -> str:
    """Pretty-print the eval results as a console table."""
    lines = []
    total_correct = 0
    total_fields = 0
    for ticker, fields in scores.items():
        lines.append(f"\n{ticker}")
        lines.append("-" * 70)
        for field, r in fields.items():
            total_fields += 1
            if r["within_tolerance"]:
                total_correct += 1
                marker = "PASS"
            else:
                marker = "FAIL"
            ext = r["extracted"]
            ext_str = f"${ext / 1e9:7.2f}B" if ext is not None else "(none)   "
            exp_str = f"${r['expected'] / 1e9:7.2f}B"
            conf = r.get("confidence")
            conf_str = f" conf={conf:.2f}" if conf is not None else ""
            lines.append(
                f"  [{marker}] {field:35s}  extracted: {ext_str}  expected: {exp_str}{conf_str}"
            )
    lines.append("")
    lines.append("=" * 70)
    pct = (total_correct / total_fields * 100) if total_fields else 0
    lines.append(
        f"Aggregate: {total_correct}/{total_fields} fields within ±0.5% ({pct:.1f}%)"
    )
    lines.append(f"Prompt hash: {PROMPT_HASH}")
    lines.append(f"Ground truth pinned: {EVAL_LAST_REFRESHED}")
    return "\n".join(lines)


async def main(tickers: Optional[list[str]] = None, output_json: bool = False) -> int:
    selected = {t: GROUND_TRUTH[t] for t in (tickers or list(GROUND_TRUTH))}
    if not selected:
        print(f"No matching tickers in ground truth. Available: {list(GROUND_TRUTH)}")
        return 1

    edgar = EdgarClient()
    anthropic = AsyncAnthropic()  # picks up ANTHROPIC_API_KEY from env

    scores: dict[str, dict[str, Any]] = {}
    for ticker, fields in selected.items():
        try:
            scores[ticker] = await _eval_ticker(ticker, fields, edgar, anthropic)
        except Exception as e:
            print(f"  ERROR running {ticker}: {e}", file=sys.stderr)
            scores[ticker] = {
                f: {
                    "extracted": None,
                    "expected": v,
                    "within_tolerance": False,
                    "miss_reason": f"runner_error: {type(e).__name__}",
                }
                for f, v in fields.items()
            }

    if output_json:
        print(
            json.dumps(
                {
                    "scores": scores,
                    "prompt_hash": PROMPT_HASH,
                    "ground_truth_refreshed": EVAL_LAST_REFRESHED,
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(_format_table(scores))

    # Exit non-zero if any field misses tolerance — useful for CI/cron.
    all_pass = all(
        r["within_tolerance"] for fields in scores.values() for r in fields.values()
    )
    return 0 if all_pass else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tickers",
        type=str,
        default=None,
        help="Comma-separated list of tickers to eval (default: all in ground_truth)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON output"
    )
    args = parser.parse_args()
    selected_tickers = (
        [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    )
    sys.exit(asyncio.run(main(selected_tickers, args.json)))
