"""Cisco's filed cash-flow D&A/other adjustment, not its PPE-only component."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

from bs4 import BeautifulSoup

from schemas import Company, ExtractionFlag, ExtractionSource, LineItem

CIK = "858877"
CONCEPT = "csco:DepreciationAmortizationAndOther"
PPE_CONCEPT = "us-gaap:DepreciationDepletionAndAmortization"


def _period_start(end: date) -> date | None:
    # Cisco's fiscal year ends on the last Saturday of July (52/53 weeks).
    july_end = date(end.year, 7, 31)
    if end != july_end - timedelta(days=(july_end.weekday() - 5) % 7):
        return None
    previous = date(end.year - 1, 7, 31)
    return previous - timedelta(days=(previous.weekday() - 5) % 7) + timedelta(days=1)


def extract_filing_da(html: str, document_end: date, accession: str, period_ends: list[date]) -> dict[date, LineItem]:
    """Read only consolidated annual USD facts from a resolved annual filing."""
    if not re.fullmatch(r"0000858877-\d{2}-\d{6}", accession):
        return {}
    soup = BeautifulSoup(html, "xml")
    metadata = {f.get("name"): f.get_text(strip=True) for f in soup.find_all("nonNumeric")}
    if (metadata.get("dei:DocumentType") not in {"10-K", "10-K/A"}
            or metadata.get("dei:EntityCentralIndexKey", "").lstrip("0") != CIK):
        return {}
    raw_end = metadata.get("dei:DocumentPeriodEndDate", "")
    try:
        filed_end = date.fromisoformat(raw_end)
    except ValueError:
        try:
            filed_end = datetime.strptime(raw_end, "%B %d, %Y").date()
        except ValueError:
            return {}
    if filed_end != document_end or _period_start(document_end) is None:
        return {}
    contexts = {c.get("id"): c for c in soup.find_all("context")}
    units = {u.get("id"): u for u in soup.find_all("unit")}
    output = {}
    for pe in period_ends:
        start = _period_start(pe)
        if start is None or pe > document_end:
            continue
        values = set()
        invalid = False
        for f in soup.find_all("nonFraction", attrs={"name": CONCEPT}):
            c = contexts.get(f.get("contextRef"))
            if c is None or c.find("segment") or c.find("scenario"):
                continue
            identifier, begin, end = c.find("identifier"), c.find("startDate"), c.find("endDate")
            if (identifier is None or identifier.get_text(strip=True).lstrip("0") != CIK
                    or begin is None or end is None or begin.get_text(strip=True) != start.isoformat()
                    or end.get_text(strip=True) != pe.isoformat()):
                continue
            unit = units.get(f.get("unitRef"))
            if unit is None or unit.find("divide") or unit.get_text(strip=True) != "iso4217:USD":
                continue
            if (f.get("continuedAt") or f.get("xsi:nil", "").lower() == "true" or f.find("exclude")
                    or f.get("format") not in {None, "ixt:num-dot-decimal", "ixt:numdotdecimal"}):
                invalid = True
                continue
            try:
                scale = int(f.get("scale", "0"))
                if not -12 <= scale <= 12:
                    raise ValueError("Unsupported scale")
                value = Decimal(f.get_text(strip=True).replace(",", "")) * Decimal(10) ** scale
                if f.get("sign") == "-":
                    value = -value
                if not value.is_finite() or value < 0:
                    raise ValueError("Invalid adjustment")
                values.add(value)
            except (ValueError, InvalidOperation):
                invalid = True
        if not invalid and len(values) == 1:
            value = values.pop()
            output[pe] = LineItem(
                value=value, source=ExtractionSource.XBRL, confidence=1.0, xbrl_tag=CONCEPT,
                source_quote=(f"Filed cash-flow depreciation, amortization and other adjustment: {value} USD; "
                              f"annual period {start} to {pe}; SEC accession {accession}. "
                              "Combined cash-flow add-back used as the model's D&A proxy, not pure D&A or PPE alone."),
            )
    return output


async def filing_da_history(client, cik: str, latest: date, accession: str, period_ends: list[date], anchor_html: str | None = None) -> dict[date, LineItem]:
    if cik.lstrip("0") != CIK:
        return {}
    submissions = await client.get_submissions(cik)
    if str(submissions.get("cik", "")).lstrip("0") != CIK:
        return {}
    recent = submissions.get("filings", {}).get("recent", {})
    rows = []
    for accn, form, end, document in zip(*(recent.get(k, []) for k in ("accessionNumber", "form", "reportDate", "primaryDocument"))):
        if (form not in {"10-K", "10-K/A"} or not re.fullmatch(r"0000858877-\d{2}-\d{6}", accn)
                or accn > accession or not re.fullmatch(r"[A-Za-z0-9_.-]+", document)):
            continue
        try:
            end = date.fromisoformat(end)
        except ValueError:
            continue
        if end <= latest and _period_start(end) is not None:
            rows.append((accn, end, document))
    anchor = next((row for row in rows if row[0] == accession and row[1] == latest), None)
    if anchor is None:
        return {}
    output = {}
    ordered = [anchor, *sorted((row for row in rows if row != anchor), reverse=True)]
    for accn, end, document in ordered:
        missing = [pe for pe in period_ends if pe not in output]
        if not missing:
            break
        # Prefer the saved filing's comparatives; older annual filings fill
        # only remaining periods. Skip filings too recent for the oldest gap.
        if accn != accession and end.year > min(missing).year + 2:
            continue
        if not any(pe <= end for pe in missing):
            continue
        try:
            html = anchor_html if accn == accession and anchor_html is not None else await client.get_filing_html(
                f"https://www.sec.gov/Archives/edgar/data/{CIK}/{accn.replace('-', '')}/{document}"
            )
            output.update(extract_filing_da(html, end, accn, missing))
        except Exception:
            if accn == accession:
                raise
            # Keep successfully read periods; missing history stays explicit.
            continue
    return output


def _incomplete(item: LineItem | None) -> bool:
    return item is None or (item.source != ExtractionSource.USER_OVERRIDE and item.xbrl_tag == PPE_CONCEPT)


def needs_da_repair(company: Company) -> bool:
    return company.cik.lstrip("0") == CIK and any(
        _incomplete(getattr(stmt, "depreciation_amortization", None))
        for p in company.periods for stmt in (p.income_statement, p.cash_flow_statement)
    )


def repair_da(company: Company, items: dict[date, LineItem]) -> Company:
    if not needs_da_repair(company):
        return company
    required = {p.fiscal_period_end for p in company.periods
                if any(_incomplete(getattr(stmt, "depreciation_amortization", None))
                       for stmt in (p.income_statement, p.cash_flow_statement))}
    if not required.issubset(items):
        # Do not mix repaired totals with old components in the default
        # historical average. Preserve the saved record and its warning.
        return company
    periods = []
    for p in company.periods:
        item = items.get(p.fiscal_period_end)
        changes = {}
        if item is not None:
            for name in ("income_statement", "cash_flow_statement"):
                stmt = getattr(p, name)
                if _incomplete(getattr(stmt, "depreciation_amortization", None)):
                    changes[name] = stmt.model_copy(update={"depreciation_amortization": item})
        periods.append(p.model_copy(update=changes))
    return company.model_copy(update={"periods": periods})


def da_flags(company: Company) -> list[ExtractionFlag]:
    if company.cik.lstrip("0") != CIK:
        return []
    item = company.periods[0].cash_flow_statement.depreciation_amortization
    if _incomplete(item):
        return [ExtractionFlag(
            field_path="cash_flow_statement.depreciation_amortization", current_value=item.value,
            reason="PPE-only D&A is incomplete; verify Cisco's filed depreciation, amortization and other cash-flow adjustment before valuation.",
        )]
    return []
