"""Issuer-independent, source-only financial scope contracts.

These rules operate on a filing's primary statement roles, numeric contexts and
calculation graph. They never approve an issuer, trust an AI quote, infer a
missing debt component as zero, or shorten a failed model history.
"""

import asyncio
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import re

from filing_coverage import Selection
from filing_document import FilingDocument, norm
from filing_sources import annual_documents, linked_documents
from input_contracts import anchor_date
from schemas import ExtractionSource, Industry, LineItem
from edgar import concepts_for

RULESET = "annual-statement-evidence-v1"


@dataclass
class SourceInput(Selection):
    display_label: str | None = None
    is_proxy: bool = False
    disclosure: str | None = None


def choose(rows):
    rows = [r for r in rows if r is not None]
    if not rows or len({(r["value"], r.get("start")) for r in rows}) != 1:
        return None
    return rows[0]


def _flatten(rows):
    out = []
    for row in rows:
        out.extend(
            [
                {**r, "usage": row.get("usage", "input component")}
                for r in _flatten(row["components"])
            ]
            if row.get("components")
            else [row]
        )
    return out


def selection(
    value, rows, scope, basis, label=None, proxy=False, direct=None, disclosure=None
):
    proof = _flatten(rows)
    unique = {
        (
            r["concept"],
            r["end"],
            r.get("start"),
            tuple(sorted(r.get("dimensions", {}).items())),
            r["value"],
        ): r
        for r in proof
    }
    proof = list(unique.values())
    if not proof:
        raise ValueError("Numeric source operands are required")
    unit = proof[0]["unit"]
    operands = "; ".join(
        f"[{r.get('usage', 'input component')}] {r['concept']}={r['value']} {r['unit']}"
        + (f" {r['dimensions']}" if r.get("dimensions") else "")
        for r in proof
    )
    periods = ", ".join(
        sorted(
            {
                (r.get("start") + " to " if r.get("start") else "year-end ") + r["end"]
                for r in proof
            }
        )
    )
    accessions = ", ".join(sorted({r["accn"] for r in proof}))
    item = LineItem(
        value=value,
        source=ExtractionSource.XBRL
        if direct and not any(r.get("components") for r in rows)
        else ExtractionSource.DERIVED,
        confidence=1 if direct else 0.99,
        xbrl_tag=direct if not any(r.get("components") for r in rows) else None,
        source_quote=f"{scope}. {basis}. {operands}; selected amount {value} {unit}; {periods}; SEC accession {accessions}.",
    )
    return SourceInput(item, proof, scope, basis, label, proxy, disclosure)


def corroborates(row, facts, filing, bound):
    if row.get("components"):
        return all(corroborates(r, facts, filing, bound) for r in row["components"])
    if row.get("dimensions") or not row["concept"].startswith("us-gaap:"):
        return (
            True  # Inline-only evidence is still checked in a reconciled source role.
        )
    concept = row["concept"].split(":", 1)[1]
    candidates = (
        facts.get("facts", {})
        .get("us-gaap", {})
        .get(concept, {})
        .get("units", {})
        .get(row["unit"], [])
    )
    valid = []
    for r in candidates:
        if (
            r.get("accn") != filing["accession"]
            or r.get("end") != row["end"]
            or r.get("start") != row.get("start")
            or r.get("form") not in {"10-K", "10-K/A"}
            or r.get("fp") != "FY"
        ):
            continue
        try:
            filed = date.fromisoformat(r["filed"])
            value = Decimal(str(r["val"]))
        except (KeyError, ValueError, TypeError):
            continue
        if filed == filing["filed"] and filed <= bound and value.is_finite():
            valid.append(value)
    # The precise inline amount must also occur in the independent SEC API.
    compatible = {Decimal(v) for v in row.get("compatible_values", [str(row["value"])])}
    return row["value"] in valid and set(valid) <= compatible


def _prove(row, filing, role):
    return {
        **row,
        "accn": filing["accession"],
        "source_url": filing["url"],
        "role": role,
        **(
            {"components": [_prove(c, filing, role) for c in row["components"]]}
            if row.get("components")
            else {}
        ),
    }


def _lookup(doc, tags, pe, kind, *, instant=False, unit="USD"):
    rows = [
        doc.fact("us-gaap:" + tag, pe, kind=kind, instant=instant, unit=unit)
        for tag in tags
    ]
    return choose(rows)


def _by_label(doc, pe, kind, labels, *, instant=False):
    allowed = {norm(s) for s in labels}
    concepts = {
        r["concept"]
        for r in doc.rows
        if norm(r["label"]) in allowed and doc.member(r["concept"], kind)
    }
    rows = [doc.fact(c, pe, instant=instant, kind=kind) for c in concepts]
    # A label on another year, dimension or unit cannot certify this fact.
    rows = [r for r in rows if r is not None and norm(r["label"]) in allowed]
    return statement_total(doc, rows, pe, kind, instant=instant)


def statement_total(doc, rows, pe, kind, *, instant=False):
    common = choose(rows)
    if common is not None:
        return common
    rows = [r for r in rows if r is not None]
    if len(rows) < 2:
        return None
    candidates = []
    for role in doc.roles(kind):
        for row in rows:
            seen = set()

            def walk(c):
                if c in seen:
                    return
                seen.add(c)
                for child, _, _ in doc.children(role, c):
                    walk(child)

            walk(row["concept"])
            if all(r["concept"] in seen for r in rows) and doc.reconcile(
                role, row["concept"], pe, instant=instant
            ):
                candidates.append(row)
    return choose(candidates)


def balance_totals(doc, facts, filing, bound, pe, output):
    for role in doc.roles("balance_sheet"):
        proof = doc.reconcile(
            role, "us-gaap:LiabilitiesAndStockholdersEquity", pe, instant=True
        )
        if not proof or not all(corroborates(r, facts, filing, bound) for r in proof):
            continue
        # Derive the monetary liability subtotal only from the explicit complete
        # top-level rollup, excluding equity and redeemable ownership interests.
        if "balance_sheet.total_liabilities" not in output:
            root = "us-gaap:LiabilitiesAndStockholdersEquity"
            parts = [
                doc.role_fact(c, pe, role, instant=True)
                for c, w, _ in doc.children(role, root)
                if w == 1
                and not any(
                    x in norm(c)
                    for x in ["equity", "noncontrolling", "minorityinterest"]
                )
            ]
            if parts and all(r is not None for r in parts):
                output["balance_sheet.total_liabilities"] = selection(
                    sum(r["value"] for r in parts),
                    [_prove(r, filing, role) for r in parts + proof],
                    "Recognized liabilities, excluding equity and redeemable ownership interests",
                    "Sum of non-equity monetary children in a reconciled balance-sheet total",
                )
        parent = doc.fact(
            "us-gaap:StockholdersEquity", pe, instant=True, kind="balance_sheet"
        )
        inclusive = doc.fact(
            "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            pe,
            instant=True,
            kind="balance_sheet",
        )
        redeemable = doc.fact(
            "us-gaap:RedeemableNoncontrollingInterestEquityCarryingAmount",
            pe,
            instant=True,
            kind="balance_sheet",
        )
        minority = doc.fact(
            "us-gaap:MinorityInterest", pe, instant=True, kind="balance_sheet"
        )
        if parent and inclusive:
            value = inclusive["value"] - parent["value"]
            if minority is not None and minority["value"] != value:
                continue
            parts = [inclusive, parent] + ([redeemable] if redeemable else [])
            if value < 0 or not all(
                corroborates(r, facts, filing, bound) for r in parts
            ):
                continue
            output["balance_sheet.noncontrolling_interests"] = selection(
                value + (redeemable["value"] if redeemable else 0),
                [_prove(r, filing, role) for r in parts],
                "Noncontrolling interests including separately presented redeemable interests",
                "Inclusive equity minus parent equity, plus separately presented redeemable noncontrolling interests; monetary top-level rollup reconciles",
            )
    return output


def mapped_inputs(doc, facts, filing, bound, pe, industry):
    """Display fields also require source context, never first-tag backfill."""
    result = {}
    maps = concepts_for(industry)
    from graph import _industry_fields

    groups = _industry_fields(industry)[:3]
    for group, fields in zip(
        ["income_statement", "balance_sheet", "cash_flow_statement"], groups
    ):
        for field in fields:
            if field in {
                "depreciation_amortization",
                "short_term_debt",
                "long_term_debt",
                "operating_lease_liabilities",
            }:
                continue
            key = "diluted_shares" if field == "diluted_shares_outstanding" else field
            tags = maps.get(key, [])
            common_tags = {
                "income_before_tax": [
                    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
                ],
                "income_tax_expense": ["IncomeTaxExpenseBenefit"],
                "research_and_development": ["ResearchAndDevelopmentExpense"],
                "selling_general_administrative": [
                    "SellingGeneralAndAdministrativeExpense"
                ],
                "gross_profit": ["GrossProfit"],
                "interest_expense": [
                    "InterestExpenseNonOperating",
                    "InterestAndDebtExpense",
                ],
                "total_current_assets": ["AssetsCurrent"],
                "total_current_liabilities": ["LiabilitiesCurrent"],
                "property_plant_equipment_net": ["PropertyPlantAndEquipmentNet"],
                "accounts_payable": [
                    "AccountsPayableCurrent",
                    "AccountsPayableTradeCurrent",
                ],
                "inventory": ["InventoryNet"],
                "accounts_receivable": ["AccountsReceivableNetCurrent"],
                "short_term_investments": ["ShortTermInvestments"],
            }
            if not tags:
                tags = common_tags.get(field, [])
            if industry == Industry.BANK and field == "dividends_paid":
                tags = ["PaymentsOfDividendsCommonStock"]
            if field == "diluted_shares_outstanding":
                tags = ["WeightedAverageNumberOfDilutedSharesOutstanding"]
            # A broad cash tag is not a fallback. Cash is handled by a statement-row contract below.
            if field == "cash_and_equivalents":
                tags = ["CashAndCashEquivalentsAtCarryingValue"]
            # Use actual primary income totals, never domestic/foreign pre-tax components.
            if field == "income_before_tax":
                tags = [t for t in tags if not t.endswith(("Domestic", "Foreign"))]
            instant = group == "balance_sheet"
            unit = "shares" if field == "diluted_shares_outstanding" else "USD"
            rows = [
                doc.fact(
                    "us-gaap:" + tag,
                    pe,
                    instant=instant,
                    unit=unit,
                    kind=None
                    if field == "diluted_shares_outstanding"
                    or (industry == Industry.BANK and field == "dividends_paid")
                    else group,
                )
                for tag in tags
            ]
            # Different total/component concepts cannot be silently interchanged.
            # Exact parent equity and parent earnings have precedence over inclusive alternatives.
            if field in {"shareholders_equity", "net_income"}:
                rows = [next((r for r in rows if r is not None), None)]
            row = statement_total(doc, rows, pe, group, instant=instant)
            if row is None or not corroborates(row, facts, filing, bound):
                continue
            if field == "diluted_shares_outstanding" and row["value"] <= 0:
                continue
            if (
                field in {"capital_expenditures", "cash_and_equivalents"}
                and row["value"] < 0
            ):
                continue
            scope = f"{group} reported amount: {row['label'] or row['concept']}"
            result[group + "." + field] = selection(
                row["value"],
                [_prove(row, filing, group)],
                scope,
                "Primary-statement presentation membership, annual entity context and SEC company-facts corroboration",
                direct=row["concept"],
            )
    # Custom concepts are permitted only for explicit primary-statement totals.
    totals = {
        "income_statement.revenue": [
            "Revenue",
            "Revenues",
            "Total revenue",
            "Total revenues",
            "Net sales",
            "Sales",
        ],
        "income_statement.operating_income": [
            "Operating income",
            "Operating income (loss)",
            "Income from operations",
            "Earnings from operations",
            "Operating profit",
        ],
        "income_statement.income_before_tax": [
            "Income before income taxes",
            "Income (loss) before income taxes",
            "Income before provision for income taxes",
            "Income before taxes",
            "Earnings before income taxes",
            "Earnings before income taxes and equity income",
            "Pretax income (loss)",
        ],
        "income_statement.income_tax_expense": [
            "Income taxes",
            "Income tax expense (benefit)",
            "Provision for income taxes",
            "Provision for (benefit from) income taxes",
            "Provision (benefit) for income taxes",
        ],
        "balance_sheet.cash_and_equivalents": [
            "Cash and cash equivalents",
            "Cash and equivalents",
        ],
        "balance_sheet.total_loans": [
            "Loans and leases, net of allowance",
            "Loans and leases, net of allowance for credit losses",
            "Loans and leases, net",
            "Loans, net",
        ],
    }
    for path, labels in totals.items():
        if path in result:
            continue
        kind, field = path.split(".")
        row = _by_label(doc, pe, kind, labels, instant=kind == "balance_sheet")
        if row is None or not corroborates(row, facts, filing, bound):
            continue
        if field in {"revenue", "cash_and_equivalents"} and row["value"] < 0:
            continue
        result[path] = selection(
            row["value"],
            [_prove(row, filing, kind)],
            f"Consolidated {kind} row: {row['label']}",
            "Exact consolidated primary-statement total and context; no component or differently scoped row substitution",
            direct=row["concept"],
        )
    if industry == Industry.BANK:
        row = doc.fact(
            "us-gaap:CommonStockDividendsPerShareCashPaid", pe, unit="USD/shares"
        )
        if row and row["value"] >= 0 and corroborates(row, facts, filing, bound):
            result["cash_flow_statement.common_dividends_per_share"] = selection(
                row["value"],
                [_prove(row, filing, "annual common-dividend disclosure")],
                "Annual cash dividends paid per common share",
                "Explicit paid-common dividend concept, annual entity context, USD/share unit and same-accession SEC corroboration; not declared dividends or aggregate cash flow",
                "Common cash dividends paid per share (supplemental DDM input)",
                direct=row["concept"],
                disclosure="The bank DDM uses filed annual cash dividends paid per common share directly. This supplemental note disclosure is not total cash dividends, declared dividends or a cash-flow statement subtotal. No aggregate cash amount is estimated from average shares.",
            )
    return balance_totals(doc, facts, filing, bound, pe, result)


def _da_node(concept):
    name = norm(concept)
    return (
        "depreciation" in name or "amortization" in name or "depletion" in name
    ) and not any(
        s in name
        for s in [
            "operatinglease",
            "financingcost",
            "operatingandfinancinglease",
            "debt",
            "discount",
            "premium",
            "policyacquisition",
            "insuranceprofit",
        ]
    )


def cash_addback(doc, facts, filing, bound, pe):
    candidates = []
    for role in doc.roles("cash_flow_statement"):
        picked = []
        seen = set()
        unsafe = False

        def walk(node, weight=Decimal(1), depth=0):
            nonlocal unsafe
            if depth > 20 or node in seen:
                unsafe = True
                return
            seen.add(node)
            for child, w, _ in doc.children(role, node):
                if _da_node(child):
                    picked.append((child, weight * w))
                elif doc.children(role, child):
                    walk(child, weight * w, depth + 1)
            seen.remove(node)

        walk("us-gaap:NetCashProvidedByUsedInOperatingActivities")
        if unsafe or not picked or len({c for c, _ in picked}) != len(picked):
            continue
        # Every presented D&A component must have an explicit, nonoverlapping path.
        presented = {
            c
            for c in doc.concepts[role]
            if _da_node(c) and not c.endswith(("Abstract", "Axis", "Domain"))
        }
        if not presented <= {c for c, _ in picked}:
            continue
        rows = [doc.fact(c, pe, kind="cash_flow_statement") for c, _ in picked]
        if any(
            r is None
            or r["value"] < 0
            or w != 1
            or not corroborates(r, facts, filing, bound)
            for r, (_, w) in zip(rows, picked)
        ):
            continue
        words = " ".join(r["label"] for r in rows)
        names = " ".join(c for c, _ in picked)
        # A PP&E-only concept is not a consolidated amortization contract.
        if all("propertyplantandequipment" in norm(c) for c, _ in picked):
            continue
        if not ("depreciation" in norm(names) and "amortization" in norm(names)):
            continue
        if not ("amortization" in norm(words) or norm(words) in {"da", "dda"}):
            continue
        proxy = bool(
            re.search(
                r"other|accretion|content|lease|impairment|commission|inventory",
                words + " " + names,
                re.I,
            )
        )
        label = (
            "Combined cash-flow add-back"
            if proxy
            else (
                "D&A (sum of cash-flow components)"
                if len(rows) > 1
                else "Depreciation and amortization"
            )
        )
        value = sum((r["value"] for r in rows), Decimal(0))
        candidates.append(
            selection(
                value,
                [_prove(r, filing, role) for r in rows],
                f"Cash-flow add-back: {words}",
                "All presented D&A components follow distinct positive calculation paths into operating cash flow; not income-statement expense",
                label,
                proxy,
                direct=rows[0]["concept"] if len(rows) == 1 else None,
            )
        )
    if not candidates or len({c.item.value for c in candidates}) != 1:
        return None
    return candidates[0]


def lease_pair(doc, pe, finance):
    name = "Finance" if finance else "Operating"
    tags = [
        "us-gaap:" + name + "LeaseLiability" + s for s in ["Current", "Noncurrent", ""]
    ]
    cur, non, total = [doc.fact(t, pe, instant=True) for t in tags]
    if total and non and cur is None:
        cur = {
            **total,
            "value": total["value"] - non["value"],
            "concept": "rule:" + name + "LeaseCurrentResidual",
            "components": [total, non],
        }
    if total and cur and non is None:
        non = {
            **total,
            "value": total["value"] - cur["value"],
            "concept": "rule:" + name + "LeaseNoncurrentResidual",
            "components": [total, cur],
        }
    if cur is None or non is None or min(cur["value"], non["value"]) < 0:
        return None
    if total and total["value"] != cur["value"] + non["value"]:
        return None
    return cur, non, [r for r in [cur, non, total] if r is not None]


def _debt_node(concept):
    n = norm(concept.split(":", 1)[-1])
    return any(
        w in n
        for w in [
            "debt",
            "borrowings",
            "commercialpaper",
            "notespayable",
            "financeleaseliability",
            "financingobligation",
            "capitalleaseobligation",
        ]
    ) and not any(
        w in n
        for w in ["cost", "discount", "fairvalue", "asset", "payments", "issuance"]
    )


def carrying_debt(doc, facts, filing, bound, pe):
    candidates = []
    for role in doc.roles("balance_sheet"):
        top = "us-gaap:LiabilitiesAndStockholdersEquity"
        proof = doc.reconcile(role, top, pe, instant=True)
        current_proof = doc.reconcile(
            role, "us-gaap:LiabilitiesCurrent", pe, instant=True
        )
        if proof is None or current_proof is None:
            continue
        current_nodes = set()
        non_nodes = set()
        visited = set()
        bad = False

        def walk(node, current=False, depth=0):
            nonlocal bad
            if node in visited or depth > 20:
                bad = True
                return
            visited.add(node)
            reconciled = doc.reconcile(role, node, pe, instant=True)
            if reconciled is None:
                bad = True
                visited.remove(node)
                return
            proof.extend(reconciled)
            for child, w, _ in doc.children(role, node):
                if w != 1:
                    continue
                if child == "us-gaap:LiabilitiesCurrent":
                    walk(child, True, depth + 1)
                elif (
                    "equity" in child.lower()
                    or "noncontrolling" in child.lower()
                    or "minorityinterest" in child.lower()
                ):
                    continue
                elif _debt_node(child):
                    (current_nodes if current else non_nodes).add(child)
                elif doc.children(role, child):
                    walk(child, current, depth + 1)
            visited.remove(node)

        walk(top)
        if bad:
            continue
        cur = [doc.role_fact(c, pe, role, instant=True) for c in sorted(current_nodes)]
        non = [doc.role_fact(c, pe, role, instant=True) for c in sorted(non_nodes)]
        if any(r is None or r["value"] < 0 for r in cur + non):
            continue

        def includes_lease(nodes):
            return any(
                "capitallease" in c.lower()
                or "financelease" in c.lower()
                or c == "us-gaap:DebtCurrent"
                for c in nodes
            )

        current_inclusive = includes_lease(current_nodes)
        noncurrent_inclusive = includes_lease(non_nodes)
        inclusive = current_inclusive and noncurrent_inclusive
        finance = lease_pair(doc, pe, True)
        debt_disclosure = None
        if finance:
            for dest, row, nodes, covered in zip(
                [cur, non],
                finance[:2],
                [current_nodes, non_nodes],
                [current_inclusive, noncurrent_inclusive],
            ):
                if row["concept"] in nodes or covered:
                    continue
                # Standard notes-only taxonomy concepts exclude capital leases.
                # A custom unscoped debt total may already include them.
                if any(
                    not c.startswith("us-gaap:")
                    and "financingobligation" not in norm(c)
                    for c in nodes
                ):
                    bad = True
                    break
                dest.append(row)
            if bad:
                continue
            proof.extend(finance[2])
        elif not inclusive:
            finance_activity = [
                r
                for r in doc.rows
                if r["end"] == str(pe)
                and any(
                    s in norm(r["concept"])
                    for s in ["financelease", "capitallease", "financingobligation"]
                )
            ]
            if finance_activity:
                # Accumulated asset amortization is a historical contra-asset,
                # not a current obligation. Only this exact semantic case may
                # proceed, and only with an independently reconciled debt note.
                allowed = {"us-gaap:FinanceLeaseRightOfUseAssetAccumulatedAmortization"}
                if {r["concept"] for r in finance_activity} != allowed:
                    continue
                amortization = doc.fact(next(iter(allowed)), pe, instant=True)
                note_rows = [
                    doc.fact("us-gaap:" + tag, pe, instant=True)
                    for tag in [
                        "LongTermDebt",
                        "LongTermDebtCurrent",
                        "LongTermDebtNoncurrent",
                    ]
                ]
                if amortization is None or any(x is None for x in note_rows):
                    continue
                total, current, noncurrent = note_rows
                if (
                    any(x["value"] < 0 for x in [amortization, *note_rows])
                    or total["value"] != current["value"] + noncurrent["value"]
                    or sum(x["value"] for x in cur) != current["value"]
                    or sum(x["value"] for x in non) != noncurrent["value"]
                    or not all(
                        corroborates(x, facts, filing, bound)
                        for x in [amortization, *note_rows]
                    )
                ):
                    continue
                proof.extend([amortization, *note_rows])
                debt_disclosure = "Debt uses reported current and noncurrent carrying notes/borrowings, reconciled to the debt-note total and the balance sheet. Historical accumulated asset amortization is not a lease liability. No finance-lease liability is inferred as zero or estimated from that asset amount."
        if (
            "us-gaap:FinanceLeaseLiabilityCurrent" in current_nodes
            and "us-gaap:DebtCurrent" in current_nodes
        ):
            continue
        # An additional recognized borrowing outside the primary BS must be
        # included or proved part of an already-inclusive current debt total.
        for tag in ["us-gaap:ShortTermBorrowings", "us-gaap:CommercialPaper"]:
            extra = doc.fact(tag, pe, instant=True)
            if (
                extra
                and tag not in current_nodes
                and not any(
                    c in current_nodes
                    for c in ["us-gaap:DebtCurrent", "us-gaap:ShortTermDebtCurrent"]
                )
            ):
                if (
                    tag == "us-gaap:CommercialPaper"
                    and "us-gaap:ShortTermBorrowings" in {r["concept"] for r in cur}
                ):
                    continue
                cur.append(extra)
        # Custom recognized financing obligations cannot disappear inside other liabilities.
        custom = {
            r["concept"]
            for r in doc.rows
            if r["end"] == str(pe)
            and r["instant"]
            and not r["dimensions"]
            and "financingobligation" in norm(r["concept"])
        }
        for c in sorted(custom - current_nodes - non_nodes):
            row = doc.fact(c, pe, instant=True)
            if row is None:
                bad = True
                break
            if "noncurrent" in norm(c):
                non.append(row)
            elif "current" in norm(c):
                cur.append(row)
            elif row["value"] != 0:
                bad = True
                break
        if bad:
            continue
        if not all(corroborates(r, facts, filing, bound) for r in cur + non):
            continue
        proof += current_proof
        scope = (
            "Carrying debt"
            + (" including finance leases" if inclusive or finance else "")
            + (" and recognized financing obligations" if custom else "")
        )
        pair = {}
        for field, rows, maturity in [
            ("short_term_debt", cur, "Current"),
            ("long_term_debt", non, "Noncurrent"),
        ]:
            value = sum((r["value"] for r in rows), Decimal(0))
            # An empty component set is zero only because a complete, explicit
            # balance-sheet rollup reconciles without any such debt line.
            basis = "Nonoverlapping primary balance-sheet debt plus separately recognized components; current and total balance-sheet rollups reconcile; facility capacity and future cash payments are excluded"
            pair["balance_sheet." + field] = selection(
                value,
                [_prove({**r, "usage": "input component"}, filing, role) for r in rows]
                + [
                    _prove({**r, "usage": "scope reconciliation"}, filing, role)
                    for r in proof
                ],
                scope,
                basis,
                maturity + " " + scope.lower(),
                direct=None,
                disclosure=debt_disclosure,
            )
        candidates.append(pair)
    if (
        not candidates
        or len({tuple(p[k].item.value for k in sorted(p)) for p in candidates}) != 1
    ):
        return {}
    return candidates[0]


def _history_ends(facts, filings, latest, bound):
    ends = {f["end"] for f in filings}
    for concept in [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "NetIncomeLoss",
    ]:
        for row in (
            facts.get("facts", {})
            .get("us-gaap", {})
            .get(concept, {})
            .get("units", {})
            .get("USD", [])
        ):
            try:
                end, filed, start = (
                    date.fromisoformat(row["end"]),
                    date.fromisoformat(row["filed"]),
                    date.fromisoformat(row["start"]),
                )
            except (KeyError, ValueError, TypeError):
                continue
            if (
                row.get("form") == "10-K"
                and row.get("fp") == "FY"
                and end <= latest
                and filed <= bound
                and 330 <= (end - start).days <= 380
            ):
                ends.add(end)
    ends = sorted(ends, reverse=True)[:5]
    if (
        len(ends) < 3
        or ends[0] != latest
        or any(not 330 <= (a - b).days <= 380 for a, b in zip(ends, ends[1:]))
    ):
        raise ValueError("At least three consecutive annual model periods are required")
    return ends


async def load_reusable_inputs(
    client, facts, cik, latest, accession, industry, *, anchor_html=None
):
    if str(facts.get("cik", "")).lstrip("0") != cik.lstrip("0"):
        raise ValueError("Matching company-facts entity is required")
    bound = anchor_date(facts, accession)
    if bound is None or not latest <= bound <= datetime.now(timezone.utc).date():
        raise ValueError("Matching, nonfuture annual filing date is required")
    filings, documents = await annual_documents(client, cik, latest, accession, bound)
    ends = _history_ends(facts, filings, latest, bound)
    inputs = {pe: {} for pe in ends}
    source_documents = []
    for filing in filings[::2]:
        html = (
            anchor_html
            if filing["accession"] == accession and anchor_html is not None
            else await client.get_filing_html(filing["url"])
        )
        linked = await linked_documents(client, filing, html)
        documents.update(linked["documents"])
        doc = await asyncio.to_thread(FilingDocument, html, cik, filing["end"], linked)
        source_documents.append((filing, doc))
        for pe in ends:
            if pe > filing["end"] or (
                pe == latest and filing["accession"] != accession
            ):
                continue
            selected = mapped_inputs(doc, facts, filing, bound, pe, industry)
            if industry in {Industry.STANDARD, Industry.ENERGY, Industry.REIT}:
                da = cash_addback(doc, facts, filing, bound, pe)
                if da:
                    selected["cash_flow_statement.depreciation_amortization"] = da
                    if industry == Industry.REIT:
                        selected["income_statement.depreciation_amortization"] = (
                            replace(
                                da, display_label="Approximate-FFO add-back (cash flow)"
                            )
                        )
            if industry in {Industry.STANDARD, Industry.ENERGY}:
                leases = lease_pair(doc, pe, False)
                if leases and all(
                    corroborates(r, facts, filing, bound) for r in leases[2]
                ):
                    value = leases[0]["value"] + leases[1]["value"]
                    selected["balance_sheet.operating_lease_liabilities"] = selection(
                        value,
                        [_prove(r, filing, "lease note") for r in leases[2]],
                        "Total current plus noncurrent operating-lease liabilities",
                        "Discounted carrying liability; not undiscounted future lease payments",
                        "Operating-lease liabilities",
                    )
                if "balance_sheet.operating_lease_liabilities" not in selected:
                    total = doc.fact(
                        "us-gaap:OperatingLeaseLiability", pe, instant=True
                    )
                    if (
                        total
                        and total["value"] >= 0
                        and corroborates(total, facts, filing, bound)
                    ):
                        selected["balance_sheet.operating_lease_liabilities"] = (
                            selection(
                                total["value"],
                                [_prove(total, filing, "lease note")],
                                "Explicit total operating-lease carrying liability",
                                "Filed present-value total; not a noncurrent component or undiscounted future payments",
                                "Operating-lease liabilities",
                                direct=total["concept"],
                            )
                        )
                if pe == latest:
                    selected.update(carrying_debt(doc, facts, filing, bound, pe))
            # Newest annual comparatives take precedence; never replace them
            # with a conveniently different amount from an older document.
            for path, value in selected.items():
                inputs[pe].setdefault(path, value)
    metadata = await client.get_submissions(cik)
    model_issue = model_scope_issue(metadata, industry, source_documents[0][1], latest)
    return {
        "inputs": inputs,
        "period_ends": ends,
        "documents": documents,
        "filings": filings,
        "model_issue": model_issue,
        "ruleset": RULESET,
        "source_only": True,
    }


def model_scope_issue(metadata, industry, doc, latest):
    from industry import classify_sic

    try:
        code = int(metadata["sic"])
    except (KeyError, TypeError, ValueError):
        return "A valid SEC industry classification is required before selecting a valuation model."
    if classify_sic(code) != industry:
        return "Industry overrides require a reviewed model contract; automatic source rules cannot establish this model choice."
    if industry == Industry.STANDARD and (6000 <= code <= 6999):
        return "This financial business has no automatic model-scope contract for industrial FCFF."
    if industry == Industry.ENERGY and code != 1311:
        return "The automatic ten-year energy model is limited to oil and gas producers; services and refining require model review."
    if industry == Industry.INSURER and not 6311 <= code <= 6399:
        return "Insurance agencies and brokers require separate model review; carrier P/B is not automatically suitable."
    if industry == Industry.REIT:
        property_rows = [
            r
            for r in doc.rows
            if r["end"] == str(latest)
            and not r["dimensions"]
            and r["value"] > 0
            and doc.member(r["concept"], "balance_sheet")
            and any(
                w in norm(r["concept"])
                for w in [
                    "realestateinvestmentproperty",
                    "realestateinvestment",
                    "realestateproperties",
                ]
            )
        ]
        if not property_rows:
            return "Property-ownership evidence is required for approximate property REIT FFO; mortgage and financial REITs require a separate model review."
    return None


def compose_reusable(state, loaded):
    """Compose only independently source-selected values; never reuse Track A guesses."""
    from graph import _compose_company, _missing_required_for_period, CompositionError

    industry = state.get("industry", Industry.STANDARD)
    periods = {}
    for pe in loaded["period_ends"]:
        items = (
            {"income_depreciation_amortization": None}
            if industry in {Industry.STANDARD, Industry.ENERGY}
            else {}
        )
        for path, selected in loaded["inputs"][pe].items():
            group, field = path.split(".")
            key = (
                "income_depreciation_amortization"
                if group == "income_statement" and field == "depreciation_amortization"
                else field
            )
            items[key] = selected.item
        missing = _missing_required_for_period(items, industry)
        if missing:
            raise CompositionError(
                f"Annual {pe} is missing source-backed {missing}; refusing to shorten history"
            )
        periods[pe] = items
    company = _compose_company(
        state["ticker"],
        state["cik"],
        state["company_name"],
        loaded["period_ends"],
        state["filing_accession"],
        periods,
        industry,
        revenue_segments=None,
    )
    notes = [
        "Source-only SEC statement extraction; optional rows without established scope are left unavailable. No AI backfill was used."
    ]
    notes.extend(
        sorted(
            {
                s.disclosure
                for selected in loaded["inputs"].values()
                for s in selected.values()
                if s.disclosure
            }
        )
    )
    proxies = {
        s.scope
        for items in loaded["inputs"].values()
        for s in items.values()
        if s.is_proxy
    }
    if proxies:
        notes.extend(sorted(proxies))
        notes.append(
            "Combined cash-flow proxies are not pure D&A; their ratios are projected against revenue and model-implied EV/EBITDA is unavailable."
        )
    if industry == Industry.REIT:
        notes.append(
            "Approximate FFO is net income plus filed cash-flow D&A; it is not reported Nareit FFO or complete AFFO."
        )
    return company.model_copy(update={"valuation_note": " ".join(notes)})
