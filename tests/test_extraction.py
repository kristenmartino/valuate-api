"""Track A + derivation tests.

Run from the valuate-api/ root:

    pytest tests/

These cover the bugs / edge cases that were real failure modes during
development:

- The fy-vs-end keying issue in latest_value_per_period (a 10-K reports
  three years of comparative income statements, all tagged with the
  filing's fy — grouping by fy collides them).
- Restatement dedup (later accession wins).
- Period-type filter (FY vs Q1/Q2/Q3/Q4).
- Alternate-tag fall-through with confidence 0.95 vs primary 1.0.
- The DERIVED fallbacks for filers that don't tag operating_income or
  total_liabilities at all (JNJ, NKE, KO).
"""

from datetime import date
from decimal import Decimal

from edgar import latest_value_per_period
from extract_track_a import extract_track_a
from graph import _derive_missing_required
from schemas import ExtractionSource, LineItem


def _entry(end: str, val: int, accn: str = "0000000-25-000001", fp: str = "FY"):
    return {"end": end, "val": val, "accn": accn, "fp": fp, "fy": 2025}


# --- latest_value_per_period -------------------------------------------------


def test_latest_value_per_period_keys_by_end_not_filing_fy():
    """A 10-K reports 3 years of comparative income statements, all tagged
    with fy=<filing year>. Keying by `fy` collides them into one slot;
    keying by `end` correctly returns three entries.

    This was a real bug — the dedup helper used to group by fy, which made
    the "latest" call silently return the wrong year's value for filers
    whose 10-K has comparative columns (i.e. all of them).
    """
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            _entry("2023-09-30", 100),
                            _entry("2024-09-28", 200),
                            _entry("2025-09-27", 300),
                        ]
                    }
                }
            }
        }
    }
    by_end = latest_value_per_period(facts, "Revenues")
    assert len(by_end) == 3
    assert by_end["2023-09-30"]["val"] == 100
    assert by_end["2024-09-28"]["val"] == 200
    assert by_end["2025-09-27"]["val"] == 300


def test_latest_value_per_period_takes_latest_accession_for_restatements():
    """Same period reported in two filings; pick the higher accession number."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            _entry("2024-12-31", 100, accn="0000000-24-000100"),
                            _entry("2024-12-31", 110, accn="0000000-25-000050"),  # restated
                        ]
                    }
                }
            }
        }
    }
    by_end = latest_value_per_period(facts, "Revenues")
    assert by_end["2024-12-31"]["val"] == 110
    assert by_end["2024-12-31"]["accn"] == "0000000-25-000050"


def test_latest_value_per_period_filters_by_period_type():
    """Quarterly entries (fp='Q4' etc) shouldn't show up under fp='FY'."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            _entry("2024-12-31", 100, fp="FY"),
                            _entry("2024-12-31", 25, fp="Q4"),
                        ]
                    }
                }
            }
        }
    }
    by_end = latest_value_per_period(facts, "Revenues", period_type="FY")
    assert by_end["2024-12-31"]["val"] == 100


def test_latest_value_per_period_returns_empty_for_unknown_concept():
    """No raise; just an empty dict for concepts the filer doesn't tag."""
    facts = {"facts": {"us-gaap": {}}}
    assert latest_value_per_period(facts, "Revenues") == {}


# --- extract_track_a ---------------------------------------------------------


def test_extract_track_a_uses_primary_alternate_with_full_confidence():
    """When the primary canonical concept is present, confidence is 1.0."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_entry("2024-12-31", 100)]}},
            }
        }
    }
    items = extract_track_a(date(2024, 12, 31), facts)
    assert items["revenue"] is not None
    assert items["revenue"].confidence == 1.0
    assert items["revenue"].xbrl_tag == "us-gaap:Revenues"
    assert items["revenue"].source == ExtractionSource.XBRL


def test_extract_track_a_falls_through_to_alternate_with_lower_confidence():
    """When only an alternate concept exists, confidence is 0.95.

    Apple uses RevenueFromContractWithCustomerExcludingAssessedTax; CAT
    uses ProfitLoss for net income. Both are non-primary alternates and
    should round-trip with the appropriate confidence ding.
    """
    facts = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [_entry("2024-12-31", 200)]}
                },
            }
        }
    }
    items = extract_track_a(date(2024, 12, 31), facts)
    assert items["revenue"] is not None
    assert items["revenue"].confidence == 0.95
    assert items["revenue"].xbrl_tag.endswith(
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    )


def test_extract_track_a_returns_none_for_missing_concepts():
    """Track A never raises; missing concepts come back as None.

    This is the key contract change vs the original implementation, which
    raised TrackAGapError on any missing required field and prevented
    Track B from ever running for filers like JNJ/NKE/KO.
    """
    items = extract_track_a(date(2024, 12, 31), {"facts": {"us-gaap": {}}})
    for key in (
        "revenue",
        "operating_income",
        "net_income",
        "diluted_shares_outstanding",
        "total_assets",
    ):
        assert items[key] is None, f"{key} should be None when XBRL is empty"


def test_extract_track_a_handles_share_count_unit():
    """Diluted share count is reported under unit 'shares', not 'USD'."""
    facts = {
        "facts": {
            "us-gaap": {
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "units": {"shares": [_entry("2024-12-31", 1_500_000_000)]}
                },
            }
        }
    }
    items = extract_track_a(date(2024, 12, 31), facts)
    assert items["diluted_shares_outstanding"] is not None
    assert items["diluted_shares_outstanding"].value == Decimal("1500000000")


# --- _derive_missing_required ------------------------------------------------


def _line(value: int, source: ExtractionSource = ExtractionSource.XBRL) -> LineItem:
    return LineItem(value=Decimal(str(value)), source=source, confidence=1.0)


def test_derive_operating_income_from_ibt_plus_interest():
    """JNJ doesn't tag operating_income at all. Derive: IBT + interest."""
    items = {
        "operating_income": None,
        "income_before_tax": _line(32_581_000_000),
        "interest_expense": _line(971_000_000),
    }
    result = _derive_missing_required(items)
    assert result["operating_income"] is not None
    assert result["operating_income"].value == Decimal("33552000000")
    assert result["operating_income"].source == ExtractionSource.DERIVED
    assert result["operating_income"].confidence == 0.65
    assert "Derived" in (result["operating_income"].source_quote or "")


def test_derive_total_liabilities_from_accounting_identity():
    """NKE/KO don't tag total_liabilities. Derive: total_assets - equity."""
    items = {
        "total_liabilities": None,
        "total_assets": _line(100_000_000_000),
        "shareholders_equity": _line(40_000_000_000),
    }
    result = _derive_missing_required(items)
    assert result["total_liabilities"] is not None
    assert result["total_liabilities"].value == Decimal("60000000000")
    assert result["total_liabilities"].source == ExtractionSource.DERIVED
    assert result["total_liabilities"].confidence == 0.99


def test_derive_skips_when_inputs_missing():
    """If we don't have IBT or interest_expense, op_income stays None."""
    items = {
        "operating_income": None,
        "income_before_tax": _line(32_581_000_000),
        # interest_expense missing
    }
    result = _derive_missing_required(items)
    assert result.get("operating_income") is None


def test_derive_doesnt_overwrite_existing_values():
    """If a field is already filled, derivation skips it."""
    existing = _line(50_000_000_000, source=ExtractionSource.LLM_HTML)
    items = {
        "operating_income": existing,
        "income_before_tax": _line(60_000_000_000),
        "interest_expense": _line(2_000_000_000),
    }
    result = _derive_missing_required(items)
    assert result["operating_income"] is existing
    assert result["operating_income"].value == Decimal("50000000000")
