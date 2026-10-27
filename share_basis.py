"""Reconcile annual diluted-share history to one filing's split basis.

Year-to-year share growth is never evidence of a stock split. A conversion needs
the same annual period in two filings AND a matching SEC split-ratio disclosure.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
import re

PATH = "income_statement.diluted_shares_outstanding"
SPLIT = "StockholdersEquityNoteStockSplitConversionRatio1"
DISCLOSURE = (
    "Historical diluted shares are reconciled to the latest filing's split basis. "
    "Older amounts are adjusted only when overlapping annual share counts and an "
    "explicit SEC split ratio agree; the latest valuation denominator is unchanged."
)


def _events(facts, older, newer):
    """Corporate-action metadata can use a short/instant, non-annual context."""
    rows = facts.get("facts", {}).get("us-gaap", {}).get(SPLIT, {}).get("units", {}).get("pure", [])
    found = {}
    for row in rows:
        try:
            end, filed = date.fromisoformat(row["end"]), date.fromisoformat(row["filed"])
            start = date.fromisoformat(row["start"]) if row.get("start") else None
        except (KeyError, ValueError, TypeError):
            continue
        if (
            row.get("form") not in {"10-K", "10-Q", "10-K/A", "10-Q/A"}
            or row.get("fp") not in {"FY", "Q1", "Q2", "Q3", "Q4"}
            or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", row.get("accn", ""))
            or not older < end <= filed <= newer
            or (start is not None and start > end)
        ):
            continue
        if row["form"].endswith("/A"):
            return {}  # Reconcile an amended corporate-action disclosure first.
        try:
            value = Decimal(str(row["val"]))
        except (KeyError, ValueError, TypeError, InvalidOperation):
            return {}
        if not value.is_finite() or not Decimal("0.000001") <= value <= Decimal("1000000"):
            return {}
        found.setdefault(end, {})[value] = row
    # Repeated disclosures may describe announcement and execution dates. Do
    # not multiply them or infer multiple splits. One exact ratio must bridge
    # all overlaps; conflicting same-event ratios cannot authorize a conversion.
    if any(len(values) != 1 for values in found.values()):
        return {}
    return {value: {**row, "concept": "us-gaap:" + SPLIT, "value": value,
                    "unit": "pure", "start": row.get("start"), "dimensions": {},
                    "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK" + str(facts["cik"]).zfill(10) + ".json",
                    "role": "stock-split disclosure", "usage": "split conversion ratio"}
            for values in found.values() for value, row in values.items()}


def _tolerance(source):
    # Respect filed share-count precision, not an arbitrary percentage tolerance.
    row = source.rows[0]
    try:
        d = row.get("decimals", "INF")
        return Decimal(0) if d == "INF" else Decimal(10) ** -int(d) / 2
    except (ValueError, TypeError, InvalidOperation):
        return Decimal(0)


def reconcile_share_history(inputs, sources, facts, history_sources=None):
    """Mutate only share inputs; source order is newest filing first.

    Missing/ambiguous bridges fail closed, including unsupported cumulative
    split conversions. No issuer names or guessed share-growth thresholds.
    """
    from reusable_inputs import selection

    canonical = {}
    newest = sources[0][0]
    adjusted = False
    factors = {}
    proofs = {}
    dividend_sources = {
        item.rows[0]["accn"] for periods in inputs.values()
        for path, item in periods.items()
        if path == "cash_flow_statement.common_dividends_per_share"
    }
    for filing, shares in sources:
        missing = shares.keys() - canonical.keys()
        if not missing and filing["accession"] not in dividend_sources:
            continue
        if not canonical:
            canonical.update(shares)
            factors[filing["accession"]] = Decimal(1)
            continue
        overlap = sorted(shares.keys() & canonical.keys())
        if not overlap:
            raise ValueError("Annual share history has no overlapping filing period to establish a comparable basis")
        if any(shares[pe].rows[0].get("start") != canonical[pe].rows[0].get("start") for pe in overlap):
            raise ValueError("Overlapping annual share contexts disagree; share basis requires review")
        events = _events(facts, filing["filed"], newest["filed"])
        candidates = []
        for factor in {Decimal(1), *events}:
            if all(abs(shares[pe].item.value * factor - canonical[pe].item.value)
                   <= _tolerance(shares[pe]) * factor + _tolerance(canonical[pe])
                   for pe in overlap):
                candidates.append(factor)
        if len(candidates) != 1:
            raise ValueError("Historical share bases conflict: overlapping annual counts and one explicit SEC split ratio must reconcile; no adjustment was guessed")
        factor = candidates[0]
        factors[filing["accession"]] = factor
        proofs[filing["accession"]] = [
            {**r, "usage": "same-period share-basis comparison"}
            for pe in overlap for r in [*shares[pe].rows, *canonical[pe].rows]
        ] + ([events[factor]] if factor != 1 else [])
        for pe in sorted(missing, reverse=True):
            source = shares[pe]
            if factor == 1:
                canonical[pe] = source
                continue
            proof = [*source.rows]
            for bridge in overlap:
                proof.extend({**r, "usage": "same-period share-basis comparison"}
                             for r in [*shares[bridge].rows, *canonical[bridge].rows])
            proof.append(events[factor])
            canonical[pe] = selection(
                source.item.value * factor, proof,
                "Annual diluted shares on the latest filing's split basis",
                f"Original annual shares {source.item.value} multiplied by source-backed split factor {factor}; "
                "all overlapping annual contexts reconcile to the newer filing basis. "
                "A change between different fiscal years is not a split signal",
                disclosure=DISCLOSURE,
            )
            adjusted = True
    for pe, source in canonical.items():
        inputs[pe][PATH] = source
    if history_sources:
        _reconcile_dividends(inputs, history_sources, factors, proofs)
    return adjusted


def _reconcile_dividends(inputs, sources, factors, proofs):
    """Paid-common DPS has the inverse split conversion, never a cash total.

    Require a second same-period bridge for the per-share disclosure itself;
    do not assume that its filing's annual shares prove its own presentation.
    """
    from reusable_inputs import selection

    path = "cash_flow_statement.common_dividends_per_share"
    canonical = {}
    for filing, periods in sources:
        values = {pe: items[path] for pe, items in periods.items() if path in items}
        missing = values.keys() - canonical.keys()
        if not missing:
            continue
        factor = factors.get(filing["accession"])
        if factor is None:
            raise ValueError("Paid-common dividend history has no reconciled share basis; review is required")
        if factor != 1:
            overlap = sorted(values.keys() & canonical.keys())
            if not overlap or any(
                values[pe].rows[0].get("start") != canonical[pe].rows[0].get("start")
                or abs(values[pe].item.value / factor - canonical[pe].item.value)
                > _tolerance(values[pe]) / factor + _tolerance(canonical[pe])
                for pe in overlap
            ):
                raise ValueError("Paid-common dividend history does not reconcile to the split-adjusted same-period per-share disclosures; no conversion was guessed")
        for pe in missing:
            source = values[pe]
            if factor == 1:
                canonical[pe] = source
                continue
            bridge = [{**r, "usage": "same-period paid-common DPS comparison"}
                      for end in overlap for r in [*values[end].rows, *canonical[end].rows]]
            canonical[pe] = selection(
                source.item.value / factor,
                [*source.rows, *bridge, *proofs[filing["accession"]]],
                "Annual paid-common dividends per share on the latest split basis",
                f"Original annual paid-common DPS {source.item.value} divided by verified split factor {factor}; both annual-share and paid-common-DPS overlaps reconcile",
                disclosure="Historical paid-common DPS uses the same verified split basis as annual shares. Per-share dividends adjust inversely; aggregate cash payments and the latest model input are unchanged.",
            )
    for pe, source in canonical.items():
        inputs[pe][path] = source
