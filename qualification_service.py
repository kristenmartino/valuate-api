"""Collect independent source evidence and stage a checked record."""

import hashlib
import json
from dataclasses import is_dataclass, asdict
from datetime import date
from decimal import Decimal
from statement_coverage import specs as statement_specs, load_statement_coverage
from edgar import EdgarClient
from scoped_inputs import paths as scoped_paths, load_scoped_inputs
from schemas import Company
from qualification import qualify, POLICIES
from cisco_da import filing_da_history
from berkshire import extract_berkshire_filing
from crm_revenue import fetch_saved_filing


async def qualify_record(company: Company, client=None, facts=None):
    base = client or EdgarClient()
    documents = {}

    class EvidenceClient:
        def __getattr__(self, name):
            return getattr(base, name)

        async def get_filing_html(self, url):
            html = await base.get_filing_html(url)
            documents[url] = html
            return html

    client = EvidenceClient()
    support = {}
    latest = company.periods[0]
    if facts is None:
        facts = await client.get_company_facts(company.cik)
    metadata = await client.get_latest_10k(company.cik)
    if company.cik.lstrip("0") == "858877":
        support["cisco_da"] = await filing_da_history(
            client,
            company.cik,
            latest.fiscal_period_end,
            latest.filing_accession,
            [p.fiscal_period_end for p in company.periods],
        )
    if company.cik.lstrip("0") == "1067983":
        html = await fetch_saved_filing(client, company)
        support["inline_accession"] = latest.filing_accession
        support["berkshire"] = extract_berkshire_filing(
            html, company.ticker, [p.fiscal_period_end for p in company.periods]
        )
    if statement_specs(company.cik, latest.filing_accession):
        loaded = await load_statement_coverage(
            client,
            facts,
            company.cik,
            latest.fiscal_period_end,
            latest.filing_accession,
            [p.fiscal_period_end for p in company.periods],
        )
        support["statement_inputs"] = loaded["inputs"]
        support["zero_current_borrowings"] = loaded.get("zero_current_borrowings", {})
    if scoped_paths(company.cik, latest.filing_accession):
        loaded = await load_scoped_inputs(
            client,
            facts,
            company.cik,
            latest.fiscal_period_end,
            latest.filing_accession,
            [p.fiscal_period_end for p in company.periods],
        )
        support["scoped_inputs"] = loaded["inputs"]
    if company.cik.lstrip("0") + ":" + latest.filing_accession not in POLICIES:
        from reusable_inputs import load_reusable_inputs

        try:
            loaded = await load_reusable_inputs(
                client,
                facts,
                company.cik,
                latest.fiscal_period_end,
                latest.filing_accession,
                latest.industry,
            )
            documents.update(loaded.pop("documents"))
            support["reusable_inputs"] = loaded
        except ValueError as exc:
            support["reusable_error"] = str(exc)
    report = qualify(company, facts, support, metadata["accession_number"])
    checked = company.model_copy(update={"qualification": report})
    # Inline source documents / helper results are preserved as well as company facts.
    evidence = {
        "facts": facts,
        "latest_10k": metadata,
        "support": _source_json(support),
        "inline_accession": support.get("inline_accession"),
        "documents": documents,
    }
    report = report.model_copy(
        update={
            "evidence_digest": hashlib.sha256(
                json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        }
    )
    checked = company.model_copy(update={"qualification": report})
    return checked, evidence


def _source_json(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return _source_json(asdict(value))
    if isinstance(value, dict):
        return {str(k): _source_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_source_json(v) for v in value]
    if isinstance(value, (date, Decimal)):
        return str(value)
    return value
