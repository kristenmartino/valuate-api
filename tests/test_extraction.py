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


# --- multi-year extraction ---------------------------------------------------


def test_recent_period_ends_returns_latest_n_descending():
    """_recent_period_ends finds the N newest FY ends across high-coverage tags."""
    from graph import _recent_period_ends

    # Mix concepts so the helper has to union; oldest entry should drop at n=3.
    facts = {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            _entry("2022-12-31", 100),
                            _entry("2023-12-31", 110),
                            _entry("2024-12-31", 120),
                            _entry("2025-12-31", 130),
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "USD": [
                            _entry("2024-12-31", 1000),
                            _entry("2025-12-31", 1100),
                        ]
                    }
                },
            }
        }
    }
    out = _recent_period_ends(facts, latest_end=date(2025, 12, 31), n=3)
    assert out == [date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31)]


def test_recent_period_ends_clips_to_latest_anchor():
    """Future-dated entries (rare but possible from forward-looking forecasts
    occasionally tagged) shouldn't show up — anchor is the 10-K's reported end."""
    from graph import _recent_period_ends

    facts = {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            _entry("2024-12-31", 100),
                            _entry("2025-12-31", 110),
                            _entry("2026-12-31", 120),  # ahead of the anchor
                        ]
                    }
                }
            }
        }
    }
    out = _recent_period_ends(facts, latest_end=date(2025, 12, 31), n=3)
    assert date(2026, 12, 31) not in out
    assert out[0] == date(2025, 12, 31)


def test_compose_company_skips_older_periods_with_missing_required():
    """Older periods that don't have all required fields are dropped; the
    latest period must be complete or composition raises."""
    from graph import _compose_company

    full_items = {
        "revenue": _line(100_000_000_000),
        "operating_income": _line(20_000_000_000),
        "net_income": _line(15_000_000_000),
        "diluted_shares_outstanding": _line(1_000_000_000),
        "cash_and_equivalents": _line(10_000_000_000),
        "total_assets": _line(200_000_000_000),
        "total_liabilities": _line(80_000_000_000),
        "shareholders_equity": _line(120_000_000_000),
        "depreciation_amortization": _line(3_000_000_000),
        "cash_from_operations": _line(25_000_000_000),
        "capital_expenditures": _line(4_000_000_000),
    }
    sparse = dict(full_items)
    sparse["operating_income"] = None  # older period missing one required field

    company = _compose_company(
        ticker="TEST",
        cik="0000000001",
        company_name="Test Co",
        period_ends=[date(2025, 12, 31), date(2024, 12, 31)],
        filing_accession="0000000-25-000001",
        periods_items={
            date(2025, 12, 31): full_items,
            date(2024, 12, 31): sparse,
        },
    )
    # Latest is complete; older is dropped silently.
    assert len(company.periods) == 1
    assert company.periods[0].fiscal_year == 2025


# --- industry classification + bank DDM --------------------------------------


def test_classify_sic_routes_known_codes():
    """SIC ranges from industry.py map to the right Industry enum."""
    from industry import Industry, classify_sic

    assert classify_sic("6021") == Industry.BANK  # National Commercial Banks
    assert classify_sic(6099) == Industry.BANK  # Bank holding companies
    assert classify_sic("6311") == Industry.INSURER  # Life Insurance
    assert classify_sic("6798") == Industry.REIT
    assert classify_sic("1311") == Industry.ENERGY
    assert classify_sic("2911") == Industry.ENERGY  # Petroleum Refining
    assert classify_sic("3674") == Industry.STANDARD  # Semiconductors
    assert classify_sic(None) == Industry.STANDARD
    assert classify_sic("not-a-code") == Industry.STANDARD


def test_bank_ddm_fair_value_matches_gordon_formula():
    """compute_bank_projection should match D₀(1+g)/(r−g) by hand-computation."""
    from datetime import date as date_

    from dcf import compute_bank_projection
    from schemas import (
        Assumptions,
        BankBalanceSheet,
        BankCashFlowStatement,
        BankIncomeStatement,
        Company,
        FilingType,
        FinancialPeriod,
    )
    from industry import Industry

    # Build a synthetic bank period: 2.5B diluted shares, $15B annual dividends
    # → $6.00 D0. With g=4% and r=10%, fair value = 6 * 1.04 / 0.06 = $104.00.
    period = FinancialPeriod(
        fiscal_year=2025,
        fiscal_period_end=date_(2025, 12, 31),
        filing_accession="0000000-25-000001",
        filing_type=FilingType.FORM_10K,
        industry=Industry.BANK,
        income_statement=BankIncomeStatement(
            net_interest_income=_line(95_000_000_000),
            income_before_tax=_line(70_000_000_000),
            income_tax_expense=_line(15_000_000_000),
            net_income=_line(55_000_000_000),
            diluted_shares_outstanding=_line(2_500_000_000),
        ),
        balance_sheet=BankBalanceSheet(
            cash_and_equivalents=_line(400_000_000_000),
            total_loans=_line(1_400_000_000_000),
            total_deposits=_line(2_400_000_000_000),
            total_assets=_line(4_200_000_000_000),
            total_liabilities=_line(3_900_000_000_000),
            shareholders_equity=_line(300_000_000_000),
        ),
        cash_flow_statement=BankCashFlowStatement(
            cash_from_operations=_line(80_000_000_000),
            dividends_paid=_line(15_000_000_000),
        ),
    )
    company = Company(
        ticker="TEST",
        cik="0000000001",
        name="Test Bank",
        fiscal_year_end_month=12,
        periods=[period],
    )
    assumptions = Assumptions(
        revenue_growth=0.0,
        operating_margin=0.18,  # ROE
        terminal_growth=0.04,  # dividend growth
        wacc=0.10,  # cost of equity
        tax_rate=0.21,
        capex_ratio=0.0,
        da_ratio=0.0,
        working_capital_ratio=0.0,
    )
    proj = compute_bank_projection(company, assumptions)
    # 6.00 * 1.04 / 0.06 = 104.0
    assert abs(proj.fair_value_per_share - 104.0) < 1e-6
    # equity_value = 104.0 * 2.5B = $260B
    assert abs(proj.equity_value - 260_000_000_000) < 1
    assert proj.years == []  # DDM has no FCFF projection


def test_bank_ddm_rejects_growth_above_required_return():
    """Gordon constraint: r > g, else fair value is undefined.

    The check fires before we even need a populated Company — it's a pure
    invariant on the Assumptions object — so the assertion lives at the
    front of compute_bank_projection.
    """
    import pytest as pt

    from dcf import compute_bank_projection
    from schemas import Assumptions, Company

    company = Company(
        ticker="TEST",
        cik="0000000001",
        name="Test",
        fiscal_year_end_month=12,
        periods=[],
    )
    bad = Assumptions(
        revenue_growth=0.0,
        operating_margin=0.0,
        terminal_growth=0.10,  # g > r, violates Gordon
        wacc=0.05,
        tax_rate=0.21,
        capex_ratio=0.0,
        da_ratio=0.0,
        working_capital_ratio=0.0,
    )
    with pt.raises(ValueError, match="Cost of equity"):
        compute_bank_projection(company, bad)


def test_insurer_justified_pb_matches_formula():
    """compute_insurer_projection: BVPS × (ROE − g) / (r − g)."""
    from datetime import date as date_

    from dcf import compute_insurer_projection
    from schemas import (
        Assumptions,
        Company,
        FilingType,
        FinancialPeriod,
        InsuranceBalanceSheet,
        InsuranceCashFlowStatement,
        InsuranceIncomeStatement,
    )
    from industry import Industry

    # Synthetic insurer: $30B equity, 0.4B diluted shares → BVPS = $75.
    # ROE 10%, g 3%, r 9% → justified P/B = (10−3)/(9−3) = 7/6 ≈ 1.1667.
    # Fair value = 75 × 1.1667 = $87.50.
    period = FinancialPeriod(
        fiscal_year=2025,
        fiscal_period_end=date_(2025, 12, 31),
        filing_accession="0000000-25-000001",
        filing_type=FilingType.FORM_10K,
        industry=Industry.INSURER,
        income_statement=InsuranceIncomeStatement(
            premiums_earned=_line(40_000_000_000),
            income_before_tax=_line(4_000_000_000),
            income_tax_expense=_line(800_000_000),
            net_income=_line(3_000_000_000),
            diluted_shares_outstanding=_line(400_000_000),
        ),
        balance_sheet=InsuranceBalanceSheet(
            cash_and_equivalents=_line(20_000_000_000),
            total_assets=_line(750_000_000_000),
            total_liabilities=_line(720_000_000_000),
            shareholders_equity=_line(30_000_000_000),
        ),
        cash_flow_statement=InsuranceCashFlowStatement(
            cash_from_operations=_line(10_000_000_000),
        ),
    )
    company = Company(
        ticker="TEST",
        cik="0000000001",
        name="Test Insurance",
        fiscal_year_end_month=12,
        periods=[period],
    )
    assumptions = Assumptions(
        revenue_growth=0.0,
        operating_margin=0.10,  # ROE
        terminal_growth=0.03,  # g
        wacc=0.09,  # r
        tax_rate=0.21,
        capex_ratio=0.0,
        da_ratio=0.0,
        working_capital_ratio=0.0,
    )
    proj = compute_insurer_projection(company, assumptions)
    # 75 × (0.10−0.03)/(0.09−0.03) = 75 × 1.16666... = 87.5
    assert abs(proj.fair_value_per_share - 87.5) < 1e-6
    assert abs(proj.equity_value - 35_000_000_000) < 1
    assert proj.years == []


def test_default_assumptions_averages_across_periods():
    """Ratios should average over every available period; CAGR drives the
    initial revenue_growth slider when ≥2 periods exist."""
    from datetime import date as date_

    from dcf import default_assumptions
    from schemas import (
        BalanceSheet,
        CashFlowStatement,
        Company,
        FilingType,
        FinancialPeriod,
        IncomeStatement,
    )

    def _fp(year: int, revenue: int, op: int) -> FinancialPeriod:
        return FinancialPeriod(
            fiscal_year=year,
            fiscal_period_end=date_(year, 12, 31),
            filing_accession="0000000-25-000001",
            filing_type=FilingType.FORM_10K,
            income_statement=IncomeStatement(
                revenue=_line(revenue),
                operating_income=_line(op),
                net_income=_line(op // 2),
                diluted_shares_outstanding=_line(1_000_000_000),
            ),
            balance_sheet=BalanceSheet(
                cash_and_equivalents=_line(10_000_000_000),
                total_assets=_line(revenue * 2),
                total_liabilities=_line(revenue),
                shareholders_equity=_line(revenue),
            ),
            cash_flow_statement=CashFlowStatement(
                depreciation_amortization=_line(revenue // 30),
                cash_from_operations=_line(op),
                capital_expenditures=_line(revenue // 25),
            ),
        )

    # 3-year history: revenue 100 → 110 → 121 (10% YoY); op_income 20/22/24.2
    company = Company(
        ticker="TEST",
        cik="0000000001",
        name="Test Co",
        fiscal_year_end_month=12,
        periods=[
            _fp(2025, 121_000_000_000, 24_200_000_000),
            _fp(2024, 110_000_000_000, 22_000_000_000),
            _fp(2023, 100_000_000_000, 20_000_000_000),
        ],
    )

    a = default_assumptions(company)
    # Op margins all 20%; average is 20%.
    assert abs(a.operating_margin - 0.20) < 1e-9
    # CAGR from 100 → 121 over 2 steps = 10%.
    assert abs(a.revenue_growth - 0.10) < 1e-9


def test_reit_ffo_multiple_matches_formula():
    """compute_reit_projection: FFO/share × (1+g)/(r−g)."""
    from datetime import date as date_

    from dcf import compute_reit_projection
    from schemas import (
        Assumptions,
        Company,
        FilingType,
        FinancialPeriod,
        REITBalanceSheet,
        REITCashFlowStatement,
        REITIncomeStatement,
    )
    from industry import Industry

    # Synthetic REIT: $3.0B net income + $2.0B D&A → FFO $5.0B.
    # 1B diluted shares → FFO/share $5.00.
    # r = 8%, g = 3% → fair value = 5.00 × 1.03 / 0.05 = $103.00.
    period = FinancialPeriod(
        fiscal_year=2025,
        fiscal_period_end=date_(2025, 12, 31),
        filing_accession="0000000-25-000001",
        filing_type=FilingType.FORM_10K,
        industry=Industry.REIT,
        income_statement=REITIncomeStatement(
            revenue=_line(8_000_000_000),
            depreciation_amortization=_line(2_000_000_000),
            net_income=_line(3_000_000_000),
            diluted_shares_outstanding=_line(1_000_000_000),
        ),
        balance_sheet=REITBalanceSheet(
            cash_and_equivalents=_line(1_000_000_000),
            real_estate_at_cost=_line(90_000_000_000),
            accumulated_depreciation=_line(15_000_000_000),
            real_estate_net=_line(75_000_000_000),
            total_assets=_line(95_000_000_000),
            long_term_debt=_line(35_000_000_000),
            total_liabilities=_line(40_000_000_000),
            shareholders_equity=_line(55_000_000_000),
        ),
        cash_flow_statement=REITCashFlowStatement(
            cash_from_operations=_line(5_000_000_000),
        ),
    )
    company = Company(
        ticker="TEST",
        cik="0000000001",
        name="Test REIT",
        fiscal_year_end_month=12,
        periods=[period],
    )
    assumptions = Assumptions(
        revenue_growth=0.0,
        operating_margin=0.0,
        terminal_growth=0.03,  # FFO growth
        wacc=0.08,  # cost of equity
        tax_rate=0.21,
        capex_ratio=0.0,
        da_ratio=0.0,
        working_capital_ratio=0.0,
    )
    proj = compute_reit_projection(company, assumptions)
    # 5.00 × 1.03 / 0.05 = 103.0
    assert abs(proj.fair_value_per_share - 103.0) < 1e-6
    # equity_value = 103.0 × 1B = $103B
    assert abs(proj.equity_value - 103_000_000_000) < 1
    assert proj.years == []  # no FCFF projection in the FFO model


def test_reit_real_estate_net_derived_from_components():
    """When XBRL tags real_estate_at_cost + accumulated_depreciation but not
    real_estate_net, the derivation backstop fills it in (REITs that report
    only the gross/contra split — common pattern)."""
    from industry import Industry

    items = {
        "real_estate_at_cost": LineItem(
            value=Decimal("90000000000"),
            source=ExtractionSource.XBRL,
            confidence=1.0,
            xbrl_tag="us-gaap:RealEstateInvestmentPropertyAtCost",
        ),
        "accumulated_depreciation": LineItem(
            value=Decimal("15000000000"),
            source=ExtractionSource.XBRL,
            confidence=1.0,
            xbrl_tag="us-gaap:RealEstateInvestmentPropertyAccumulatedDepreciation",
        ),
        "real_estate_net": None,
        "total_assets": LineItem(
            value=Decimal("95000000000"),
            source=ExtractionSource.XBRL,
            confidence=1.0,
        ),
        "shareholders_equity": LineItem(
            value=Decimal("55000000000"),
            source=ExtractionSource.XBRL,
            confidence=1.0,
        ),
    }
    out = _derive_missing_required(items, industry=Industry.REIT)
    ren = out["real_estate_net"]
    assert ren is not None
    assert ren.value == Decimal("75000000000")
    assert ren.source == ExtractionSource.DERIVED
    assert "real_estate_at_cost" in (ren.source_quote or "")
