"""Statement-role-aware inline XBRL evidence, independent of ticker identity.

A taxonomy name alone cannot distinguish a cash-flow total from an income
expense line, or prove that a broad cash tag represents available cash.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

from bs4 import BeautifulSoup


def normalized(text):
    return re.sub(r"[^a-z]", "", text.lower())


def statement_role(table):
    text = normalized(table.get_text(" ", strip=True))
    if all(s in text for s in ["totalassets", "totalcurrentliabilities", "cashandcashequivalents"]):
        return "balance_sheet"
    if all(s in text for s in ["operatingactivities", "investingactivities", "financingactivities", "depreciation"]):
        return "cash_flow_statement"
    if all(s in text for s in ["operatingincome", "interestexpense", "incometax", "depreciation"]):
        return "income_statement"
    return None


def read_statement_facts(html, cik, document_end):
    """Return validated consolidated USD facts with row label and role.

    Dimensioned, quarterly, hidden, nil, continued and unsupported numeric
    contexts are excluded. The caller must cross-check the original annual
    accession and company-facts amount before using this evidence.
    """
    soup = BeautifulSoup(html, "xml")
    metadata = {}
    for key in ["dei:DocumentType", "dei:EntityCentralIndexKey", "dei:DocumentPeriodEndDate"]:
        values = {f.get_text(strip=True) for f in soup.find_all("nonNumeric") if f.get("name") == key}
        if len(values) != 1:
            return []
        metadata[key] = values.pop()
    end = metadata.get("dei:DocumentPeriodEndDate", "")
    try:
        end = date.fromisoformat(end)
    except ValueError:
        try:
            end = datetime.strptime(end, "%B %d, %Y").date()
        except ValueError:
            return []
    if (metadata.get("dei:DocumentType") not in {"10-K", "10-K/A"}
            or metadata.get("dei:EntityCentralIndexKey", "").lstrip("0") != cik.lstrip("0")
            or end != document_end):
        return []
    contexts = {c.get("id"): c for c in soup.find_all("context")}
    units = {u.get("id"): u.get_text(strip=True) for u in soup.find_all("unit")}
    output = []
    for table in soup.find_all("table"):
        role = statement_role(table)
        if role is None:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if not cells:
                continue
            label = cells[0].get_text(" ", strip=True)
            for fact in row.find_all("nonFraction"):
                if fact.find_parent("table") is not table:
                    continue
                if (fact.get("continuedAt") or fact.get("xsi:nil") == "true"
                        or fact.find_parent("hidden") or units.get(fact.get("unitRef")) != "iso4217:USD"
                        or fact.get("format") not in {None, "ixt:num-dot-decimal", "ixt:numdotdecimal"}):
                    continue
                context = contexts.get(fact.get("contextRef"))
                if context is None or context.find("explicitMember") or context.find("typedMember"):
                    continue
                identifier = context.find("identifier")
                if identifier is None or identifier.get_text(strip=True).lstrip("0") != cik.lstrip("0"):
                    continue
                instant, start, end = context.find("instant"), context.find("startDate"), context.find("endDate")
                if instant is not None and (start is not None or end is not None):
                    continue
                try:
                    pe = date.fromisoformat((instant if instant is not None else end).get_text(strip=True))
                    start_date = date.fromisoformat(start.get_text(strip=True)) if start is not None else None
                    if pe > document_end or (instant is None and (start_date is None or not 330 <= (pe - start_date).days <= 380)):
                        continue
                    value = Decimal(fact.get_text(strip=True).replace(",", "")) * Decimal(10) ** int(fact.get("scale", "0"))
                    if fact.get("sign") == "-":
                        value = -value
                    if not value.is_finite() or value < 0:
                        continue
                    if value == value.to_integral_value():
                        value = value.to_integral_value()
                except (AttributeError, ValueError, InvalidOperation):
                    continue
                output.append({"role": role, "label": label, "concept": fact.get("name"), "value": value,
                               "end": str(pe), "start": str(start_date) if start_date else None,
                               "instant": instant is not None, "unit": "USD", "dimensions": {}})
    return output


def select_statement_fact(rows, spec, pe):
    labels = {normalized(s) for s in spec["labels"]}
    matches = [r for r in rows if r["role"] == spec["role"] and r["end"] == str(pe)
               and r["concept"] in spec["concepts"] and normalized(r["label"]) in labels
               and r["instant"] == spec["instant"]]
    # Duplicate presentation of the same fact is fine; different amounts or
    # annual starts require reconciliation rather than a first-match guess.
    if not matches or len({(r["value"], r["start"]) for r in matches}) != 1:
        return None
    return matches[0]
