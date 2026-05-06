"""Apply user-provided overrides to a Company.

A user reviewing extraction_flags can correct an extracted value via the
PUT /company/{ticker}/override endpoint. This module is the pure logic:
locate the LineItem at field_path, replace it with a new LineItem with
source=USER_OVERRIDE and confidence=1.0, return the updated Company.

The graph's validate node should be re-run after each override so that
flags stay consistent with the updated values (a fixed line might clear
a flag; a balance-sheet override might create or clear the BS-identity
flag).
"""

from decimal import Decimal
from typing import Optional

from schemas import (
    BalanceSheet,
    CashFlowStatement,
    Company,
    ExtractionSource,
    IncomeStatement,
    LineItem,
)


_VALID_STATEMENTS: dict[str, type] = {
    "income_statement": IncomeStatement,
    "balance_sheet": BalanceSheet,
    "cash_flow_statement": CashFlowStatement,
}


def parse_field_path(field_path: str) -> tuple[str, str]:
    """Split '<statement>.<field>' into (statement_name, field_name).

    Raises ValueError for malformed paths or unrecognized statement / field.
    """
    parts = field_path.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            f"field_path must be '<statement>.<field>', got {field_path!r}"
        )
    statement_name, field_name = parts
    statement_cls = _VALID_STATEMENTS.get(statement_name)
    if statement_cls is None:
        valid = ", ".join(_VALID_STATEMENTS)
        raise ValueError(
            f"Unknown statement {statement_name!r}; expected one of {valid}"
        )
    if field_name not in statement_cls.model_fields:
        raise ValueError(f"Field {field_name!r} not found on {statement_name}")
    return statement_name, field_name


def apply_override(
    company: Company,
    field_path: str,
    value: Decimal,
    source_quote: Optional[str] = None,
) -> Company:
    """Replace a LineItem at field_path with a USER_OVERRIDE entry.

    The new LineItem has confidence=1.0 and source=USER_OVERRIDE. Source
    quote is optional but recommended for auditability — capturing the
    user's reasoning helps the next reviewer.
    """
    statement_name, field_name = parse_field_path(field_path)

    period = company.periods[0]
    statement = getattr(period, statement_name)

    new_item = LineItem(
        value=value,
        source=ExtractionSource.USER_OVERRIDE,
        confidence=1.0,
        source_quote=source_quote,
    )

    new_statement = statement.model_copy(update={field_name: new_item})
    new_period = period.model_copy(update={statement_name: new_statement})
    return company.model_copy(update={"periods": [new_period]})
