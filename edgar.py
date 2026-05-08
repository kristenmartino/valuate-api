"""SEC EDGAR async client.

Notes on the SEC API:
- Requires User-Agent header with name + email; non-conforming requests are blocked.
- Rate limit: 10 requests/second per IP. We use a semaphore + sleep for headroom.
- All endpoints are JSON over HTTPS, no auth required.
- CIK is the SEC's company identifier; padded to 10 digits in submissions
  and companyfacts URLs, but not in Archives URLs (where it's an int).

Endpoint map:
    Ticker → CIK lookup:    https://www.sec.gov/files/company_tickers.json
    Submissions metadata:   https://data.sec.gov/submissions/CIK{cik:010d}.json
    XBRL company facts:     https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json
    Filing HTML:            https://www.sec.gov/Archives/edgar/data/{cik:int}/{accn_clean}/{primary_doc}
"""

import asyncio
import os
from typing import Any, Optional

import httpx

SEC_DATA_BASE = "https://data.sec.gov"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class EdgarClient:
    """Async client for SEC EDGAR APIs."""

    def __init__(
        self,
        user_agent: Optional[str] = None,
        max_concurrent_requests: int = 5,
    ):
        ua = user_agent or os.environ.get("SEC_USER_AGENT")
        if not ua:
            raise ValueError(
                "SEC_USER_AGENT required. Format: 'Your Name your.email@domain.com'"
            )
        self.headers = {
            "User-Agent": ua,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._ticker_cik_map: Optional[dict[str, str]] = None

    async def _get_json(self, url: str) -> dict[str, Any]:
        """GET JSON with rate limiting and standard headers."""
        async with self.semaphore:
            await asyncio.sleep(0.1)  # SEC asks for ≤10 req/sec; this gives headroom
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=self.headers)
                resp.raise_for_status()
                return resp.json()

    async def _get_text(self, url: str) -> str:
        """GET raw text (for filing HTML)."""
        async with self.semaphore:
            await asyncio.sleep(0.1)
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(url, headers=self.headers)
                resp.raise_for_status()
                return resp.text

    async def _load_ticker_map(self) -> dict[str, str]:
        """Load and cache the ticker → CIK mapping from SEC."""
        if self._ticker_cik_map is not None:
            return self._ticker_cik_map

        data = await self._get_json(SEC_TICKERS_URL)
        # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        self._ticker_cik_map = {
            entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
            for entry in data.values()
        }
        return self._ticker_cik_map

    async def get_cik_from_ticker(self, ticker: str) -> str:
        """Look up CIK (zero-padded to 10 digits) from ticker symbol."""
        ticker_map = await self._load_ticker_map()
        cik = ticker_map.get(ticker.upper())
        if cik is None:
            raise ValueError(f"Ticker {ticker!r} not found in SEC database")
        return cik

    async def get_submissions(self, cik: str) -> dict[str, Any]:
        """Get filing submissions metadata for a company."""
        cik_padded = cik.zfill(10)
        url = f"{SEC_DATA_BASE}/submissions/CIK{cik_padded}.json"
        return await self._get_json(url)

    async def get_latest_10k(self, cik: str) -> dict[str, Any]:
        """Get metadata for the most recent 10-K filing.

        Returns:
            {
                'accession_number': '0000320193-24-000123',
                'filing_date': '2024-11-01',
                'period_of_report': '2024-09-28',
                'primary_document': 'aapl-20240928.htm',
                'primary_doc_url': 'https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm',
            }
        """
        submissions = await self.get_submissions(cik)
        recent = submissions.get("filings", {}).get("recent", {})

        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        filing_dates = recent.get("filingDate", [])
        periods = recent.get("reportDate", [])
        primary_docs = recent.get("primaryDocument", [])

        cik_int = int(cik)
        for i, form in enumerate(forms):
            if form == "10-K":
                accession = accessions[i]
                accession_clean = accession.replace("-", "")
                primary_doc = primary_docs[i]
                return {
                    "accession_number": accession,
                    "filing_date": filing_dates[i],
                    "period_of_report": periods[i],
                    "primary_document": primary_doc,
                    "primary_doc_url": (
                        f"https://www.sec.gov/Archives/edgar/data/"
                        f"{cik_int}/{accession_clean}/{primary_doc}"
                    ),
                }

        raise ValueError(f"No 10-K found in recent filings for CIK {cik}")

    async def get_company_facts(self, cik: str) -> dict[str, Any]:
        """Get all XBRL company facts (canonical line items across all filings)."""
        cik_padded = cik.zfill(10)
        url = f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json"
        return await self._get_json(url)

    async def get_filing_html(self, primary_doc_url: str) -> str:
        """Fetch the HTML of a 10-K filing."""
        return await self._get_text(primary_doc_url)


def latest_value_per_period(
    facts: dict[str, Any],
    concept: str,
    unit: str = "USD",
    period_type: str = "FY",
    taxonomy: str = "us-gaap",
) -> dict[str, dict[str, Any]]:
    """Extract the latest-filed value per fiscal period end from XBRL company facts.

    XBRL company facts structure:
        facts['facts']['us-gaap']['Revenues']['units']['USD'] = [
            {
                'start': '2022-01-01', 'end': '2022-12-31', 'val': 394328000000,
                'accn': '0000320193-23-000106', 'fy': 2022, 'fp': 'FY', ...
            },
            ...
        ]

    The `fy` field is the *filing's* fiscal year, not the data point's. A 10-K
    for FY2025 reports comparative income statements for FY2024 and FY2023; all
    three rows are tagged fy=2025. So we key by `end` date, which uniquely
    identifies a fiscal period for fp='FY' entries (full-year duration for
    income/cash flow, year-end snapshot for balance sheet).

    Same period can have multiple entries due to restatements; we keep the one
    with the highest accession number (most recent filing).

    Args:
        facts: Output of EdgarClient.get_company_facts()
        concept: us-gaap concept name (e.g. 'Revenues', 'OperatingIncomeLoss')
        unit: Reporting unit (default 'USD'; use 'shares' for share counts)
        period_type: 'FY' for full year, 'Q1'/'Q2'/'Q3' for quarters
        taxonomy: Usually 'us-gaap'; some concepts are under 'dei' or 'ifrs-full'

    Returns:
        Dict of {end_date_iso: entry_dict}, latest filing's value per period.
        Empty dict if concept not present or no matching entries.
    """
    taxonomy_data = facts.get("facts", {}).get(taxonomy, {})
    concept_data = taxonomy_data.get(concept)
    if concept_data is None:
        return {}

    units = concept_data.get("units", {}).get(unit, [])
    by_end: dict[str, dict[str, Any]] = {}

    for entry in units:
        if entry.get("fp") != period_type:
            continue
        end = entry.get("end")
        if end is None:
            continue
        existing = by_end.get(end)
        if existing is None or entry["accn"] > existing["accn"]:
            by_end[end] = entry

    return by_end


# Common us-gaap concept names you'll want for Track A extraction.
# Companies are inconsistent — many fall back to one of the alternates.
#
# Per-industry concept maps live below. Use `concepts_for(industry)` to pick
# the right one at extraction time. The keys in each map MUST match the
# corresponding industry's schema field names exactly — graph.py uses them
# verbatim when composing the FinancialPeriod.

STANDARD_CANONICAL_CONCEPTS: dict[str, list[str]] = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "Depreciation",  # GOOGL et al. report only depreciation as the CFO add-back
    ],
    "capital_expenditures": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",  # NVDA, HD
    ],
    "cash_from_operations": ["NetCashProvidedByUsedInOperatingActivities"],
    "cash_from_investing": ["NetCashProvidedByUsedInInvestingActivities"],
    "cash_from_financing": ["NetCashProvidedByUsedInFinancingActivities"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "cash_and_equivalents": ["CashAndCashEquivalentsAtCarryingValue"],
    "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "shareholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
}


# Banks tag their P&L on a different conceptual axis: interest spread vs.
# fee income, with provision-for-credit-losses replacing COGS. Balance sheet
# is loans + deposits dominated. The fields here mirror BankIncomeStatement
# / BankBalanceSheet / BankCashFlowStatement in schemas.py.
BANK_CANONICAL_CONCEPTS: dict[str, list[str]] = {
    # Income statement
    "interest_income": [
        "InterestAndDividendIncomeOperating",
        "InterestIncomeOperating",
    ],
    "interest_expense": ["InterestExpense"],
    "net_interest_income": [
        "InterestIncomeExpenseNet",
        "InterestIncomeExpenseAfterProvisionForLoanLoss",
    ],
    "provision_for_credit_losses": [
        "ProvisionForLoanLeaseAndOtherLosses",
        "ProvisionForLoanAndLeaseLosses",
        "FinancingReceivableCreditLossExpenseReversal",  # CECL era
    ],
    "non_interest_income": [
        "NoninterestIncome",
        "RevenuesNetOfInterestExpense",
    ],
    "non_interest_expense": ["NoninterestExpense"],
    "income_before_tax": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "income_tax_expense": ["IncomeTaxExpenseBenefit"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    # Balance sheet
    "cash_and_equivalents": [
        "CashAndCashEquivalentsAtCarryingValue",
        "Cash",
        "CashAndDueFromBanks",
    ],
    "securities": [
        "DebtSecuritiesAvailableForSaleExcludingAccruedInterest",
        "AvailableForSaleSecuritiesDebtSecurities",
        "AvailableForSaleSecurities",
    ],
    "total_loans": [
        # Post-CECL (2020+) tag — used by JPM, BAC. After-allowance net loans.
        "FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLoss",
        # Pre-CECL legacy tags
        "LoansAndLeasesReceivableNetReportedAmount",
        "LoansAndLeasesReceivableNetOfDeferredIncome",
    ],
    "allowance_for_loan_losses": [
        # Post-CECL primary tag
        "FinancingReceivableAllowanceForCreditLossExcludingAccruedInterest",
        "FinancingReceivableAllowanceForCreditLossesExcludingAccruedInterest",
        # Pre-CECL legacy tags
        "AllowanceForLoanAndLeaseLosses",
        "LoansAndLeasesReceivableAllowance",
    ],
    "total_deposits": ["Deposits"],
    "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "shareholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    # Cash flow
    "cash_from_operations": ["NetCashProvidedByUsedInOperatingActivities"],
    "cash_from_investing": ["NetCashProvidedByUsedInInvestingActivities"],
    "cash_from_financing": ["NetCashProvidedByUsedInFinancingActivities"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
    ],
    "capital_expenditures": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}


# Backward-compat alias used by extract_track_a's older signature; new code
# should reach for STANDARD_CANONICAL_CONCEPTS or call concepts_for(industry).
CANONICAL_CONCEPTS = STANDARD_CANONICAL_CONCEPTS


def concepts_for(industry: "Industry") -> dict[str, list[str]]:  # type: ignore[name-defined]
    """Return the right XBRL concept map for the given industry.

    Defaults to the standard map for industries we don't yet have a variant
    for (insurer, REIT, energy E&P → Phase 2-4); Track A will still find
    common fields like net_income / total_assets, just won't find the
    industry-specific ones until those phases land.
    """
    # Late import to avoid a circular dep (industry → schemas → edgar would
    # be a cycle; industry is small enough to import here on demand).
    from industry import Industry

    if industry == Industry.BANK:
        return BANK_CANONICAL_CONCEPTS
    return STANDARD_CANONICAL_CONCEPTS
