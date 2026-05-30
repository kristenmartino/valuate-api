"""Ground-truth values for extraction-quality scoring.

Each ticker pins (a) the industry (which concept map / statement shape it
uses), (b) the fiscal year the values correspond to, and (c) a dict of
`{field_name: FieldTruth(value, source)}`. The runner checks the FY before
scoring — if the live 10-K has rolled to a newer fiscal year, the runner
emits a "ground truth needs refresh" warning and skips the ticker rather
than falsely reporting an extraction regression.

`source` records which extraction track our *pipeline* is expected to use
for the field, so the eval can report accuracy per track:

    "xbrl"      Track A pulls it from XBRL company-facts (it's in the
                industry's canonical concept map in edgar.py).
    "llm_html"  Not in our concept map → Track B (Claude) fills the gap by
                reading the filing HTML. (The filer still tags the value in
                XBRL under a non-canonical concept, which is how we can
                source authoritative ground truth for it.)
    "derived"   Produced by the accounting-identity backstop in graph.py.
    "either"    Acceptable from Track A or Track B (scored against whichever
                produced a value, Track A first).

Why ±0.5%: most line items are reported in millions in the filing and the
extraction multiplies through to actual USD. A round-off in the millions
place is within tolerance; a units-multiplication error (e.g. $94,930
instead of $94,930,000,000) blows past it.

Sources of truth: values are the filer's official figures, read from SEC
XBRL company-facts for the pinned fiscal year (see eval/README or the
`fetch` notes in the PR). When a filer rolls to a new fiscal year, refresh
the entry — bump `fiscal_year`, update `fields`, and bump EVAL_LAST_REFRESHED.
"""

from __future__ import annotations

from dataclasses import dataclass

# Valid `source` markers (see module docstring).
SOURCE_XBRL = "xbrl"
SOURCE_LLM = "llm_html"
SOURCE_DERIVED = "derived"
SOURCE_EITHER = "either"
VALID_SOURCES = frozenset({SOURCE_XBRL, SOURCE_LLM, SOURCE_DERIVED, SOURCE_EITHER})


@dataclass(frozen=True)
class FieldTruth:
    """One expected line item: its value (actual USD) and the track our
    pipeline is expected to source it from."""

    value: float
    source: str = SOURCE_XBRL

    def __post_init__(self) -> None:
        if self.source not in VALID_SOURCES:
            raise ValueError(
                f"FieldTruth.source must be one of {sorted(VALID_SOURCES)}, got {self.source!r}"
            )


@dataclass(frozen=True)
class TickerGroundTruth:
    """Ground truth for one ticker, anchored to a specific fiscal year.

    `industry` selects the concept map / statement shape (std, bank,
    insurer, reit, energy). The runner compares against `fields` only when
    the live filer's latest-period fiscal_year matches `fiscal_year`; if the
    filer has moved on, the runner reports "needs refresh" instead of
    false-positiving an extraction regression.
    """

    industry: str  # "std" | "bank" | "insurer" | "reit" | "energy"
    fiscal_year: int
    fields: dict[str, FieldTruth]


def _x(value: float) -> FieldTruth:
    """XBRL-sourced field (Track A)."""
    return FieldTruth(value, SOURCE_XBRL)


def _llm(value: float) -> FieldTruth:
    """LLM-sourced field (Track B) — not in our canonical XBRL concept map."""
    return FieldTruth(value, SOURCE_LLM)


# Values read from SEC XBRL company-facts for each filer's pinned FY.
# Coverage spans all five industry categories; `_llm(...)` fields demonstrate
# the cases where XBRL alone is insufficient and Claude fills the gap.
GROUND_TRUTH: dict[str, TickerGroundTruth] = {
    # --- Standard / tech-industrial -----------------------------------------
    "AAPL": TickerGroundTruth(
        industry="std",
        fiscal_year=2025,  # FY ends late September; FY2025 ended 2025-09-27
        fields={
            "revenue": _x(416_161_000_000),
            "operating_income": _x(133_050_000_000),
            "net_income": _x(112_010_000_000),
            "total_assets": _x(359_241_000_000),
            "shareholders_equity": _x(73_733_000_000),
            "cash_from_operations": _x(111_482_000_000),
            # Not in STANDARD_CANONICAL_CONCEPTS → Track B fills these.
            "income_before_tax": _llm(132_729_000_000),
            "income_tax_expense": _llm(20_719_000_000),
        },
    ),
    "MSFT": TickerGroundTruth(
        industry="std",
        fiscal_year=2025,  # FY ends 2025-06-30
        fields={
            "revenue": _x(281_724_000_000),
            "operating_income": _x(128_528_000_000),
            "net_income": _x(101_832_000_000),
            "total_assets": _x(619_003_000_000),
            "income_before_tax": _llm(123_627_000_000),
        },
    ),
    "GOOGL": TickerGroundTruth(
        industry="std",
        fiscal_year=2025,
        fields={
            "revenue": _x(402_836_000_000),
            "operating_income": _x(129_039_000_000),
            "net_income": _x(132_170_000_000),
            "total_assets": _x(595_281_000_000),
            "income_before_tax": _llm(158_826_000_000),
        },
    ),
    "AMZN": TickerGroundTruth(
        industry="std",
        fiscal_year=2025,
        fields={
            "revenue": _x(716_924_000_000),
            "operating_income": _x(79_975_000_000),
            "net_income": _x(77_670_000_000),
            "total_assets": _x(818_042_000_000),
        },
    ),
    "NKE": TickerGroundTruth(
        industry="std",
        fiscal_year=2025,  # FY ends 2025-05-31
        fields={
            "revenue": _x(46_309_000_000),
            "net_income": _x(3_219_000_000),
            "total_assets": _x(36_579_000_000),
        },
    ),
    "KO": TickerGroundTruth(
        industry="std",
        fiscal_year=2025,
        fields={
            "revenue": _x(47_941_000_000),
            "net_income": _x(13_107_000_000),
            "total_assets": _x(104_816_000_000),
        },
    ),
    # --- Banks --------------------------------------------------------------
    "JPM": TickerGroundTruth(
        industry="bank",
        fiscal_year=2025,
        fields={
            "net_interest_income": _x(95_443_000_000),
            "net_income": _x(57_048_000_000),
            "total_assets": _x(4_424_900_000_000),
        },
    ),
    "BAC": TickerGroundTruth(
        industry="bank",
        fiscal_year=2025,
        fields={
            "net_interest_income": _x(60_096_000_000),
            "net_income": _x(30_509_000_000),
            "total_assets": _x(3_411_738_000_000),
        },
    ),
    # --- Insurer ------------------------------------------------------------
    "PRU": TickerGroundTruth(
        industry="insurer",
        fiscal_year=2025,
        fields={
            "premiums_earned": _x(30_797_000_000),
            "net_income": _x(3_576_000_000),
            "total_assets": _x(773_740_000_000),
        },
    ),
    # --- REIT ---------------------------------------------------------------
    "PLD": TickerGroundTruth(
        industry="reit",
        fiscal_year=2025,
        fields={
            "revenue": _x(8_790_127_000),
            "net_income": _x(3_328_231_000),
            "depreciation_amortization": _x(2_626_028_000),
        },
    ),
    # --- Energy E&P (standard schema, NAV/reserve-life valuation) -----------
    "EOG": TickerGroundTruth(
        industry="energy",
        fiscal_year=2025,
        fields={
            "revenue": _x(22_632_000_000),
            "operating_income": _x(6_385_000_000),
            "net_income": _x(4_980_000_000),
        },
    ),
}


# Tolerance: ±0.5% per field. Within this is "correct"; outside is a
# regression worth flagging.
TOLERANCE = 0.005


# Bump this whenever ground-truth values are re-pinned to a newer fiscal year.
EVAL_LAST_REFRESHED = "2026-05-30"


def is_within_tolerance(extracted: float, expected: float) -> bool:
    """True iff `extracted` is within ±TOLERANCE of `expected`.

    Relative tolerance (|diff| / |expected|) so the threshold is
    scale-invariant — $5M off on a $300B revenue line is acceptable; $5M off
    on a $50M expense is not.
    """
    if expected == 0:
        return extracted == 0
    return abs(extracted - expected) / abs(expected) <= TOLERANCE
