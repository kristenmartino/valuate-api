"""Independent publication checks for issuer-independent statement rules."""

import math
from qualification_schema import InputEvidence, InputIssue
from schemas import LineItem, Industry


def required_paths(industry, latest=False, available=None):
    from graph import _industry_fields

    schema = _industry_fields(industry)
    required = {
        group + "." + field
        for group, fields in zip(
            ["income_statement", "balance_sheet", "cash_flow_statement"], schema[3:]
        )
        for field in fields
    }
    required |= {
        "income_statement.income_before_tax",
        "income_statement.income_tax_expense",
    }
    if industry in {Industry.STANDARD, Industry.ENERGY} and latest:
        required |= {
            "balance_sheet.short_term_debt",
            "balance_sheet.long_term_debt",
            "balance_sheet.operating_lease_liabilities",
        }
    if industry == Industry.BANK:
        path = "cash_flow_statement.common_dividends_per_share"
        required.add(
            path
            if available is not None and path in available
            else "cash_flow_statement.dividends_paid"
        )
    if industry == Industry.REIT:
        required.add("income_statement.depreciation_amortization")
    return required


def qualify_reusable(company, facts, loaded, latest_filing, source_error=None):
    from qualification import _report

    issues = []
    inputs = []
    disclosures = []

    def issue(code, path, message, pe=None):
        issues.append(
            InputIssue(
                code=code,
                field_path=path,
                period_end=str(pe) if pe else None,
                message=message,
            )
        )

    if not loaded:
        issue(
            "statement_evidence_unavailable",
            "company",
            source_error
            or "Original annual statement relationships and contexts have not established complete model inputs. Valuation is withheld.",
        )
        return _report(company, issues, inputs, disclosures, latest_filing).model_copy(
            update={"method": "reusable_statement_rules"}
        )
    for problem in loaded.get("history_check", {}).get("issues", []):
        issue("history_basis_unverified", problem["path"], problem["reason"], problem["period"])
    for revision in loaded.get("history_check", {}).get("revisions", []):
        disclosures.append(
            f"{revision['path']} for {revision['period']} uses the newer filed comparative "
            f"({revision['new_value']}; {revision['new_accession']}) rather than "
            f"{revision['old_value']} from {revision['old_accession']}. "
            "The same independently established input scope is retained; no different-year value was rescaled."
        )
    latest = company.periods[0]
    industry = latest.industry
    if loaded.get("model_issue"):
        issue("model_scope_unverified", "company", loaded["model_issue"])
    if latest_filing != latest.filing_accession:
        issue(
            "filing_changed",
            "company",
            "The latest annual filing changed while checking sources; retry against one consistent filing.",
        )
    if [p.fiscal_period_end for p in company.periods] != loaded["period_ends"]:
        issue(
            "history_coverage_changed",
            "company",
            "All discovered consecutive annual model periods must be retained; incomplete years cannot be dropped to qualify.",
        )
    for p in company.periods:
        pe = p.fiscal_period_end
        if p.industry != industry or p.filing_accession != latest.filing_accession:
            issue(
                "inconsistent_history",
                "company",
                "Annual model, anchor accession and periods must agree.",
                pe,
            )
        expected = loaded["inputs"].get(pe, {})
        required = required_paths(industry, p is latest, expected)
        actual = {
            g + "." + field: getattr(stmt, field)
            for g in ["income_statement", "balance_sheet", "cash_flow_statement"]
            for stmt in [getattr(p, g)]
            for field in type(stmt).model_fields
            if isinstance(getattr(stmt, field), LineItem)
        }
        for path in sorted(required | actual.keys()):
            source = expected.get(path)
            item = actual.get(path)
            if source is None or item is None:
                messages = {
                    "cash_flow_statement.dividends_paid": "Common cash-dividend payments have not been established separately. Total dividends can include preferred or noncontrolling distributions; declared dividends are not cash paid.",
                    "balance_sheet.short_term_debt": "Current carrying borrowings and finance-lease scope have not reconciled with the complete balance sheet. Missing debt is not zero.",
                    "balance_sheet.long_term_debt": "Noncurrent carrying debt and finance-lease scope have not reconciled with the complete balance sheet. An inclusive total cannot replace a noncurrent amount.",
                    "cash_flow_statement.depreciation_amortization": "All presented cash-flow depreciation and amortization components must have nonoverlapping source paths. A depreciation-only component cannot stand for full D&A.",
                    "balance_sheet.operating_lease_liabilities": "Current and noncurrent operating-lease carrying liabilities must establish a complete total; future payments and missing components are not substitutes.",
                }
                issue(
                    "input_coverage_unverified",
                    path,
                    messages.get(
                        path,
                        "Complete primary-statement scope and matching annual source operands are required. Missing inputs are not zero.",
                    ),
                    pe,
                )
                continue
            if not item.value.is_finite() or item.value != source.item.value:
                issue(
                    "filing_value_unverified",
                    path,
                    "Saved amount differs from independently reconstructed annual filing evidence; reviews do not bypass numeric checks.",
                    pe,
                )
                continue
            if path.endswith("diluted_shares_outstanding") and item.value <= 0:
                issue(
                    "invalid_share_basis",
                    path,
                    "Positive annual diluted shares are required.",
                    pe,
                )
                continue
            if (
                industry in {Industry.BANK, Industry.INSURER}
                and path == "balance_sheet.shareholders_equity"
                and item.xbrl_tag != source.item.xbrl_tag
            ):
                issue("equity_scope_unverified", path,
                      "Saved equity must retain the independently established parent ownership basis; a matching amount alone is insufficient.", pe)
                continue
            if (
                path.endswith(
                    (
                        "capital_expenditures",
                        "cash_and_equivalents",
                        "lease_liabilities",
                        "short_term_debt",
                        "long_term_debt",
                        "depreciation_amortization",
                        "common_dividends_per_share",
                    )
                )
                and item.value < 0
            ):
                issue(
                    "invalid_sign",
                    path,
                    "This source contract requires a nonnegative amount.",
                    pe,
                )
                continue
            operands = []
            for row in source.rows:
                op = {
                    "concept": row["concept"],
                    "value": str(row["value"]),
                    "unit": row["unit"],
                    "end": row["end"],
                    "start": row.get("start") or "",
                    "accession": row["accn"],
                    "source_url": row["source_url"],
                    "role": row.get("role", ""),
                    "usage": row.get("usage", "input component"),
                }
                if row.get("dimensions"):
                    import json

                    op["dimensions"] = json.dumps(row["dimensions"], sort_keys=True)
                operands.append(op)
            starts = {row["start"] for row in source.rows if row.get("start")}
            inputs.append(
                InputEvidence(
                    field_path=path,
                    period_end=str(pe),
                    value=str(item.value),
                    unit=source.rows[0]["unit"],
                    scope=source.scope,
                    basis=source.basis,
                    accession=source.rows[0]["accn"],
                    start=next(iter(starts)) if len(starts) == 1 else None,
                    concepts=sorted({row["concept"] for row in source.rows}),
                    operands=operands,
                    display_label=source.display_label,
                    is_proxy=source.is_proxy,
                )
            )
            if source.disclosure:
                disclosures.append(source.disclosure)
            if source.is_proxy:
                disclosures.append(
                    source.scope
                    + ". This combined cash-flow proxy is not pure D&A; model-implied EV/EBITDA is unavailable."
                )
    for flag in company.extraction_flags:
        if flag.field_path == "balance_sheet":
            issue("reconciliation_failed", flag.field_path, flag.reason)
    disclosures.extend(
        [
            "Inputs were checked by reusable SEC statement rules for this filing and record. This is not a human review, extraction-accuracy score or validation of the valuation model.",
            "Capex uses the filed cash-investment row and may exclude other reinvestment. Revenue segments and supplementary disclosures are unavailable unless separately extracted.",
            "The model projects historical ratios using fixed policy assumptions; qualification does not make it a market-price estimate.",
        ]
    )
    if industry == Industry.REIT:
        disclosures.append(
            "Approximate FFO equals net income plus the filed D&A add-back; it is not reported Nareit FFO or a complete AFFO calculation."
        )
    if company.valuation_note:
        disclosures.append(company.valuation_note)
    if not issues:
        from dcf import default_assumptions, compute_projection

        try:
            projection = compute_projection(company, default_assumptions(company))

            def finite(value):
                if isinstance(value, dict):
                    return all(finite(v) for v in value.values())
                if isinstance(value, (list, tuple)):
                    return all(finite(v) for v in value)
                return not isinstance(value, float) or math.isfinite(value)

            if not finite(projection.model_dump()):
                raise ValueError("Nonfinite model result")
        except (ValueError, ZeroDivisionError, OverflowError):
            issue(
                "model_constraints_failed",
                "company",
                "Starting assumptions and qualified inputs do not support a finite valuation.",
            )
    return _report(company, issues, inputs, disclosures, latest_filing).model_copy(
        update={"method": "reusable_statement_rules", "ruleset": loaded["ruleset"]}
    )
