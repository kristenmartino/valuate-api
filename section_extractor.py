"""Slice a 10-K HTML document down to the financial statements section.

10-K HTML files are 5-10 MB and full of inline XBRL markup. We:
1. Strip with BeautifulSoup to plain text (preserving line breaks)
2. Find the canonical "Item 8. Financial Statements..." heading
3. Slice to the next "Item 9" heading
4. Return the section text

The 'second occurrence' heuristic handles the standard 10-K shape: Item 8
appears once in the TOC and once at the actual section header, both with
the canonical "Financial Statements" title. Prose references like "see
Item 8 of this Form 10-K" don't match because they lack the title keyword.
"""

import re
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning


# 10-Ks are filed as inline-XBRL (XHTML); BeautifulSoup's XML-vs-HTML warning
# is noisy and benign for our text-extraction purpose.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


# Section-header regexes anchored on the canonical title keywords.
_ITEM_8_HEADING_RE = re.compile(
    r"\bITEM[\s ]+8[\.\s\-–—]+\s*FINANCIAL\s+STATEMENTS",
    re.IGNORECASE,
)
_ITEM_9_HEADING_RE = re.compile(
    r"\bITEM[\s ]+9[A-Z]?[\.\s\-–—]+\s*"
    r"(?:CHANGES|CONTROLS|OTHER\s+INFORMATION|MINE\s+SAFETY|DISCLOSURE)",
    re.IGNORECASE,
)


def extract_financial_statements_section(html: str) -> str:
    """Return the financial statements section text from a 10-K HTML.

    Falls back to the full document text if Item 8 boundaries can't be
    identified — Track B will still see the right content, just with more
    surrounding noise.
    """
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    text = "\n".join(line for line in lines if line)

    item_8_starts = [m.start() for m in _ITEM_8_HEADING_RE.finditer(text)]
    if not item_8_starts:
        return text

    # First match is typically the TOC entry; second is the actual section.
    section_start = item_8_starts[1] if len(item_8_starts) >= 2 else item_8_starts[0]

    next_section = _ITEM_9_HEADING_RE.search(text, section_start + 1)
    section_end = next_section.start() if next_section else len(text)

    return text[section_start:section_end]


# Keywords that anchor the SMOG / standardized-measure disclosure. The
# SEC-mandated section (ASC 932-235) is usually titled some variant of
# "Standardized Measure of Discounted Future Net Cash Flows" or
# "Supplemental Information on Oil and Gas Producing Activities".
_SMOG_ANCHOR_RE = re.compile(
    r"standardized\s+measure\s+of\s+discounted\s+future\s+net\s+cash\s+flows?",
    re.IGNORECASE,
)
_SMOG_BACKUP_ANCHOR_RE = re.compile(
    r"supplemental(?:ary)?\s+(?:information|disclosures?)\s+(?:on|relating\s+to)?\s*oil\s+and\s+gas",
    re.IGNORECASE,
)


def extract_oil_and_gas_supplemental_section(
    html: str,
    window_chars: int = 6_000,
) -> str:
    """Return the oil & gas supplemental section text from a 10-K HTML.

    Targets the SEC-mandated standardized-measure disclosure (ASC 932-235)
    that lives outside the standard financial statements — usually in
    "Supplementary Information on Oil and Gas Producing Activities
    (Unaudited)" at the tail of Item 8 or in a separate unaudited section.

    Strategy: find all anchor matches and use the LAST one. The phrase
    "standardized measure of discounted future net cash flows" appears
    early in 10-Ks as a forward reference in footnotes, and again later
    as the actual disclosure table heading. The table is what we want,
    and it consistently appears as the LAST occurrence in the supplementary
    information section (the "Changes in Standardized Measure" reconciliation
    table that immediately follows it is captured by the window).

    Falls back to a broader anchor ("Supplementary Information ... Oil and
    Gas") if the canonical phrase is absent. Returns an empty string if
    neither matches — Track B sees nothing rather than getting noise.

    Returns:
        Section text (typically 5-15kB) suitable for Claude extraction,
        or empty string if no oil-and-gas section is detected.
    """
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    text = "\n".join(line for line in lines if line)

    matches = list(_SMOG_ANCHOR_RE.finditer(text))
    match = matches[-1] if matches else _SMOG_BACKUP_ANCHOR_RE.search(text)
    if match is None:
        return ""

    start = max(0, match.start() - window_chars // 2)
    end = min(len(text), match.end() + window_chars // 2)
    return text[start:end]
