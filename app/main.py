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

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Optional

from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

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
from graph import CompositionError, build_graph, validate_company
from overrides import apply_override
from repository import CompanyRepo, make_repo
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
    iterations: int = 10_000
    revenue_growth_std: float = 0.02
    operating_margin_std: float = 0.02
    terminal_growth_std: float = 0.005
    wacc_std: float = 0.005
    seed: Optional[int] = None


class SensitivityParams(BaseModel):
    revenue_growth_min: Optional[float] = None
    revenue_growth_max: Optional[float] = None
    revenue_growth_steps: int = 7
    operating_margin_min: Optional[float] = None
    operating_margin_max: Optional[float] = None
    operating_margin_steps: int = 7


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
async def version() -> dict[str, str | None]:
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
    company = await _require_repo().get(ticker)
    if company is None:
        raise HTTPException(
            404, f"No extraction found for {ticker}. POST /extract first."
        )
    return company


@app.post("/extract", response_model=Company)
async def extract(
    req: ExtractRequest,
    _ratelimit: None = Depends(require_extract_rate_limit),
) -> Company:
    if _graph is None:
        raise HTTPException(503, "Service not ready")
    try:
        result = await _graph.ainvoke({"ticker": req.ticker})
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
    company = await _require_company(ticker)
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
    return default_assumptions(company)


@app.post("/value/{ticker}", response_model=ValuationResponse)
async def value(ticker: str, req: ValuationRequest) -> ValuationResponse:
    company = await _require_company(ticker)
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
    return await get_peer_comps(ticker.upper())
