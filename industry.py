"""SIC code → industry classification.

Used at ingest time to route extraction down the right path. The standard
(industrial / tech) shape is the catch-all default; banks, insurers, REITs,
and energy E&P each have their own schema variants and DCF math because the
underlying business model is different enough that the industrial template
produces nonsense answers (banks have no "operating margin"; REITs are
valued on FFO not FCFF; etc.).

SIC code reference (selected):
- 6020-6099: depository institutions / bank holding companies
- 6311-6411: insurance carriers / agents
- 6798: real estate investment trusts (REITs)
- 1311, 1381, 1389, 2911: petroleum / oil & gas extraction and refining

Anything else falls through to STANDARD.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class Industry(str, Enum):
    STANDARD = "standard"
    BANK = "bank"
    INSURER = "insurer"
    REIT = "reit"
    ENERGY = "energy"


# Pin canonical SIC ranges per industry. Inclusive on both ends.
_SIC_RANGES: list[tuple[int, int, Industry]] = [
    (6020, 6099, Industry.BANK),     # commercial banks + bank holding cos
    (6311, 6411, Industry.INSURER),  # life, P&C, accident/health, agents
    (6798, 6798, Industry.REIT),     # REITs
    (1311, 1311, Industry.ENERGY),   # crude petroleum & natural gas
    (1381, 1389, Industry.ENERGY),   # drilling + oil/gas field services
    (2911, 2911, Industry.ENERGY),   # petroleum refining (integrated majors)
]


def classify_sic(sic: Optional[str | int]) -> Industry:
    """Map an SEC SIC code to an Industry.

    Accepts the SIC field as either a string (the shape submissions returns)
    or an int. Returns Industry.STANDARD for anything outside the explicit
    ranges, including missing/malformed input.
    """
    if sic is None or sic == "":
        return Industry.STANDARD
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return Industry.STANDARD
    for low, high, industry in _SIC_RANGES:
        if low <= code <= high:
            return industry
    return Industry.STANDARD


# Human-friendly label per industry, used in the UI / error messages.
INDUSTRY_LABEL: dict[Industry, str] = {
    Industry.STANDARD: "Industrial / Tech",
    Industry.BANK: "Bank",
    Industry.INSURER: "Insurer",
    Industry.REIT: "REIT",
    Industry.ENERGY: "Energy (Oil & Gas)",
}
