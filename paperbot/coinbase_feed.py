"""
Live Coinbase spot-price poller for the standalone Coinbase-momentum bot.

First live-network-feed architecture in this project -- every other
signal comes from Polymarket's own book/Gamma. Deliberately isolated to
this one module: nothing in strategy.py/behavior_config.py (the
trader-replica code) ever imports this, and this module never imports
those either. See config.py's Coinbase-momentum section for why this
exists as its own thing.

Uses Coinbase Exchange's public (no API key) ticker endpoint -- the same
host already used (offline, historically) by /opt/trader-intel's
fetch_spot_candles.py to build the research candle cache this bot's
design was validated against. Confirmed reachable from this project's
AWS server without the geo-blocking issue Binance's public API has there
(HTTP 451 from a US IP; see live-momentum-wiring-tested-rejected.md).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional

import requests

logger = logging.getLogger("paperbot.coinbase_feed")

PRODUCT_BY_ASSET = {
    "Bitcoin": "BTC-USD",
    "Ethereum": "ETH-USD",
    "Solana": "SOL-USD",
}

# How much history to retain per asset, beyond the longest lookback this
# bot will ever ask trailing_return() for -- a fixed generous buffer
# rather than threading a config value in here, since this is cheap
# (a few hundred small tuples per asset at most) and this module is
# meant to stay decision-agnostic (config.MOMENTUM_LOOKBACK_MINUTES is a
# strategy concern, not a feed concern).
_HISTORY_SECONDS = 20 * 60.0


class CoinbaseSpotFeed:
    """Polls Coinbase's public ticker for each asset no more often than
    `poll_interval` seconds, keeping a rolling (ts, price) history per
    asset so trailing_return() can answer "what was the return over the
    last N minutes" at any later call, without re-fetching."""

    def __init__(self, assets: list[str], session: Optional[requests.Session] = None,
                 poll_interval: float = 15.0):
        self.assets = [a for a in assets if a in PRODUCT_BY_ASSET]
        self.session = session or requests.Session()
        self.poll_interval = poll_interval
        self._history: dict[str, deque] = {a: deque() for a in self.assets}
        self._last_poll_at: dict[str, float] = {}

    async def poll_tick(self, now: Optional[float] = None) -> None:
        """Polls every asset whose poll_interval has elapsed. Never
        raises -- a single asset's fetch failure (network blip, rate
        limit, transient 5xx) is logged and skipped, leaving that asset's
        history exactly as stale as it already was; trailing_return()
        simply returns None once the gap gets too old to trust."""
        now = now if now is not None else time.time()
        for asset in self.assets:
            last = self._last_poll_at.get(asset, 0.0)
            if now - last < self.poll_interval:
                continue
            self._last_poll_at[asset] = now
            product = PRODUCT_BY_ASSET[asset]
            try:
                price = await asyncio.to_thread(self._fetch_ticker, product)
            except Exception:
                logger.warning("coinbase ticker fetch failed for %s", product, exc_info=True)
                continue
            if price is None:
                continue
            history = self._history[asset]
            history.append((now, price))
            cutoff = now - _HISTORY_SECONDS
            while history and history[0][0] < cutoff:
                history.popleft()

    def _fetch_ticker(self, product: str) -> Optional[float]:
        url = f"https://api.exchange.coinbase.com/products/{product}/ticker"
        resp = self.session.get(url, timeout=5.0, headers={"User-Agent": "coinbase-momentum-bot/1.0"})
        resp.raise_for_status()
        data = resp.json()
        price = data.get("price")
        return float(price) if price is not None else None

    def trailing_return(self, asset: str, now: Optional[float] = None,
                         minutes: float = 3.0) -> Optional[float]:
        """(current_price - price_minutes_ago) / price_minutes_ago, or
        None if there isn't a current sample or nothing old enough in
        history yet (e.g. right after startup, before `minutes` worth of
        polling has accumulated). "price_minutes_ago" is the OLDEST
        sample still at or after the target time -- matches the offline
        validation's minute-bucketed-close methodology closely enough at
        this poll cadence (default 15s, vs. a 3-minute window) without
        needing to reproduce exact minute bucketing live."""
        now = now if now is not None else time.time()
        history = self._history.get(asset)
        if not history:
            return None
        target = now - minutes * 60.0
        if history[0][0] > target:
            # Not enough history yet -- even the OLDEST sample we have is
            # more recent than "target minutes ago". Using it anyway (the
            # loop below would happily pick it as the earliest match >=
            # target) would silently understate the real window and
            # report a return computed over less time than `minutes`,
            # without any way for the caller to tell the difference from
            # a genuine reading. Caught by a real test, not by inspection.
            return None
        cur_ts, cur_price = history[-1]
        past_price = None
        for ts, price in history:
            if ts >= target:
                past_price = price
                break
        if past_price is None or past_price == 0:
            return None
        return (cur_price - past_price) / past_price
