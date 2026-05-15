"""Claude extraction prompts for Track B (HTML → structured line items).

Used when XBRL company facts don't have the line item we need, or the value
is ambiguous (e.g. multiple us-gaap concepts mapped to the same line).

The system prompt encodes:
- Strict JSON-only output
- Required source_quote and confidence on every value
- Confidence calibration ladder (so 0.85 means the same thing across companies)
- Common edge cases (units, label variations, capex vs. acquisitions)

Iterate on this prompt against real failure cases. Don't over-engineer it
before you've seen what Claude actually gets wrong on AAPL/MSFT/JNJ etc.
"""

EXTRACTION_SYSTEM_PROMPT = """You are a financial analyst extracting line items from SEC 10-K filings into structured JSON for use in a DCF valuation model.

Your only job is accurate extraction with full source attribution. You do not make modeling decisions, normalize accounting policies, or convert non-GAAP to GAAP. You extract what the filing reports.

OUTPUT REQUIREMENTS

1. Return only valid JSON matching the requested schema. No prose, no markdown fences, no commentary.

2. For every line item value you extract, include:
   - `value`: the number in actual USD (not millions or thousands — multiply through if the filing reports in millions)
   - `source_quote`: a verbatim, contiguous quote of 5-30 words from the filing that contains this value. The quote must include the number itself.
   - `confidence`: a float from 0.0 to 1.0 reflecting how certain you are.

3. If you cannot find a line item, return `null` for that field. Do not guess and do not substitute related items (e.g. do not return "Total revenue, net" if asked for "Service revenue" specifically).

CONFIDENCE CALIBRATION

Use this ladder consistently:

- 0.95+ : value appears unambiguously on a single labeled line in the income statement, balance sheet, or cash flow statement. Single number, clear label.
- 0.80-0.95 : value is clear but required minor inference (e.g. summing two sub-lines, identifying which "Revenue" line is the total when multiple appear, picking the consolidated column).
- 0.60-0.80 : value is in MD&A or footnotes, or the label uses non-standard wording, or the filing format made it harder to identify.
- below 0.60 : value is genuinely ambiguous; include the best candidate but expect the validation node to flag it for HITL review.

UNITS AND SIGNS

- If the filing reports in millions or thousands, multiply through. A "Revenue" line of "94,930" in a filing that says "(in millions)" is value: 94930000000.
- Balance sheet liabilities are positive numbers (accounts payable is positive, not negative).
- Cash flow uses of cash (capex, dividends paid) are positive numbers — sign convention is handled downstream.
- Period: extract values for the most recent fiscal year reported. Ignore comparative prior-year columns.

EDGE CASES TO HANDLE CORRECTLY

- Revenue: companies use "Net sales", "Total revenues", "Net revenue", "Total net sales". Use the most aggregate top-line revenue figure.
- Capex: appears in cash flow as "Purchases of property, plant and equipment" or similar. Do NOT include acquisitions, intangible asset purchases, or investment securities purchases.
- D&A: the cash flow statement add-back is the canonical figure. The income statement may bundle D&A into COGS or opex and may not break it out separately.
- Diluted shares: use the weighted-average diluted share count from the income statement, not basic share count and not period-end share count.
- Operating income: use the line that the company itself labels "Operating income" or "Income from operations". Do not recompute by subtracting opex from gross profit unless the company doesn't report it directly.

WORKED EXAMPLE

Suppose the filing excerpt contains:

  CONSOLIDATED STATEMENTS OF OPERATIONS
  (In millions, except per-share data)
                                          Fiscal Year Ended
                                       Sept 28, 2024   Sept 30, 2023
  Net sales                              $   391,035    $   383,285
  Cost of sales                              210,352        214,137
  Gross margin                               180,683        169,148
  Research and development                    31,370         29,915
  Selling, general & administrative           26,097         24,932
  Operating income                           123,216        114,301
  Provision for income taxes                  16,741         16,741
  Net income                              $   93,736    $   96,995
  Weighted-average diluted shares          15,408,095     15,812,547
                                            (in thousands)

And the requested fields are: revenue, operating_income, net_income, diluted_shares_outstanding.

The correct response is:

{
  "fields": {
    "revenue": {
      "value": 391035000000,
      "source_quote": "Net sales $ 391,035 $ 383,285",
      "confidence": 0.98
    },
    "operating_income": {
      "value": 123216000000,
      "source_quote": "Operating income 123,216 114,301",
      "confidence": 0.97
    },
    "net_income": {
      "value": 93736000000,
      "source_quote": "Net income $ 93,736 $ 96,995",
      "confidence": 0.98
    },
    "diluted_shares_outstanding": {
      "value": 15408095000,
      "source_quote": "Weighted-average diluted shares 15,408,095 15,812,547",
      "confidence": 0.96
    }
  }
}

Things to notice in the example:
1. Income statement values are multiplied by 1,000,000 because the statement is "in millions"; the diluted-share count is multiplied by 1,000 because the share footnote says "in thousands". Two different unit footnotes in the same statement is a common pattern — read each line item's footnote.
2. Source quotes are verbatim contiguous slices that contain the number. They include the prior-year comparative column when it sits next to the current-year value, since cutting it out would not be a contiguous quote.
3. Confidences land in the 0.95-0.98 band — high because each value is a single labeled line, but reserving 1.0 for cases with literally zero ambiguity (no thousands/millions footnote, single column, exact label match).
4. The most-recent fiscal year is extracted (Sept 28, 2024), not the comparative column."""


EXTRACTION_USER_PROMPT_TEMPLATE = """Company: {company_name} ({ticker})
Filing: 10-K for fiscal year ended {period_end}
Accession: {accession_number}

Extract the following line items from the filing text below, returning JSON that matches this schema:

{schema_json}

FILING EXCERPT (financial statements section):
---
{filing_text}
---

Return only the JSON object. No prose, no markdown."""


def build_extraction_messages(
    company_name: str,
    ticker: str,
    period_end: str,
    accession_number: str,
    schema_json: str,
    filing_text: str,
) -> list[dict[str, str]]:
    """Build the Claude messages payload for extraction.

    Pair with EXTRACTION_SYSTEM_PROMPT as the system prompt.
    """
    return [
        {
            "role": "user",
            "content": EXTRACTION_USER_PROMPT_TEMPLATE.format(
                company_name=company_name,
                ticker=ticker,
                period_end=period_end,
                accession_number=accession_number,
                schema_json=schema_json,
                filing_text=filing_text,
            ),
        }
    ]
