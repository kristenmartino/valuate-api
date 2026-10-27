"""Track A extraction: pull canonical line items from XBRL company facts.

Pure / synchronous: takes (period_end, facts, concepts) inputs and returns a
flat dict mapping schema field names to LineItems. Missing concepts come back
as None — Track B (Phase 3) fills them in. No exceptions are raised; the
final Company is composed downstream after both tracks have run.

Per-industry concept maps live in edgar.py (`STANDARD_CANONICAL_CONCEPTS`,
`BANK_CANONICAL_CONCEPTS`, etc.). The caller decides which to pass based on
the filer's industry classification.

Confidence is 1.0 for primary-tag matches and 0.95 when we fall through to
an alternate tag (alternate matches are slightly more error-prone since the
filer chose a non-canonical concept).
"""

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from edgar import STANDARD_CANONICAL_CONCEPTS, latest_value_per_period
from input_contracts import anchor_date, annual_fact
from schemas import ExtractionSource, LineItem


# Maps canonical concept-map keys → schema field names. The standard map's
# `diluted_shares` is the only one that needs renaming (the schema field is
# `diluted_shares_outstanding`); for all other concept keys we use them as-is.
# Bank concept-map keys already match BankIncomeStatement field names, so
# only this one rename is needed.
_CANONICAL_TO_SCHEMA_RENAME: dict[str, str] = {
    "diluted_shares": "diluted_shares_outstanding",
}


# Canonical keys whose XBRL unit is 'shares' rather than 'USD'.
_SHARE_UNIT_KEYS = {"diluted_shares"}


def _build_line_item(
    facts: dict[str, Any],
    canonical_key: str,
    target_period_end: date,
    concepts: dict[str, list[str]],
    context: tuple | None = None,
) -> Optional[LineItem]:
    """Try each alternate concept tag for the canonical key.

    Returns the first match whose period end equals target_period_end.
    """
    target_iso = target_period_end.isoformat()
    unit = "shares" if canonical_key in _SHARE_UNIT_KEYS else "USD"
    for i, concept in enumerate(concepts[canonical_key]):
        if context:
            latest, accession, bound = context
            instant = canonical_key in {
                "cash_and_equivalents",
                "short_term_investments",
                "accounts_receivable",
                "inventory",
                "total_current_assets",
                "property_plant_equipment_net",
                "total_assets",
                "accounts_payable",
                "short_term_debt",
                "long_term_debt",
                "total_current_liabilities",
                "total_liabilities",
                "shareholders_equity",
                "noncontrolling_interests",
                "operating_lease_liabilities",
                "securities",
                "total_loans",
                "allowance_for_loan_losses",
                "total_deposits",
                "investments",
                "insurance_reserves",
                "real_estate_at_cost",
                "accumulated_depreciation",
                "real_estate_net",
            }
            entry = (
                annual_fact(
                    facts,
                    concept,
                    target_period_end,
                    latest,
                    accession,
                    bound,
                    instant=instant,
                    unit=unit,
                )
                if bound
                else None
            )
        else:
            by_end = latest_value_per_period(facts, concept, unit=unit)
            entry = by_end.get(target_iso)
        if entry is not None:
            return LineItem(
                value=Decimal(str(entry["val"])),
                source=ExtractionSource.XBRL,
                confidence=1.0 if i == 0 else 0.95,
                xbrl_tag=f"us-gaap:{concept}",
                source_quote=(
                    f"Filed {concept}: {entry['val']} {unit}; period {entry.get('start', 'year-end instant')} to {target_iso}; SEC accession {entry['accn']}."
                    if context
                    else None
                ),
            )
    return None


def extract_track_a(
    period_end: date,
    facts: dict[str, Any],
    concepts: Optional[dict[str, list[str]]] = None,
    *,
    latest_end: date | None = None,
    accession: str | None = None,
) -> dict[str, Optional[LineItem]]:
    """Extract every canonical concept from XBRL for one fiscal period.

    `concepts` defaults to STANDARD_CANONICAL_CONCEPTS. Pass BANK_CANONICAL_CONCEPTS
    (or another industry's map) to extract a different shape. Returns a flat
    dict keyed by schema field name; downstream composition picks fields out
    of this dict to build the appropriate IncomeStatement / BalanceSheet /
    CashFlowStatement variant.
    """
    concepts = concepts or STANDARD_CANONICAL_CONCEPTS
    context = (
        (latest_end, accession, anchor_date(facts, accession))
        if latest_end and accession
        else None
    )
    return {
        _CANONICAL_TO_SCHEMA_RENAME.get(canonical_key, canonical_key): _build_line_item(
            facts, canonical_key, period_end, concepts, context
        )
        for canonical_key in concepts
    }
