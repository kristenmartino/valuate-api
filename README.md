# valuate-api

> Backend for **Valuate** — AI-augmented DCF agent over SEC 10-K filings.
> Live at [valuate.kristenmartino.ai](https://valuate.kristenmartino.ai)
> · Frontend repo: [valuate-web](https://github.com/kristenmartino/valuate-web)
> · Case study: [kristenmartino.ai/work/valuate](https://kristenmartino.ai/work/valuate)

The case study has the full design narrative; this README is a working reference for the code.

## What this service does

```
POST /extract { ticker } ──▶ Company (cached server-side)
                              │
                              ├──▶ GET  /company/{ticker}                    read cached
                              ├──▶ PUT  /company/{ticker}/override           HITL correction
                              ├──▶ GET  /value/{ticker}/defaults             starting Assumptions
                              ├──▶ POST /value/{ticker}                      DCF + MC + sensitivity
                              └──▶ GET  /comps/{ticker}                      peer multiples
```

A single FastAPI app serving a LangGraph state machine that extracts financial line items from a company's most recent 10-K, lets a reviewer override flagged extractions, then computes a 5-year DCF projection plus 10K-iteration Monte Carlo and a 7×7 sensitivity grid.

## Pipeline

The graph (`graph.py`) runs `ingest → track_a → track_b → validate → END`:

1. **Ingest** — `EdgarClient` fetches the latest 10-K's metadata, the XBRL company-facts JSON, and the filing's primary HTML URL. Rate-limited to SEC's 10 req/s limit.
2. **Track A — XBRL** (`extract_track_a.py`). Walks `CANONICAL_CONCEPTS` against the company-facts JSON. Returns a flat dict of LineItems for the **3 most-recent fiscal years** (XBRL company-facts already carries every year the filer has tagged, so multi-period costs zero extra HTTP). Missing concepts come back as `None`; never raises.
3. **Track B — Claude** (`extract_track_b.py`). For the *latest* period only, asks Claude (`claude-sonnet-4-6`, prompt-cached system prompt) to fill any fields Track A left blank, plus extract **revenue by segment** if the filer reports it. Every value carries a verbatim source quote and a confidence score.
4. **Derivation backstop** (in `graph.py`). For fields neither track filled, applies accounting-identity fallbacks:
   - `operating_income ≈ income_before_tax + interest_expense` (handles JNJ, NKE)
   - `total_liabilities = total_assets − shareholders_equity` (handles NKE, KO)

   Both write `source=DERIVED` with a synthetic source quote.
5. **Composition** — builds a `Company` with up to 3 `FinancialPeriod`s. The latest period must be complete or `CompositionError` raises (HTTP 422). Older periods with thin coverage are silently dropped from the response.
6. **Validate** — flags low-confidence items (<0.80) and balance-sheet identity violations (>50bps tolerance) as `ExtractionFlag`s on the response.

## HITL overrides

`PUT /company/{ticker}/override` accepts `{ field_path, value, source_quote? }` and replaces the LineItem at the given path with `source=USER_OVERRIDE`. Validation re-runs after every override so flags reflect the new state.

Two repos back this:

- **InMemoryRepo** (default in local dev) — process-local dict, wiped on restart.
- **PostgresRepo** — turns on automatically when `DATABASE_URL` is set. One JSONB-backed `companies` table; the override audit trail lives inside the Company JSON itself (every overridden LineItem keeps its history via `source` + `source_quote`).

## Tech stack

- **Python 3.11**, FastAPI + LangGraph
- **SEC EDGAR** for both XBRL company-facts and 10-K HTML
- **Anthropic SDK** (`claude-sonnet-4-6`) with prompt caching on the static system prompt
- **BeautifulSoup + lxml** for slicing the 10-K's Item 8 financial-statements section before sending to Claude
- **yfinance** for peer market multiples (no API key, runs in a thread pool)
- **asyncpg** for Postgres persistence (optional)

## Local development

```bash
git clone https://github.com/kristenmartino/valuate-api
cd valuate-api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in SEC_USER_AGENT and ANTHROPIC_API_KEY
uvicorn app.main:app --reload
```

The server listens on `http://127.0.0.1:8000` by default. Without `DATABASE_URL` it uses InMemoryRepo, which means overrides are lost when you restart `uvicorn` — that's fine for local exploration.

### Running the tests

```bash
pytest tests/
```

16 tests cover the bugs and edge cases that bit during development:

- `latest_value_per_period` keying by `end` date rather than the filing's `fy` (a 10-K filed for FY2025 reports comparative income statements for FY2024 and FY2023, all tagged `fy=2025`; grouping by `fy` collides three years of data into one slot)
- restatement dedup picks the higher-accession version
- alternate-tag fall-through with confidence 0.95 vs primary 1.0
- missing concepts return `None`, never raise
- the DERIVED fallbacks (op income from IBT + interest, total liabilities from the balance-sheet identity)
- `_recent_period_ends` ordering and anchor clipping
- `_compose_company` silently drops older periods with required-field gaps but raises on the latest
- `default_assumptions` averages ratios across the multi-year window and estimates `revenue_growth` from observed CAGR

## Deployment

The service runs on Railway. The repo is set up for one-click deploy:

- `Procfile` runs `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- `railway.toml` sets the healthcheck path to `/healthz` and the restart policy
- `runtime.txt` pins Python 3.11.10

Required env vars (set in the Railway project UI):

| Variable | Required | Notes |
|---|---|---|
| `SEC_USER_AGENT` | yes | SEC blocks requests without one. Format: `"Your Name your.email@domain.com"` |
| `ANTHROPIC_API_KEY` | yes | Track B and segment extraction need it; `sk-ant-...` from `console.anthropic.com/settings/keys` |
| `DATABASE_URL` | optional | Auto-injected by Railway's Postgres plugin. Without it, persistence falls back to in-memory. |

## Universe

10 hand-picked S&P 500 tickers: AAPL, MSFT, GOOGL, NVDA, COST, HD, NKE, JNJ, KO, CAT.

Three of those needed Track B or DERIVED fallback to compose successfully — XBRL tagging consistency is worse than the universe size suggests. The two-track-plus-derivation architecture earns its keep on this universe.

## Scope ceiling

The universe is intentional. Banks, insurers, REITs, and energy E&P companies report on fundamentally different financial-statement structures, and pretending one extraction logic works for all of them is the standard demo's failure mode. Expansion to those filers is tracked as [issue #4](https://github.com/kristenmartino/valuate-api/issues/4).

Other items deliberately parked in the [`later` label](https://github.com/kristenmartino/valuate-api/issues?q=label%3Alater): segment-aware DCF (currently consolidated only), comparable-company DCF cross-check, multi-period filing-accession attribution, saved scenarios.

## License

MIT
