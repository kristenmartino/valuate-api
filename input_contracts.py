"""Annual, filing-bounded fact selection with explicit financial coverage.

This selector is intentionally stricter than the legacy first-tag fallback.
It returns ambiguity as missing evidence rather than selecting a convenient value.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any


@dataclass(frozen=True)
class Contract:
    concepts: tuple[str, ...]
    scope: str
    instant: bool = False
    unit: str = "USD"
    basis: str = "reported GAAP amount"


CONTRACTS = {
    "revenue": Contract(
        (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "OilAndGasRevenue",
        ),
        "consolidated revenue; selected concept retained",
    ),
    "operating_income": Contract(("OperatingIncomeLoss",), "GAAP operating income"),
    "net_income": Contract(
        (
            "NetIncomeLoss",
            "ProfitLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
        ),
        "reported GAAP earnings; attributable coverage follows concept",
    ),
    "income_before_tax": Contract(
        (
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign",
        ),
        "consolidated pre-tax continuing income",
    ),
    "income_tax_expense": Contract(
        ("IncomeTaxExpenseBenefit",), "reported income-tax expense or benefit"
    ),
    "interest_expense": Contract(
        (
            "InterestExpense",
            "InterestAndDebtExpense",
            "InterestExpenseNonoperating",
            "InterestExpenseDebt",
        ),
        "reported interest expense",
    ),
    "depreciation_amortization": Contract(
        ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization"),
        "cash-flow depreciation and amortization; depletion if reported",
    ),
    "capital_expenditures": Contract(
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "PaymentsToAcquireOilAndGasPropertyAndEquipment",
            "PaymentsToAcquireRealEstate",
            "PaymentsForCapitalImprovements",
        ),
        "reported cash investment component; not total reinvestment",
    ),
    "cash_and_equivalents": Contract(
        ("CashAndCashEquivalentsAtCarryingValue",),
        "unrestricted cash and cash equivalents",
        True,
    ),
    "shareholders_equity": Contract(
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
        "equity; attributable coverage follows concept",
        True,
    ),
    "total_assets": Contract(("Assets",), "consolidated assets", True),
    "total_liabilities": Contract(("Liabilities",), "consolidated liabilities", True),
    "noncontrolling_interests": Contract(
        ("MinorityInterest",), "noncontrolling interests", True
    ),
    "operating_lease_liabilities": Contract(
        ("OperatingLeaseLiability",),
        "current plus noncurrent operating-lease liabilities",
        True,
        basis="carrying amount",
    ),
    "diluted_shares_outstanding": Contract(
        ("WeightedAverageNumberOfDilutedSharesOutstanding",),
        "annual weighted-average diluted shares",
        unit="shares",
    ),
    "dividends_paid": Contract(
        ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends"),
        "reported cash dividends; common/total scope follows concept",
    ),
}
# Domestic/foreign pre-tax amounts are components, never alternatives for the total.
CONTRACTS["income_before_tax"] = Contract(
    CONTRACTS["income_before_tax"].concepts[:2], CONTRACTS["income_before_tax"].scope
)
ACC = re.compile(r"\d{10}-\d{2}-\d{6}")


def anchor_date(facts: dict, accession: str) -> date | None:
    dates = set()
    for concepts in facts.get("facts", {}).values():
        for data in concepts.values():
            for rows in data.get("units", {}).values():
                for r in rows:
                    if r.get("accn") == accession and r.get("form") in {
                        "10-K",
                        "10-K/A",
                    }:
                        try:
                            dates.add(date.fromisoformat(r["filed"]))
                        except (ValueError, TypeError, KeyError):
                            return None
    return next(iter(dates)) if len(dates) == 1 else None


def annual_fact(
    facts: dict,
    concept: str,
    pe: date,
    latest: date,
    accession: str,
    bound: date,
    *,
    instant=False,
    unit="USD",
    namespace="us-gaap",
) -> dict[str, Any] | None:
    by_acc = {}
    for r in (
        facts.get("facts", {})
        .get(namespace, {})
        .get(concept, {})
        .get("units", {})
        .get(unit, [])
    ):
        try:
            end = date.fromisoformat(r["end"])
            filed = date.fromisoformat(r["filed"])
            start = date.fromisoformat(r["start"]) if r.get("start") else None
        except (KeyError, TypeError, ValueError):
            continue
        if (
            end != pe
            or filed > bound
            or filed < pe
            or r.get("form") not in {"10-K", "10-K/A"}
            or r.get("fp") != "FY"
            or not ACC.fullmatch(r.get("accn", ""))
            or (pe == latest and r["accn"] != accession)
            or (
                start is not None
                if instant
                else start is None or not 330 <= (end - start).days <= 380
            )
        ):
            continue
        by_acc.setdefault(r["accn"], []).append(r)
    if not by_acc:
        return None
    selected = max(by_acc, key=lambda a: (max(r["filed"] for r in by_acc[a]), a))
    rows = by_acc[selected]
    try:
        values = {Decimal(str(r["val"])) for r in rows}
    except (KeyError, InvalidOperation, ValueError):
        return None
    if (
        len(values) != 1
        or not next(iter(values)).is_finite()
        or len({(r.get("start"), r["filed"]) for r in rows}) != 1
    ):
        return None
    return {
        **rows[0],
        "value": next(iter(values)),
        "concept": namespace + ":" + concept,
        "unit": unit,
    }


def contract_fact(facts, contract, pe, latest, accession, bound, preferred_tag=None):
    rows = [
        r
        for concept in contract.concepts
        if (
            r := annual_fact(
                facts,
                concept,
                pe,
                latest,
                accession,
                bound,
                instant=contract.instant,
                unit=contract.unit,
            )
        )
        is not None
    ]
    if preferred_tag:
        rows = [r for r in rows if r["concept"] == preferred_tag]
    # A report cannot quietly choose among contradictory total definitions.
    if not rows or len({r["value"] for r in rows}) != 1:
        return None
    return rows[0]


def carrying_debt_items(facts, cik, pe, latest, accession):
    """Generic non-overlapping carrying pair; absence is never a zero."""
    from schemas import LineItem, ExtractionSource

    if str(facts.get("cik", "")).lstrip("0") != cik.lstrip("0"):
        return {}
    bound = anchor_date(facts, accession)
    if bound is None:
        return {}

    def fact(tag):
        return annual_fact(facts, tag, pe, latest, accession, bound, instant=True)

    noncurrent = fact("LongTermDebtNoncurrent")
    current = fact("DebtCurrent")
    maturities = fact("LongTermDebtCurrent")
    paper = fact("CommercialPaper")
    total = fact("LongTermDebt")
    if current is None and maturities and paper:
        if maturities["accn"] != paper["accn"]:
            return {}
        current = {
            **maturities,
            "value": maturities["value"] + paper["value"],
            "concept": "current maturities + commercial paper",
        }
    if not noncurrent or not current or current["accn"] != noncurrent["accn"]:
        return {}
    if min(noncurrent["value"], current["value"]) < 0:
        return {}
    if (
        total
        and maturities
        and (
            total["accn"] != noncurrent["accn"]
            or maturities["accn"] != noncurrent["accn"]
            or total["value"] != maturities["value"] + noncurrent["value"]
        )
    ):
        return {}
    out = {}
    for field, row in [("long_term_debt", noncurrent), ("short_term_debt", current)]:
        calculated = row["concept"] == "current maturities + commercial paper"
        out[field] = LineItem(
            value=row["value"],
            source=ExtractionSource.DERIVED if calculated else ExtractionSource.XBRL,
            confidence=0.99 if calculated else 1,
            xbrl_tag=None if calculated else row["concept"],
            source_quote=f"Carrying-value debt contract: {row['concept']} = {row['value']} USD; year-end {pe}; SEC accession {row['accn']}. Noncurrent debt excludes current maturities; current debt includes filed current borrowings. Missing components are not assumed zero.",
        )
    return out
