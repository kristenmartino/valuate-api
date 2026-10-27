"""Reviewed debt/D&A contracts for five specific annual filings.

Scope comes from the reviewed filing policy; amounts always come from matching
annual USD contexts. Unknown filings and missing operands are never inferred.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

from input_contracts import anchor_date, annual_fact
from schemas import Company, ExtractionSource, LineItem

POLICIES = json.loads((Path(__file__).parent / "qualification_policies.json").read_text())
SUPPORTED = {"796343", "1108524", "821189", "77476", "97476"}
DEBT_FIELDS = {"short_term_debt", "long_term_debt"}


@dataclass
class Selection:
    item: LineItem
    rows: list[dict]
    scope: str
    basis: str


def configured_fields(cik, accession):
    policy = POLICIES.get(cik.lstrip("0") + ":" + accession, {})
    scope = policy.get("reviewed_input_scope", {})
    if not scope or cik.lstrip("0") not in SUPPORTED:
        return set()
    return DEBT_FIELDS | ({"depreciation_amortization"} if scope.get("da") else set())


def _common(facts, tags, pe, latest, accession, bound, instant):
    """Select one common annual filing, not separately convenient operands."""
    by_tag = []
    for tag in tags:
        by_acc = {}
        for row in facts.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {}).get("USD", []):
            acc = row.get("accn", "")
            try:
                filed = date.fromisoformat(row["filed"])
            except (KeyError, TypeError, ValueError):
                continue
            if (row.get("end") != str(pe) or row.get("fp") != "FY" or row.get("form") not in {"10-K", "10-K/A"}
                    or filed > bound or filed < pe or (pe == latest and acc != accession)):
                continue
            by_acc.setdefault(acc, []).append(row)
        by_tag.append(by_acc)
    common = set.intersection(*(set(rows) for rows in by_tag))
    common = {a for a in common if len({r["filed"] for rows in by_tag for r in rows[a]}) == 1}
    if not common:
        return None
    acc = max(common, key=lambda a: (by_tag[0][a][0]["filed"], a))
    selected = [annual_fact(facts, tag, pe, pe, acc, bound, instant=instant) for tag in tags]
    if any(r is None or r["value"] < 0 for r in selected):
        return None
    return selected


def _selection(value, rows, scope, basis, *, tag=None):
    operands = "; ".join(f"{r['concept']}={r['value']} USD" for r in rows)
    row = rows[0]
    dates = f"{row['start']} to {row['end']}" if row.get("start") else f"year-end {row['end']}"
    item = LineItem(value=value, source=ExtractionSource.XBRL if tag else ExtractionSource.DERIVED,
                    confidence=1 if tag else 0.99, xbrl_tag=tag,
                    source_quote=f"{scope}. {basis}. {operands}; selected amount {value} USD; {dates}; SEC accession {row['accn']}.")
    return Selection(item, rows, scope, basis)


def selections(facts, cik, pe, accession, latest):
    cik = cik.lstrip("0")
    policy = POLICIES.get(cik + ":" + accession, {})
    scope = policy.get("reviewed_input_scope", {})
    if (cik not in SUPPORTED or not scope or str(facts.get("cik", "")).lstrip("0") != cik
            or str(pe) not in policy.get("period_ends", []) or pe > latest):
        return {}
    bound = anchor_date(facts, accession)
    if bound is None or bound < latest:
        return {}
    output = {}
    debt_tags = {
        "796343": ["LongTermDebt", "DebtCurrent"],
        "1108524": ["LongTermDebtNoncurrent", "LongTermDebtCurrent", "LongTermDebt"],
        "821189": ["LongTermDebtNoncurrent", "LongTermDebtCurrent"],
        "77476": ["LongTermDebtNoncurrent", "DebtCurrent", "LongTermDebt"],
        "97476": ["LongTermDebtNoncurrent", "LongTermDebtCurrent", "CommercialPaper", "LongTermDebt"],
    }[cik]
    debt = _common(facts, debt_tags, pe, latest, accession, bound, True)
    if debt:
        noncurrent, current = debt[:2]
        short_value = current["value"]
        valid = True
        if cik == "1108524":
            valid = debt[2]["value"] == noncurrent["value"] + short_value
        elif cik == "77476":
            # Total notes include their current portion; DebtCurrent also
            # contains other short-term borrowings. It is not added twice.
            valid = Decimal(0) <= debt[2]["value"] - noncurrent["value"] <= short_value
        elif cik == "97476":
            valid = debt[3]["value"] == noncurrent["value"] + short_value
            short_value += debt[2]["value"]
        if valid:
            basis = {
                "796343": "Adobe balance-sheet noncurrent carrying debt, including the filed swap basis adjustment; DebtCurrent is the separate current carrying amount. The LongTermDebt tag is not a total in this filing",
                "1108524": "Current plus noncurrent carrying debt reconciles to the filed total; the current debt line is not an additional amount beside that total",
                "821189": "EOG balance-sheet carrying debt includes finance leases; the separately filed current portion is counted once, including current finance leases",
                "77476": "Noncurrent carrying debt plus total current borrowings; the inclusive long-term notes total is not added to DebtCurrent",
                "97476": "Noncurrent carrying notes plus current maturities and explicitly filed commercial paper; notes reconcile before commercial paper is added",
            }[cik]
            output["long_term_debt"] = _selection(noncurrent["value"], debt, "Noncurrent debt, excluding current maturities", basis, tag=noncurrent["concept"])
            output["short_term_debt"] = _selection(short_value, debt, "Current debt, including filed current borrowings", basis,
                tag=current["concept"] if cik != "97476" else None)
    if scope.get("da") == "adobe_cashflow" and cik == "796343":
        rows = _common(facts, ["DepreciationDepletionAndAmortization"], pe, latest, accession, bound, False)
        if rows:
            output["depreciation_amortization"] = _selection(rows[0]["value"], rows,
                "Adobe consolidated cash-flow depreciation, amortization and accretion",
                "Combined cash-flow add-back proxy, not pure D&A. Values are retained; the historical ratio is projected against revenue and DCF-implied EV/EBITDA is unavailable",
                tag=rows[0]["concept"])
    if scope.get("da") == "pepsico_cashflow" and cik == "77476":
        rows = _common(facts, ["DepreciationDepletionAndAmortization"], pe, latest, accession, bound, False)
        if rows:
            output["depreciation_amortization"] = _selection(rows[0]["value"], rows,
                "PepsiCo consolidated cash-flow depreciation and amortization", "The cash-flow total is used throughout history. In older notes, DepreciationAndAmortization means depreciation and other amortization excluding separately shown intangible amortization; it is not an alternative cash-flow total",
                tag=rows[0]["concept"])
    if scope.get("da") == "ti_cashflow" and cik == "97476":
        # Reviewed cash-flow statements: 2024/2025 list depreciation and
        # software amortization; 2021-2023 also explicitly report acquisition
        # amortization (including real zero facts for 2022/2023).
        tags = ["Depreciation", "CapitalizedComputerSoftwareAmortization1"]
        if pe.year <= 2023:
            tags.append("AmortizationOfIntangibleAssets")
        rows = _common(facts, tags, pe, latest, accession, bound, False)
        if rows:
            output["depreciation_amortization"] = _selection(sum((r["value"] for r in rows), Decimal(0)), rows,
                "TXN cash-flow depreciation plus software amortization and separately reported acquisition-related intangible amortization",
                "Reviewed annual cash-flow components are non-overlapping. Acquisition amortization is included in the 2021-2023 statements; missing software or required acquisition operands cannot be assumed zero")
    return output


def filing_items(facts, cik, pe, accession, latest):
    return {k: s.item for k, s in selections(facts, cik, pe, accession, latest).items()}


def repair_company(company: Company, facts: dict) -> Company:
    """All required history must be available before any amount changes."""
    anchor = company.periods[0]
    fields = configured_fields(company.cik, anchor.filing_accession)
    if not fields:
        raise ValueError("No reviewed coverage repair for this filing")
    selected = [filing_items(facts, company.cik, p.fiscal_period_end, anchor.filing_accession, anchor.fiscal_period_end) for p in company.periods]
    if any(not fields <= items.keys() for items in selected):
        raise ValueError("Complete matching annual debt/D&A evidence is required for every saved period")
    periods = []
    for p, items in zip(company.periods, selected):
        changes = {}
        for name in ["balance_sheet", "income_statement", "cash_flow_statement"]:
            stmt = getattr(p, name)
            targets = DEBT_FIELDS if name == "balance_sheet" else fields - DEBT_FIELDS
            # A reviewer-held debt pair is indivisible. Qualification still
            # compares its amounts against the independent filed contract.
            if name == "balance_sheet" and any(getattr(stmt, f) and getattr(stmt, f).source == ExtractionSource.USER_OVERRIDE for f in DEBT_FIELDS):
                continue
            patch = {f: items[f] for f in targets if f in type(stmt).model_fields and (getattr(stmt, f) is None or getattr(stmt, f).source != ExtractionSource.USER_OVERRIDE)}
            changes[name] = stmt.model_copy(update=patch)
        periods.append(p.model_copy(update=changes))
    note = POLICIES[company.cik.lstrip("0") + ":" + anchor.filing_accession]["disclosure"]
    if company.valuation_note:
        note = company.valuation_note if note in company.valuation_note else company.valuation_note + " " + note
    return company.model_copy(update={"periods": periods, "valuation_note": note, "qualification": None})
