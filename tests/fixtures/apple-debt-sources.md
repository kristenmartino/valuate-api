# Apple debt fixture provenance

`apple-debt-facts.json` contains selected public SEC company-facts USD rows for `LongTermDebtNoncurrent`, `LongTermDebt` and `LongTermDebtCurrent`, clipped to the five saved annual periods and accessions no later than the saved FY2025 filing.

The [FY2025 filing](https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm), accession `0000320193-25-000079`, reports $90.678B total term debt less $12.350B current portion, leaving $78.328B noncurrent. Commercial paper is $7.979B. Earlier selected facts retain their original accessions and dates.

`apple-saved-company.json` is the public pre-repair Company record used for exact non-debt-field preservation and valuation regression checks. It does not assert that every financial field has been manually reviewed. Reviewer notes added inside unit tests are synthetic test inputs, not production reviews. These fixtures do not expand the historical May 2026 evaluation.
