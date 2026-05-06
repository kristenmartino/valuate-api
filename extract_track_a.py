"""Track A extraction: pull canonical line items from XBRL company facts.

Pure / synchronous: takes the (period_end, facts) inputs and returns a flat
dict mapping schema field names to LineItems. Missing concepts are returned
as None — Track B (Phase 3) fills them in. No exceptions are raised here;
the final Company is composed downstream after both tracks have run.

Confidence is 1.0 for primary-tag matches and 0.95 when we fall through to
an alternate tag (alternate matches are slightly more error-prone since
the filer chose a non-canonical concept).
"""

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from edgar import CANONICAL_CONCEPTS, latest_value_per_period
from schemas import ExtractionSource, LineItem


# Maps canonical_key (in CANONICAL_CONCEPTS) → schema field name. Most are
# identical; we just rename diluted_shares to its schema-side spelling.
_CANONICAL_TO_SCHEMA: dict[str, str] = {
    "revenue": "revenue",
    "cost_of_revenue": "cost_of_revenue",
    "operating_income": "operating_income",
    "net_income": "net_income",
    "depreciation_amortization": "depreciation_amortization",
    "capital_expenditures": "capital_expenditures",
    "cash_from_operations": "cash_from_operations",
    "cash_and_equivalents": "cash_and_equivalents",
    "long_term_debt": "long_term_debt",
    "diluted_shares": "diluted_shares_outstanding",
    "total_assets": "total_assets",
    "total_liabilities": "total_liabilities",
    "shareholders_equity": "shareholders_equity",
}


# Canonical keys whose XBRL unit is 'shares' rather than 'USD'.
_SHARE_UNIT_KEYS = {"diluted_shares"}


def _build_line_item(
    facts: dict[str, Any],
    canonical_key: str,
    target_period_end: date,
) -> Optional[LineItem]:
    """Try each alternate concept tag for the canonical key.

    Returns the first match whose period end equals target_period_end.
    """
    target_iso = target_period_end.isoformat()
    unit = "shares" if canonical_key in _SHARE_UNIT_KEYS else "USD"
    for i, concept in enumerate(CANONICAL_CONCEPTS[canonical_key]):
        by_end = latest_value_per_period(facts, concept, unit=unit)
        entry = by_end.get(target_iso)
        if entry is not None:
            return LineItem(
                value=Decimal(str(entry["val"])),
                source=ExtractionSource.XBRL,
                confidence=1.0 if i == 0 else 0.95,
                xbrl_tag=f"us-gaap:{concept}",
            )
    return None


def extract_track_a(
    period_end: date,
    facts: dict[str, Any],
) -> dict[str, Optional[LineItem]]:
    """Extract every canonical concept from XBRL. Missing values are None.

    Returns a flat dict keyed by *schema* field name (e.g. "revenue",
    "diluted_shares_outstanding"). Track B and the composer downstream
    use the same key namespace.
    """
    return {
        _CANONICAL_TO_SCHEMA[canonical_key]: _build_line_item(
            facts, canonical_key, period_end
        )
        for canonical_key in CANONICAL_CONCEPTS
    }
