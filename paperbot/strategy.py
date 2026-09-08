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
    """Tracks what's already happened in one market, for position-index,
    side-persistence, AND hedge-leg purposes. One instance per
    condition_id."""
    entry_count: int = 0
    last_side: Optional[str] = None  # "Up" or "Down"
    last_price_seen: Optional[float] = None
    first_entry_regime: Optional[str] = None
    cost_by_side: dict = field(default_factory=lambda: {"Up": 0.0, "Down": 0.0})
    hedge_placed: bool = False

    def position_tier(self) -> str:
        return bc.position_tier_for_index(self.entry_count)

    def dominant_side(self) -> Optional[str]:
        """The side with more cumulative $ committed so far, or None if
        nothing's been placed yet."""
        up, down = self.cost_by_side.get("Up", 0.0), self.cost_by_side.get("Down", 0.0)
        if up == 0.0 and down == 0.0:
            return None
        return "Up" if up >= down else "Down"

    def record_entry(self, side: str, notional_usd: float = 0.0, regime: Optional[str] = None,
                      is_hedge: bool = False) -> None:
        if self.entry_count == 0 and regime is not None:
            self.first_entry_regime = regime
        self.cost_by_side[side] = self.cost_by_side.get(side, 0.0) + notional_usd
        self.entry_count += 1
        self.last_side = side
        if is_hedge:
            self.hedge_placed = True


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
    is_hedge: bool = False
    reason: str = ""


def decide_side(asset: str, activity: MarketActivityState, rng: random.Random,
                 held_side_price: Optional[float] = None) -> str:
    """If the market already has an established side, keep it with
    probability side_persistence_for(asset, regime of the currently-held
    side); otherwise flip. If this is the first entry in the market,
    there's no historical parameter for the *initial* side (only
    persistence of subsequent entries was measured), so we pick uniformly
    at random -- a documented simplification.

    Regime-dependent since 2026-09-08 (see behavior_config.SIDE_PERSISTENCE's
    docstring): keyed by the CURRENTLY-HELD side's regime specifically,
    because that's the only regime knowable before this decision is made --
    what regime persisting or switching actually RESOLVES to is a
    consequence of the choice, not an input to it. `held_side_price` is the
    last-known book price for `activity.last_side`; build_order_intent
    passes it in since it already has both books. Falls back to the
    asset's CHEAP-band rate if the price isn't available for some reason
    (should not happen in practice once last_side is set, but avoids a
    crash over a defensive gap) -- deliberately not a silent 50/50, since
    that would understate the trader's strong observed persistence in
    every regime."""
    if activity.last_side is None:
        return rng.choice(["Up", "Down"])
    held_regime = bc.classify_regime(held_side_price) if held_side_price is not None else "CHEAP"
    persistence = bc.side_persistence_for(asset, held_regime)
    if rng.random() < persistence:
        return activity.last_side
    return "Down" if activity.last_side == "Up" else "Up"


def decide_hedge(asset: str, activity: MarketActivityState, rng: random.Random) -> Optional[str]:
    """
    Returns the hedge side ("Up"/"Down") if this market's SECOND entry
    should be a deliberate insurance leg on the opposite side from the
    first entry, or None if not (in which case the caller falls through
    to the ordinary decide_side/decide_size flow).

    Only ever fires at entry_count == 1 (about to place the 2nd entry) and
    only if no hedge has been placed yet for this market -- see
    HEDGE_TRIGGER_PROBABILITY's docstring in behavior_config.py for why
    this is modeled as a single roll rather than a repeated one.
    """
    if activity.entry_count != 1 or activity.hedge_placed or activity.first_entry_regime is None:
        return None
    dominant = activity.dominant_side()
    if dominant is None:
        return None
    p = bc.hedge_trigger_probability(asset, activity.first_entry_regime)
    if rng.random() < p:
        return "Down" if dominant == "Up" else "Up"
    return None


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


def build_order_intent(market: Market, up_book: BookState, down_book: BookState,
                        activity: MarketActivityState, rng: random.Random,
                        recent_price_delta: Optional[float] = None,
                        now: Optional[float] = None) -> Optional[OrderIntent]:
    """
    Runs the full per-market pipeline (steps 1-6 of Part 5) and returns an
    OrderIntent, or None if this market isn't tradeable right now. Does NOT
    check for an existing pending order in this market -- bot.py enforces
    at-most-one-resting-order-per-market before calling this.

    Requires BOTH the Up and Down token's books, not just one: Down's
    price is (roughly) 1 - Up's price, NOT the same value, so once `side`
    is decided, regime classification, sizing, and the resting price must
    all come from whichever token is actually about to be traded.

    An earlier version took a single `book` (always the Up token's,
    regardless of which side got chosen) and used it for everything,
    treating Down as if it "shared the same regime dynamics" as Up. That
    assumption is only defensible for liquidity shape (spread/depth), not
    price -- and confirmed live to badly mis-classify and mis-size Down
    orders: a HIGH-band Up price (e.g. 0.95, meaning Up is heavily
    favored) was used to size a Down order that actually got postOnly-
    corrected down to Down's real ~0.03 price, producing a HIGH-regime
    notional divided by a CHEAP-regime price -- a 1354-share order that
    should have been a normal small CHEAP-band size.
    """
    if not timing_ok(market, now):
        return None

    hedge_side = decide_hedge(market.asset, activity, rng)
    is_hedge = hedge_side is not None
    if is_hedge:
        side = hedge_side
    else:
        # Regime-dependent persistence needs the CURRENTLY-HELD side's
        # live price, not a stale one -- both books are already available
        # here, so look it up fresh rather than threading a cached value
        # through MarketActivityState. None (not yet an established side,
        # or its book briefly missing) is handled inside decide_side.
        held_book = None
        if activity.last_side == "Up":
            held_book = up_book
        elif activity.last_side == "Down":
            held_book = down_book
        held_side_price = held_book.best_bid if held_book is not None else None
        side = decide_side(market.asset, activity, rng, held_side_price=held_side_price)
    side_book = up_book if side == "Up" else down_book

    price = side_book.best_bid
    if price is None:
        return None
    regime = bc.classify_regime(price)

    ok, reason = availability_check(side_book)
    if not ok:
        return None

    position_tier = activity.position_tier()

    if is_hedge:
        # Hedge sizing scales with what it's protecting (the dominant
        # side's cost so far), not the position-count curve -- that's the
        # whole point: it's proportional insurance, not another ordinary
        # entry. See HEDGE_SIZE_RATIO's docstring in behavior_config.py.
        dominant_cost = activity.cost_by_side.get(activity.dominant_side(), 0.0)
        ratio = bc.hedge_size_ratio(market.asset, activity.first_entry_regime)
        jitter = 1.0 + rng.uniform(-config.SIZING_JITTER_FRACTION, config.SIZING_JITTER_FRACTION)
        notional = max(dominant_cost * ratio * jitter, 0.0)
        is_floor_lot = False
    else:
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

    if is_hedge:
        reason = "hedge"
    elif is_floor_lot:
        reason = "floor_lot"
    else:
        reason = "entry_curve"

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
        is_hedge=is_hedge,
        reason=reason,
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
