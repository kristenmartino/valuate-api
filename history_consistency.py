"""Do not splice model history across unresolved filing restatements.

An annual value can match its source and still be incomparable with the next
filing. Same-period overlaps, not different-year growth, establish continuity.
"""

from decimal import Decimal, InvalidOperation
from schemas import Industry


def history_paths(industry):
    if industry in {Industry.STANDARD, Industry.ENERGY}:
        return {
            "income_statement.revenue", "income_statement.operating_income",
            "income_statement.income_before_tax", "income_statement.income_tax_expense",
            "cash_flow_statement.capital_expenditures",
            "cash_flow_statement.depreciation_amortization",
        }
    if industry in {Industry.BANK, Industry.INSURER}:
        return {"income_statement.net_income", "balance_sheet.shareholders_equity"}
    if industry == Industry.REIT:
        return {"income_statement.revenue"}
    return set()


def precision(source):
    """Propagate declared source rounding through additive monetary operands."""
    total = Decimal(0)
    for row in source.rows:
        if row.get("usage", "input component") != "input component":
            continue
        try:
            places = row.get("decimals", "INF")
            total += Decimal(0) if places == "INF" else Decimal(10) ** -int(places) / 2
        except (TypeError, ValueError, InvalidOperation):
            pass  # Unknown precision never grants an extra tolerance.
    return total


def validate_history(sources, industry):
    issues, revisions, canonical = [], [], {}
    for filing, periods in sources:
        for path in sorted(history_paths(industry)):
            values = {pe: items[path] for pe, items in periods.items() if path in items}
            prior = canonical.setdefault(path, {})
            missing = values.keys() - prior.keys()
            if not missing:
                continue
            if prior:
                overlap = values.keys() & prior.keys()
                if not overlap:
                    issues.append({"path": path, "period": None, "reason": "No same-period filing bridge establishes comparable model history"})
                for pe in overlap:
                    old, new = values[pe], prior[pe]
                    a, b = old.rows[0], new.rows[0]
                    def concepts(source):
                        if path == "balance_sheet.shareholders_equity" and source.item.xbrl_tag == "us-gaap:StockholdersEquity":
                            # Independently reconciled parent columns and a
                            # direct parent total establish the same scope.
                            return {source.item.xbrl_tag}
                        return {r["concept"] for r in source.rows if r.get("usage", "input component") == "input component"}
                    if (a.get("start"), a["unit"]) != (b.get("start"), b["unit"]):
                        issues.append({"path": path, "period": str(pe), "reason": "Overlapping annual dates or units differ"})
                    elif old.is_proxy != new.is_proxy or (
                        concepts(old) != concepts(new)
                        and abs(old.item.value - new.item.value) > precision(old) + precision(new)
                    ):
                        issues.append({"path": path, "period": str(pe), "reason": "Model history changes source coverage across filings; reconcile the scope before combining periods"})
                    elif abs(old.item.value - new.item.value) > precision(old) + precision(new):
                        # A same-concept comparative revision is not evidence of
                        # a split or a scope conversion. Keep the newest filed
                        # comparative and expose the revision; do not rescale
                        # a different fiscal year or reject it merely for moving.
                        revisions.append({"path": path, "period": str(pe), "new_value": str(new.item.value), "old_value": str(old.item.value), "new_accession": new.rows[0]["accn"], "old_accession": filing["accession"]})
            for pe in missing:
                prior[pe] = values[pe]
    if industry in {Industry.BANK, Industry.INSURER}:
        for pe, source in canonical.get("balance_sheet.shareholders_equity", {}).items():
            if source.item.xbrl_tag != "us-gaap:StockholdersEquity":
                issues.append({"path": "balance_sheet.shareholders_equity", "period": str(pe), "reason": "ROE and book value require parent shareholders' equity; consolidated equity including noncontrolling interests cannot silently replace it"})
        for pe, source in canonical.get("income_statement.net_income", {}).items():
            if source.item.xbrl_tag == "us-gaap:ProfitLoss":
                issues.append({"path": "income_statement.net_income", "period": str(pe), "reason": "Parent-equity ROE requires parent earnings; consolidated profit including noncontrolling interests is not interchangeable"})
    return {"issues": issues, "revisions": revisions}
