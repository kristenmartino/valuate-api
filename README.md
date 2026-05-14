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

1. **Ingest** — `EdgarClient` fetches the latest 10-K's metadata, the XBRL company-facts JSON, and the filing's primary HTML URL. The SIC code from the SEC submissions response routes the rest of the pipeline through the right industry path (`industry.py` → `Industry.STANDARD` for industrials/tech, `Industry.BANK` for depositories, `Industry.INSURER` for insurance carriers, `Industry.REIT` for real estate trusts, `Industry.ENERGY` for oil & gas E&P / refining). Rate-limited to SEC's 10 req/s limit.
2. **Track A — XBRL** (`extract_track_a.py`). Walks the industry-specific concept map (`STANDARD_CANONICAL_CONCEPTS` for industrials, `BANK_CANONICAL_CONCEPTS` for banks — banks tag net interest income, loans, deposits, etc. that don't exist in the standard schema; `REIT_CANONICAL_CONCEPTS` adds the real-estate-at-cost / accumulated-depreciation contra-asset / real-estate-net trio that REITs report on the balance sheet). Returns a flat dict of LineItems for the **3 most-recent fiscal years** (XBRL company-facts already carries every year the filer has tagged, so multi-period costs zero extra HTTP). Missing concepts come back as `None`; never raises.
3. **Track B — Claude** (`extract_track_b.py`). For the *latest* period only, asks Claude (`claude-sonnet-4-6`, prompt-cached system prompt) to fill any fields Track A left blank, plus extract **revenue by segment** if the filer reports it. Every value carries a verbatim source quote and a confidence score.
4. **Derivation backstop** (in `graph.py`). For fields neither track filled, applies accounting-identity fallbacks:
   - `operating_income ≈ income_before_tax + interest_expense` (handles JNJ, NKE)
   - `total_liabilities = total_assets − shareholders_equity` (handles NKE, KO)
   - `real_estate_net = real_estate_at_cost − accumulated_depreciation` (REIT-only, for filers that tag the components but not the net)

   All write `source=DERIVED` with a synthetic source quote.
5. **Composition** — builds a `Company` with up to 3 `FinancialPeriod`s, dispatching to the right schema variants per the industry: `IncomeStatement` / `BalanceSheet` / `CashFlowStatement` for standard *and* energy filers (E&P companies report on the same shape as industrials — what differs is the *valuation*, not the line items), `BankIncomeStatement` / `BankBalanceSheet` / `BankCashFlowStatement` for banks, the equivalent `Insurance*` triples for insurers, and `REIT*` triples for REITs. Pydantic discriminated unions on each statement (`kind` literal) keep the JSON shape unambiguous on the wire. The latest period must be complete or `CompositionError` raises (HTTP 422); older periods with thin coverage are silently dropped.
6. **Validate** — flags low-confidence items (<0.80) and balance-sheet identity violations (>50bps tolerance) as `ExtractionFlag`s on the response.

## Valuation flavors

`compute_projection` in `dcf.py` dispatches by industry:

- **Standard** (industrials / tech): 5-year FCFF DCF with Gordon-growth terminal, plus 10K-iteration Monte Carlo and a 7×7 sensitivity grid over (revenue growth × operating margin).
- **Bank**: Gordon dividend discount model — `P = D₀(1 + g) / (r − g)`, where `wacc` is reinterpreted as cost of equity and `terminal_growth` as long-term dividend growth. The default `g` is observed dividend CAGR capped 200bps under default `r` so the Gordon constraint holds out of the box.
- **Insurer**: justified price-to-book — `P/B = (ROE − g) / (r − g)`, then `fair_value/share = book_value/share × P/B`. Reserves and the general-account investment portfolio dominate the balance sheet, so book value is the economic anchor.
- **REIT**: FFO-multiple Gordon growth — `fair_value/share = FFO/share × (1 + g) / (r − g)`, where `FFO = net income + D&A`. GAAP depreciation overstates economic depreciation for well-maintained real estate, so FFO is the conventional pre-distribution earnings measure REIT analysts anchor on.
- **Energy E&P**: 10-year reserve-life-capped FCFF, **no terminal value**. Reserves deplete; Gordon-growth-to-infinity is conceptually wrong for an asset that will run out. `revenue_growth` is reinterpreted as production growth/decline. E&P is the only flavor that doesn't require a separate schema variant — the line items are standard us-gaap, what differs is the valuation math.

Monte Carlo runs for all five flavors (degenerate axes are simply unsampled); sensitivity is hidden client-side for bank/insurer/REIT paths because the grid axes (revenue growth × operating margin) don't enter their formulas. For energy E&P the heatmap IS shown — the FCFF math still uses both axes, just without a terminal value.

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

34 tests across three files:

- `test_extraction.py` (23) — bugs that bit during development plus per-industry valuation math
- `test_auth_and_rate_limit.py` (10) — bearer-token auth on `/override` and the IP rate limiter for `/extract`
- `test_integration.py` (1, network-gated) — end-to-end against a real AAPL 10-K

The default `pytest tests/` skips the network-gated test. Run it explicitly with `pytest tests/ -m network` (requires `SEC_USER_AGENT`). The intent is that a weekly Railway cron runs it as a deploy health check.

The extraction tests cover:

- `latest_value_per_period` keying by `end` date rather than the filing's `fy` (a 10-K filed for FY2025 reports comparative income statements for FY2024 and FY2023, all tagged `fy=2025`; grouping by `fy` collides three years of data into one slot)
- restatement dedup picks the higher-accession version
- alternate-tag fall-through with confidence 0.95 vs primary 1.0
- missing concepts return `None`, never raise
- the DERIVED fallbacks (op income from IBT + interest, total liabilities from the balance-sheet identity, REIT real-estate-net from at-cost minus accumulated depreciation)
- `_recent_period_ends` ordering and anchor clipping
- `_compose_company` silently drops older periods with required-field gaps but raises on the latest
- `default_assumptions` averages ratios across the multi-year window and estimates `revenue_growth` from observed CAGR
- `classify_sic` routes the five industry buckets (and rejects malformed input)
- per-industry valuation formulas match hand-computed expected values: Gordon DDM for banks (with `r > g` constraint enforcement), justified P/B for insurers, FFO-multiple Gordon for REITs, and 10-year FCFF with zero terminal value for energy E&P

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
| `VALUATE_OVERRIDE_TOKEN` | optional | When set, `PUT /company/{ticker}/override` requires `Authorization: Bearer <token>`. The Vercel frontend injects the same token via its `proxy.ts` from a same-named env var. When unset, `/override` runs unauthenticated (fine for local dev). |
| `VALUATE_EXTRACT_RATE_LIMIT` | optional | IP rate limit on `POST /extract`, format `<count>/<window-seconds>`. Default `10/3600`. Only `/extract` is limited (it's the Anthropic-burning endpoint); read endpoints aren't. |

### Auth + rate-limit model

The threat model is "random scraping / accidental corruption," not "sophisticated adversary." `/override` is the only destructive endpoint and is gated by a single shared bearer token; `/extract` is rate-limited by IP because each call costs real Anthropic credits. A production deployment would put both behind real per-user auth; the case study acknowledges this.

Local dev with both env vars unset behaves identically to the pre-auth era — useful for poking at the override flow without setting up tokens.

## Universe

14 hand-picked S&P 500 tickers — 10 industrial / tech filers (AAPL, MSFT, GOOGL, NVDA, COST, HD, NKE, JNJ, KO, CAT), one bank (JPM), one life insurer (PRU), one industrial REIT (PLD), and one pure-play E&P (EOG). Three of the original ten needed Track B or DERIVED fallback to compose successfully; JPM, PRU, PLD, and EOG all extracted cleanly through Track A alone — their per-industry XBRL tags are well-standardized — so the multi-industry architecture earns its keep on filers it wasn't originally designed for.

## Industry coverage

| Industry | Status | Valuation method | Sample ticker |
|---|---|---|---|
| Industrial / tech | shipped | 5-year FCFF DCF + Monte Carlo + sensitivity | AAPL, MSFT, ... |
| Banks | shipped | Gordon DDM | JPM |
| Insurers | shipped | Justified P/B | PRU |
| REITs | shipped | FFO-multiple Gordon growth | PLD |
| Energy E&P | shipped | 10-year reserve-life-capped FCFF (no terminal) | EOG |

E&P is the only industry that doesn't carry a separate schema variant — the line items E&P companies report (revenue, op income, capex, D&A) are standard us-gaap; the conceptual difference is the *valuation*, not the data shape. The architecture supports both: schema variants when the line-item set fundamentally differs (banks, insurers, REITs), and dispatch-only when only the valuation differs (E&P). Anything classified outside these five falls back to `Industry.STANDARD` and runs the FCFF path with Gordon terminal — which produces nonsense for filers it shouldn't apply to. The home page surfaces the 14 curated tickers as the primary entry point and a free-text search box as an escape hatch; the search copy makes the fallback caveat explicit.

Other items deliberately parked in the [`later` label](https://github.com/kristenmartino/valuate-api/issues?q=label%3Alater): segment-aware DCF (currently consolidated only), multi-period filing-accession attribution, saved scenarios.

## License

MIT
