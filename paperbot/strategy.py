"""
Per-cycle decision logic: regime classification, availability, side
persistence, sizing (including the floor-lot roll), and the 90-second
timing gate. Produces OrderIntent objects; does not touch the network or
the fill simulator directly (bot.py wires those together).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from . import behavior_config as bc
from . import config
from .book import BookState
from .market_discovery import Market


@dataclass
class MarketActivityState:
    """Tracks what's already happened in one market, for position-index
    and side-persistence purposes. One instance per condition_id."""
    entry_count: int = 0
    last_side: Optional[str] = None  # "Up" or "Down"
    last_price_seen: Optional[float] = None

    def position_tier(self) -> str:
        return bc.position_tier_for_index(self.entry_count)

    def record_entry(self, side: str) -> None:
        self.entry_count += 1
        self.last_side = side


@dataclass
class OrderIntent:
    condition_id: str
    token_id: str
    asset: str
    side: str            # "Up" or "Down"
    regime: str
    position_tier: str
    price: float
    size_shares: float
    notional_usd: float
    is_floor_lot: bool
    reason: str = ""


def decide_side(asset: str, activity: MarketActivityState, rng: random.Random) -> str:
    """If the market already has an established side, keep it with
    probability SIDE_PERSISTENCE[asset]; otherwise flip. If this is the
    first entry in the market, there's no historical parameter for the
    *initial* side (only persistence of subsequent entries was measured),
    so we pick uniformly at random -- a documented simplification."""
    if activity.last_side is None:
        return rng.choice(["Up", "Down"])
    persistence = bc.SIDE_PERSISTENCE[asset]
    if rng.random() < persistence:
        return activity.last_side
    return "Down" if activity.last_side == "Up" else "Up"


def gradient_score(asset: str, regime: str, recent_price_delta: Optional[float]) -> float:
    """
    Soft tiebreak score in [0, 1] for how well a candidate opportunity
    matches the universal weakness/strength gradient: CHEAP entries tend to
    follow a recent drop, HIGH entries tend to follow a recent rise. Used
    only to ORDER multiple simultaneously-eligible candidates within one
    bot cycle -- never to filter one out.
    """
    if recent_price_delta is None:
        return 0.5  # neutral -- no recent history to judge by
    bias = bc.GRADIENT_BIAS_PCT.get(asset, {})
    if regime == "CHEAP" and recent_price_delta < -bc.GRADIENT_FLAT_EPSILON:
        return bias.get("CHEAP_follows_drop", 50.0) / 100.0
    if regime == "HIGH" and recent_price_delta > bc.GRADIENT_FLAT_EPSILON:
        return bias.get("HIGH_follows_rise", 50.0) / 100.0
    return 0.5


def availability_check(book: BookState) -> tuple[bool, str]:
    """
    Simple, UNIFORM liquidity sanity check -- deliberately NOT scaled by
    regime band. Returns (ok, reason_if_not_ok).
    """
    if book.best_bid is None or book.best_ask is None:
        return False, "no two-sided book"
    if book.spread is None or book.spread > config.MAX_SPREAD:
        return False, f"spread {book.spread} exceeds {config.MAX_SPREAD}"
    depth = book.total_depth_usd()
    if depth < config.MIN_BOOK_DEPTH_USD:
        return False, f"depth ${depth:.2f} below ${config.MIN_BOOK_DEPTH_USD}"
    return True, ""


def timing_ok(market: Market, now: Optional[float] = None) -> bool:
    return market.seconds_remaining(now) >= config.MIN_SECONDS_BEFORE_CLOSE


def decide_size(asset: str, regime: str, position_tier: str, price: float,
                 rng: random.Random) -> tuple[float, bool]:
    """
    Returns (notional_usd, is_floor_lot). Rolls the floor-lot tier first for
    assets where it's modeled (Part 4); falls back to the normal
    entry-count sizing curve (Part 3) with a small symmetric jitter around
    the confirmed median, since only medians -- not full distributions --
    were measured.
    """
    floor_p = bc.floor_lot_probability(asset, regime, position_tier)
    if floor_p > 0 and rng.random() < floor_p:
        notional = config.FLOOR_LOT_SIZE_SHARES * price
        return notional, True

    median = bc.median_entry_notional(asset, regime, position_tier)
    jitter = 1.0 + rng.uniform(-config.SIZING_JITTER_FRACTION, config.SIZING_JITTER_FRACTION)
    return max(median * jitter, 0.0), False


def build_order_intent(market: Market, book: BookState, activity: MarketActivityState,
                        rng: random.Random, recent_price_delta: Optional[float] = None,
                        now: Optional[float] = None) -> Optional[OrderIntent]:
    """
    Runs the full per-market pipeline (steps 1-6 of Part 5) and returns an
    OrderIntent, or None if this market isn't tradeable right now. Does NOT
    check for an existing pending order in this market -- bot.py enforces
    at-most-one-resting-order-per-market before calling this.
    """
    if not timing_ok(market, now):
        return None

    price = book.best_bid
    if price is None:
        return None
    regime = bc.classify_regime(price)

    ok, reason = availability_check(book)
    if not ok:
        return None

    side = decide_side(market.asset, activity, rng)
    position_tier = activity.position_tier()
    notional, is_floor_lot = decide_size(market.asset, regime, position_tier, price, rng)

    min_size = market.order_min_size
    if min_size is not None and notional < min_size * price:
        # This simulated order would be rejected by the exchange's own
        # runtime-fetched minimum order size -- most relevant for the
        # floor-lot tier, whose whole point is a tiny notional. Respect the
        # real platform constraint rather than force an unrealistic fill.
        return None

    size_shares = notional / price if price > 0 else 0.0
    if size_shares <= 0:
        return None

    token_id = market.token_id_up if side == "Up" else market.token_id_down

    return OrderIntent(
        condition_id=market.condition_id,
        token_id=token_id,
        asset=market.asset,
        side=side,
        regime=regime,
        position_tier=position_tier,
        price=price,
        size_shares=size_shares,
        notional_usd=notional,
        is_floor_lot=is_floor_lot,
        reason="floor_lot" if is_floor_lot else "entry_curve",
    )


def would_cross_spread(book: BookState, price: float) -> bool:
    """postOnly semantics: a BUY at `price` is marketable (would cross) if
    price >= best_ask. Polymarket rejects such an order rather than filling
    it -- this bot must never even construct one, per Part 1."""
    if book.best_ask is None:
        return False
    return price >= book.best_ask


def safe_postonly_price(book: BookState, intended_price: float) -> Optional[float]:
    """
    Returns a price guaranteed not to cross the spread against the CURRENT
    book, or None if no safe price exists (e.g. crossed/locked book). Per
    Part 1: retry once against fresh book data (the caller re-fetches book
    and calls this again), then skip the cycle if still marketable.
    """
    if book.best_bid is None or book.best_ask is None:
        return None
    price = min(intended_price, book.best_bid)
    if would_cross_spread(book, price):
        # one tick below best_ask, but not above the intended price
        price = min(intended_price, book.best_ask - book.tick_size)
    if price <= 0 or would_cross_spread(book, price):
        return None
    return round(price, 6)
