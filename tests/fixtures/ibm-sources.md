# IBM annual-fact fixture

Public SEC company facts, CIK 51143, retrieved October 26, 2026:
https://data.sec.gov/api/xbrl/companyfacts/CIK0000051143.json

Anchor annual filing: 0000051143-26-000010, December 31, 2025:
https://www.sec.gov/Archives/edgar/data/51143/000005114326000010/ibm-20251231.htm

The incorporated annual-report consolidated income statement is Exhibit 13, page 42:
https://www.sec.gov/Archives/edgar/data/51143/000005114326000010/ibm-20251231_d2.htm

The fixture retains selected USD/share facts for December 31, 2021–2025 from annual filings dated no later than the anchor. It is not a full filing or an accuracy evaluation. The FY2025 operands are GAAP pre-tax income from continuing operations 10,328 million and separately reported interest expense 1,935 million; tax benefit is -242 million. The 12,263 million sum is a calculated EBIT proxy, not IBM-reported operating income or non-GAAP earnings. Other income/expense remains included and financing interest in cost of revenue remains deducted.

Historical proxy values for 2025–2021 are 12,263; 7,509; 10,297; 2,372; 5,992 million. The fixture composes all five years. Future records still compose only periods with all required fields. Tests preserve the automated proxy-review flag. The test review note is synthetic and does not represent a live human approval. Targeted coverage does not update the historical May 2026 baseline.

The FY2025 consolidated cash-flow statement (Exhibit 13 p. 45) separately reports depreciation 2,284 million and amortization of capitalized software/acquired intangibles 2,737 million; their 5,021 million sum supplies D&A. The balance sheet (p. 44) reports current borrowings 6,424 million and noncurrent debt 54,836 million. The model still uses its generic PP&E-only capex input; software investment is excluded and this limitation is disclosed. The five-year D&A series is 5,021; 4,667; 4,396; 4,802; 6,417 million.
