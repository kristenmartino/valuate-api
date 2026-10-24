"""Berkshire's dimensioned inline XBRL facts, omitted by company-facts.

Read only the requested annual/class/segment contexts from the actual filing.
Never substitute Class A, actual Class B-only shares, parent cash, or a
quarterly number. Missing/conflicting facts remain missing for composition.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

from schemas import ExtractionSource, LineItem
from tickers import normalize_ticker


BERKSHIRE_FIELDS = {"diluted_shares_outstanding", "premiums_earned", "cash_and_equivalents"}
_SEGMENT_AXIS = "srt:ProductOrServiceAxis"
_INSURANCE = "brka:InsuranceAndOtherMember"
_RAIL_ENERGY = "brka:RailroadUtilitiesAndEnergyMember"


def extract_berkshire_filing(
    html: str, ticker: str, period_ends: list[date]
) -> dict[date, dict[str, LineItem]]:
    ticker = normalize_ticker(ticker)
    if ticker not in {"BRK-A", "BRK-B"}:
        return {}
    soup = BeautifulSoup(html, "xml")
    contexts = {c.get("id"): c for c in soup.find_all("context")}
    units = {u.get("id"): u.get_text(strip=True) for u in soup.find_all("unit")}
    facts = soup.find_all("nonFraction")

    def read(concept: str, pe: date, dimensions: dict[str, str], unit: str, annual: bool):
        values: set[Decimal] = set()
        for fact in facts:
            if fact.get("name") != concept or fact.get("continuedAt") or fact.get("xsi:nil") == "true":
                continue
            context = contexts.get(fact.get("contextRef"))
            if context is None:
                continue
            identifier = context.find("identifier")
            if identifier is None or identifier.get_text(strip=True).lstrip("0") != "1067983":
                continue
            members = context.find_all("explicitMember")
            if context.find("typedMember") or len(members) != len(dimensions):
                continue
            if {m.get("dimension"): m.get_text(strip=True) for m in members} != dimensions:
                continue
            if annual:
                start, end = context.find("startDate"), context.find("endDate")
                if (start is None or end is None or pe.month != 12 or pe.day != 31
                        or start.get_text(strip=True) != f"{pe.year}-01-01"
                        or end.get_text(strip=True) != pe.isoformat()):
                    continue
            else:
                instant = context.find("instant")
                if instant is None or instant.get_text(strip=True) != pe.isoformat():
                    continue
            if units.get(fact.get("unitRef")) != unit:
                continue
            if fact.get("format") not in {None, "ixt:num-dot-decimal", "ixt:numdotdecimal"}:
                continue
            try:
                value = Decimal(fact.get_text(strip=True).replace(",", ""))
                value *= Decimal(10) ** int(fact.get("scale", "0"))
                if fact.get("sign") == "-":
                    value = -value
                if value.is_finite() and value >= 0:
                    values.add(value)
            except (InvalidOperation, ValueError):
                continue
        # Repeated presentation of the same context is common. A conflict
        # must not silently choose whichever number appeared first.
        return next(iter(values)) if len(values) == 1 else None

    share_class = ticker[-1]
    output: dict[date, dict[str, LineItem]] = {}
    for pe in period_ends:
        items: dict[str, LineItem] = {}
        share_tag = "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic"
        share_member = f"brka:EquivalentClass{share_class}Member"
        shares = read(share_tag, pe, {"us-gaap:StatementClassOfStockAxis": share_member}, "xbrli:shares", True)
        if shares is not None and shares > 0:
            items["diluted_shares_outstanding"] = LineItem(
                value=shares, source=ExtractionSource.XBRL, confidence=0.95,
                xbrl_tag=share_tag,
                source_quote=f"{pe.year} annual average equivalent Class {share_class} shares: {shares}. "
                             f"Inline XBRL member {share_member}; reported equivalent basic shares, not generic diluted shares.",
            )
        premium_tag = "us-gaap:PremiumsEarnedNet"
        premiums = read(premium_tag, pe, {_SEGMENT_AXIS: _INSURANCE}, "iso4217:USD", True)
        if premiums is not None:
            items["premiums_earned"] = LineItem(
                value=premiums, source=ExtractionSource.XBRL, confidence=0.95,
                xbrl_tag=premium_tag,
                source_quote=f"{pe.year} insurance premiums earned: {premiums} USD. Inline XBRL member {_INSURANCE}.",
            )
        cash_tag = "us-gaap:CashAndCashEquivalentsAtCarryingValue"
        insurance_cash = read(cash_tag, pe, {_SEGMENT_AXIS: _INSURANCE}, "iso4217:USD", False)
        rail_cash = read(cash_tag, pe, {_SEGMENT_AXIS: _RAIL_ENERGY}, "iso4217:USD", False)
        if insurance_cash is not None and rail_cash is not None:
            items["cash_and_equivalents"] = LineItem(
                value=insurance_cash + rail_cash, source=ExtractionSource.DERIVED, confidence=0.95,
                source_quote=f"Derived consolidated cash at {pe}: insurance and other ({insurance_cash}) + "
                             f"railroad, utilities and energy ({rail_cash}) USD. Both are {cash_tag} "
                             f"with members {_INSURANCE} and {_RAIL_ENERGY}. Excludes restricted cash and Treasury bills.",
            )
        output[pe] = items
    return output


def berkshire_model_context(ticker: str) -> tuple[str, str]:
    share_class = normalize_ticker(ticker)[-1]
    label = f"Average equivalent Class {share_class} shares"
    note = (
        "Simplified Berkshire P/B estimate using consolidated GAAP earnings and Berkshire shareholders' equity, "
        "not a sum-of-the-parts valuation. Investment gains can distort ROE. Per-share amounts use reported "
        f"annual average equivalent Class {share_class} shares rather than year-end shares."
    )
    return label, note
