"""Apply user-provided overrides to a Company.

A user reviewing extraction_flags can correct an extracted value via the
PUT /company/{ticker}/override endpoint. This module is the pure logic:
locate the LineItem at field_path, replace it with a new LineItem with
source=USER_OVERRIDE and confidence=1.0, return the updated Company.

Field paths are validated against the *actual* statement variant on the
company (standard / bank / insurer / REIT), not a hardcoded standard
schema — a bank's `income_statement.net_interest_income` and a REIT's
`income_statement.depreciation_amortization` are valid override targets
even though they don't all exist on the standard IncomeStatement. The
override applies to the latest period; every earlier period is preserved.

The graph's validate node should be re-run after each override so that
flags stay consistent with the updated values (a fixed line might clear
a flag; a balance-sheet override might create or clear the BS-identity
flag).
"""

from decimal import Decimal
from typing import Optional

from schemas import Company, ExtractionSource, LineItem


# The three statement attributes an override path can target. The concrete
# class behind each (standard / bank / insurer / REIT) is resolved per-company
# at apply time from the actual statement instance, so industry-specific
# fields (net_interest_income, premiums_earned, …) validate correctly.
_VALID_STATEMENT_NAMES: tuple[str, ...] = (
    "income_statement",
    "balance_sheet",
    "cash_flow_statement",
)


def parse_field_path(field_path: str) -> tuple[str, str]:
    """Split '<statement>.<field>' into (statement_name, field_name).

    Validates the path shape and that the statement name is one of the three
    known statements. The *field* is validated later against the actual
    statement variant (see apply_override) because which fields are valid
    depends on the company's industry — a hardcoded standard schema would
    reject every bank/insurer/REIT field.
    """
    parts = field_path.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            f"field_path must be '<statement>.<field>', got {field_path!r}"
        )
    statement_name, field_name = parts
    if statement_name not in _VALID_STATEMENT_NAMES:
        valid = ", ".join(_VALID_STATEMENT_NAMES)
        raise ValueError(
            f"Unknown statement {statement_name!r}; expected one of {valid}"
        )
    return statement_name, field_name


def apply_override(
    company: Company,
    field_path: str,
    value: Decimal,
    source_quote: Optional[str] = None,
) -> Company:
    """Replace a LineItem at field_path with a USER_OVERRIDE entry.

    The override targets the latest period (``periods[0]``); every earlier
    period is preserved unchanged. The field is validated against the actual
    statement variant on that period, so bank/insurer/REIT-specific fields
    are overridable.

    The new LineItem has confidence=1.0 and source=USER_OVERRIDE. Source
    quote is optional but recommended for auditability — capturing the
    user's reasoning helps the next reviewer.
    """
    statement_name, field_name = parse_field_path(field_path)

    if not company.periods:
        raise ValueError("Company has no periods; nothing to override")

    period = company.periods[0]
    statement = getattr(period, statement_name)

    # Validate the field against the ACTUAL statement variant (standard /
    # bank / insurer / REIT) rather than a hardcoded standard schema. This is
    # the fix for overrides 400-ing on every non-standard-industry field.
    if field_name not in type(statement).model_fields:
        raise ValueError(
            f"Field {field_name!r} not found on {statement_name} "
            f"({type(statement).__name__})"
        )

    new_item = LineItem(
        value=value,
        source=ExtractionSource.USER_OVERRIDE,
        confidence=1.0,
        source_quote=source_quote,
    )

    new_statement = statement.model_copy(update={field_name: new_item})
    new_period = period.model_copy(update={statement_name: new_statement})
    # Preserve all earlier periods — only the latest period is overridden.
    # (The prior implementation rebuilt the company with [new_period] only,
    # silently discarding multi-year history.)
    new_periods = [new_period, *company.periods[1:]]
    return company.model_copy(update={"periods": new_periods})
