"""
Gamma API polling and active-market tracking for the six 5-minute crypto
Up/Down markets.

fetch_active_markets queries Gamma by exact slug for each asset's current
and next 5-minute window, rather than paginating the full active-markets
table. That's not just an efficiency choice: an earlier version did paginate
`GET /markets?active=true&closed=false` and, on a real Railway deployment,
Gamma returned `422 Unprocessable Entity` once the offset got deep enough
(that endpoint covers every active market on the whole platform, not just
these six, so it got there fast). Slug lookups are immune to that and
confirmed working in that same live deployment.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from . import config

logger = logging.getLogger("paperbot.market_discovery")


@dataclass(frozen=True)
class Market:
    condition_id: str
    slug: str
    question: str
    asset: str                     # canonical name, e.g. "Bitcoin"
    end_time: float                # unix seconds
    token_id_up: str
    token_id_down: str
    order_min_size: Optional[float]     # from Gamma's orderMinSize, bootstrap value
    order_min_tick_size: Optional[float]  # from Gamma's orderPriceMinTickSize, bootstrap value

    def seconds_remaining(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        return self.end_time - now


def _asset_for_slug(slug: str) -> Optional[str]:
    for asset, prefix in config.ASSET_SLUG_PREFIXES.items():
        if slug.startswith(prefix):
            return asset
    return None


def _parse_iso(ts: str) -> float:
    # Gamma returns e.g. "2026-08-23T18:55:00Z"
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts).timestamp()


def parse_market(raw: dict) -> Optional[Market]:
    """Parse one Gamma market object into a Market, or None if it isn't one
    of our six tracked 5-minute Up/Down markets."""
    slug = raw.get("slug", "")
    asset = _asset_for_slug(slug)
    if asset is None:
        return None

    try:
        token_ids = json.loads(raw["clobTokenIds"]) if isinstance(raw.get("clobTokenIds"), str) \
            else raw.get("clobTokenIds")
        outcomes = json.loads(raw["outcomes"]) if isinstance(raw.get("outcomes"), str) \
            else raw.get("outcomes")
    except (KeyError, json.JSONDecodeError, TypeError):
        logger.warning("skipping market %s: could not parse clobTokenIds/outcomes", slug)
        return None

    if not token_ids or not outcomes or len(token_ids) != 2 or len(outcomes) != 2:
        logger.warning("skipping market %s: expected exactly 2 outcomes, got %r", slug, outcomes)
        return None

    outcome_to_token = dict(zip(outcomes, token_ids))
    token_up = outcome_to_token.get("Up")
    token_down = outcome_to_token.get("Down")
    if token_up is None or token_down is None:
        logger.warning("skipping market %s: outcomes were %r, expected Up/Down", slug, outcomes)
        return None

    end_date = raw.get("endDate") or raw.get("end_date_iso")
    if not end_date:
        logger.warning("skipping market %s: no endDate", slug)
        return None

    def _to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return Market(
        condition_id=raw.get("conditionId", raw.get("condition_id", "")),
        slug=slug,
        question=raw.get("question", ""),
        asset=asset,
        end_time=_parse_iso(end_date),
        token_id_up=str(token_up),
        token_id_down=str(token_down),
        order_min_size=_to_float(raw.get("orderMinSize")),
        order_min_tick_size=_to_float(raw.get("orderPriceMinTickSize")),
    )


# 5-minute markets sit on a fixed 300-second grid: a market's slug embeds
# the epoch second its window STARTS at, and that epoch is always a
# multiple of BUCKET_SECONDS (confirmed from the historical trade data,
# e.g. "bnb-updown-5m-1786127400" -- 1786127400 % 300 == 0).
BUCKET_SECONDS = 300


def _candidate_slugs(asset: str, now: Optional[float] = None) -> list[str]:
    """
    The slug(s) for `asset`'s current and next 5-minute window. Checking
    two buckets (not just the current one) covers both "the next window
    already exists and is worth onboarding early" and "we polled right at
    a boundary and the current window's market hasn't rolled over in our
    cache yet" -- without ever having to enumerate markets we don't care
    about.
    """
    now = now if now is not None else time.time()
    prefix = config.ASSET_SLUG_PREFIXES[asset]
    current_bucket = int(now // BUCKET_SECONDS) * BUCKET_SECONDS
    return [
        f"{prefix}{current_bucket}",
        f"{prefix}{current_bucket + BUCKET_SECONDS}",
    ]


def fetch_active_markets(session: Optional[requests.Session] = None,
                          assets: Optional[list[str]] = None,
                          timeout: float = 10.0,
                          now: Optional[float] = None) -> list[Market]:
    """
    Fetch exactly the markets we can trade right now: for each tracked
    asset, look up its current and next 5-minute window BY SLUG, directly.

    This deliberately does NOT enumerate Gamma's full active-markets table
    (an earlier version did `GET /markets?active=true&closed=false` and
    paginated through it) -- that endpoint returns every active market on
    the entire platform across every category, not just these six, and in
    production that pagination ran into a `422 Unprocessable Entity` once
    the offset got large enough (Gamma appears to cap how deep you can
    paginate). Querying the ~12 slugs we actually care about is both
    immune to that limit and far cheaper per poll.
    """
    assets = assets or config.ALL_ASSETS
    sess = session or requests
    markets: list[Market] = []
    for asset in assets:
        for slug in _candidate_slugs(asset, now):
            try:
                raw = fetch_market_by_slug(slug, session=sess, timeout=timeout)
            except requests.HTTPError:
                logger.warning("lookup failed for slug %s; skipping this cycle", slug,
                                exc_info=True)
                continue
            if raw is None or raw.get("closed"):
                continue
            m = parse_market(raw)
            if m is not None:
                markets.append(m)
    return markets


def fetch_market_by_slug(slug: str, session: Optional[requests.Session] = None,
                          timeout: float = 10.0) -> Optional[dict]:
    """Look up a single market by slug -- used post-close to check for
    resolution."""
    sess = session or requests
    resp = sess.get(f"{config.GAMMA_BASE_URL}/markets", params={"slug": slug}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data[0] if data else None
    return data


def parse_resolution(raw: dict) -> Optional[str]:
    """
    Given a raw Gamma market object, return the winning outcome name
    ("Up"/"Down") if the market has decisively resolved, else None. Pure
    parsing logic, independent of the fetch above -- unit testable with
    synthetic payloads.
    """
    if not raw.get("closed"):
        return None
    try:
        outcomes = json.loads(raw["outcomes"]) if isinstance(raw.get("outcomes"), str) \
            else raw.get("outcomes")
        prices = json.loads(raw["outcomePrices"]) if isinstance(raw.get("outcomePrices"), str) \
            else raw.get("outcomePrices")
    except (KeyError, json.JSONDecodeError, TypeError):
        return None
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    prices = [float(p) for p in prices]
    best_idx = max(range(len(prices)), key=lambda i: prices[i])
    if prices[best_idx] < 0.9:
        return None  # closed but not decisively settled yet -- keep waiting
    return outcomes[best_idx]


class MarketDiscovery:
    """Tracks the currently-active set of Market objects, refreshed on a
    poll interval. Emits nothing by itself -- bot.py diffs successive
    `active` snapshots to manage WS subscriptions and per-market state."""

    def __init__(self, session: Optional[requests.Session] = None,
                 poll_seconds: float = config.MARKET_DISCOVERY_POLL_SECONDS,
                 assets: Optional[list[str]] = None):
        self._session = session or requests.Session()
        self.poll_seconds = poll_seconds
        self.assets = assets or config.ALL_ASSETS
        self.active: dict[str, Market] = {}  # condition_id -> Market
        self._last_poll = 0.0

    def poll(self) -> dict[str, Market]:
        markets = fetch_active_markets(session=self._session, assets=self.assets)
        self.active = {m.condition_id: m for m in markets}
        self._last_poll = time.time()
        return self.active

    def due_for_poll(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self._last_poll) >= self.poll_seconds
