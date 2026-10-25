"""Salesforce's tagged revenue categories, with both subtotal checks.

Five subscription offerings plus professional services form one exhaustive
breakdown. Subscription and support is a subtotal, never a seventh bar.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

from schemas import Company, ExtractionSource, LineItem, RevenueSegment

CIK = "1108524"
CONCEPT = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
AXIS = "srt:ProductOrServiceAxis"
OFFERINGS = (
    ("AgentforceSalesMember", "Agentforce Sales"),
    ("AgentforceServiceMember", "Agentforce Service"),
    ("Agentforce360PlatformSlackAndOtherMember", "Agentforce 360 Platform, Slack and Other"),
    ("AgentforceMarketingAndAgentforceCommerceMember", "Agentforce Marketing and Agentforce Commerce"),
    ("AgentforceIntegrationAndAgentforceAnalyticsMember", "Agentforce Integration and Agentforce Analytics"),
    ("ProfessionalServicesandOtherMember", "Professional services and other"),
)


def extract_crm_revenue(html: str, period_end: date, accession: str, expected_total: Decimal) -> list[RevenueSegment]:
    if (period_end.month, period_end.day) != (1, 31):
        return []
    soup = BeautifulSoup(html, "xml")
    metadata = {f.get("name"): f.get_text(strip=True) for f in soup.find_all("nonNumeric")}
    if metadata.get("dei:DocumentType") not in {"10-K", "10-K/A"} or metadata.get("dei:EntityCentralIndexKey", "").lstrip("0") != CIK:
        return []
    document_end = metadata.get("dei:DocumentPeriodEndDate", "")
    try:
        document_end = date.fromisoformat(document_end)
    except ValueError:
        try:
            document_end = datetime.strptime(document_end, "%B %d, %Y").date()
        except ValueError:
            return []
    if document_end != period_end:
        return []
    contexts = {c.get("id"): c for c in soup.find_all("context")}
    units = {u.get("id"): u.get_text(strip=True) for u in soup.find_all("unit")}
    facts = soup.find_all("nonFraction", attrs={"name": CONCEPT})

    def read(member: str | None):
        values = set()
        for f in facts:
            if f.get("continuedAt") or f.get("xsi:nil") == "true" or units.get(f.get("unitRef")) != "iso4217:USD":
                continue
            c = contexts.get(f.get("contextRef"))
            if c is None:
                continue
            identifier, start, end = c.find("identifier"), c.find("startDate"), c.find("endDate")
            if identifier is None or identifier.get_text(strip=True).lstrip("0") != CIK:
                continue
            if start is None or end is None or start.get_text(strip=True) != f"{period_end.year - 1}-02-01" or end.get_text(strip=True) != period_end.isoformat():
                continue
            dimensions = c.find_all("explicitMember")
            expected = {} if member is None else {AXIS: f"crm:{member}"}
            if c.find("typedMember") or len(dimensions) != len(expected) or {m.get("dimension"): m.get_text(strip=True) for m in dimensions} != expected:
                continue
            if f.get("format") not in {None, "ixt:num-dot-decimal", "ixt:numdotdecimal"}:
                continue
            try:
                value = Decimal(f.get_text(strip=True).replace(",", "")) * Decimal(10) ** int(f.get("scale", "0"))
                if f.get("sign") == "-": value = -value
                if value.is_finite() and value >= 0: values.add(value)
            except (ValueError, InvalidOperation):
                continue
        return next(iter(values)) if len(values) == 1 else None

    total = read(None)
    subscription = read("SubscriptionandSupportMember")
    values = [read(member) for member, _ in OFFERINGS]
    if total is None or subscription is None or any(v is None for v in values):
        return []
    if sum(values[:5]) != subscription or subscription + values[5] != total or total != expected_total:
        return []
    return [RevenueSegment(name=label, revenue=LineItem(
        value=value, source=ExtractionSource.XBRL, confidence=1.0, xbrl_tag=CONCEPT,
        source_quote=(f"{label}: {value} USD; annual period ending {period_end}; "
                      f"SEC accession {accession}; inline XBRL {AXIS}=crm:{member}. "
                      "Revenue category, not a separate operating segment."),
    )) for (member, label), value in zip(OFFERINGS, values)]


def needs_revenue_repair(company: Company) -> bool:
    if company.cik.lstrip("0") != CIK:
        return False
    p = company.periods[0]
    segments = p.income_statement.revenue_segments or []
    if any(s.revenue.source == ExtractionSource.USER_OVERRIDE for s in segments):
        return False
    return (tuple(s.name for s in segments) != tuple(label for _, label in OFFERINGS)
            or any(s.revenue.source != ExtractionSource.XBRL or s.revenue.xbrl_tag != CONCEPT for s in segments)
            or sum(s.revenue.value for s in segments) != p.income_statement.revenue.value)


def repair_revenue(company: Company, html: str) -> Company:
    if not needs_revenue_repair(company):
        return company
    p = company.periods[0]
    segments = extract_crm_revenue(html, p.fiscal_period_end, p.filing_accession, p.income_statement.revenue.value)
    if not segments:
        return company
    income = p.income_statement.model_copy(update={"revenue_segments": segments})
    return company.model_copy(update={"periods": [p.model_copy(update={"income_statement": income}), *company.periods[1:]]})


async def fetch_saved_filing(client, company: Company) -> str:
    """Resolve the saved accession, never replace it with the latest filing."""
    submissions = await client.get_submissions(company.cik)
    recent = submissions.get("filings", {}).get("recent", {})
    accession = company.periods[0].filing_accession
    accessions = recent.get("accessionNumber", [])
    if accession not in accessions:
        raise ValueError("Saved CRM filing unavailable in recent SEC submissions")
    i = accessions.index(accession)
    if recent.get("form", [])[i] not in {"10-K", "10-K/A"}:
        raise ValueError("Saved CRM accession is not an annual filing")
    document = recent["primaryDocument"][i]
    url = f"https://www.sec.gov/Archives/edgar/data/{int(company.cik)}/{accession.replace('-', '')}/{document}"
    return await client.get_filing_html(url)
