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

from .ground_truth import GROUND_TRUTH, EVAL_LAST_REFRESHED, TickerGroundTruth, is_within_tolerance


# Sentinel returned by _eval_ticker when the filer has rolled to a new
# fiscal year and the ground truth needs hand-refreshing. The runner
# surfaces it distinctly from real extraction misses so a cron isn't
# spammed with false positives every fall.
NEEDS_REFRESH_SENTINEL = "__needs_refresh__"


async def _eval_ticker(
    ticker: str,
    truth: TickerGroundTruth,
    edgar: EdgarClient,
    anthropic: AsyncAnthropic,
) -> dict[str, Any]:
    """Run Track B against one ticker and score against ground truth.

    Returns a per-field map of {field: {extracted, expected, within_tolerance}},
    OR — when the live 10-K has rolled to a fiscal year newer than the
    ground truth pinned in eval/ground_truth.py — a single-key dict with
    NEEDS_REFRESH_SENTINEL so the runner can surface a "refresh needed"
    warning instead of false-positiving an extraction regression.
    """
    cik = await edgar.get_cik_from_ticker(ticker)
    submissions = await edgar.get_submissions(cik)
    name = submissions.get("name", ticker)
    filing_meta = await edgar.get_latest_10k(cik)

    from datetime import date

    period_end = date.fromisoformat(filing_meta["period_of_report"])
    actual_fy = period_end.year

    # Bail early with a clear signal if the FY no longer matches.
    if actual_fy != truth.fiscal_year:
        return {
            NEEDS_REFRESH_SENTINEL: {
                "ground_truth_fy": truth.fiscal_year,
                "actual_fy": actual_fy,
                "actual_period_end": period_end.isoformat(),
                "accession": filing_meta["accession_number"],
            }
        }

    html = await edgar.get_filing_html(filing_meta["primary_doc_url"])
    section_text = extract_financial_statements_section(html)

    extracted = await extract_track_b(
        client=anthropic,
        ticker=ticker,
        company_name=name,
        period_end=period_end,
        accession_number=filing_meta["accession_number"],
        filing_section_text=section_text,
        fields_to_extract=list(truth.fields.keys()),
    )

    results: dict[str, Any] = {}
    for field, expected in truth.fields.items():
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
    refresh_needed: list[tuple[str, dict[str, Any]]] = []
    for ticker, fields in scores.items():
        if NEEDS_REFRESH_SENTINEL in fields:
            refresh_needed.append((ticker, fields[NEEDS_REFRESH_SENTINEL]))
            continue
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

    if refresh_needed:
        lines.append("")
        lines.append("=" * 70)
        lines.append("GROUND TRUTH NEEDS REFRESH (filer rolled to new fiscal year):")
        for ticker, info in refresh_needed:
            lines.append(
                f"  {ticker}: pinned FY{info['ground_truth_fy']}, "
                f"latest 10-K is FY{info['actual_fy']} "
                f"(period {info['actual_period_end']}, accession {info['accession']})"
            )
        lines.append("  → Refresh eval/ground_truth.py with current-FY numbers.")

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
    for ticker, truth in selected.items():
        try:
            scores[ticker] = await _eval_ticker(ticker, truth, edgar, anthropic)
        except Exception as e:
            print(f"  ERROR running {ticker}: {e}", file=sys.stderr)
            scores[ticker] = {
                f: {
                    "extracted": None,
                    "expected": v,
                    "within_tolerance": False,
                    "miss_reason": f"runner_error: {type(e).__name__}",
                }
                for f, v in truth.fields.items()
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

    # Exit-policy: real extraction failures = exit 1; ground-truth-needs-
    # refresh = exit 2 (so a cron can distinguish "model regressed" from
    # "human action needed"); all-pass = exit 0.
    has_real_fail = any(
        not r["within_tolerance"]
        for fields in scores.values()
        if NEEDS_REFRESH_SENTINEL not in fields
        for r in fields.values()
    )
    any_needs_refresh = any(NEEDS_REFRESH_SENTINEL in fields for fields in scores.values())
    if has_real_fail:
        return 1
    if any_needs_refresh:
        return 2
    return 0


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
