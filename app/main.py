"""Valuate API — FastAPI service for the DCF extraction agent.

Phase 4: adds HITL review endpoints. The /extract result is cached in an
in-process dict keyed by ticker; subsequent calls to GET /company/{ticker}
or PUT /company/{ticker}/override read and mutate that cached state.

Note: in-memory storage means state is lost on restart. The README's V1
explicitly excludes save/share, so this is fine for the MVP.

Run locally:
    SEC_USER_AGENT="Your Name you@example.com" \
    ANTHROPIC_API_KEY="sk-ant-..." \
    uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Optional

from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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
_companies: dict[str, Company] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _graph
    _graph = build_graph(EdgarClient(), AsyncAnthropic())
    yield
    _graph = None
    _companies.clear()


app = FastAPI(title="Valuate API", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/extract", response_model=Company)
async def extract(req: ExtractRequest) -> Company:
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
    _companies[req.ticker.upper()] = company
    return company


@app.get("/company/{ticker}", response_model=Company)
async def get_company(ticker: str) -> Company:
    company = _companies.get(ticker.upper())
    if company is None:
        raise HTTPException(
            404, f"No extraction found for {ticker}. POST /extract first."
        )
    return company


@app.put("/company/{ticker}/override", response_model=Company)
async def override(ticker: str, req: OverrideRequest) -> Company:
    ticker_upper = ticker.upper()
    company = _companies.get(ticker_upper)
    if company is None:
        raise HTTPException(
            404, f"No extraction found for {ticker}. POST /extract first."
        )
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
    _companies[ticker_upper] = updated
    return updated


@app.get("/value/{ticker}/defaults", response_model=Assumptions)
async def value_defaults(ticker: str) -> Assumptions:
    company = _companies.get(ticker.upper())
    if company is None:
        raise HTTPException(
            404, f"No extraction found for {ticker}. POST /extract first."
        )
    return default_assumptions(company)


@app.post("/value/{ticker}", response_model=ValuationResponse)
async def value(ticker: str, req: ValuationRequest) -> ValuationResponse:
    company = _companies.get(ticker.upper())
    if company is None:
        raise HTTPException(
            404, f"No extraction found for {ticker}. POST /extract first."
        )
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
