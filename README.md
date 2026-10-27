# valuate-api

> Backend for **Valuate** — AI-augmented DCF agent over SEC 10-K filings.
> Live at [valuate.kristenmartino.ai](https://valuate.kristenmartino.ai)
> · Frontend repo: [valuate-web](https://github.com/kristenmartino/valuate-web)
> · Case study: [kristenmartino.ai/work/valuate](https://kristenmartino.ai/work/valuate)

The case study has the full design narrative; this README is a working reference for the code.


## Publication qualification

Extraction and public valuation are separate states. A saved record or an extraction score does not establish coverage. The API returns a structured `Company.qualification` report with `qualified`, `review_required` or `unsupported` status, source evidence and reasons. Both public default-assumption and valuation endpoints require a valid report for the exact saved record, annual filing and current input/model policy. Caller-supplied assumptions, cleared flags and user corrections cannot bypass the gate. Unqualified financials remain available for inspection, but the workspace withholds estimates, assumptions, projections and simulations.

Qualification requires a filing-specific scope policy plus matching annual evidence for every model-driving input. Reports retain period dates, units, source accession, concept, accounting scope, basis and calculation operands. Generic extraction no longer substitutes total debt for noncurrent debt, depreciation alone for full D&A, restricted-plus-unrestricted cash for unrestricted cash, or noncurrent operating leases for total operating leases. Missing debt remains unknown. Debt components must be non-overlapping and use a supported carrying basis. Explicit company-specific proxies retain their labels and limitations; passing checks does not mean every displayed field was manually reviewed or that the valuation predicts a market price.

The report is bound to the complete saved record and a fingerprint of the financial pipeline, model code and filing policies. A changed record or policy invalidates it. Valuation responses identify their source record and filing; the frontend sends its record digest to reject calculations against changed financials. New filings require their own scope review. Saved, qualified older filings keep their displayed dates rather than silently claiming freshness.

Production uses PostgreSQL and refuses to start with process-local reviewer storage. Active records, immutable prior revisions, cold candidates and raw filing-evidence snapshots are stored separately. Versioned writes reject stale updates; same-filing publication preserves saved reviewer corrections. A new filing does not inherit old reviews as if they applied to its new figures. Revisions retain prior values and notes, but the shared demo credential does not identify individual reviewers.

Cold tests start with no saved record. CI replays 19 ticker/share-class cases covering 18 annual filings through the real extraction graph and qualification logic using selected SEC facts, inline-XBRL fixtures and recorded AI backfill responses. All 19 reviewed cases qualify. Oracle and Amazon use source-only contracts; the other cases replay recorded AI backfill where needed. Separate missing-operand, conflicting-value, wrong-unit, wrong-period and changed-record tests must withhold valuation; a safe rejection is not scored as extraction accuracy. Separate live cold runs exercise fresh retrieval and AI extraction, stage their candidates without replacing active records, and test rejection of unqualified publication. The historical May extraction baseline remains separate and has not been rerun for these mappings.

## Reviewed debt and cash-flow coverage

The eight filings below have explicit source contracts. Amounts are USD billions; debt columns are non-overlapping carrying amounts, not face values or credit-facility capacity.

| Filing | Noncurrent debt | Current debt | Cash-flow add-back | Scope |
|---|---:|---:|---:|---|
| [ADBE FY2025](https://www.sec.gov/Archives/edgar/data/796343/000079634326000003/adbe-20251128.htm) | 6.210 | 0.000 | 0.818 | Debt includes the filed swap basis adjustment. Add-back includes accretion; it is a combined proxy, not pure D&A. |
| [CRM FY2026](https://www.sec.gov/Archives/edgar/data/1108524/000110852426000060/crm-20260131.htm) | 10.439 | 4.000 | 3.631 | Current and noncurrent debt reconcile to the 14.439 total. Existing cash-flow D&A is retained. |
| [EOG FY2025](https://www.sec.gov/Archives/edgar/data/821189/000082118926000054/eog-20251231.htm) | 7.909 | 0.027 | 4.461 | Debt already includes finance leases. The add-back includes depletion. Operating leases remain separate. |
| [PEP FY2025](https://www.sec.gov/Archives/edgar/data/77476/000007747626000007/pep-20251227.htm) | 42.321 | 6.861 | 3.451 | Current debt includes current notes and other borrowings. Historical add-backs follow the cash-flow total. |
| [TXN FY2025](https://www.sec.gov/Archives/edgar/data/97476/000009747626000059/txn-20251231.htm) | 13.548 | 0.500 | 1.999 | Notes reconcile to 14.048; explicitly filed commercial paper is zero. Add-back sums depreciation and software amortization. |
| [SBUX FY2025](https://www.sec.gov/Archives/edgar/data/829224/000082922425000114/sbux-20250928.htm) | 14.5759 | 1.4989 | 1.7715 | Notes reconcile to 16.0748; cash-flow D&A differs from the 1.6847 income expense line. |
| [ORCL FY2026](https://www.sec.gov/Archives/edgar/data/1341439/000119312526277521/orcl-20260531.htm) | 129.423 | 7.819 | 9.294 | Notes and borrowings plus separately recognized finance leases; D&A sums the two cash-flow components. |
| [AMZN FY2025](https://www.sec.gov/Archives/edgar/data/1018724/000101872426000004/amzn-20251231.htm) | 84.190 | 5.105 | 65.756 | Carrying notes, short-term borrowings, finance leases and recognized financing obligations; the add-back includes content, lease assets and other adjustments. |

Adobe's `LongTermDebt` concept labels the balance-sheet noncurrent carrying amount in this filing; it is not treated as an inclusive total. Its zero current debt is an explicit filed amount. Statement rows, assumptions and projections label **D&A + accretion**, and the DCF-implied EV/EBITDA multiple is unavailable for that combined proxy. These disclosures retain the existing add-back values and valuation formula.

PepsiCo's older `DepreciationAndAmortization` note amounts exclude separately reported intangible amortization, while the cash-flow total includes it. The existing cash-flow series of 3.451, 3.160, 2.948, 2.763 and 2.710 is retained with that scope. The generic ambiguity rule remains in place for other filers.

TXN's complete five-year cash-flow add-back series is 1.999, 1.580, 1.238, 0.979 and 0.954. The 2021 amount includes 0.142 of acquisition-related intangible amortization as well as 0.755 depreciation and 0.057 software amortization. Required components come from one common annual filing per period, bounded by the reviewed anchor; absent components cannot be guessed or supplied by AI. The capex/revenue starting ratio and exact restore behavior are unchanged.

SBUX's reviewed window is FY2021–FY2025. Its cash series is 3.2198, 3.2862, 3.5515, 2.8184 and 6.4557; the four recent balance sheets explicitly label those amounts cash and cash equivalents despite using a broader taxonomy concept. A generic restricted-cash fallback remains prohibited. Statement-role selection cross-checks the row label, entity, annual context, units, accession and company-facts amount. Cash-flow D&A is 1.7715, 1.5924, 1.4503, 1.5294 and 1.5241; the separate operating-expense series is 1.6847, 1.5126, 1.3626, 1.4479 and 1.4417. Current plus noncurrent notes must reconcile across all five years. Historical current debt includes separate short-term borrowings: $0.175B of commercial paper in FY2022 and $0.0335B from Japanese facilities in FY2023. Current debt totals are 1.4989, 1.2489, 1.8521, 1.9240 and 0.9989 across the five years. A notes-only reconciliation would miss those borrowings. The latest filing reports no commercial-paper or facility borrowings; the zero is established by explicit dated filing statements, not missing tags, and capacity is not debt. Restoring missing years changes the historical starting ratios. GAAP operating income still includes restructuring and impairment costs; the model does not normalize them. Non-model optional fields are not certified by this repair.

Oracle and Amazon use reviewed, source-only onboarding contracts: neither extraction calls AI backfill or AI revenue segmentation. Exact annual contexts, statement rows and allowed dimensions are checked against two pinned accessions per company; standard undimensioned facts also require matching company-facts amounts. Missing operands or failed reconciliations cannot be replaced with estimates. Both contracts require five years of model-driving income, tax and cash-flow inputs. Older debt cells remain unknown when all recognized components cannot be established; they are not used as zeros or in historical debt averages.

Oracle's FY2026 cash-flow D&A is 7.623 depreciation plus 1.671 intangible amortization. Current/noncurrent notes and other borrowings of 7.199/122.342 reconcile to 129.541 and already include 1.468 commercial paper. Separate finance leases of 0.620/7.081 bring the modeled debt slots to 7.819/129.423. Operating leases of 30.190 remain separate in the net-debt bridge; cash is 31.289. Consolidated pre-tax income comes from Oracle's exact inline custom total, not a domestic or foreign subtotal. The five-year D&A series is 9.294, 6.174, 6.139, 6.108 and 3.122. The starting DCF produces a **negative equity residual**, displayed as an unfloored model residual per share with no DCF-implied trading multiples. It is not a negative stock-price estimate. The simple historical-input model does not value cloud backlog or separately project 260 billion of disclosed uncommenced lease commitments.

Amazon's 68.836 face-value notes less 0.440 discounts/issuance costs reconcile to 68.396 carrying notes, split 2.748 current and 65.648 noncurrent. The slots also include 0.455 separate short-term borrowings, 1.544/10.742 finance leases and 0.358/7.800 recognized financing obligations. The 7.800 amount is reported rounded to 0.1 billion. Recognized financing obligations are distinct from future gross payments including interest. Operating leases of 89.252 remain separate; cash of 86.810 excludes restricted cash and marketable securities. The discount requires the exact senior-notes dimension; financing obligations require the reviewed custom inline concepts and consolidated contexts.

Amazon's cash-flow add-back covers depreciation/amortization of property, equipment and capitalized content costs, operating-lease assets, and other adjustments. Its 65.756, 52.795, 48.663, 41.921 and 34.433 series is a **combined model proxy, not pure D&A**. Statements, assumptions and projections retain that scope, and DCF-implied EV/EBITDA is unavailable. Neither filing's cash-flow aggregate is copied into an income-expense row. Both DCFs use gross cash PP&E capex, omit separate noncash additions/future commitments and retain GAAP operating effects; they do not reproduce company-reported free cash flow or forecast spending plans. Qualification establishes the reviewed input scope, not predictive accuracy or a positive equity result.

A reusable statement reader and declarative filing contracts protect these inputs from AI replacement. Reviewed policies set the required history window; missing years cannot be silently dropped. The backend's `python -m eval.preflight --tickers SBUX --output preflight.json` checks SEC source completeness without AI calls, estimates, repository writes or publication. The manually dispatched **SEC source preflight** GitHub Actions workflow runs the same command and retains the checklist artifact. Run it on proposed tickers during onboarding, resolve missing or ambiguous scope, pin a cold regression fixture, then require independent qualification before publishing. Source completeness is a checklist, not approval; new or unreviewed filings can still be withheld.

Repairs under the existing six-filing repair contracts require complete matching evidence across the existing annual history before changing any amount. They preserve reviewer values and full notes, never split a reviewer-held debt pair automatically, and publish only after the independent qualification gate passes. Missing or conflicting evidence leaves the saved record unchanged. New filings still require separate scope review; these checks do not certify unrelated fields or expand the historical May extraction-accuracy baseline.


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
5. **Composition** — builds a `Company` with up to 5 `FinancialPeriod`s, dispatching to the right schema variants per the industry: `IncomeStatement` / `BalanceSheet` / `CashFlowStatement` for standard *and* energy filers (E&P companies report on the same shape as industrials — what differs is the *valuation*, not the line items), `BankIncomeStatement` / `BankBalanceSheet` / `BankCashFlowStatement` for banks, the equivalent `Insurance*` triples for insurers, and `REIT*` triples for REITs. Pydantic discriminated unions on each statement (`kind` literal) keep the JSON shape unambiguous on the wire. The latest period must be complete or `CompositionError` raises (HTTP 422); candidate composition may drop older periods with thin coverage. Publication qualification then checks the exact historical-period set in the filing policy and rejects shortened model history.
6. **Validate** — flags low-confidence items (<0.80) and balance-sheet identity violations (>50bps tolerance) as `ExtractionFlag`s on the response.

After the graph completes, the API independently collects qualification evidence and stages the candidate. Successful graph completion does not authorize public default assumptions or valuation.

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

## Cisco debt and cash-flow adjustment coverage

The shared `debt.py` selector uses `LongTermDebtNoncurrent` for CSCO and AAPL before AI fallback and repairs older machine-extracted inclusive-debt fields on read. Its facts must match the requested company's CIK, USD units, year-end instant, annual form and filing accession. Latest-period values require the saved accession; older periods prefer its comparatives and can use earlier annual filings identified in the source note. Equal repeated facts are deduplicated; conflicting or invalid values remain unavailable. Existing reviewer corrections and all other financial fields are preserved. This selector is limited to the two identified filers; it does not establish coverage for every company's debt disclosures.

The [FY2026 filing, Note 11](https://www.sec.gov/Archives/edgar/data/858877/000085887726000132/csco-20260725.htm) separates $19.372B noncurrent debt from $3.500B current maturities. The latter is already included in $10.161B short-term debt. Replacing the inclusive $22.872B mapping avoids double-counting and changes both the net-debt bridge and default WACC. The reviewed $14.320B SG&A amount and its complete source note are preserved. Failed SEC repairs retain saved values, surface the known latest-period coverage warning and retry at most once per five minutes per process.

`cisco_da.py` reads `csco:DepreciationAmortizationAndOther` from consolidated annual inline-XBRL contexts. Cisco's generic `DepreciationDepletionAndAmortization` fact is PPE-only ($0.700B for FY2026), so it is not the full model add-back. The filed cash-flow adjustment is $2.540B and includes other items; it is used as a combined proxy, not labeled as pure D&A. No guessed residual or sum of differently scoped amortization components is substituted.

The reader resolves the saved accession in SEC submissions, checks annual document metadata, entity, USD unit, undimensioned context, and Cisco's exact 52/53-week annual dates. Matching repeated facts are deduplicated; conflicts, invalid values and quarterly contexts are rejected. The saved filing's comparatives take precedence; earlier annual filings supply missing older years and are identified in each source note. The FY2026–FY2022 series is $2.540B, $2.811B, $2.507B, $1.726B and $1.957B, from the [FY2026](https://www.sec.gov/Archives/edgar/data/858877/000085887726000132/csco-20260725.htm) and [FY2024](https://www.sec.gov/Archives/edgar/data/858877/000085887724000017/csco-20240727.htm) filings.

Fresh Cisco extraction requires this tagged latest-period input; Claude cannot replace it with the PPE component or a guess. Saved-record repair changes only incomplete machine-extracted D&A fields in both statement slots and preserves debt and reviewer corrections, including SG&A. It requires all affected historical periods before replacing any, avoiding a default average that mixes repaired totals with old components. Failed repairs keep the saved record and latest-period coverage warning, with a five-minute retry throttle. The five-year combined adjustment/revenue average drives the default `da_ratio`; other assumptions and the net-debt bridge are unchanged. The workspace labels its ratio, statement rows and projection column with the combined scope.

These targeted regressions do not rerun or expand the historical extraction baseline.

## Apple debt coverage

AAPL's [FY2025 filing](https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm) separates $78.328B noncurrent term debt from $12.350B current term debt. Short-term debt already contains that current component plus $7.979B commercial paper, totaling $20.329B. The inclusive $90.678B `LongTermDebt` fact must not be added again as though it were noncurrent debt.

`debt.py` applies the same guarded noncurrent selector to fresh Apple extraction and to known inclusive machine fields in saved history. Already-correct fields and reviewer overrides remain unchanged. A failed saved repair keeps the previous amounts and exposes the known debt-coverage warning; retries are throttled to once per five minutes per process. Current debt, cash, leases, other financial fields, and correction notes are not replaced.

With the saved FY2025 inputs, the corrected debt bridge is $75.213B rather than $87.563B. The default WACC calculation runs again but remains at its existing 7% floor; the other assumptions are unchanged. Fixtures pin the filed noncurrent debt series and the pre-repair saved record. The new targeted tests cover the valuation effect and preservation rules without expanding or rerunning the historical May extraction baseline.

## IBM operating-input coverage

IBM's [FY2025 10-K](https://www.sec.gov/Archives/edgar/data/51143/000005114326000010/ibm-20251231.htm) incorporates its income statement in [Exhibit 13, p. 42](https://www.sec.gov/Archives/edgar/data/51143/000005114326000010/ibm-20251231_d2.htm). The primary document does not contain that statement, and the company-facts feed has no consolidated `OperatingIncomeLoss` subtotal. The targeted `ibm.py` fallback reads annual GAAP pre-tax income from continuing operations and separately reported interest from company facts, then calculates an **EBIT proxy** in the existing `operating_income` wire field. FY2025: $10.328B + $1.935B = $12.263B.

This is not reported operating income or IBM's operating (non-GAAP) earnings. Other income/expense remains included; financing interest in cost of revenue remains deducted. `Company.operating_income_label` and `valuation_note` carry this scope to the workspace. The derived proxy retains a 0.65 heuristic score and an automated review flag; successful valuation does not mark it manually reviewed. The historical proxy/revenue average drives the normal FCFF model. Historical years with other missing required fields remain excluded.

Operands must match IBM's CIK, USD units, full January–December duration, annual form and one common filing accession. The latest period requires the selected filing; older periods prefer its comparatives, then earlier annual filings identified in the source note. Conflicts, nonfinite values and negative interest are rejected; tax benefits keep their sign. Missing operands fail explicitly rather than accepting an AI guess or substituting non-GAAP earnings. The helper also supplies the matching pre-tax, interest and optional tax-benefit/expense fields. It does not change other companies or overwrite saved reviewer corrections. These targeted regressions do not rerun or expand the historical May extraction baseline.

IBM also selects matching annual noncurrent debt and current borrowings ($54.836B + $6.424B for FY2025) and calculates the cash-flow D&A total from the separately filed depreciation and amortization adjustments ($2.284B + $2.737B = $5.021B). Both D&A statement slots use that total across the supported annual history. Missing latest D&A components or either debt component prevent valuation; they are not replaced by a partial amount or zero. Existing corrections for other companies are unaffected. The generic capex input still covers PP&E only, excluding separately reported investment in software; IBM's model note discloses this remaining simplification.

## Intuit D&A and debt coverage

INTU's D&A input sums filed depreciation and the full cash-flow amortization adjustment, rather than depreciation alone or the narrower other-intangibles expense. FY2026: $187M + $659M = $846M. The five-year total series is $846M, $809M, $789M, $806M and $746M. Both statement slots identify the calculated total through source details. Noncurrent debt is $6.420B, separate from the $1.249B current maturities; the inclusive $7.669B amount is not added to current debt again.

The INTU-only selection requires matching annual USD facts from one filing, with the latest period tied to its selected 10-K and older periods bounded by that filing's date. It uses `AdjustmentForAmortization`, which includes acquired technology and other acquired intangibles; `AmortizationOfIntangibleAssets` alone covers only the other-intangibles expense component. Missing, conflicting or unsafe components cannot be replaced with AI guesses. Saved repair is atomic across affected history and preserves reviewer overrides and other correct fields. If repair cannot establish complete coverage, the record retains its amounts and a warning, and valuation/default requests remain unavailable until the inputs are recovered.

With the captured FY2026 inputs, net debt is $3.716B, default WACC is about 7.86%, and the D&A/revenue ratio is about 4.91%. Other assumptions and the existing corrections for other companies are unchanged. The company note discloses the remaining PP&E-only capex input, which excludes separately reported capitalized internal-use software. This targeted repair does not manually approve INTU's unrelated gross-profit field or expand the historical extraction evaluation.

## Microsoft D&A and debt split

MSFT uses pure D&A expense from two non-overlapping filed disclosures: depreciation plus intangible-asset amortization. FY2026 is $34.3B + $4.7B = $39.0B; the five-year series is $39.0B, $28.0B, $20.0B, $13.5B and $14.6B. The $38.534B cash-flow row includes other adjustments and is not substituted for pure D&A. Both statement slots label the sum as calculated and expose the operands, annual dates and source accession; the company note explains the scope.

Current maturities of $9.227B and noncurrent debt of $31.067B must reconcile to the filed $40.294B total. The saved MSFT record previously held that total in the long-term slot with current debt blank. Repairing both slots together preserves aggregate debt, net debt and default WACC; adding current debt beside the inclusive total would double-count it. The corrected five-year D&A/revenue average changes that assumption and the valuation, while other starting assumptions stay unchanged.

The MSFT-only selector requires annual USD expense facts and year-end debt instants from one common annual filing for each period, with the latest tied to the selected 10-K and older periods bounded by its filing date. Missing, conflicting, nonfinite, negative, differently scoped or incoherent debt facts cannot be replaced by AI guesses. Saved repair requires every affected historical period before changing any, preserves user corrections and other fields, and does not split a reviewer-held debt pair automatically. Unresolved coverage keeps the saved record and an explicit flag while valuation/default requests fail. These targeted checks do not approve other MSFT fields or expand the historical extraction baseline.

Source: [Microsoft FY2026 annual report](https://www.microsoft.com/investor/reports/ar26/), Notes 6, 9 and 10.

## Starting-assumption provenance

Starting assumptions combine historical calculations and model defaults. For standard DCF, eligible periods have positive revenue; tax expense / positive pre-tax income is limited to 0–50% per period, averaged, then limited to 15–30%. The tax fallback is 21%. IBM therefore starts at 15% despite its FY2025 filed effective rate of −2.3%; this is a normalized projection assumption, not a replacement for the filed tax benefit. Energy averages the same per-period bounded rates without the additional 15–30% normalization. Standard and energy working capital starts at a fixed 5% of the change in revenue, not a ratio calculated from historical working-capital balances.

Capex/revenue and cash-flow add-back/revenue start at equal-weight averages of available annual ratios with positive revenue. Missing-ratio fallbacks are 4% for standard DCF and 20% for energy, and coverage follows the extracted cash-flow fields. Bank, insurer and REIT tax figures are read-only model context: an average of available tax/pre-tax ratios limited to 0–50% per period, or 21% if unavailable; these tax figures do not drive those models. The workspace labels the cash-flow assumptions and explains these starting rules beside the controls. Editing a scenario or restoring its starting assumptions does not change saved financial figures or review notes.

## HITL overrides

`PUT /company/{ticker}/override` accepts `{ field_path, value, source_quote? }` and replaces the LineItem at the given path with `source=USER_OVERRIDE`. Validation re-runs after every override so flags reflect the new state. The changed record needs fresh qualification before it can produce another public valuation.

Two repos back this:

- **InMemoryRepo** (default in local dev) — process-local dict, wiped on restart.
- **PostgresRepo** — turns on automatically when `DATABASE_URL` is set. Active JSONB records plus immutable versions, staged cold candidates and raw evidence snapshots. Writes are transactional and reject stale revisions; data-store failures propagate rather than becoming cache misses or dropped corrections.

## Tech stack

- **Python 3.11**, FastAPI + LangGraph
- **SEC EDGAR** for both XBRL company-facts and 10-K HTML
- **Anthropic SDK** (`claude-sonnet-4-6`) with prompt caching on the static system prompt
- **BeautifulSoup + lxml** for slicing the 10-K's Item 8 financial-statements section before sending to Claude
- **yfinance** for peer market multiples (no API key, runs in a thread pool)
- **asyncpg** for PostgreSQL persistence (required in deployed environments)

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

496 collected tests across 23 files: 495 are selected by default, and the network integration test is deselected. Check the current inventory with `pytest tests/ --collect-only -q`.

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
- `test_cisco.py` (16) — noncurrent-debt mapping across history, unchanged reviewed SG&A and other fields, WACC/net-debt effects, unsafe-fact rejection, fresh extraction and saved-record repair/failure behavior
- `test_cisco_da.py` (25) — filed combined cash-flow adjustment across five annual periods, exact contexts and source accessions, unsafe-fact rejection, preserved debt/reviews, atomic history repair, valuation math, and fresh/saved extraction failure behavior
- `test_apple_debt.py` (16) — filed noncurrent debt, preserved current debt and review notes, unsafe-fact rejection, unchanged assumptions, corrected bridge/value, and fresh/saved repair and failure behavior
- `test_ibm.py` (26) — same-filing EBIT proxy, signed tax benefits, unsafe-fact rejection, explicit review status, successful no-LLM composition, missing-data failure and preserved correction state
- `test_intuit.py` (24) — full cash-flow amortization versus expense-component coverage, five-year D&A and noncurrent debt, unsafe-context rejection, atomic repair, preserved reviewer values, fresh/saved failure behavior and valuation effects
- `test_microsoft.py` (30) — pure D&A expense scope, five-year consistent repair, reconciled current/noncurrent debt, unsafe-fact rejection, preserved reviews and aggregate debt, fresh/saved failure behavior and valuation effects
- `test_alphabet.py` (29) — carrying-value debt and current/noncurrent split, historical finance-lease scope, PPE-depreciation proxy disclosures, and preserved inputs
- `test_filing_coverage.py` (63) — reviewed debt and cash-flow contracts, complete historical operands, unsafe-evidence rejection, source-only atomic repair, and saved-review preservation
- `test_qualification.py` (54) — cold graph replays, filing/record/policy-bound qualification, public valuation gating, candidate publication, and preserved reviews
- `test_repository_persistence.py` (3) — PostgreSQL persistence across repository restarts, immutable revisions and evidence, and stale-write rejection
- `test_statement_coverage.py` (30) — statement-role evidence, SBUX five-year recovery, separate historical borrowings, explicit zero-borrowing disclosures, source-only preflight, and atomic repair
- `test_scoped_inputs.py` (49) — Oracle/Amazon source-only cold onboarding, exact lease/financing operands, five-year history, unsafe-source rejection, preserved optional reviews and rejected conflicting model reviews
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
| `DATABASE_URL` | required in production; optional locally | Deployed startup fails without persistent storage. Use a service reference such as `${{Postgres.DATABASE_URL}}`. Local development may use in-memory storage. |
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

Measured across 11 filers in five industries. In that historical run, Track A (XBRL) reproduced the pinned values for all 39 fields within ±0.5%. This result does not establish the coverage of the current mappings. Track B (Claude) covers the four income-tax fields the XBRL concept map intentionally leaves to the LLM (`income_before_tax` / `income_tax_expense`): 3 of 4 within ±0.5%. The single miss is an *omission*, not a wrong number — Claude returned no value for MSFT's `income_before_tax` (`not_extracted`), a field that isn't required for the DCF. (Refresh these figures with `python -m eval.run_eval --readme`.)

This is an eval baseline over a curated public-filer set, not a guarantee across all SEC filers.

### Auth + rate-limit model

The threat model is "random scraping / accidental corruption," not "sophisticated adversary." The public correction route and the separate administrative routes are gated by the existing shared service credential; `/extract` is rate-limited by IP because each call costs real Anthropic credits. A production deployment would put both behind real per-user auth.

In local development, leaving `VALUATE_OVERRIDE_TOKEN` unset disables authentication only for the public correction endpoint. All `/admin/*` routes still require a configured token, including locally. `/extract` remains rate-limited: when `VALUATE_EXTRACT_RATE_LIMIT` is unset, the default is 10 requests per hour per IP.

## Universe

18 sample tickers are entry points, not an approved universe. Their exact filings must pass qualification before producing a public valuation. The sample includes 14 industrial / tech filers (AAPL, MSFT, GOOGL, AMZN, META, NVDA, COST, WMT, HD, NKE, JNJ, KO, F, CAT), one bank (JPM), one life insurer (PRU), one industrial REIT (PLD), and one pure-play E&P (EOG). Historical extraction success established that records composed, not that their inputs were qualified. Current availability is determined by the filing report.

## Industry coverage

| Industry | Status | Valuation method | Sample ticker |
|---|---|---|---|
| Industrial / tech | implemented; filing qualification required | 5-year FCFF DCF + Monte Carlo + sensitivity | AAPL, MSFT, ... |
| Banks | implemented; filing qualification required | Gordon DDM | JPM |
| Insurers | implemented; filing qualification required | Justified P/B | PRU |
| REITs | implemented; filing qualification required | FFO-multiple Gordon growth | PLD |
| Energy E&P | implemented; filing qualification required | Fixed ten-year FCFF (no terminal) | EOG |

E&P is the only industry that doesn't carry a separate schema variant — the line items E&P companies report (revenue, op income, capex, D&A) are standard us-gaap; the conceptual difference is the *valuation*, not the data shape. The architecture supports both: schema variants when the line-item set fundamentally differs (banks, insurers, REITs), and dispatch-only when only the valuation differs (E&P). Anything classified outside these five falls back to `Industry.STANDARD` and runs the FCFF path with Gordon terminal — which can be unsuitable. A reviewed filing/model policy is required before publication; the home page and ticker lookup do not grant approval.

Other items deliberately parked in the [`later` label](https://github.com/kristenmartino/valuate-api/issues?q=label%3Alater): segment-aware DCF (currently consolidated only), multi-period filing-accession attribution, saved scenarios.

## License

MIT

## Alphabet debt basis and depreciation scope

GOOGL's FY2025 debt uses carrying amounts: $49.085B face value less $0.542B discounts/issuance costs equals $48.543B, split into $1.996B current maturities and $46.547B noncurrent debt. Current debt also includes filed commercial paper ($0 in FY2025; $2.300B in FY2024). Matching annual USD facts from one filing must reconcile before use. FY2022's legacy debt table includes finance leases, unlike the later notes-only figures; statement cells and source details disclose the different historical coverage. The existing net-debt bridge separately adds reported operating-lease liabilities.

The $21.136B FY2025 add-back is **property-and-equipment depreciation**, used as a proxy, not verified full D&A. Its historical values and revenue ratio remain unchanged; no unverified amortization is added. Statement rows, the assumption control, projection and model note disclose that scope. DCF-implied EV/EBITDA is unavailable on this basis; provider market multiples retain their own definitions.

Scoped extraction protects these inputs from AI backfill. Saved repair requires matching facts for every saved period before changing any, preserves reviewer overrides and other fields, and never automatically splits a reviewed debt pair. Unresolved debt coverage retains the record and warning while valuation/default requests fail. These targeted checks do not approve other fields or expand the historical extraction baseline. See the [FY2025 filing](https://www.sec.gov/Archives/edgar/data/1652044/000165204426000018/goog-20251231.htm) and [FY2023 filing with the legacy comparison](https://www.sec.gov/Archives/edgar/data/1652044/000165204424000022/goog-20231231.htm).

## Qualification operations and cold coverage

```bash
# Deterministic, cost-free cold graph replay (recorded AI where needed; source-only ORCL/AMZN).
python -m eval.cold
# SEC-only onboarding preflight; no AI, repository writes or publication.
python -m eval.preflight --tickers SBUX --output preflight.json
# Run with the existing authenticated service environment; no credential is printed.
railway run --no-local -- python -m ops.migrate_records --snapshots /path/to/backup --output /path/to/evidence --commit EXPECTED_RELEASE_SHA
# At most 30 fresh extractions, once per ticker; no automatic AI retries or publication.
railway run --no-local -- python -m eval.live_cold --output /path/to/cold-evidence --max-extractions 30
```

Administrative import, qualification, coverage repair, cold extraction, candidate publication and history endpoints always require the configured service token, including in local mode. The frontend proxy does not inject credentials onto these routes. Import strips supplied qualification metadata and refuses to overwrite a saved record. Candidate publication rechecks qualification and preserves same-filing reviews; an unqualified candidate receives HTTP 422. These are maintainer operations, not public approval controls.

The live cold cohort is the 18 sample tickers, eleven additional regression tickers (including SBUX and ORCL) and one held-out ticker (MCD): 30 total. Coverage results must distinguish qualified, withheld and composition-failed cases. MCD has no reviewed scope policy and must not be published automatically. ORCL and AMZN are now reviewed source-only cases. The original 29-ticker live cold run predates these changes; it is not a fresh AI-accuracy result for the updated pipeline. CI runs the offline cases and real PostgreSQL restart/conflict tests. A weekday GitHub Actions job checks SEC annual-filing metadata without AI calls or publication writes and fails when a policy's filing has been superseded; the new filing needs source review and a new policy.

`POST /admin/repair-coverage/{ticker}` applies the reviewed six-filing contracts, including SBUX history recovery without AI calls. It fetches SEC evidence, checks every saved period, retains reviewer corrections, stages the result, then writes only if qualification passes and the saved revision is still current. It returns 422 for incomplete or conflicting coverage, 503 when evidence is unavailable, and does not replace the active record on either failure. Requalify other saved records with `POST /admin/qualify/{ticker}` after a financial-policy fingerprint changes. These routes require the existing service token; the frontend proxy never injects it for administrative requests.

ORCL and AMZN onboarding uses `POST /admin/cold-extract` to stage a source-only candidate, followed by independent qualification and `POST /admin/publish/{candidate_id}`. Staging leaves the active record unchanged. The six-filing `repair-coverage` route does not apply these two onboarding contracts. Publication retains same-filing saved corrections and full review notes; a conflicting model-input correction must be resolved before the candidate can qualify.
