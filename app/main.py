"""Valuate API — FastAPI service for the DCF extraction agent.

Storage: persistence is repository-backed. PostgresRepo turns on whenever
DATABASE_URL is in the environment (Railway's Postgres plugin sets this
automatically); otherwise InMemoryRepo runs and the behavior matches the
prior in-memory dict for local dev.

Run locally:
    SEC_USER_AGENT="Your Name you@example.com" \
    ANTHROPIC_API_KEY="sk-ant-..." \
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from time import monotonic
from typing import Any, Optional

from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.auth import require_override_auth
from app.logging_middleware import install_structured_logging
from app.rate_limit import require_extract_rate_limit
from app.sentry_setup import init_sentry
from app.version import get_version_info
from comps import get_peer_comps
from dcf import (
    compute_projection,
    default_assumptions,
    monte_carlo,
    sensitivity_grid,
)
from edgar import EdgarClient
from equity import backfill_noncontrolling_interests, needs_equity_backfill
from salesforce import needs_mapping_repair, repair_company
from debt import needs_debt_repair, repair_debt
from cisco_da import needs_da_repair, repair_da, filing_da_history
from crm_revenue import fetch_saved_filing, needs_revenue_repair, repair_revenue
from graph import CompositionError, build_graph, validate_company
from intuit import needs_repair as needs_intu_repair, repair_company as repair_intu, incomplete_inputs as incomplete_intu
from overrides import apply_override
from repository import CompanyRepo, make_repo
from tickers import normalize_ticker
from schemas import (
    Assumptions,
    Company,
    CompsResponse,
    MonteCarloResult,
    Projection,
    SensitivityGrid,
)


class ExtractRequest(BaseModel):
    ticker: str


class OverrideRequest(BaseModel):
    field_path: str
    value: Decimal
    source_quote: Optional[str] = None


class MonteCarloParams(BaseModel):
    # Bounded so a direct POST /value can't pin a worker with a huge iteration
    # count (the endpoint isn't auth'd or rate-limited; /extract is the gated
    # one). 25k is well above the 10k the frontend uses.
    iterations: int = Field(10_000, ge=100, le=25_000)
    # None defaults let monte_carlo() derive these from each filer's
    # historical sample-σ (revenue growth YoY, op margin). The prior
    # hardcoded 2% / 2% / 0.5% / 0.5% defaults masked the historical-vol
    # derivation by always providing concrete values to the function.
    # Frontend doesn't pass these explicitly; backend derives per company.
    revenue_growth_std: Optional[float] = None
    operating_margin_std: Optional[float] = None
    # terminal_growth and wacc aren't observable from financials (they're
    # forward inputs the user sets), so their σ stays fixed.
    terminal_growth_std: float = 0.005
    wacc_std: float = 0.005
    seed: Optional[int] = None


class SensitivityParams(BaseModel):
    revenue_growth_min: Optional[float] = None
    revenue_growth_max: Optional[float] = None
    revenue_growth_steps: int = Field(7, ge=2, le=15)
    operating_margin_min: Optional[float] = None
    operating_margin_max: Optional[float] = None
    operating_margin_steps: int = Field(7, ge=2, le=15)


class ValuationRequest(BaseModel):
    assumptions: Assumptions
    monte_carlo: Optional[MonteCarloParams] = None
    sensitivity: Optional[SensitivityParams] = None


class ValuationResponse(BaseModel):
    projection: Projection
    monte_carlo: Optional[MonteCarloResult] = None
    sensitivity: Optional[SensitivityGrid] = None


_graph: Any = None
_repo: Optional[CompanyRepo] = None
_company_locks: dict[str, asyncio.Lock] = {}
_equity_backfill_attempts: dict[tuple[str, str], float] = {}
_mapping_repair_attempts: dict[tuple[str, str], float] = {}
_cisco_da_repair_attempts: dict[tuple[str, str], float] = {}
_intu_repair_attempts: dict[tuple[str, str], float] = {}
_revenue_repair_attempts: dict[tuple[str, str], float] = {}
_logger = logging.getLogger(__name__)


def _company_lock(ticker: str) -> asyncio.Lock:
    return _company_locks.setdefault(normalize_ticker(ticker), asyncio.Lock())


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _graph, _repo
    _graph = build_graph(EdgarClient(), AsyncAnthropic())
    _repo = await make_repo()
    yield
    _graph = None
    if _repo is not None:
        await _repo.close()
        _repo = None


# Optional Sentry: activates when SENTRY_DSN is set in the env. Initialized
# before app construction so any startup-phase exception (lifespan failures,
# config problems) is captured. No-op when SENTRY_DSN is absent.
init_sentry()

app = FastAPI(title="Valuate API", lifespan=lifespan)

# Structured request logging with X-Request-ID correlation. Skipped only
# when VALUATE_DISABLE_STRUCTURED_LOGGING=1 (local dev where you'd rather
# see plain Python tracebacks).
install_structured_logging(app)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/version")
async def version() -> dict[str, Optional[str]]:
    """Build-time identity for the running container. Useful for
    diagnosing 'is the deploy stale?' without needing to probe behavior.
    Returns commit SHA, deployment ID, start timestamp, and environment —
    all pulled from Railway's auto-injected env vars (None for fields
    that aren't set, e.g. when running locally)."""
    return get_version_info()


def _require_repo() -> CompanyRepo:
    if _repo is None:
        raise HTTPException(503, "Service not ready")
    return _repo


async def _require_company(ticker: str) -> Company:
    repo = _require_repo()
    company = await repo.get(ticker)
    if company is None:
        raise HTTPException(
            404, f"No extraction found for {ticker}. POST /extract first."
        )
    if needs_equity_backfill(company):
        # Upgrade an old saved record without extracting again or replacing
        # existing amounts/corrections. Serialize this with reviewer writes.
        async with _company_lock(ticker):
            company = await repo.get(ticker)
            if company is None:
                raise HTTPException(404, f"No extraction found for {ticker}.")
            key = (normalize_ticker(ticker), company.periods[0].filing_accession)
            if needs_equity_backfill(company) and monotonic() - _equity_backfill_attempts.get(key, float("-inf")) >= 300:
                _equity_backfill_attempts[key] = monotonic()
                try:
                    facts = await EdgarClient().get_company_facts(company.cik)
                    updated = backfill_noncontrolling_interests(company, facts)
                    if updated != company:
                        updated = validate_company(updated)
                        await repo.set(ticker, updated)
                        company = updated
                except Exception as exc:
                    # Keep the warning and saved figures if SEC is unavailable.
                    _logger.warning("Equity component backfill unavailable for %s (%s)", normalize_ticker(ticker), type(exc).__name__)
    if needs_mapping_repair(company) or needs_debt_repair(company):
        async with _company_lock(ticker):
            company = await repo.get(ticker)
            if company is None:
                raise HTTPException(404, f"No extraction found for {ticker}.")
            key = (normalize_ticker(ticker), company.periods[0].filing_accession)
            if (needs_mapping_repair(company) or needs_debt_repair(company)) and monotonic() - _mapping_repair_attempts.get(key, float("-inf")) >= 300:
                _mapping_repair_attempts[key] = monotonic()
                try:
                    facts = await EdgarClient().get_company_facts(company.cik)
                    updated = validate_company(repair_debt(repair_company(company, facts), facts))
                    if updated != company:
                        await repo.set(ticker, updated)
                        company = updated
                except Exception as exc:
                    _logger.warning("Statement mapping repair unavailable for %s (%s)", normalize_ticker(ticker), type(exc).__name__)
    if needs_da_repair(company):
        async with _company_lock(ticker):
            company = await repo.get(ticker)
            if company is None:
                raise HTTPException(404, f"No extraction found for {ticker}.")
            key = (normalize_ticker(ticker), company.periods[0].filing_accession)
            if needs_da_repair(company) and monotonic() - _cisco_da_repair_attempts.get(key, float("-inf")) >= 300:
                _cisco_da_repair_attempts[key] = monotonic()
                try:
                    latest = company.periods[0]
                    items = await filing_da_history(EdgarClient(), company.cik, latest.fiscal_period_end,
                                                    latest.filing_accession, [p.fiscal_period_end for p in company.periods])
                    updated = validate_company(repair_da(company, items))
                    if updated != company:
                        await repo.set(ticker, updated)
                        company = updated
                except Exception as exc:
                    _logger.warning("Cisco D&A repair unavailable for %s (%s)", normalize_ticker(ticker), type(exc).__name__)
    if needs_revenue_repair(company):
        async with _company_lock(ticker):
            company = await repo.get(ticker)
            if company is None:
                raise HTTPException(404, f"No extraction found for {ticker}.")
            key = (normalize_ticker(ticker), company.periods[0].filing_accession)
            if needs_revenue_repair(company) and monotonic() - _revenue_repair_attempts.get(key, float("-inf")) >= 300:
                _revenue_repair_attempts[key] = monotonic()
                try:
                    html = await fetch_saved_filing(EdgarClient(), company)
                    updated = repair_revenue(company, html)
                    if updated != company:
                        await repo.set(ticker, updated)
                        company = updated
                except Exception as exc:
                    _logger.warning("Revenue breakdown repair unavailable for %s (%s)", normalize_ticker(ticker), type(exc).__name__)
    if needs_intu_repair(company):
        async with _company_lock(ticker):
            company = await repo.get(ticker)
            if company is None:
                raise HTTPException(404, f"No extraction found for {ticker}.")
            key = (normalize_ticker(ticker), company.periods[0].filing_accession)
            if needs_intu_repair(company) and monotonic() - _intu_repair_attempts.get(key, float("-inf")) >= 300:
                _intu_repair_attempts[key] = monotonic()
                try:
                    facts = await EdgarClient().get_company_facts(company.cik)
                    updated = validate_company(repair_intu(company, facts))
                    if updated != company:
                        await repo.set(ticker, updated)
                        company = updated
                except Exception as exc:
                    _logger.warning("INTU input repair unavailable for %s (%s)", normalize_ticker(ticker), type(exc).__name__)
    return validate_company(company)


def _check_intu_coverage(company: Company) -> None:
    if incomplete_intu(company):
        raise HTTPException(422, "INTU annual D&A/current/noncurrent debt coverage is incomplete; matching filing inputs are required before valuation.")


@app.post("/extract", response_model=Company)
async def extract(
    req: ExtractRequest,
    _ratelimit: None = Depends(require_extract_rate_limit),
) -> Company:
    if _graph is None:
        raise HTTPException(503, "Service not ready")
    try:
        result = await _graph.ainvoke({"ticker": normalize_ticker(req.ticker)})
    except ValueError as e:
        # Unknown ticker, missing 10-K in recent filings, etc.
        raise HTTPException(400, str(e))
    except CompositionError as e:
        # Required fields still missing after both Track A and Track B.
        raise HTTPException(422, str(e))
    company = result["company"]
    await _require_repo().set(req.ticker, company)
    return company


@app.get("/company/{ticker}", response_model=Company)
async def get_company(ticker: str) -> Company:
    return await _require_company(ticker)


@app.put("/company/{ticker}/override", response_model=Company)
async def override(
    ticker: str,
    req: OverrideRequest,
    _auth: None = Depends(require_override_auth),
) -> Company:
    await _require_company(ticker)
    async with _company_lock(ticker):
        company = await _require_repo().get(ticker)
        if company is None:
            raise HTTPException(404, f"No extraction found for {ticker}.")
        try:
            updated = apply_override(
                company,
                field_path=req.field_path,
                value=req.value,
                source_quote=req.source_quote,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        # Re-run validation so flags reflect the post-override state.
        updated = validate_company(updated)
        await _require_repo().set(ticker, updated)
    return updated


@app.get("/value/{ticker}/defaults", response_model=Assumptions)
async def value_defaults(ticker: str) -> Assumptions:
    company = await _require_company(ticker)
    _check_intu_coverage(company)
    return default_assumptions(company)


@app.post("/value/{ticker}", response_model=ValuationResponse)
async def value(ticker: str, req: ValuationRequest) -> ValuationResponse:
    company = await _require_company(ticker)
    _check_intu_coverage(company)
    try:
        projection = compute_projection(company, req.assumptions)
    except ValueError as e:
        raise HTTPException(400, str(e))

    mc_result = None
    if req.monte_carlo is not None:
        mc = req.monte_carlo
        mc_result = monte_carlo(
            company,
            req.assumptions,
            iterations=mc.iterations,
            revenue_growth_std=mc.revenue_growth_std,
            operating_margin_std=mc.operating_margin_std,
            terminal_growth_std=mc.terminal_growth_std,
            wacc_std=mc.wacc_std,
            seed=mc.seed,
        )

    sens_result = None
    if req.sensitivity is not None:
        s = req.sensitivity
        sens_result = sensitivity_grid(
            company,
            req.assumptions,
            revenue_growth_min=s.revenue_growth_min,
            revenue_growth_max=s.revenue_growth_max,
            revenue_growth_steps=s.revenue_growth_steps,
            operating_margin_min=s.operating_margin_min,
            operating_margin_max=s.operating_margin_max,
            operating_margin_steps=s.operating_margin_steps,
        )

    return ValuationResponse(
        projection=projection,
        monte_carlo=mc_result,
        sensitivity=sens_result,
    )


@app.get("/comps/{ticker}", response_model=CompsResponse)
async def comps(ticker: str) -> CompsResponse:
    """Return peer trading multiples for the workspace's cross-check panel.

    Independent of the /extract cache — peer market data lives outside our
    extraction pipeline and can be fetched any time. Falls back gracefully
    to an empty peers list if Yahoo Finance is unreachable rather than
    failing the request.
    """
    return await get_peer_comps(normalize_ticker(ticker))
