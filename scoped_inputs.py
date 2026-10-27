"""Reviewed filing algebra over independently retrieved inline XBRL evidence.

A contract names exact source concepts, contexts and statement rows. Arithmetic
and reconciliations are explicit; missing operands never become zero or AI input.
"""
from datetime import date
from decimal import Decimal

from filing_coverage import Selection, _selection
from input_contracts import anchor_date, annual_fact
from statement_coverage import policy
from statement_evidence import read_statement_facts, normalized


def contract(cik, accession):
    return policy(cik, accession).get("scoped_source", {})


def paths(cik, accession):
    return set(contract(cik, accession).get("inputs", {}))


def fields(cik, accession):
    return {p.split(".")[1] for p in paths(cik, accession)}


def apply_inputs(items, selections):
    for path, selection in selections.items():
        stmt, field = path.split(".")
        items[field] = selection.item
    # These filings do not present the cash-flow aggregate as an income row.
    items["income_depreciation_amortization"] = None


def select_operand(rows, spec, pe):
    selected = [r for r in rows if r["concept"] == spec["concept"] and r["end"] == str(pe)
                and r["instant"] == spec.get("instant", True)
                and r["dimensions"] == spec.get("dimensions", {})
                and (not spec.get("role") or r["role"] == spec["role"])
                and (not spec.get("labels") or normalized(r["label"]) in {normalized(s) for s in spec["labels"]})]
    if not selected or len({(r["value"], r["start"]) for r in selected}) != 1:
        return None
    return selected[0]


def calculate(formula, selected):
    return sum((selected[key]["value"] * Decimal(str(coefficient)) for key, coefficient in formula.items()), Decimal(0))


async def load_scoped_inputs(client, facts, cik, latest, accession, period_ends, *, anchor_html=None):
    cfg = contract(cik, accession)
    if not cfg or str(facts.get("cik", "")).lstrip("0") != cik.lstrip("0"):
        raise ValueError("Matching reviewed filing contract and entity are required")
    bound = anchor_date(facts, accession)
    if bound is None:
        raise ValueError("Annual source filing date is unavailable")
    recent = (await client.get_submissions(cik))["filings"]["recent"]
    results = {pe: {} for pe in period_ends}
    documents = {}
    for acc in cfg["documents"]:
        matches = [i for i, candidate in enumerate(recent["accessionNumber"]) if candidate == acc]
        if len(matches) != 1:
            raise ValueError("Reviewed source document is unavailable in annual metadata")
        i = matches[0]
        doc_end = date.fromisoformat(recent["reportDate"][i])
        filed = date.fromisoformat(recent["filingDate"][i])
        if recent["form"][i] not in {"10-K", "10-K/A"} or not doc_end <= filed <= bound or doc_end > latest:
            raise ValueError("Reviewed source document has unsafe annual metadata")
        if doc_end == latest and acc != accession:
            raise ValueError("Latest source accession does not match the reviewed anchor")
        url = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace("-", "")}/{recent["primaryDocument"][i]}'
        html = anchor_html if acc == accession and anchor_html is not None else await client.get_filing_html(url)
        documents[url] = html
        rows = read_statement_facts(html, cik, doc_end, include_notes=True, allow_dimensions=True)
        for pe in period_ends:
            if pe == latest and acc != accession:
                continue
            selected = {}
            for key, spec in cfg["facts"].items():
                row = select_operand(rows, spec, pe)
                if row is None:
                    continue
                if spec.get("company_facts", True):
                    namespace, concept = spec["concept"].split(":", 1)
                    corroboration = annual_fact(facts, concept, pe, pe, acc, bound,
                                               instant=spec.get("instant", True), namespace=namespace)
                    if (corroboration is None or corroboration["value"] != row["value"]
                            or corroboration.get("start") != row.get("start")):
                        continue
                selected[key] = {**row, "accn": acc, "source_url": url, "filed": str(filed)}
            for path, spec in cfg["inputs"].items():
                if path in results[pe]:
                    continue
                checks = [cfg["checks"][name] for name in spec.get("checks", [])]
                keys = set(spec["formula"])
                for check in checks:
                    keys.update(check["lhs"]);keys.update(check["rhs"])
                if not keys <= selected.keys() or any(calculate(c["lhs"], selected) != calculate(c["rhs"], selected) for c in checks):
                    continue
                value = calculate(spec["formula"], selected)
                if not value.is_finite() or value < 0:
                    continue
                operands = [selected[key] for key in sorted(keys)]
                direct = next(iter(spec["formula"])) if len(spec["formula"]) == 1 and next(iter(spec["formula"].values())) == 1 else None
                results[pe][path] = _selection(value, operands, spec["scope"], spec["basis"], tag=selected[direct]["concept"] if direct else None)
    missing = [{"period_end": str(pe), "field_path": path} for pe in period_ends for path, spec in cfg["inputs"].items()
               if (spec.get("required", "history") == "history" or pe == latest) and path not in results[pe]]
    return {"inputs": results, "documents": documents, "missing": missing}
