"""Keep an established parent model distinct from common-share valuation.

These diagnostics operate on every issuer. They never infer preferred carrying
capital from a par-value tag, convert a parent ratio into a common ratio, or
change saved figures to make a historical balance sheet appear reconciled.
"""

from decimal import Decimal

from equity import PARENT_EQUITY_TAG, TOTAL_EQUITY_TAG, reconciliation_equity
from qualification_schema import InputIssue, StatementWarning, ValueScope
from schemas import Industry


PARENT_PROXY_NOTE = (
    "This P/B output is a parent-equity model proxy per annual model share, "
    "not a verified common-stock price estimate. It uses parent equity and "
    "parent net income, including preferred shareholders' claims where present, "
    "with the annual diluted or equivalent share count shown in the statements. "
    "Preferred capital, preferred dividends and redemption premiums are not "
    "deducted. A zero preferred-stock par value does not establish zero "
    "preferred capital. Common-share valuation remains unverified."
)


def value_scope(company):
    if not company.periods or company.periods[0].industry != Industry.INSURER:
        return None
    return ValueScope(
        share_basis=company.share_count_label or "Annual diluted common shares",
        description=PARENT_PROXY_NOTE,
    )


def parent_model_issues(company):
    if value_scope(company) is None:
        return []
    issues = []
    for p in company.periods:
        for path, item, expected in [
            ("balance_sheet.shareholders_equity", p.balance_sheet.shareholders_equity, PARENT_EQUITY_TAG),
            ("income_statement.net_income", p.income_statement.net_income, "us-gaap:NetIncomeLoss"),
        ]:
            if item.xbrl_tag != expected:
                issues.append(InputIssue(
                    code="parent_proxy_scope_unverified", field_path=path,
                    period_end=str(p.fiscal_period_end),
                    message="The parent-equity proxy requires explicit parent equity and parent earnings; inclusive or unestablished ownership scope cannot replace them.",
                ))
    return issues


def historical_statement_warnings(company, inputs):
    warnings = []
    sources = {(i.period_end, i.field_path): i.accession for i in inputs}
    paths = ["balance_sheet." + f for f in [
        "total_assets", "total_liabilities", "shareholders_equity", "noncontrolling_interests",
    ]]
    # Latest-period reconciliation remains a blocking extraction/input check.
    # Historical display diagnostics do not become editable latest-year flags.
    for p in company.periods[1:]:
        bs = p.balance_sheet
        if bs.shareholders_equity.xbrl_tag not in {PARENT_EQUITY_TAG, TOTAL_EQUITY_TAG}:
            continue  # No unsupported ownership arithmetic is asserted.
        assets = bs.total_assets.value
        difference = assets - bs.total_liabilities.value - reconciliation_equity(bs)
        if abs(difference) <= abs(assets) * Decimal("0.005"):
            continue
        missing_nci = bs.shareholders_equity.xbrl_tag == PARENT_EQUITY_TAG and bs.noncontrolling_interests is None
        pe = str(p.fiscal_period_end)
        accessions = {path: sources[(pe, path)] for path in paths if (pe, path) in sources}
        reason = (
            "Noncontrolling interests are unavailable; they are not assumed to be zero."
            if missing_nci else
            "The selected assets, liabilities and ownership amounts do not reconcile. "
            "A consistent historical accounting basis has not been established for these display rows."
        )
        warnings.append(StatementWarning(
            code="historical_ownership_incomplete" if missing_nci else "historical_balance_unreconciled",
            fiscal_year=p.fiscal_year, period_end=pe, difference=str(difference),
            field_paths=paths, source_accessions=accessions,
            message=f"FY{p.fiscal_year} balance-sheet reconciliation is unresolved. {reason} "
                    "These figures must not be read as a reconciled same-basis balance sheet. "
                    "The selected model inputs retain their separately checked source scope.",
        ))
    return warnings
