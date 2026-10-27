"""Fail-closed publication gate for a specific record and annual filing.

A reviewed filing contract or reusable primary-statement rules AND matching
independent source evidence are required. Neither a cache hit, a confidence score nor an override grants it.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from input_contracts import (
    CONTRACTS,
    ACC,
    anchor_date,
    annual_fact,
    contract_fact,
)
from qualification_schema import InputEvidence, InputIssue, Qualification
from schemas import Company, ExtractionSource
from filing_coverage import configured_fields, selections as reviewed_selections
from scoped_inputs import contract as scoped_contract, paths as scoped_paths

POLICY_FILES = [
    "qualification.py",
    "reusable_inputs.py",
    "reusable_qualification.py",
    "filing_document.py",
    "filing_sources.py",
    "filing_coverage.py",
    "statement_evidence.py",
    "statement_coverage.py",
    "scoped_inputs.py",
    "qualification_schema.py",
    "qualification_service.py",
    "input_contracts.py",
    "qualification_policies.json",
    "extract_track_a.py",
    "graph.py",
    "edgar.py",
    "dcf.py",
    "schemas.py",
    "industry.py",
    "alphabet.py",
    "microsoft.py",
    "intuit.py",
    "ibm.py",
    "salesforce.py",
    "debt.py",
    "equity.py",
    "cisco_da.py",
    "berkshire.py",
    "crm_revenue.py",
]
POLICY_VERSION = (
    "filing-inputs-v1-"
    + hashlib.sha256(
        b"".join(
            (Path(__file__).parent / filename).read_bytes() for filename in POLICY_FILES
        )
    ).hexdigest()[:12]
)

POLICIES = json.loads(
    (Path(__file__).parent / "qualification_policies.json").read_text()
)


def record_digest(company: Company) -> str:
    payload = company.model_dump(mode="json", exclude={"qualification"})
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def current_qualification(company: Company) -> Qualification:
    q = company.qualification
    if (
        q
        and q.policy_version == POLICY_VERSION
        and q.record_digest == record_digest(company)
        and q.filing_accession
        == (company.periods[0].filing_accession if company.periods else "")
        and (
            q.status != "qualified" or (not q.issues and q.inputs and q.evidence_digest)
        )
    ):
        return q
    return Qualification(
        status="review_required",
        policy_version=POLICY_VERSION,
        record_digest=record_digest(company),
        filing_accession=company.periods[0].filing_accession if company.periods else "",
        checked_at=None,
        issues=[
            InputIssue(
                code="qualification_required",
                field_path="company",
                message="This exact filing and saved record have not passed the current publication checks. Financials remain available for review; valuation is withheld.",
            )
        ],
    )


def _item(p, path):
    stmt, field = path.split(".")
    return getattr(getattr(p, stmt), field, None)


def qualify(
    company: Company,
    facts: dict,
    support: dict | None = None,
    latest_filing: str | None = None,
) -> Qualification:
    support = support or {}
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

    def evidence(
        p, path, item, row, scope, basis="reported GAAP amount", operands=None
    ):
        inputs.append(
            InputEvidence(
                field_path=path,
                period_end=str(p.fiscal_period_end),
                value=str(item.value),
                unit=row.get("unit", "USD"),
                scope=scope,
                basis=basis,
                accession=row["accn"],
                start=row.get("start"),
                concepts=[row["concept"]] if row.get("concept") else [],
                operands=operands or [],
                dimensions=row.get("dimensions", {}),
            )
        )

    if not company.periods:
        issue("missing_periods", "company", "No annual periods are available.")
        return _report(company, issues, inputs, disclosures, latest_filing)
    latest = company.periods[0]
    pe0 = latest.fiscal_period_end
    cik = company.cik.lstrip("0")
    policy = POLICIES.get(cik + ":" + latest.filing_accession)
    if not policy:
        from reusable_qualification import qualify_reusable

        return qualify_reusable(
            company,
            facts,
            support.get("reusable_inputs"),
            latest_filing,
            support.get("reusable_error"),
        )
    if policy and [str(p.fiscal_period_end) for p in company.periods] != policy.get(
        "period_ends"
    ):
        issue(
            "history_coverage_changed",
            "company",
            "The model history differs from the filing scope policy; dropping a problematic year cannot qualify a shortened ratio history.",
        )
    if latest_filing and latest_filing != latest.filing_accession:
        disclosures.append(
            "A newer annual filing is available. This report applies only to the displayed saved filing; the newer filing requires separate qualification."
        )
    bound = anchor_date(facts, latest.filing_accession)
    if str(facts.get("cik", "")).lstrip("0") != cik or bound is None or bound < pe0:
        issue(
            "filing_context_unavailable",
            "company",
            "Matching company and annual filing evidence is unavailable.",
        )
        return _report(company, issues, inputs, disclosures, latest_filing)
    # Required special scope operands are selected from the original filing,
    # never trusted from a saved free-text explanation.
    reviewed = {
        p.fiscal_period_end: reviewed_selections(
            facts,
            company.cik,
            p.fiscal_period_end,
            latest.filing_accession,
            pe0,
            zero_current_borrowings=support.get("zero_current_borrowings"),
        )
        for p in company.periods
    }
    special = {}
    for p in company.periods:
        pe = p.fiscal_period_end
        items = {}
        if cik == "789019":
            from microsoft import filing_items

            items = filing_items(facts, cik, pe, latest.filing_accession, pe0)
        elif cik == "896878":
            from intuit import filing_items

            items = filing_items(facts, cik, pe, latest.filing_accession, pe0)
        elif cik == "1652044":
            from alphabet import filing_items

            items = filing_items(facts, cik, pe, latest.filing_accession, pe0)
        elif cik == "51143":
            from ibm import filing_items, model_items

            items = {
                **filing_items(facts, cik, pe, latest.filing_accession, pe0),
                **model_items(facts, cik, pe, latest.filing_accession, pe0),
            }
        elif cik == "1108524":
            from salesforce import filing_items

            items = filing_items(facts, cik, pe, latest.filing_accession, pe0)
        if cik == "858877" and pe in support.get("cisco_da", {}):
            items["depreciation_amortization"] = support["cisco_da"][pe]
        if cik == "1067983":
            items.update(support.get("berkshire", {}).get(pe, {}))
        special[pe] = items

    def verify(p, path, required=True):
        field = path.split(".")[1]
        if path in (policy or {}).get("required_history_context", []):
            required = True
        item = _item(p, path)
        pe = p.fiscal_period_end
        if item is None:
            if required:
                issue(
                    "missing_input",
                    path,
                    "Required model input is unknown; it cannot be treated as zero.",
                    pe,
                )
            else:
                disclosures.append(
                    f"{path} ({pe}) is unavailable; the documented model fallback applies."
                )
            return
        if not item.value.is_finite():
            issue("invalid_number", path, "Input must be finite.", pe)
            return
        if path in scoped_paths(company.cik, latest.filing_accession):
            scoped = support.get("scoped_inputs", {}).get(pe, {}).get(path)
            if scoped is None or scoped.item.value != item.value:
                issue(
                    "scoped_input_unverified",
                    path,
                    "Every reviewed source operand, context and carrying-value reconciliation must match; missing components cannot become zero.",
                    pe,
                )
                return
            row = scoped.rows[0]
            evidence(
                p,
                path,
                item,
                row,
                scoped.scope,
                scoped.basis,
                [
                    {
                        "concept": r["concept"],
                        "value": str(r["value"]),
                        "start": r.get("start") or "",
                        "end": r["end"],
                        "accession": r["accn"],
                        "source_url": r["source_url"],
                        "statement_role": r["role"],
                        "row_label": r["label"],
                        "decimals": r.get("decimals", ""),
                        "dimensions": json.dumps(
                            r.get("dimensions", {}), sort_keys=True
                        ),
                    }
                    for r in scoped.rows
                ],
            )
            return
        if path in (policy or {}).get("statement_inputs", {}):
            scoped = support.get("statement_inputs", {}).get(pe, {}).get(path)
            if scoped is None or scoped.item.value != item.value:
                issue(
                    "statement_scope_unverified",
                    path,
                    "Matching consolidated statement role, row label, annual context and company-facts amount are required.",
                    pe,
                )
                return
            row = scoped.rows[0]
            evidence(
                p,
                path,
                item,
                row,
                scoped.scope,
                scoped.basis,
                [
                    {
                        "concept": row["concept"],
                        "value": str(row["value"]),
                        "start": row.get("start") or "",
                        "end": row["end"],
                        "accession": row["accn"],
                        "statement_role": row.get(
                            "role", "standard unrestricted-cash fact"
                        ),
                        "row_label": row.get("row_label", ""),
                        "source_url": row.get("source_url", ""),
                    }
                ],
            )
            disclosures.append((policy or {}).get("disclosure", scoped.scope))
            return
        scoped = reviewed[pe].get(field)
        if scoped is not None:
            if scoped.item.value != item.value:
                issue(
                    "scope_or_value_mismatch",
                    path,
                    "Saved input does not match the reviewed filing coverage and amount.",
                    pe,
                )
                return
            rows = scoped.rows
            evidence(
                p,
                path,
                item,
                {**rows[0], "concept": scoped.item.xbrl_tag},
                scoped.scope,
                scoped.basis,
                [
                    {
                        "concept": r["concept"],
                        "value": str(r["value"]),
                        "start": r.get("start", ""),
                        "end": r["end"],
                        "accession": r["accn"],
                        **(
                            {"source_url": r["source_url"], "disclosure": r["basis"]}
                            if r.get("basis")
                            else {}
                        ),
                    }
                    for r in rows
                ],
            )
            disclosures.append((policy or {}).get("disclosure", scoped.scope))
            return
        if field in configured_fields(company.cik, latest.filing_accession):
            issue(
                "scope_evidence_unavailable",
                path,
                "Required complete debt/D&A operands are unavailable or conflict; no generic fallback can qualify this filing.",
                pe,
            )
            return
        expected = special[pe].get(field)
        special_fields = {
            "789019": {
                "depreciation_amortization",
                "short_term_debt",
                "long_term_debt",
            },
            "896878": {
                "depreciation_amortization",
                "short_term_debt",
                "long_term_debt",
            },
            "1652044": {
                "depreciation_amortization",
                "short_term_debt",
                "long_term_debt",
            },
            "51143": {
                "operating_income",
                "income_before_tax",
                "interest_expense",
                "income_tax_expense",
                "depreciation_amortization",
                "short_term_debt",
                "long_term_debt",
            },
            "858877": {"depreciation_amortization"},
            "1108524": {"depreciation_amortization", "long_term_debt"},
            "1067983": {
                "diluted_shares_outstanding",
                "cash_and_equivalents",
                "premiums_earned",
            },
        }
        if field in special_fields.get(cik, set()):
            if expected is None or expected.value != item.value:
                issue(
                    "scope_or_value_mismatch",
                    path,
                    "Saved input does not match the supported filing calculation or its complete scope.",
                    pe,
                )
                return
            accs = ACC.findall(expected.source_quote or "")
            if (
                cik == "1067983"
                and support.get("inline_accession") == latest.filing_accession
            ):
                accs = [latest.filing_accession]
            if len(set(accs)) != 1:
                issue(
                    "missing_lineage",
                    path,
                    "Verified calculation has no unambiguous source accession.",
                    pe,
                )
                return
            acc = accs[0]
            operands = []
            # Capture the exact numeric context operands used by the scoped
            # selector. Its calculation explanation is retained alongside them.
            operand_tags = []
            if field == "depreciation_amortization":
                operand_tags = {
                    "789019": ["Depreciation", "AmortizationOfIntangibleAssets"],
                    "896878": ["Depreciation", "AdjustmentForAmortization"],
                    "51143": ["Depreciation", "AmortizationOfIntangibleAssets"],
                    "1652044": ["Depreciation"],
                    "1108524": ["DepreciationAndAmortization"],
                }.get(cik, [])
            elif field == "operating_income" and cik == "51143":
                operand_tags = [
                    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                    "InterestExpense",
                ]
            elif expected.xbrl_tag and expected.xbrl_tag.startswith("us-gaap:"):
                operand_tags = [expected.xbrl_tag.split(":")[1]]
            elif cik == "1652044" and field == "short_term_debt":
                operand_tags = (
                    [
                        "LongTermDebt",
                        "LongTermDebtCurrent",
                        "LongTermDebtNoncurrent",
                        "CommercialPaper",
                        "DebtInstrumentUnamortizedDiscountPremiumAndDebtIssuanceCostsNet",
                    ]
                    if pe.year != 2022
                    else [
                        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
                        "LongTermDebtAndCapitalLeaseObligations",
                        "CommercialPaper",
                        "DebtInstrumentUnamortizedDiscountPremiumAndDebtIssuanceCostsNet",
                    ]
                )
            for concept in operand_tags:
                row = annual_fact(
                    facts,
                    concept,
                    pe,
                    pe,
                    acc,
                    bound,
                    instant=field.endswith("debt"),
                    unit="shares" if field == "diluted_shares_outstanding" else "USD",
                )
                if row:
                    operands.append(
                        {
                            "concept": row["concept"],
                            "value": str(row["value"]),
                            "start": row.get("start", ""),
                            "end": row["end"],
                            "accession": row["accn"],
                        }
                    )
            scope = expected.source_quote or "Explicit filing scope"
            row = {
                "accn": acc,
                "unit": "shares" if field == "diluted_shares_outstanding" else "USD",
                "concept": expected.xbrl_tag,
            }
            starts = {r.get("start") for r in operands if r.get("start")}
            if len(starts) == 1:
                row["start"] = next(iter(starts))
            if cik == "858877" and field == "depreciation_amortization":
                from cisco_da import _period_start

                row["start"] = str(_period_start(pe))
            if cik == "1067983" and field == "diluted_shares_outstanding":
                row["start"] = f"{pe.year}-01-01"

                row["dimensions"] = {
                    "us-gaap:StatementClassOfStockAxis": f"brka:EquivalentClass{company.ticker[-1]}Member"
                }
            evidence(
                p,
                path,
                item,
                row,
                scope,
                "filed amount or explicitly reconciled calculation",
                operands,
            )
            if field in {
                "depreciation_amortization",
                "operating_income",
                "diluted_shares_outstanding",
            }:
                disclosures.append(
                    (policy or {}).get(
                        "disclosure",
                        "Source-specific coverage is shown in the saved model note.",
                    )
                )
            return
        contract = CONTRACTS.get(field)
        if contract is None:
            issue(
                "unsupported_contract",
                path,
                "No explicit financial-coverage contract is available for this model input.",
                pe,
            )
            return
        preferred = item.xbrl_tag if item.source == ExtractionSource.XBRL else None
        row = contract_fact(
            facts, contract, pe, pe0, latest.filing_accession, bound, preferred
        )
        if row is None or row["value"] != item.value:
            issue(
                "filing_value_unverified",
                path,
                "No unique, matching annual filing fact establishes this input and coverage.",
                pe,
            )
            return
        if field == "depreciation_amortization":
            # A conflicting total is a review, not a first-tag preference.
            candidates = [
                annual_fact(facts, t, pe, pe0, latest.filing_accession, bound)
                for t in contract.concepts
            ]
            if len({r["value"] for r in candidates if r}) > 1:
                issue(
                    "ambiguous_da_scope",
                    path,
                    "Annual D&A concepts report different amounts; coverage requires review.",
                    pe,
                )
                return
        if field == "diluted_shares_outstanding" and item.value <= 0:
            issue("invalid_share_basis", path, "Share count must be positive.", pe)
            return
        if (
            field
            in {
                "capital_expenditures",
                "common_dividends_per_share",
                "cash_and_equivalents",
                "operating_lease_liabilities",
                "depreciation_amortization",
            }
            and item.value < 0
        ):
            issue(
                "invalid_sign",
                path,
                "This model contract requires a nonnegative amount.",
                pe,
            )
            return
        evidence(p, path, item, row, contract.scope, contract.basis)
        if field == "capital_expenditures":
            disclosures.append(
                "Capex coverage follows the reported cash-investment concept; it may exclude software or other reinvestment. The model does not claim total reinvestment coverage."
            )

    industry = latest.industry.value
    for p in company.periods:
        if p.industry != latest.industry or p.fiscal_period_end > pe0:
            issue(
                "inconsistent_history",
                "company",
                "Annual history must use one model and no future periods.",
                p.fiscal_period_end,
            )
        for path in sorted(
            set((policy or {}).get("statement_inputs", {}))
            | set((policy or {}).get("required_history_inputs", []))
        ):
            verify(p, path)
        for path, cfg in (
            scoped_contract(company.cik, latest.filing_accession)
            .get("inputs", {})
            .items()
        ):
            if (
                cfg.get("required", "history") == "history"
                or p is latest
                or _item(p, path) is not None
            ):
                verify(p, path)
        verify(p, "income_statement.net_income")
        if industry in {"standard", "energy"}:
            for path in [
                "income_statement.revenue",
                "income_statement.operating_income",
                "cash_flow_statement.depreciation_amortization",
                "cash_flow_statement.capital_expenditures",
            ]:
                verify(p, path)
            for field in ["income_before_tax", "income_tax_expense"]:
                verify(p, "income_statement." + field, False)
        elif industry in {"bank", "insurer"}:
            verify(p, "balance_sheet.shareholders_equity")
            # Both values influence the displayed historical tax context.
            for field in ["income_before_tax", "income_tax_expense"]:
                verify(p, "income_statement." + field)
            if industry == "bank":
                verify(p, "cash_flow_statement.dividends_paid", False)
                if (
                    getattr(p.cash_flow_statement, "common_dividends_per_share", None)
                    is not None
                ):
                    verify(p, "cash_flow_statement.common_dividends_per_share")
        elif industry == "reit":
            verify(p, "income_statement.revenue")
            for field in ["income_before_tax", "income_tax_expense"]:
                verify(p, "income_statement." + field, False)
        else:
            issue(
                "unsupported_model",
                "company",
                "This industry model has no publication contract.",
            )
    verify(latest, "income_statement.diluted_shares_outstanding")
    if industry in {"standard", "energy"}:
        verify(latest, "balance_sheet.cash_and_equivalents")
        verify(latest, "balance_sheet.operating_lease_liabilities")
        if industry == "standard":
            verify(latest, "balance_sheet.shareholders_equity")
            verify(latest, "income_statement.interest_expense", False)
        for field in ["long_term_debt", "short_term_debt"]:
            if (
                field in special[pe0]
                or field in configured_fields(company.cik, latest.filing_accession)
                or "balance_sheet." + field
                in scoped_paths(company.cik, latest.filing_accession)
            ):
                verify(latest, "balance_sheet." + field)
            else:
                _verify_debt(company, facts, bound, inputs, issues)
        # _verify_debt checks the pair together once.
        inputs = list({(i.period_end, i.field_path): i for i in inputs}.values())
        issues = list(
            {(i.code, i.field_path, i.period_end): i for i in issues}.values()
        )
    if industry == "reit":
        verify(latest, "income_statement.depreciation_amortization")
        verify(latest, "cash_flow_statement.capital_expenditures", False)
        disclosures.append(
            "REIT valuation uses approximate FFO (net income plus D&A), not reported Nareit FFO or a complete AFFO calculation."
        )
    for flag in company.extraction_flags:
        if flag.field_path == "balance_sheet":
            issue("reconciliation_failed", flag.field_path, flag.reason)
    if policy and policy["model"] != industry:
        issue(
            "policy_model_mismatch",
            "company",
            "The filing policy does not match this valuation model.",
        )
    if company.valuation_note:
        disclosures.append(company.valuation_note)
    if not issues:
        from dcf import default_assumptions, compute_projection

        try:
            compute_projection(company, default_assumptions(company))
        except (ValueError, ZeroDivisionError, OverflowError):
            issue(
                "model_constraints_failed",
                "company",
                "The qualified inputs do not support this model under its starting assumptions.",
            )
    return _report(
        company,
        issues,
        inputs,
        disclosures,
        latest_filing,
        evidence_digest=hashlib.sha256(
            json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


def _verify_debt(company, facts, bound, inputs, issues):
    p = company.periods[0]
    pe = p.fiscal_period_end
    acc = p.filing_accession

    def fact(tag):
        return annual_fact(facts, tag, pe, pe, acc, bound, instant=True)

    noncurrent = fact("LongTermDebtNoncurrent")
    current = fact("DebtCurrent")
    maturities = fact("LongTermDebtCurrent")
    paper = fact("CommercialPaper")
    total = fact("LongTermDebt")
    operands = []
    if current is None and maturities and paper and maturities["accn"] == paper["accn"]:
        current = {
            **maturities,
            "value": maturities["value"] + paper["value"],
            "concept": "current maturities + commercial paper",
        }
        operands = [
            {"concept": r["concept"], "value": str(r["value"]), "accession": r["accn"]}
            for r in [maturities, paper]
        ]
    # Same filing carrying-amount reconciliation; face value is not a total.
    valid = (
        noncurrent is not None
        and current is not None
        and noncurrent["value"] >= 0
        and current["value"] >= 0
    )
    if (
        total
        and maturities
        and noncurrent
        and total["value"] != maturities["value"] + noncurrent["value"]
    ):
        valid = False
    for field, row, scope in [
        ("long_term_debt", noncurrent, "noncurrent debt carrying amount"),
        ("short_term_debt", current, "current borrowings including current maturities"),
    ]:
        item = getattr(p.balance_sheet, field, None)
        if not valid or item is None or item.value != row["value"]:
            issues.append(
                InputIssue(
                    code="debt_basis_unverified",
                    field_path="balance_sheet." + field,
                    period_end=str(pe),
                    message="Current/noncurrent carrying-value debt and current-borrowing coverage have not been established together. Missing debt is not zero.",
                )
            )
        else:
            inputs.append(
                InputEvidence(
                    field_path="balance_sheet." + field,
                    period_end=str(pe),
                    value=str(item.value),
                    unit="USD",
                    scope=scope,
                    basis="carrying amount",
                    accession=row["accn"],
                    concepts=[row["concept"]],
                    operands=operands,
                )
            )


def _report(company, issues, inputs, disclosures, latest_filing, evidence_digest=None):
    return Qualification(
        status="review_required" if issues else "qualified",
        policy_version=POLICY_VERSION,
        record_digest=record_digest(company),
        filing_accession=company.periods[0].filing_accession if company.periods else "",
        checked_at=datetime.now(timezone.utc).isoformat(),
        issues=issues,
        inputs=inputs,
        disclosures=list(dict.fromkeys(x for x in disclosures if x)),
        evidence_digest=evidence_digest,
        latest_filing_checked=latest_filing,
    )
