# Valuate — AI-augmented DCF agent

**Live at:** [valuate.kristenmartino.ai](https://valuate.kristenmartino.ai)

An agentic system that ingests SEC 10-K filings, extracts financial line items via Claude with human-in-the-loop verification, and produces a Monte Carlo DCF valuation.

## Why this exists

Most "AI reads financial statements" projects quietly limit themselves to the easiest cases — clean industrial mid-caps with standard reporting — without saying so. The hard part of automated valuation isn't the math, it's getting reliable structured data out of filings written for human readers. Valuate makes that scope choice explicit and builds verification into the agent flow rather than hiding extraction errors.

## Architecture

Two services, mirroring my [Sift](https://siftnews.kristenmartino.ai) news intelligence platform:

- **`valuate-web`** — Next.js 15 + TypeScript, deployed on Vercel
- **`valuate-api`** — Python FastAPI + LangGraph, deployed on Railway

The agent flow:

1. **Ingestion** — pull most recent 10-K from SEC EDGAR
2. **Extraction (Track A)** — parse XBRL company facts for canonical line items
3. **Extraction (Track B)** — fallback to Claude-based HTML extraction when XBRL is incomplete or non-standard, with source quotes and confidence scores
4. **Validation** — flag low-confidence extractions and balance-sheet inconsistencies
5. **Human-in-the-loop review** — user reviews flagged items against source quotes
6. **Modeling** — 5-year three-statement projection with assumption sliders
7. **Monte Carlo** — 10,000 iterations across revenue growth, operating margin, terminal growth, and WACC
8. **Sensitivity** — 2-D grid on revenue growth × operating margin

## Engineering decisions worth defending

**Two-track extraction.** XBRL is fast and clean when it works but inconsistently tagged across filers. Claude-based HTML extraction handles the gaps but is slower and less deterministic. Running them in that order gives the best of both: the typical case is XBRL-served and instant, while edge cases get the LLM treatment with full source attribution.

**Source quotes, not summaries.** Every Claude-extracted value carries a verbatim quote from the filing. This makes the HITL review one click, not a manual hunt — and makes the system auditable rather than a black box.

**Deliberate scope ceiling.** I capped the universe at 10 hand-picked clean-reporting tickers so I could focus engineering effort on the agent architecture and verification UX. Banks, insurers, REITs, and energy E&P are V2 problems that would dilute the MVP.

## Scope decisions

In:

- 10 S&P 500 tickers: AAPL, MSFT, GOOGL, NVDA, COST, HD, NKE, JNJ, KO, CAT
- Single most recent 10-K
- 5-year projection horizon
- 4-driver Monte Carlo
- Single-currency, single-segment

Intentionally out:

- Banks, insurers, REITs (different financial statement structures)
- Energy E&P (commodity-driven, reserve-based accounting)
- Multi-segment forecasting
- Restatement handling beyond "use most recent filing"
- Comparable company pulls
- Multi-user / save / share

## Tech stack

- **Frontend:** Next.js 15, TypeScript, Tailwind, shadcn/ui, Recharts
- **Backend:** Python 3.11, FastAPI, LangGraph, Anthropic SDK (`claude-sonnet-4-6` for extraction)
- **Data:** SEC EDGAR API (free), XBRL company facts
- **Hosting:** Vercel (web), Railway (api)

## Local development

Backend:

```bash
cd valuate-api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Frontend:

```bash
cd valuate-web
npm install
npm run dev
```

Required environment variables:

- `ANTHROPIC_API_KEY` — for Track B extraction
- `SEC_USER_AGENT` — required by SEC, format: `"Your Name your.email@domain.com"`

## What's V2

- Expand universe beyond clean industrials/tech
- Segment-level revenue forecasting
- Comparable company multiples and trading-comp valuation cross-check
- Save / share / scenario library
- Looker-style dashboard view across the universe
- Restatement and prior-period adjustment handling

## License

MIT
