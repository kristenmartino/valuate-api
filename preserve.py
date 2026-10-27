"""Carry reviews only within their original filing; retain older versions."""

from schemas import Company, ExtractionSource


def retain_reviews(saved: Company, candidate: Company) -> Company:
    if saved.cik.lstrip("0") != candidate.cik.lstrip("0"):
        raise ValueError("Cannot merge reviews across entities")
    saved_periods = {
        (p.filing_accession, p.fiscal_period_end): p for p in saved.periods
    }
    periods = []
    for p in candidate.periods:
        old = saved_periods.get((p.filing_accession, p.fiscal_period_end))
        updates = {}
        if old:
            for name in ["income_statement", "balance_sheet", "cash_flow_statement"]:
                stmt = getattr(p, name)
                previous = getattr(old, name)
                fields = {}
                for field in type(previous).model_fields:
                    item = getattr(previous, field)
                    if getattr(item, "source", None) == ExtractionSource.USER_OVERRIDE:
                        fields[field] = item
                if fields:
                    updates[name] = stmt.model_copy(update=fields)
        periods.append(p.model_copy(update=updates))
    return candidate.model_copy(update={"periods": periods})
