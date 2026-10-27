"""Collect independent source evidence and stage a checked record."""

import hashlib
import json
from edgar import EdgarClient
from schemas import Company
from qualification import qualify
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
    report = qualify(company, facts, support, metadata["accession_number"])
    checked = company.model_copy(update={"qualification": report})
    # Inline source documents / helper results are preserved as well as company facts.
    evidence = {
        "facts": facts,
        "latest_10k": metadata,
        "support": {
            key: {
                str(pe): {
                    field: item.model_dump(mode="json") for field, item in items.items()
                }
                if isinstance(items, dict)
                else items.model_dump(mode="json")
                for pe, items in values.items()
            }
            for key, values in support.items()
            if isinstance(values, dict)
        },
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
