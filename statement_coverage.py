"""Declarative statement contracts and bounded history recovery.

Recovery improves candidates; qualification still requires a reviewed filing
policy. A broader cash taxonomy tag never becomes a generic cash fallback.
"""

from datetime import date
from decimal import Decimal
import json
import re
from bs4 import BeautifulSoup
from pathlib import Path

from filing_coverage import Selection, _selection, filing_items as debt_items
from input_contracts import CONTRACTS, anchor_date, annual_fact, contract_fact
from schemas import Company, ExtractionSource, Industry, LineItem
from statement_evidence import read_statement_facts, select_statement_fact

POLICIES = json.loads((Path(__file__).parent / "qualification_policies.json").read_text())
DEFAULT_CASH = {"role": "balance_sheet", "concepts": ["us-gaap:CashAndCashEquivalentsAtCarryingValue", "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
                "labels": ["Cash and cash equivalents"], "instant": True,
                "scope": "Balance-sheet cash and cash equivalents, selected from the explicitly labeled consolidated row",
                "basis": "Filed statement label and context; a broad taxonomy name alone does not establish available cash",
                "fallback_concepts": ["CashAndCashEquivalentsAtCarryingValue"]}


def policy(cik, accession):
    return POLICIES.get(cik.lstrip("0") + ":" + accession, {})


def specs(cik, accession):
    return policy(cik, accession).get("statement_inputs", {})


def protected_fields(cik, accession):
    return {p.split(".")[1] for p in specs(cik, accession)}


async def load_statement_coverage(client, facts, cik, latest, accession, period_ends, *, anchor_html=None, candidate_cash_only=False):
    selected_specs = {"balance_sheet.cash_and_equivalents": DEFAULT_CASH} if candidate_cash_only else specs(cik, accession)
    if not selected_specs or str(facts.get("cik", "")).lstrip("0") != cik.lstrip("0"):
        return {"inputs": {}, "documents": {}, "missing": []}
    bound = anchor_date(facts, accession)
    if bound is None:
        raise ValueError("Annual filing date is unavailable")
    results = {pe: {} for pe in period_ends}
    # A standard unrestricted-cash fact may establish older coverage without
    # another HTML download. The broader cash concept has no such fallback.
    for pe in period_ends:
        for path, spec in selected_specs.items():
            for concept in spec.get("fallback_concepts", []):
                row = annual_fact(facts, concept, pe, latest, accession, bound, instant=spec["instant"])
                if row and row["value"] >= 0:
                    results[pe][path] = _selection(row["value"], [row], spec["scope"], "Standard unrestricted-cash concept; matching annual context", tag=row["concept"])
    submissions = await client.get_submissions(cik)
    recent = submissions.get("filings", {}).get("recent", {})
    filings = []
    for i, acc in enumerate(recent.get("accessionNumber", [])):
        try:
            filed = date.fromisoformat(recent["filingDate"][i]);pe = date.fromisoformat(recent["reportDate"][i])
        except (KeyError, ValueError, IndexError):
            continue
        if recent["form"][i] not in {"10-K", "10-K/A"} or filed > bound or filed < pe or pe > latest:
            continue
        if pe == latest and acc != accession:
            continue
        filings.append({"accn": acc, "end": pe, "filed": filed,
                        "url": f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace("-", "")}/{recent["primaryDocument"][i]}'})
    anchor = next((f for f in filings if f["accn"] == accession and f["end"] == latest), None)
    if anchor is None:
        raise ValueError("Saved annual filing does not match source metadata")
    documents = {};used = set();current = anchor
    zero_borrowings = {}
    # At most three annual documents for a five-year window. Missing coverage
    # remains explicit; this function never calls AI or writes a repository.
    for _ in range(3):
        html = anchor_html if current["accn"] == accession and anchor_html is not None else await client.get_filing_html(current["url"])
        documents[current["url"]] = html;used.add(current["accn"])
        rows = read_statement_facts(html, cik, current["end"])
        zero_quotes = policy(cik, accession).get("reviewed_input_scope", {}).get("additional_current_borrowings", {}).get("zero_disclosures", {}).get(str(current["end"]), [])
        if current["accn"] == accession and rows and zero_quotes:
            text = re.sub(r"\s+", " ", BeautifulSoup(html, "xml").get_text(" ", strip=True))
            if all(quote in text for quote in zero_quotes):
                zero_borrowings[latest] = {"concept": "reviewed-filing:NoOutstandingShortTermBorrowings", "value": Decimal(0),
                    "end": str(latest), "start": "", "accn": accession, "unit": "USD", "source_url": current["url"],
                    "basis": " ".join(zero_quotes)}
        for pe in period_ends:
            if pe == latest and current["accn"] != accession:
                continue
            for path, spec in selected_specs.items():
                if path in results[pe]:
                    continue
                row = select_statement_fact(rows, spec, pe)
                if row is None or not row["concept"].startswith("us-gaap:"):
                    continue
                fact = annual_fact(facts, row["concept"].split(":")[1], pe, pe, current["accn"], bound, instant=spec["instant"])
                if fact is None or fact["value"] != row["value"] or fact.get("start") != row.get("start"):
                    continue
                proof = {**fact, "role": row["role"], "row_label": row["label"], "source_url": current["url"]}
                results[pe][path] = _selection(row["value"], [proof], spec["scope"], spec["basis"] + f'; {row["role"]} row "{row["label"]}"', tag=row["concept"])
        missing = [(pe, path) for pe in period_ends for path in selected_specs if path not in results[pe]]
        if not missing:
            break
        newest_missing = max(pe for pe, _ in missing)
        candidates = [f for f in filings if f["accn"] not in used and f["end"] == newest_missing and f["filed"] <= bound]
        if not candidates:
            break
        current = max(candidates, key=lambda f: (f["filed"], f["accn"]))
    missing = [{"period_end": str(pe), "field_path": path} for pe in period_ends for path in selected_specs if path not in results[pe]]
    for pe in period_ends:
        if str(pe) in policy(cik, accession).get("reviewed_input_scope", {}).get("additional_current_borrowings", {}).get("zero_disclosures", {}) and pe not in zero_borrowings:
            missing.append({"period_end": str(pe), "field_path": "balance_sheet.short_term_debt"})
    return {"inputs": results, "documents": documents, "missing": missing, "zero_current_borrowings": zero_borrowings}


def apply_items(items, selected):
    """Graph fields are flat; preserve the different income/cash-flow D&A."""
    for path, selection in selected.items():
        stmt, field = path.split(".")
        key = "income_depreciation_amortization" if stmt == "income_statement" and field == "depreciation_amortization" else field
        items[key] = selection.item
    return items


def fill_history_context(items, facts, cik, pe, accession, latest):
    p = policy(cik, accession)
    bound = anchor_date(facts, accession)
    for path in p.get("required_history_context", []):
        field = path.split(".")[1]
        row = contract_fact(facts, CONTRACTS[field], pe, latest, accession, bound) if bound else None
        if row:
            items[field] = LineItem(value=row["value"], source=ExtractionSource.XBRL, confidence=1, xbrl_tag=row["concept"],
                source_quote=f"Filed {field}: {row['value']} USD; annual period {row.get('start')} to {row['end']}; SEC accession {row['accn']}.")
    return items


def repair_statement_history(company: Company, facts, loaded):
    from graph import _build_financial_period, _derive_missing_required, _missing_required_for_period, concepts_for
    from extract_track_a import extract_track_a
    p = policy(company.cik, company.periods[0].filing_accession)
    expected = [date.fromisoformat(s) for s in p.get("period_ends", [])]
    if not expected or not p.get("statement_inputs") or loaded["missing"]:
        raise ValueError("Complete statement-role evidence is required before repairing history")
    if not {x.fiscal_period_end for x in company.periods} <= set(expected):
        raise ValueError("Saved periods fall outside the reviewed history; no data was dropped")
    anchor = company.periods[0];old = {x.fiscal_period_end:x for x in company.periods};periods=[]
    for pe in expected:
        items = extract_track_a(pe, facts, concepts=concepts_for(anchor.industry), latest_end=anchor.fiscal_period_end, accession=anchor.filing_accession)
        debt = debt_items(facts, company.cik, pe, anchor.filing_accession, anchor.fiscal_period_end, zero_current_borrowings=loaded.get("zero_current_borrowings"))
        if not {"long_term_debt", "short_term_debt"} <= debt.keys():
            raise ValueError("Complete same-filing carrying debt is required across the reviewed history")
        items.update(debt);apply_items(items, loaded["inputs"][pe]);fill_history_context(items, facts, company.cik, pe, anchor.filing_accession, anchor.fiscal_period_end)
        items = _derive_missing_required(items, anchor.industry)
        if (_missing_required_for_period(items, anchor.industry)
                or any(items.get(path.split(".")[1]) is None for path in p.get("required_history_context", []) + p.get("required_history_inputs", []))):
            raise ValueError(f"Required historical fields are missing for {pe}")
        built = _build_financial_period(pe, anchor.filing_accession, items, industry=anchor.industry)
        if pe in old:
            existing = old[pe];changes={}
            paths = set(p["statement_inputs"]) | set(p.get("required_history_context", [])) | {"balance_sheet.short_term_debt", "balance_sheet.long_term_debt"}
            for stmt in ["income_statement", "balance_sheet", "cash_flow_statement"]:
                current = getattr(existing,stmt);fresh=getattr(built,stmt);patch={}
                locked_debt = stmt=="balance_sheet" and any(getattr(current,k) and getattr(current,k).source==ExtractionSource.USER_OVERRIDE for k in ["short_term_debt","long_term_debt"])
                for path in paths:
                    name,field=path.split(".")
                    if name!=stmt or (locked_debt and field in {"short_term_debt","long_term_debt"}):continue
                    before=getattr(current,field)
                    if before is None or before.source!=ExtractionSource.USER_OVERRIDE:
                        # Retain correct, already-saved context fields and notes.
                        if path in p.get("required_history_context", []) and before is not None and before.value==getattr(fresh,field).value:continue
                        patch[field]=getattr(fresh,field)
                changes[stmt]=current.model_copy(update=patch)
            built=existing.model_copy(update=changes)
        periods.append(built)
    note=p["disclosure"]
    if company.valuation_note:note=company.valuation_note if note in company.valuation_note else company.valuation_note+" "+note
    return company.model_copy(update={"periods":periods,"valuation_note":note,"qualification":None})
