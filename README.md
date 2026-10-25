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
                              ├──▶ PUT  /company/{ticker}/override           HITL correction (Bearer-token auth)
                              ├──▶ GET  /value/{ticker}/defaults             starting Assumptions
                              ├──▶ POST /value/{ticker}                      DCF + MC + sensitivity
                              └──▶ GET  /comps/{ticker}                      peer multiples
GET /healthz                                                                liveness probe
GET /version                                                                commit SHA + prompt hash + start time
```

A FastAPI app serving a LangGraph state machine for SEC filing extraction, flagged-value corrections, and five simplified valuation models. The workspace reads saved company records when available; uncached tickers require extraction and can fail when required values remain missing. Saved records do not establish filing freshness. Standard and energy models include a sensitivity grid; all models support Monte Carlo simulation.

Ticker aliases are trimmed, uppercased, and normalized to the SEC/Yahoo hyphen form (`BRK.B` → `BRK-B`) for lookup, extraction state, repository keys, and comps. Dot and hyphen requests share saved records and corrections. A resolved ticker can still fail composition when required financial values are missing; normalization does not establish model coverage.

## Pipeline

The graph (`graph.py`) runs `ingest → track_a → track_b → validate → END`:

1. **Ingest** — `EdgarClient` fetches the latest 10-K's metadata, the XBRL company-facts JSON, and the filing's primary HTML URL. The SIC code from the SEC submissions response routes the rest of the pipeline through the right industry path (`industry.py` → `Industry.STANDARD` for industrials/tech, `Industry.BANK` for depositories, `Industry.INSURER` for insurance carriers, `Industry.REIT` for real estate trusts, `Industry.ENERGY` for oil & gas E&P / refining). Rate-limited to SEC's 10 req/s limit.
2. **Track A — XBRL** (`extract_track_a.py`). Walks the industry-specific concept map (`STANDARD_CANONICAL_CONCEPTS` for industrials, `BANK_CANONICAL_CONCEPTS` for banks — banks tag net interest income, loans, deposits, etc. that don't exist in the standard schema; `REIT_CANONICAL_CONCEPTS` adds the real-estate-at-cost / accumulated-depreciation contra-asset / real-estate-net trio that REITs report on the balance sheet). Returns a flat dict of LineItems for the **5 most-recent fiscal years** (XBRL company-facts already carries every year the filer has tagged, so multi-period costs zero extra HTTP). Missing concepts come back as `None`; never raises.
3. **Track B — Claude** (`extract_track_b.py`). For the *latest* period only, asks Claude (`claude-sonnet-4-6`, prompt-cached system prompt) to fill any fields Track A left blank, plus extract **revenue categories** if the filer reports them. CRM instead reads its complete revenue breakdown from tagged filing contexts. AI values carry a model-reported score and a supporting excerpt where available. The score is not a calibrated probability of correctness; the excerpt must be checked against the filing.
4. **Derivation backstop** (in `graph.py`). For fields neither track filled, applies accounting-identity fallbacks:
   - `operating_income ≈ income_before_tax + interest_expense` (handles JNJ, NKE)
   - `total_liabilities = total_assets − shareholders_equity − noncontrolling_interests` (the last component is subtracted only for parent-equity coverage) (handles NKE, KO)
   - `real_estate_net = real_estate_at_cost − accumulated_depreciation` (REIT-only, for filers that tag the components but not the net)

   All write `source=DERIVED` with a synthetic source quote.
5. **Composition** — builds a `Company` with up to 5 `FinancialPeriod`s, dispatching to the right schema variants per the industry: `IncomeStatement` / `BalanceSheet` / `CashFlowStatement` for standard *and* energy filers (E&P companies report on the same shape as industrials — what differs is the *valuation*, not the line items), `BankIncomeStatement` / `BankBalanceSheet` / `BankCashFlowStatement` for banks, the equivalent `Insurance*` triples for insurers, and `REIT*` triples for REITs. Pydantic discriminated unions on each statement (`kind` literal) keep the JSON shape unambiguous on the wire. The latest period must be complete or `CompositionError` raises (HTTP 422); older periods with thin coverage are silently dropped.
6. **Validate** — flags low-confidence items (<0.80) and balance-sheet identity violations (>50bps tolerance) as `ExtractionFlag`s on the response.

## Valuation flavors

`compute_projection` in `dcf.py` dispatches by industry:

- **Standard** (industrials / tech): 5-year FCFF DCF with Gordon-growth terminal, plus 10K-iteration Monte Carlo and a 7×7 sensitivity grid over (revenue growth × operating margin).
- **Bank**: Gordon dividend discount model — `P = D₀(1 + g) / (r − g)`, where `wacc` is reinterpreted as cost of equity and `terminal_growth` as long-term dividend growth. The default `g` is observed dividend CAGR capped 200bps under default `r` so the Gordon constraint holds out of the box.
- **Insurer**: justified price-to-book — `P/B = (ROE − g) / (r − g)`, then `fair_value/share = book_value/share × P/B`. Reserves and the general-account investment portfolio dominate the balance sheet, so book value is the economic anchor.
- **REIT**: Gordon growth using approximate FFO — `fair_value/share = FFO/share × (1 + g) / (r − g)`, where approximate `FFO = net income + D&A`. This omits other Nareit adjustments. The supplementary estimated AFFO subtracts positive reported capex, or uses 80% of approximate FFO otherwise. These are simplified calculations, not company-reported FFO/AFFO.
- **Energy E&P**: fixed ten-year FCFF projection, **no terminal value**. `revenue_growth` changes projected revenue. The model does not estimate company-specific reserve life, production volumes, or commodity prices. The line items use the standard statement schema.

Monte Carlo runs for all five models; sensitivity is hidden client-side for bank/insurer/REIT paths because the grid axes (revenue growth × operating margin) don't enter their formulas. For energy the grid uses revenue growth and operating margin, without terminal value. DCF-implied multiples appear only for standard and energy models; other comps tables provide market context.

The response retains the full-range `histogram` and adds an optional `central_histogram`: bins recalculated from actual samples between p10 and p90, with `lower_bound`, `upper_bound`, `below_count`, and `above_count`. The workspace defaults to this window and offers a full-range view. Counts disclose the excluded tails; all summary statistics still use every completed sample. This is a display change, not a change to sampling or valuation formulas.

## Berkshire filing support

`BRK.B` / `BRK-B` now produces a simplified justified-P/B valuation; `BRK.A` / `BRK-A` uses its own equivalent share basis. Berkshire is a diversified conglomerate: this reuses the insurer formula with consolidated GAAP earnings and Berkshire shareholders' equity, not a sum-of-the-parts model. Investment gains can distort historical ROE. Per-share amounts use annual average equivalent shares rather than year-end shares.

The SEC company-facts feed omits the required dimensioned facts. `berkshire.py` reads the annual filing's inline XBRL before Claude backfill:

- `WeightedAverageNumberOfSharesOutstandingBasic` with the requested `EquivalentClassA/BMember`. These are reported equivalent shares, not generic diluted shares. The existing wire field `diluted_shares_outstanding` carries them with an explicit source note and `Company.share_count_label` for display.
- `PremiumsEarnedNet` for `InsuranceAndOtherMember`.
- Cash from `InsuranceAndOtherMember` plus `RailroadUtilitiesAndEnergyMember`, labeled as a calculation. Treasury bills already classified as cash equivalents remain included. Separately reported restricted cash and short-term Treasury-bill investments are excluded; parent-only cash is not added again.

Facts must match Berkshire's CIK, annual dates (or year-end cash instant), exact dimension set, and units. Scales/signs are applied; matching repeats are deduplicated and conflicts remain missing. Claude cannot fill these three fields if the tagged contexts are unavailable. Missing required data still fails explicitly.

Regression fixtures pin selected public facts from the [FY2025 SEC 10-K](https://www.sec.gov/Archives/edgar/data/1067983/000119312526083899/brka-20251231.htm): Class B average equivalent shares 2,157,335,139; premiums $88.902 billion; cash $51.877 billion ($47.719 billion + $4.158 billion). This targeted regression coverage is separate from the historical extraction baseline below; the May baseline has not been rerun.

## Balance-sheet reconciliation

Parent stockholders' equity and `noncontrolling_interests` are separate fields. The check adds reported noncontrolling interests when equity uses `us-gaap:StockholdersEquity`; it does not add them again when equity uses the consolidated-equity tag. Unknown equity coverage is not assumed to be parent-only. Equity corrections keep the original concept as their coverage basis while retaining `source=user_override`.

The optional noncontrolling-interest component comes only from `us-gaap:MinorityInterest` USD instant facts matching the company's CIK, annual filing accession, and period end. Duplicate equal facts are accepted; conflicts, wrong periods/units/entities, and duration facts are rejected. Claude does not fill this component.

For an older saved record with a material parent-equity gap and no component, a read attempts the same-filing backfill, adds only the missing component, and reruns validation. Existing financial amounts and correction notes are preserved. SEC failures leave the saved data and warning intact; unsuccessful attempts are retried no more than once per five minutes per process. This is a targeted compatibility upgrade, not a filing-freshness refresh.

Prologis's FY2025 filing reports assets $98.724256B, liabilities $40.970248B, parent equity $53.193178B, and noncontrolling interests $4.560830B. The last component resolves its false warning without replacing any of the first three amounts or changing valuation inputs. See the [SEC filing](https://www.sec.gov/Archives/edgar/data/1045609/000119312526051453/pld-20251231.htm). The regression fixtures pin these selected facts; they do not expand the historical May extraction baseline.

## Salesforce statement coverage

CRM uses filing-tagged noncurrent debt (`LongTermDebtNoncurrent`) rather than debt including current maturities, the cash-flow D&A total (`DepreciationAndAmortization`) rather than fixed-asset D&A alone, and the reported combined payables/accruals concept. The frontend labels the combined liability coverage explicitly instead of calling it standalone accounts payable. Unknown payables coverage stays unspecified.

`salesforce.py` applies these selections before AI fallback and repairs older machine-extracted CRM records on read, preserving user-corrected amounts and notes, including the reviewed SG&A field. It checks Salesforce's CIK, USD units, annual dates, annual filing form, and accession. Latest-period facts must match the saved filing; older periods prefer its comparatives and may use earlier annual filings, whose accessions are recorded in each source note. Conflicting facts remain unavailable. Failed repairs retain saved amounts, expose known latest-period debt/D&A coverage flags, and retry no more than once per five minutes per process.

The [FY2026 filing](https://www.sec.gov/Archives/edgar/data/1108524/000110852426000060/crm-20260131.htm) reports noncurrent debt $10.439B and current debt $4.000B, cash-flow D&A $3.631B, and combined payables/accruals $8.253B. Fixtures also cover the historical D&A series used by the model. These targeted tests do not rerun or expand the historical May extraction baseline. Correcting debt affects both the net-debt bridge and default WACC; correcting historical D&A changes the default D&A/revenue ratio.

### Salesforce revenue breakdown

`crm_revenue.py` reads five subscription and support service offerings plus professional services and other from the saved annual filing's inline XBRL. It selects Salesforce's entity, annual duration, USD units, revenue concept, and exact product/service dimension members. Equal repeated facts are deduplicated; conflicting, missing, wrong-period, wrong-unit, and differently dimensioned facts are rejected.

The five offerings must equal the reported subscription/support subtotal, and that subtotal plus professional services must equal both the filing's consolidated total and the saved revenue amount. The subtotal is not added as another category, and no residual is invented. For FY2026, $39.388B + $2.137B = $41.525B. These are revenue categories, not separate operating segments; Salesforce reports one operating segment.

New CRM extractions bypass the AI revenue-breakdown call. Older partial records are repaired on read using the saved accession, without changing financial fields, corrections, assumptions, or valuation. Failed repairs leave the prior display and its reconciliation warning intact and retry no more than once per five minutes per process. If a new filing lacks the complete tagged breakdown, the optional breakdown remains unavailable rather than being filled with guessed categories. These targeted checks are separate from the historical May extraction baseline.

## HITL overrides

`PUT /company/{ticker}/override` accepts `{ field_path, value, source_quote? }` and replaces the LineItem at the given path with `source=USER_OVERRIDE`. Validation re-runs after every override so flags reflect the new state.

Two repos back this:

- **InMemoryRepo** (default in local dev) — process-local dict, wiped on restart.
- **PostgresRepo** — turns on automatically when `DATABASE_URL` is set. One JSONB-backed `companies` table; an override is recorded inline on the Company JSON — the corrected LineItem is replaced by one tagged `source=user_override` (confidence 1.0) carrying the reviewer's `source_quote`. It's a current-state snapshot, not a change-history log.

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

131 collected tests across eleven files (the network integration test is skipped by default):

- `test_extraction.py` (26) — bugs that bit during development plus per-industry valuation math
- `test_dcf.py` (7) — standard FCFF DCF, Monte Carlo, and sensitivity-grid math
- `test_overrides.py` (10) — HITL override across every industry variant, with multi-period history preserved
- `test_auth_and_rate_limit.py` (12) — bearer-token auth on `/override` and the IP rate limiter for `/extract` (incl. X-Forwarded-For spoof resistance)
- `test_histogram_view.py` (4) — central-window binning, ties, sample accounting, and empty simulations
- `test_equity_reconciliation.py` (22) — Prologis filing regression, parent versus consolidated equity, preserved values/results, same-filing backfill, unsafe-context rejection, and cached-record upgrade/failure behavior
- `test_ticker_aliases.py` (7) — SEC lookup aliases, shared repository/value/correction state, and canonical Postgres keys
- `test_berkshire.py` (10) — filing-backed Class A/B shares, premiums, cash aggregation, unsafe-context rejection, composition, and P/B simulation
- `test_salesforce.py` (13) — filing-backed debt/D&A/payables coverage, five-period repair, valuation effects, review preservation, unsafe-fact rejection, and cached repair/failure behavior
- `test_crm_revenue.py` (19) — complete six-category reconciliation, subtotal exclusion, unsafe-context rejection, saved-record repair, unchanged valuation/reviews, and deterministic fresh extraction
- `test_integration.py` (1, network-gated) — end-to-end against a real AAPL 10-K

The default `pytest tests/` skips the network-gated test. Run it explicitly with `pytest tests/ -m network` (requires `SEC_USER_AGENT`). It's intended as a periodic deploy health check, but is run manually today — there's no scheduled CI job wired up yet.

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
| `VALUATE_DISABLE_STRUCTURED_LOGGING` | optional | Set to `1` to skip the JSON-per-request middleware (`app/logging_middleware.py`). Useful for local dev when reading plain Python tracebacks in stderr is preferable. |
| `SENTRY_DSN` | optional | When set, FastAPI exceptions get reported to Sentry. Requires `pip install sentry-sdk[fastapi]` (commented out in `requirements.txt`; uncomment to bake in). Off by default; lazy-imports the SDK so the dep is opt-in. |

### Observability

- **`/version`** returns the running commit SHA (from Railway's auto-injected `RAILWAY_GIT_COMMIT_SHA`), deployment ID, service start timestamp, environment name, and the **system-prompt sha256** — a "did the deployed prompt change?" diagnostic distinct from the commit SHA, since the prompt can change without a code commit (e.g. after a prompt-eval iteration).
- **Structured request logging** (`app/logging_middleware.py`) emits one JSON line per request to stdout with `request_id`, `method`, `path`, `status`, `duration_ms`, `client_ip`. The `X-Request-ID` header is set on every response so error reports are grep-able. Skipped via `VALUATE_DISABLE_STRUCTURED_LOGGING=1`.
- **Optional Sentry** (`app/sentry_setup.py`) activates when `SENTRY_DSN` is set in the env. Lazy-imports `sentry-sdk` so it's truly opt-in.

### Extraction eval

`eval/` holds hand-pinned, SEC-sourced ground-truth values spanning all five industry categories — standard/tech (AAPL, MSFT, GOOGL, AMZN, NKE, KO), banks (JPM, BAC), insurer (PRU), REIT (PLD), and energy E&P (EOG). The runner scores **both** extraction tracks per field within ±0.5% tolerance:

- **Track A (XBRL)** — `extract_track_a` over the filer's company-facts, for fields in the industry's canonical concept map.
- **Track B (Claude)** — `extract_track_b` reading the filing HTML, for fields XBRL doesn't cover (e.g. `income_before_tax` / `income_tax_expense` on standard filers).

Each field resolves to `PASS` / `FAIL` / `REFRESH` (filer rolled to a fiscal year newer than the pinned ground truth — a refresh is needed, not an extraction bug) / `SKIP` (the field's track wasn't run).

```bash
SEC_USER_AGENT="..." ANTHROPIC_API_KEY="..." python -m eval.run_eval
#   --readme         emit the baseline table below      --json    machine-readable
#   --tickers AAPL,JPM   run a subset                    --track-a-only   XBRL only, no API key
```

Exit codes: `0` all pass, `1` a real extraction mismatch, `2` no fails but a ground-truth refresh is needed (so a cron can tell "model regressed" from "human action needed"). Intended use: run before merging changes to `extraction_prompt.py`, and on a cadence to catch model-version regressions.

### Extraction eval baseline

Last refreshed: 2026-05-30

| Scope | Tickers | Fields | Accuracy |
|---|---:|---:|---:|
| XBRL Track A | 11 | 39 | 100.0% |
| Claude Track B | 3 | 4 | 75.0% |
| Overall | 11 | 43 | 97.7% |

Measured across 11 filers in five industries. Track A (XBRL) reproduces every filer's officially-tagged value for all 39 canonical-concept fields within ±0.5%. Track B (Claude) covers the four income-tax fields the XBRL concept map intentionally leaves to the LLM (`income_before_tax` / `income_tax_expense`): 3 of 4 within ±0.5%. The single miss is an *omission*, not a wrong number — Claude returned no value for MSFT's `income_before_tax` (`not_extracted`), a field that isn't required for the DCF. (Refresh these figures with `python -m eval.run_eval --readme`.)

This is an eval baseline over a curated public-filer set, not a guarantee across all SEC filers.

### Auth + rate-limit model

The threat model is "random scraping / accidental corruption," not "sophisticated adversary." `/override` is the only destructive endpoint and is gated by a single shared bearer token; `/extract` is rate-limited by IP because each call costs real Anthropic credits. A production deployment would put both behind real per-user auth.

Local dev with both env vars unset behaves identically to the pre-auth era — useful for poking at the override flow without setting up tokens.

## Universe

18 hand-picked S&P 500 tickers — 14 industrial / tech filers (AAPL, MSFT, GOOGL, AMZN, META, NVDA, COST, WMT, HD, NKE, JNJ, KO, F, CAT), one bank (JPM), one life insurer (PRU), one industrial REIT (PLD), and one pure-play E&P (EOG). Three of the original ten industrials needed Track B or DERIVED fallback to compose successfully; the four variant-industry filers (JPM, PRU, PLD, EOG) and the four later-added megacaps (AMZN, META, WMT, F) all extracted cleanly. WMT specifically needed Track B to fill `depreciation_amortization` from the filing text — exactly the case Track B was designed for.

## Industry coverage

| Industry | Status | Valuation method | Sample ticker |
|---|---|---|---|
| Industrial / tech | shipped | 5-year FCFF DCF + Monte Carlo + sensitivity | AAPL, MSFT, ... |
| Banks | shipped | Gordon DDM | JPM |
| Insurers | shipped | Justified P/B | PRU |
| REITs | shipped | FFO-multiple Gordon growth | PLD |
| Energy E&P | shipped | Fixed ten-year FCFF (no terminal) | EOG |

E&P is the only industry that doesn't carry a separate schema variant — the line items E&P companies report (revenue, op income, capex, D&A) are standard us-gaap; the conceptual difference is the *valuation*, not the data shape. The architecture supports both: schema variants when the line-item set fundamentally differs (banks, insurers, REITs), and dispatch-only when only the valuation differs (E&P). Anything classified outside these five falls back to `Industry.STANDARD` and runs the FCFF path with Gordon terminal — which produces nonsense for filers it shouldn't apply to. The home page surfaces the 18 curated tickers as the primary entry point and a free-text search box as an escape hatch; the search copy makes the fallback caveat explicit.

Other items deliberately parked in the [`later` label](https://github.com/kristenmartino/valuate-api/issues?q=label%3Alater): segment-aware DCF (currently consolidated only), multi-period filing-accession attribution, saved scenarios.

## License

MIT
