"""SIC code → industry classification.

Used at ingest time to route extraction down the right path. The standard
(industrial / tech) shape is the catch-all default; banks, insurers, REITs,
and energy E&P each have their own schema variants and DCF math because the
underlying business model is different enough that the industrial template
produces nonsense answers (banks have no "operating margin"; REITs are
valued on FFO not FCFF; etc.).

SIC code reference (selected):
- 6020-6099: depository institutions / bank holding companies
- 6311-6411: insurance carriers / agents
- 6798: real estate investment trusts (REITs)
- 1311, 1381, 1389, 2911: petroleum / oil & gas extraction and refining

Anything else falls through to STANDARD.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class Industry(str, Enum):
    STANDARD = "standard"
    BANK = "bank"
    INSURER = "insurer"
    REIT = "reit"
    ENERGY = "energy"


# Pin canonical SIC ranges per industry. Inclusive on both ends.
_SIC_RANGES: list[tuple[int, int, Industry]] = [
    (6020, 6099, Industry.BANK),     # commercial banks + bank holding cos
    (6311, 6411, Industry.INSURER),  # life, P&C, accident/health, agents
    (6798, 6798, Industry.REIT),     # REITs
    (1311, 1311, Industry.ENERGY),   # crude petroleum & natural gas
    (1381, 1389, Industry.ENERGY),   # drilling + oil/gas field services
    (2911, 2911, Industry.ENERGY),   # petroleum refining (integrated majors)
]


def classify_sic(sic: Optional[str | int]) -> Industry:
    """Map an SEC SIC code to an Industry.

    Accepts the SIC field as either a string (the shape submissions returns)
    or an int. Returns Industry.STANDARD for anything outside the explicit
    ranges, including missing/malformed input.
    """
    if sic is None or sic == "":
        return Industry.STANDARD
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return Industry.STANDARD
    for low, high, industry in _SIC_RANGES:
        if low <= code <= high:
            return industry
    return Industry.STANDARD


# Hand-overrides for tickers where SIC mis-classifies the consolidated 10-K.
# These are diversified holding companies where the SIC code reflects the
# largest subsidiary (typically insurance, because that's where these
# entities historically started) but the consolidated filing is more
# accurately treated as a standard industrial.
#
# The override fixes industry-classification only. It does NOT fix
# missing-XBRL-tag issues that the same filers may also have. Verified
# cases:
#
# - MKL (Markel Corp): override flips to STANDARD; extraction expected
#   to succeed (similar XBRL coverage to standard industrials).
# - L (Loews Corp): override flips to STANDARD; similar expectation.
#
# Notable case we tried and removed:
#
# - BRK-B (Berkshire Hathaway): the override flips classification, but
#   Berkshire's own XBRL tagging is genuinely unusual — they stopped
#   tagging `OperatingIncomeLoss` after 2012 and never tag
#   `WeightedAverageNumberOfDilutedSharesOutstanding` (A/B share
#   structure makes the standard concept inapplicable). Even with the
#   override, composition fails on those two required fields. A real
#   Berkshire valuation is sum-of-the-parts, which is out of scope for
#   the demo. Leaving BRK-B to fail loudly with the friendly error UI
#   is more honest than overriding it into a different kind of failure.
#
# The override is checked AFTER classify_sic in the graph's ingest step.
TICKER_INDUSTRY_OVERRIDES: dict[str, Industry] = {
    "MKL": Industry.STANDARD,  # Markel Corp (specialty insurance holding co)
    "L": Industry.STANDARD,    # Loews Corp (diversified holdings)
}


def classify_with_overrides(ticker: str, sic: Optional[str | int]) -> Industry:
    """Same as classify_sic, but consults TICKER_INDUSTRY_OVERRIDES first.

    Use this from the ingest node so SIC-driven mis-classification gets
    corrected for known diversified holding companies. Falls through to
    classify_sic for the common case (no override).
    """
    override = TICKER_INDUSTRY_OVERRIDES.get(ticker.upper())
    if override is not None:
        return override
    return classify_sic(sic)


# Human-friendly label per industry, used in the UI / error messages.
INDUSTRY_LABEL: dict[Industry, str] = {
    Industry.STANDARD: "Industrial / Tech",
    Industry.BANK: "Bank",
    Industry.INSURER: "Insurer",
    Industry.REIT: "REIT",
    Industry.ENERGY: "Energy (Oil & Gas)",
}
