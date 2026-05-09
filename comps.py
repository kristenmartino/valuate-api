"""Trading-comps cross-check.

Hand-picked peer groups for each universe ticker. Pulls current market
multiples (market cap, EV, P/E, EV/Revenue, EV/EBITDA) for the target plus
its peers from Yahoo Finance via the unofficial yfinance package. Computes
peer medians so the workspace can show how the DCF-implied multiple compares
to where the market is currently pricing similar names.

yfinance is unofficial and can break with Yahoo's HTML changes. We:
- Run all fetches in a thread pool concurrently (the underlying client is
  blocking).
- Treat individual peer failures as non-fatal — drop missing peers from the
  response rather than failing the whole request.
- Skip caching for V1 (in-process state evaporates on Railway redeploy
  anyway, see issue #5). Latency for ~4 parallel fetches is ~1-3s.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import yfinance as yf

from schemas import CompsResponse, PeerMultiples


# Peer mapping for the 10-ticker universe. 3-4 names each, biased toward
# direct competitors with similar size and business model. AAPL/MSFT/GOOGL
# overlap at the mega-cap-tech tier; CAT/DE/CMI cluster around heavy
# industrials; etc. These are not investment advice — they're for visual
# anchoring of the DCF output.
PEER_GROUPS: dict[str, list[str]] = {
    "AAPL": ["MSFT", "GOOGL", "NVDA", "META"],
    "MSFT": ["AAPL", "GOOGL", "AMZN", "META"],
    "GOOGL": ["META", "MSFT", "AAPL", "AMZN"],
    "NVDA": ["AMD", "AVGO", "INTC", "QCOM"],
    "COST": ["WMT", "TGT", "BJ"],
    "HD": ["LOW", "TSCO", "FND"],
    "NKE": ["LULU", "UAA", "DECK"],
    "JNJ": ["PFE", "MRK", "ABBV", "LLY"],
    "KO": ["PEP", "KDP", "MNST"],
    "CAT": ["DE", "CMI", "OSK", "PCAR"],
    "JPM": ["BAC", "WFC", "C", "GS"],
    "PRU": ["MET", "AIG", "ALL", "LNC"],
}


def _safe_float(x: Any) -> Optional[float]:
    """yfinance occasionally returns numpy types or NaN; coerce to float-or-None."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _fetch_market_data_sync(ticker: str) -> Optional[PeerMultiples]:
    """Blocking yfinance call. Wrap with asyncio.to_thread for the async path."""
    try:
        info: dict[str, Any] = yf.Ticker(ticker).info
    except Exception:
        return None
    if not info or not isinstance(info, dict):
        return None

    market_cap = _safe_float(info.get("marketCap"))
    ev = _safe_float(info.get("enterpriseValue"))
    revenue = _safe_float(info.get("totalRevenue"))
    ebitda = _safe_float(info.get("ebitda"))
    pe = _safe_float(info.get("trailingPE"))

    ev_revenue = (ev / revenue) if ev and revenue and revenue > 0 else None
    ev_ebitda = (ev / ebitda) if ev and ebitda and ebitda > 0 else None

    return PeerMultiples(
        ticker=ticker.upper(),
        name=info.get("longName") or info.get("shortName") or ticker,
        market_cap=market_cap,
        enterprise_value=ev,
        revenue=revenue,
        ebitda=ebitda,
        pe_ratio=pe,
        ev_revenue=ev_revenue,
        ev_ebitda=ev_ebitda,
    )


async def _fetch_market_data(ticker: str) -> Optional[PeerMultiples]:
    return await asyncio.to_thread(_fetch_market_data_sync, ticker)


def _median(values: list[Optional[float]]) -> Optional[float]:
    xs = sorted(v for v in values if v is not None and v > 0)
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


async def get_peer_comps(target_ticker: str) -> CompsResponse:
    """Fetch market multiples for target + peers; return the full table.

    Always returns a CompsResponse (with empty peers list if everything
    fails) — the endpoint never raises. The frontend decides whether to
    render the panel based on whether `peers` is empty.
    """
    target = target_ticker.upper()
    peer_tickers = PEER_GROUPS.get(target, [])
    target_data, *peer_data = await asyncio.gather(
        _fetch_market_data(target),
        *[_fetch_market_data(t) for t in peer_tickers],
    )
    valid_peers: list[PeerMultiples] = [p for p in peer_data if p is not None]

    return CompsResponse(
        target_ticker=target,
        target_market=target_data,
        peers=valid_peers,
        median_pe=_median([p.pe_ratio for p in valid_peers]),
        median_ev_revenue=_median([p.ev_revenue for p in valid_peers]),
        median_ev_ebitda=_median([p.ev_ebitda for p in valid_peers]),
    )
