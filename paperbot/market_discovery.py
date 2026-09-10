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
    # Added 2026-09-10 for HEDGE_LIQUIDITY_MULTIPLIER (behavior_config.py):
    # confirmed real, well-powered correlation between a market's
    # liquidity and whether he hedges it (t=5.263, n=1023/322) -- this is
    # Gamma's OWN liquidityNum field (a platform-computed aggregate
    # metric), NOT the same thing as BookState.total_depth_usd() (summed
    # from live CLOB bid/ask levels) -- the two are different quantities,
    # and the calibration was measured against THIS one (via
    # market_snapshots.jsonl, which stores Gamma's liquidity/liquidityNum
    # field, matching market_snapshotter.py's own exact fallback order).
    # Already present in the raw Gamma response fetch_active_markets()
    # fetches every poll -- no new API call needed, just parsing a field
    # that was already being fetched and discarded.
    liquidity: Optional[float] = None

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
        liquidity=_to_float(raw.get("liquidityNum") if raw.get("liquidityNum") is not None else raw.get("liquidity")),
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
                          timeout: float = 10.0, closed: Optional[bool] = None) -> Optional[dict]:
    """Look up a single market by slug -- used post-close to check for
    resolution. `closed`, if given, is passed through as Gamma's own
    `closed` query filter; omitted (None, the default) queries with no
    filter at all, exactly the previous behavior. See
    fetch_market_for_resolution for why resolution checks need BOTH
    forms, not just this one."""
    sess = session or requests
    params = {"slug": slug}
    if closed is not None:
        params["closed"] = "true" if closed else "false"
    resp = sess.get(f"{config.GAMMA_BASE_URL}/markets", params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data[0] if data else None
    return data


def fetch_market_for_resolution(slug: str, session: Optional[requests.Session] = None,
                                 timeout: float = 10.0) -> Optional[dict]:
    """
    Resolution-specific lookup -- use this (not fetch_market_by_slug directly)
    from resolution_tick.

    CONFIRMED LIVE (2026-09-08, via direct curl against gamma-api.polymarket.com
    for real stuck slugs): the unfiltered `/markets?slug=X` query stops
    returning a market a few minutes after that market's own window
    closes -- well before Gamma has actually flagged it `closed=true`
    internally (separately confirmed: `closed` can stay False for 30+
    minutes past a fully decisive outcome, see infer_resolution_from_price's
    docstring). That leaves a real gap where a market is invisible to
    BOTH the unfiltered query (already aged out of it) AND an explicit
    `closed=true` query (Gamma hasn't flagged it yet) for several
    minutes in between.

    Querying only the unfiltered form (the previous behavior) meant that
    once a market aged out, every subsequent resolution check returned
    nothing FOREVER, no matter how many times it was retried -- the
    confirmed root cause of a 31% market-abandonment rate (103/333
    markets in one 8h live window) before this fix: any filled orders in
    an abandoned market are silently excluded from realized P&L. See
    trader_intel/README.md's status log for the live diagnosis.

    Fix: try the unfiltered query first (catches a market in the first
    few minutes after close, often resolvable immediately via
    infer_resolution_from_price even though `closed` isn't set yet). If
    that comes back empty, retry once with `closed=true` explicitly
    (catches it once Gamma has since finished flagging it closed). Two
    requests in the worst case, but resolution_tick's own cooldown/backoff
    already bounds how often this runs per market -- and closing the gap
    means far FEWER total retries in aggregate, since markets stop
    getting stuck for the full 2h abandon window.
    """
    raw = fetch_market_by_slug(slug, session=session, timeout=timeout)
    if raw is not None:
        return raw
    return fetch_market_by_slug(slug, session=session, timeout=timeout, closed=True)


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


def infer_resolution_from_price(raw: dict, threshold: float = 0.99) -> Optional[str]:
    """
    Fallback resolution signal for a market Gamma hasn't marked `closed`
    yet. Confirmed live: these 5-minute crypto Up/Down markets can sit
    with closed=False for 30+ minutes after their trading window ends,
    even while outcomePrices are already fully decisive (observed
    0.995-0.9995, stable across many checks 10+ minutes apart) -- `closed`
    is evidently not a prompt signal for this market type, if it ever
    flips via this endpoint at all.

    Only call this for a market this bot has independently confirmed is
    done trading (i.e. already in pending_resolution, retired from
    _candidate_slugs because its own window ended) -- never for a market
    still considered tradeable. That's what makes trusting a very
    decisive, unqualified-by-`closed` price a reasonable signal here: it
    isn't "the market might still move," it's "our own state machine
    already knows this window is over, and the price has settled."

    Ignores `closed` entirely (unlike parse_resolution) and uses a higher
    bar (default 0.99 vs 0.9) since there's no `closed` confirmation to
    lean on.
    """
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
    if prices[best_idx] < threshold:
        return None
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
