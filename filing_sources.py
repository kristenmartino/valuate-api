"""Bounded discovery of annual SEC source documents, including archive pages."""

from datetime import date, timedelta
import json
import re

MAX_ARCHIVES = 24
MAX_ANNUALS = 5


def annual_rows(recent, cik):
    rows = []
    for i, form in enumerate(recent.get("form", [])):
        if form not in {"10-K", "10-K/A"}:
            continue
        try:
            acc = recent["accessionNumber"][i]
            end, filed = (
                date.fromisoformat(recent["reportDate"][i]),
                date.fromisoformat(recent["filingDate"][i]),
            )
            name = recent["primaryDocument"][i]
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        if (
            not re.fullmatch(r"\d{10}-\d{2}-\d{6}", acc)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:htm|html)", name)
            or filed < end
        ):
            continue
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"
        rows.append(
            {
                "accession": acc,
                "end": end,
                "filed": filed,
                "form": form,
                "url": base + name,
                "base": base,
            }
        )
    return rows


async def annual_documents(client, cik, latest, accession, bound):
    sub = await client.get_submissions(cik)
    if str(sub.get("cik", "")).lstrip("0") != str(cik).lstrip("0"):
        raise ValueError("SEC submissions entity does not match the requested entity")
    sources = {
        "https://data.sec.gov/submissions/CIK" + cik.zfill(10) + ".json": json.dumps(
            sub, sort_keys=True
        )
    }
    rows = annual_rows(sub.get("filings", {}).get("recent", {}), cik)
    earliest = latest - timedelta(days=5 * 366)
    archive_count = 0
    for entry in sub.get("filings", {}).get("files", []):
        try:
            start, end = (
                date.fromisoformat(entry["filingFrom"]),
                date.fromisoformat(entry["filingTo"]),
            )
        except (ValueError, KeyError, TypeError):
            continue
        if end < earliest or start > bound:
            continue
        archive_count += 1
        if archive_count > MAX_ARCHIVES:
            raise ValueError(
                "Annual history exceeds bounded SEC archive discovery; source review is required"
            )
        name = entry.get("name", "")
        if not re.fullmatch(
            r"CIK" + re.escape(cik.zfill(10)) + r"-submissions-\d{3}\.json", name
        ):
            raise ValueError("Unsafe SEC submissions archive name")
        url = "https://data.sec.gov/submissions/" + name
        data = await client._get_json(url)
        sources[url] = json.dumps(data, sort_keys=True)
        rows.extend(annual_rows(data, cik))
    unique = {
        r["accession"]: r
        for r in rows
        if earliest <= r["end"] <= latest and r["end"] <= r["filed"] <= bound
    }
    anchor = unique.get(accession)
    if anchor is None or anchor["end"] != latest or anchor["form"] != "10-K":
        raise ValueError("Annual anchor metadata does not match the requested filing")
    # An amendment may change financials. Do not silently mix original and amended facts.
    amendments = [
        r
        for r in rows
        if r["form"] == "10-K/A"
        and r["end"] == latest
        and anchor["filed"] <= r["filed"] <= date.today()
    ]
    if amendments:
        raise ValueError("An amended annual filing requires source reconciliation")
    by_end = {}
    for row in unique.values():
        if row["form"] != "10-K" or (
            row["end"] == latest and row["accession"] != accession
        ):
            continue
        if row["end"] not in by_end or (row["filed"], row["accession"]) > (
            by_end[row["end"]]["filed"],
            by_end[row["end"]]["accession"],
        ):
            by_end[row["end"]] = row
    selected = sorted(by_end.values(), key=lambda r: r["end"], reverse=True)[
        :MAX_ANNUALS
    ]
    selected_ends = {r["end"] for r in selected}
    if any(
        r["form"] == "10-K/A" and r["end"] in selected_ends and r["filed"] <= bound
        for r in rows
    ):
        raise ValueError(
            "An amended historical annual filing requires source reconciliation"
        )
    return selected, sources


async def linked_documents(client, filing, html):
    """Discover only filing-local schema/presentation/calculation resources."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "xml")
    names = [s.get("xlink:href", "") for s in soup.find_all("schemaRef")]
    if len(set(names)) != 1:
        raise ValueError("One filing-local XBRL schema reference is required")
    name = names[0]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.xsd", name):
        raise ValueError("Unsafe filing schema reference")
    schema_url = filing["base"] + name
    schema = await client.get_filing_html(schema_url)
    docs = {filing["url"]: html, schema_url: schema}
    parsed = BeautifulSoup(schema, "xml")
    roles = {}
    for ref in parsed.find_all("linkbaseRef"):
        kind = ref.get("xlink:role", "").rsplit("/", 1)[-1]
        if kind not in {"presentationLinkbaseRef", "calculationLinkbaseRef"}:
            continue
        name = ref.get("xlink:href", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.xml", name) or kind in roles:
            raise ValueError("Unsafe or ambiguous filing linkbase reference")
        url = filing["base"] + name
        text = await client.get_filing_html(url)
        docs[url] = text
        roles[kind] = text
    if len(roles) != 2:
        raise ValueError("Presentation and calculation evidence are required")
    return {
        "schema": schema,
        "presentation": roles["presentationLinkbaseRef"],
        "calculation": roles["calculationLinkbaseRef"],
        "documents": docs,
    }
