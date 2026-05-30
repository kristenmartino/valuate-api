"""Extraction-quality eval runner.

Scores the extraction pipeline against the hand-pinned, SEC-sourced ground
truth in eval/ground_truth.py, separately for each track:

  Track A (XBRL)   — extract_track_a over the filer's company-facts.
  Track B (Claude) — extract_track_b reading the filing HTML, for fields our
                     canonical concept map doesn't cover.

Each field resolves to one of four states:

  PASS     extracted value within ±0.5% of ground truth
  FAIL     extraction mismatch (or the expected track didn't extract it)
  REFRESH  the live 10-K rolled to a fiscal year newer than the pinned
           ground truth — needs a hand refresh, NOT an extraction bug
  SKIP     field's expected track wasn't run (e.g. --track-a-only skips
           Track-B fields), or field marked not-applicable

Reports per ticker, per field, per track, and an overall accuracy baseline.

Usage:

    SEC_USER_AGENT="Your Name your@email.com" \\
    ANTHROPIC_API_KEY="sk-ant-..." \\
    python -m eval.run_eval

Flags:
    --tickers AAPL,MSFT   run only these (default: all in ground truth)
    --json                machine-readable JSON instead of the table
    --readme              emit the README accuracy-baseline markdown table
    --track-a-only        skip Track B / Claude (no ANTHROPIC_API_KEY needed);
                          Track-B fields are reported SKIP

Exit codes: 0 = all PASS/SKIP, 1 = a real extraction FAIL, 2 = no fails but a
ground-truth refresh is needed (so a cron can tell "model regressed" from
"human action needed").

The eval is intentionally a script, not a pytest test: Track B is slow (one
Anthropic call per ticker), it occasionally needs human review when a filing
legitimately changes a value year-over-year, and it shouldn't gate PRs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from typing import Any, Optional

from anthropic import AsyncAnthropic

from edgar import (
    BANK_CANONICAL_CONCEPTS,
    INSURANCE_CANONICAL_CONCEPTS,
    REIT_CANONICAL_CONCEPTS,
    STANDARD_CANONICAL_CONCEPTS,
    EdgarClient,
)
from extract_track_a import extract_track_a
from extract_track_b import extract_track_b
from extraction_prompt import PROMPT_HASH
from section_extractor import extract_financial_statements_section

from .ground_truth import (
    EVAL_LAST_REFRESHED,
    GROUND_TRUTH,
    SOURCE_DERIVED,
    SOURCE_EITHER,
    SOURCE_LLM,
    SOURCE_XBRL,
    FieldTruth,
    TickerGroundTruth,
    is_within_tolerance,
)

# Field states.
PASS = "PASS"
FAIL = "FAIL"
REFRESH = "REFRESH"
SKIP = "SKIP"

# Industry → which canonical concept map Track A should use. Energy filers use
# the standard schema (the difference is the valuation, not the line items).
INDUSTRY_CONCEPTS: dict[str, dict[str, list[str]]] = {
    "std": STANDARD_CANONICAL_CONCEPTS,
    "energy": STANDARD_CANONICAL_CONCEPTS,
    "bank": BANK_CANONICAL_CONCEPTS,
    "insurer": INSURANCE_CANONICAL_CONCEPTS,
    "reit": REIT_CANONICAL_CONCEPTS,
}

# Track label used in the per-track rollup.
TRACK_A = "Track A (XBRL)"
TRACK_B = "Track B (Claude)"
TRACK_DERIVED = "Derived"


def _track_label(source: str, used_track_b: bool) -> str:
    if source == SOURCE_XBRL:
        return TRACK_A
    if source == SOURCE_LLM:
        return TRACK_B
    if source == SOURCE_DERIVED:
        return TRACK_DERIVED
    # "either": labelled by whichever track actually produced the value.
    return TRACK_B if used_track_b else TRACK_A


def _score_field(
    field: str,
    truth: FieldTruth,
    a_items: dict[str, Any],
    b_items: dict[str, Any],
    track_a_only: bool,
) -> dict[str, Any]:
    """Score one field against the track its `source` indicates."""
    a_li = a_items.get(field)
    b_li = b_items.get(field)

    # Resolve the line item per the expected source.
    if truth.source == SOURCE_XBRL:
        li, used_b = a_li, False
    elif truth.source == SOURCE_LLM:
        li, used_b = b_li, True
    elif truth.source == SOURCE_EITHER:
        li, used_b = (a_li, False) if a_li is not None else (b_li, True)
    else:  # derived — try whichever track surfaced it
        li, used_b = (a_li, False) if a_li is not None else (b_li, True)

    track = _track_label(truth.source, used_b)
    base = {"track": track, "source": truth.source, "expected": truth.value}

    # SKIP when the field's expected track wasn't run this invocation.
    needs_b = truth.source in (SOURCE_LLM, SOURCE_EITHER, SOURCE_DERIVED)
    if needs_b and track_a_only and a_li is None:
        return {**base, "state": SKIP, "extracted": None, "reason": "track_b_not_run"}

    if li is None:
        return {**base, "state": FAIL, "extracted": None, "reason": "not_extracted"}

    extracted = float(li.value)
    within = is_within_tolerance(extracted, truth.value)
    return {
        **base,
        "state": PASS if within else FAIL,
        "extracted": extracted,
        "confidence": getattr(li, "confidence", None),
    }


async def _eval_ticker(
    ticker: str,
    truth: TickerGroundTruth,
    edgar: EdgarClient,
    anthropic: Optional[AsyncAnthropic],
    track_a_only: bool,
) -> dict[str, Any]:
    """Run both tracks against one ticker and score against ground truth."""
    cik = await edgar.get_cik_from_ticker(ticker)
    submissions = await edgar.get_submissions(cik)
    name = submissions.get("name", ticker)
    filing_meta = await edgar.get_latest_10k(cik)

    period_end = date.fromisoformat(filing_meta["period_of_report"])
    actual_fy = period_end.year

    # Bail early if the filer has rolled to a newer fiscal year.
    if actual_fy != truth.fiscal_year:
        return {
            "status": REFRESH,
            "info": {
                "ground_truth_fy": truth.fiscal_year,
                "actual_fy": actual_fy,
                "actual_period_end": period_end.isoformat(),
                "accession": filing_meta["accession_number"],
            },
        }

    concepts = INDUSTRY_CONCEPTS.get(truth.industry, STANDARD_CANONICAL_CONCEPTS)
    facts = await edgar.get_company_facts(cik)
    a_items = extract_track_a(period_end, facts, concepts)

    # Track B only for fields our XBRL map doesn't cover, and only when a
    # client is available (skipped under --track-a-only / no API key).
    b_fields = [
        f
        for f, fd in truth.fields.items()
        if fd.source in (SOURCE_LLM, SOURCE_EITHER, SOURCE_DERIVED)
    ]
    b_items: dict[str, Any] = {}
    if b_fields and anthropic is not None and not track_a_only:
        html = await edgar.get_filing_html(filing_meta["primary_doc_url"])
        section_text = extract_financial_statements_section(html)
        b_items = await extract_track_b(
            client=anthropic,
            ticker=ticker,
            company_name=name,
            period_end=period_end,
            accession_number=filing_meta["accession_number"],
            filing_section_text=section_text,
            fields_to_extract=b_fields,
        )

    fields = {
        field: _score_field(field, fd, a_items, b_items, track_a_only)
        for field, fd in truth.fields.items()
    }
    return {"status": "scored", "industry": truth.industry, "fields": fields}


def _aggregate(scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Roll up per-track and overall PASS/FAIL/SKIP counts + ticker sets."""
    tracks: dict[str, dict[str, Any]] = {}
    overall = {"pass": 0, "fail": 0, "skip": 0, "tickers": set()}
    refresh: list[tuple[str, dict[str, Any]]] = []

    for ticker, res in scores.items():
        if res.get("status") == REFRESH:
            refresh.append((ticker, res["info"]))
            continue
        for r in res["fields"].values():
            t = tracks.setdefault(
                r["track"], {"pass": 0, "fail": 0, "skip": 0, "tickers": set()}
            )
            if r["state"] == PASS:
                t["pass"] += 1
                overall["pass"] += 1
                t["tickers"].add(ticker)
                overall["tickers"].add(ticker)
            elif r["state"] == FAIL:
                t["fail"] += 1
                overall["fail"] += 1
                t["tickers"].add(ticker)
                overall["tickers"].add(ticker)
            else:  # SKIP — not counted toward accuracy
                t["skip"] += 1
                overall["skip"] += 1
    return {"tracks": tracks, "overall": overall, "refresh": refresh}


def _acc(passed: int, failed: int) -> Optional[float]:
    denom = passed + failed
    return (passed / denom * 100.0) if denom else None


def _format_table(scores: dict[str, dict[str, Any]]) -> str:
    agg = _aggregate(scores)
    lines: list[str] = []

    for ticker, res in scores.items():
        if res.get("status") == REFRESH:
            continue
        lines.append(f"\n{ticker}  [{res['industry']}]")
        lines.append("-" * 78)
        for field, r in res["fields"].items():
            ext = r.get("extracted")
            ext_str = f"${ext / 1e9:9.3f}B" if ext is not None else "   (none)   "
            exp_str = f"${r['expected'] / 1e9:9.3f}B"
            conf = r.get("confidence")
            conf_str = f" conf={conf:.2f}" if conf is not None else ""
            extra = f"  ({r['reason']})" if r.get("reason") and r["state"] != PASS else ""
            lines.append(
                f"  [{r['state']:4s}] {r['track']:16s} {field:28s}"
                f" got:{ext_str} exp:{exp_str}{conf_str}{extra}"
            )

    if agg["refresh"]:
        lines.append("\n" + "=" * 78)
        lines.append("GROUND TRUTH NEEDS REFRESH (filer rolled to a new fiscal year):")
        for ticker, info in agg["refresh"]:
            lines.append(
                f"  {ticker}: pinned FY{info['ground_truth_fy']}, latest 10-K is "
                f"FY{info['actual_fy']} (period {info['actual_period_end']}, "
                f"accession {info['accession']})"
            )
        lines.append("  → Refresh eval/ground_truth.py with current-FY numbers.")

    lines.append("\n" + "=" * 78)
    lines.append("By track:")
    for track, t in sorted(agg["tracks"].items()):
        acc = _acc(t["pass"], t["fail"])
        acc_str = f"{acc:5.1f}%" if acc is not None else "  n/a"
        skip_str = f", {t['skip']} skipped" if t["skip"] else ""
        lines.append(
            f"  {track:16s} {t['pass']}/{t['pass'] + t['fail']} within ±0.5% "
            f"({acc_str}) across {len(t['tickers'])} tickers{skip_str}"
        )
    o = agg["overall"]
    acc = _acc(o["pass"], o["fail"])
    acc_str = f"{acc:.1f}%" if acc is not None else "n/a"
    skip_str = f", {o['skip']} skipped" if o["skip"] else ""
    lines.append(
        f"  {'Overall':16s} {o['pass']}/{o['pass'] + o['fail']} within ±0.5% "
        f"({acc_str}) across {len(o['tickers'])} tickers{skip_str}"
    )
    lines.append(f"\nPrompt hash: {PROMPT_HASH}")
    lines.append(f"Ground truth pinned: {EVAL_LAST_REFRESHED}")
    return "\n".join(lines)


def _format_readme_table(scores: dict[str, dict[str, Any]]) -> str:
    """Emit the README accuracy-baseline markdown table."""
    agg = _aggregate(scores)
    a = agg["tracks"].get(TRACK_A, {"pass": 0, "fail": 0, "tickers": set()})
    b = agg["tracks"].get(TRACK_B, {"pass": 0, "fail": 0, "tickers": set()})
    o = agg["overall"]

    def row(name: str, t: dict[str, Any]) -> str:
        acc = _acc(t["pass"], t["fail"])
        acc_str = f"{acc:.1f}%" if acc is not None else "—"
        return f"| {name} | {len(t['tickers'])} | {t['pass'] + t['fail']} | {acc_str} |"

    lines = [
        "### Extraction eval baseline",
        "",
        f"Last refreshed: {EVAL_LAST_REFRESHED}",
        "",
        "| Scope | Tickers | Fields | Accuracy |",
        "|---|---:|---:|---:|",
        row("XBRL Track A", a),
        row("Claude Track B", b),
        row("Overall", o),
        "",
        "This is an eval baseline over a curated public-filer set, not a "
        "guarantee across all SEC filers.",
    ]
    return "\n".join(lines)


async def main(
    tickers: Optional[list[str]] = None,
    output_json: bool = False,
    readme: bool = False,
    track_a_only: bool = False,
) -> int:
    selected = {t: GROUND_TRUTH[t] for t in (tickers or list(GROUND_TRUTH)) if t in GROUND_TRUTH}
    if not selected:
        print(f"No matching tickers in ground truth. Available: {list(GROUND_TRUTH)}")
        return 1

    edgar = EdgarClient()
    anthropic = None if track_a_only else AsyncAnthropic()  # picks up ANTHROPIC_API_KEY

    scores: dict[str, dict[str, Any]] = {}
    for ticker, truth in selected.items():
        try:
            scores[ticker] = await _eval_ticker(ticker, truth, edgar, anthropic, track_a_only)
        except Exception as e:
            print(f"  ERROR running {ticker}: {e}", file=sys.stderr)
            scores[ticker] = {
                "status": "scored",
                "industry": truth.industry,
                "fields": {
                    f: {
                        "state": FAIL,
                        "track": _track_label(fd.source, False),
                        "source": fd.source,
                        "extracted": None,
                        "expected": fd.value,
                        "reason": f"runner_error: {type(e).__name__}",
                    }
                    for f, fd in truth.fields.items()
                },
            }

    if readme:
        print(_format_readme_table(scores))
    elif output_json:
        agg = _aggregate(scores)
        # Sets aren't JSON-serializable; render ticker sets as sorted lists.
        agg_out = {
            "tracks": {
                k: {**v, "tickers": sorted(v["tickers"])} for k, v in agg["tracks"].items()
            },
            "overall": {**agg["overall"], "tickers": sorted(agg["overall"]["tickers"])},
            "refresh": agg["refresh"],
        }
        print(
            json.dumps(
                {
                    "scores": scores,
                    "aggregate": agg_out,
                    "prompt_hash": PROMPT_HASH,
                    "ground_truth_refreshed": EVAL_LAST_REFRESHED,
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(_format_table(scores))

    # Exit policy: real extraction failure = 1; else refresh-needed = 2; else 0.
    has_real_fail = any(
        r["state"] == FAIL
        for res in scores.values()
        if res.get("status") != REFRESH
        for r in res["fields"].values()
    )
    any_refresh = any(res.get("status") == REFRESH for res in scores.values())
    if has_real_fail:
        return 1
    if any_refresh:
        return 2
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", type=str, default=None, help="Comma-separated tickers")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    parser.add_argument("--readme", action="store_true", help="Emit README baseline table")
    parser.add_argument(
        "--track-a-only",
        action="store_true",
        help="Skip Track B / Claude (no ANTHROPIC_API_KEY needed); Track-B fields report SKIP",
    )
    args = parser.parse_args()
    selected_tickers = (
        [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    )
    sys.exit(
        asyncio.run(main(selected_tickers, args.json, args.readme, args.track_a_only))
    )
