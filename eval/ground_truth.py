"""Ground-truth values for Track B extraction-quality scoring.

Each ticker pins (a) the fiscal year the values correspond to, and
(b) a dict of {field_name: expected_value_in_actual_USD}. The runner
checks the FY before scoring — if the live 10-K has rolled to a newer
fiscal year, the runner emits a "ground truth needs refresh" warning
and skips the ticker rather than falsely reporting an extraction
regression.

Why ±0.5%: most line items are reported in millions in the filing, and
the extraction multiplies through to actual USD. A round-off in the
millions place (e.g., $94,930M reported, $94,929,500,000 internal) is
within tolerance; a units-multiplication error (e.g., $94,930 instead
of $94,930,000,000) blows past it.

Ground-truth values are deliberately limited to a few high-confidence
items per ticker so a fresh hand-source pass on each new fiscal year
isn't a major undertaking. Add new tickers / fields here as the prompt
gets exercised against new failure modes.

Sources of truth: each filing is identified by its (ticker, fiscal_year)
key. Numbers are copy-pasted from the income statement / cash flow /
balance sheet face of the actual 10-K. When a filer rolls to a new
fiscal year, refresh the entire entry — bump `fiscal_year`, update
`fields`, and update EVAL_LAST_REFRESHED.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TickerGroundTruth:
    """Ground truth for one ticker, anchored to a specific fiscal year.

    The runner compares against this only when the extracted Company's
    latest period's fiscal_year matches `fiscal_year` here. If the live
    filer has moved on (new annual 10-K dropped), the runner reports
    "needs refresh" instead of false-positiving an extraction regression.
    """

    fiscal_year: int
    fields: dict[str, float]


GROUND_TRUTH: dict[str, TickerGroundTruth] = {
    "AAPL": TickerGroundTruth(
        fiscal_year=2025,  # AAPL FY ends late September; FY2025 ended Sept 27, 2025
        fields={
            "revenue": 416_161_000_000,
            "operating_income": 133_050_000_000,
            "net_income": 112_010_000_000,
            "share_based_compensation": 12_863_000_000,
            "total_assets": 359_241_000_000,
            "shareholders_equity": 73_733_000_000,
            "long_term_debt": 90_678_000_000,
            "depreciation_amortization": 11_698_000_000,
            "capital_expenditures": 12_715_000_000,
            "cash_from_operations": 111_482_000_000,
        },
    ),
    "MSFT": TickerGroundTruth(
        fiscal_year=2025,
        fields={
            "revenue": 281_725_000_000,
            "operating_income": 128_528_000_000,
            "net_income": 96_641_000_000,
            "total_assets": 620_587_000_000,
        },
    ),
    "JPM": TickerGroundTruth(
        fiscal_year=2025,
        fields={
            "net_interest_income": 92_649_000_000,
            "net_income": 58_471_000_000,
            "total_assets": 4_357_897_000_000,
        },
    ),
    "PLD": TickerGroundTruth(
        fiscal_year=2025,
        fields={
            "revenue": 8_790_127_000,
            "net_income": 3_328_231_000,
            "depreciation_amortization": 2_626_028_000,
        },
    ),
    "EOG": TickerGroundTruth(
        fiscal_year=2025,
        fields={
            "revenue": 22_634_000_000,
            "operating_income": 6_375_000_000,
            "net_income": 4_983_000_000,
        },
    ),
}


# Tolerance: ±0.5% per field. Anything within this is "correct"; anything
# outside is a regression worth flagging.
TOLERANCE = 0.005


# Bookkeeping. Update this when re-pinning ground-truth values to a newer
# fiscal year (so the "last refreshed" line in the eval report is accurate
# without needing to git-blame).
EVAL_LAST_REFRESHED = "2026-05-15"


def is_within_tolerance(extracted: float, expected: float) -> bool:
    """True iff `extracted` is within ±TOLERANCE of `expected`.

    Uses relative tolerance (|diff| / |expected|) to make the threshold
    scale-invariant — $5M off on a $300B revenue line is acceptable;
    $5M off on a $50M expense isn't.
    """
    if expected == 0:
        return extracted == 0
    return abs(extracted - expected) / abs(expected) <= TOLERANCE
