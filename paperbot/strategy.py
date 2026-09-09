"""
Per-cycle decision logic: regime classification, availability, side
persistence, sizing (including the floor-lot roll), and the 90-second
timing gate. Produces OrderIntent objects; does not touch the network or
the fill simulator directly (bot.py wires those together).
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Optional

from . import behavior_config as bc
from . import config
from .book import BookState
from .market_discovery import Market

logger = logging.getLogger("paperbot.strategy")


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
    # CHANGED 2026-09-09 (was: hedge_placed: bool, permanently locking
    # after the market's first hedge-shaped entry). CONFIRMED LIVE:
    # collapsing raw trade rows into genuine decisions (merging same-side
    # fills within 5s, since 63.5% of raw rows turned out to be CLOB
    # fragments of one order, not separate decisions) still leaves a real
    # multi-hedge pattern -- only 39.8% of dual-sided markets stop at 1
    # hedge decision. A single boolean can't represent "how many hedges
    # so far", which decide_hedge/HEDGE_CONTINUATION_PROBABILITY now need
    # to decide whether a 2nd/3rd/4th+ hedge should follow. See
    # HEDGE_CONTINUATION_PROBABILITY's docstring in behavior_config.py.
    hedge_count: int = 0

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
            self.hedge_count += 1

    def release_unfilled(self, side: str, notional_usd: float) -> None:
        """
        CONFIRMED LIVE (2026-09-09, code-review pass): record_entry() above
        adds the FULL intended notional to cost_by_side at PLACEMENT time
        (see config.py's MAX_OPEN_ORDERS_PER_MARKET docstring for why --
        entry_count/position_tier need this to handle concurrent resting
        orders correctly), but nothing anywhere ever corrected it back down
        when an order's unfilled portion is later cancelled or expires.
        Measured live: 38.6% of placed orders get cancelled, 93% of those
        fully unfilled (1,263/1,353 cancellations in a 2h window across all
        three bots) -- meaning roughly a third of tracked cost_by_side was
        phantom exposure that never actually happened, silently corrupting
        every dominant_side() call, every hedge trigger decision, and
        (worse, post tonight's HEDGE_SIZE_RATIO CHEAP fix, where the ratio
        can exceed 1.0) every hedge's SIZE for every market that ever had a
        cancelled or expired order in it. Called from bot.py's
        manage_orders_tick for every order manage_open_orders reports as
        newly terminal-with-a-remainder (CANCELLED, EXPIRED_UNFILLED, or
        PARTIALLY_FILLED with expired_remainder/cancelled_remainder set),
        using (unfilled_shares * order.price) as the phantom amount --
        order.price is the order's own last resting price, not the
        original placement price, which drifts slightly on a REPRICE but
        is the best available estimate of what the never-filled portion
        would actually have cost. Floored at 0.0 as a defensive guard
        against float-precision overshoot, not because a real negative is
        expected.
        """
        self.cost_by_side[side] = max(0.0, self.cost_by_side.get(side, 0.0) - notional_usd)


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
    is_scout: bool = False
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
    Returns the hedge side ("Up"/"Down") if THIS entry should be a
    deliberate insurance leg on the opposite side from the first entry,
    or None if not (in which case the caller falls through to the
    ordinary decide_side/decide_size flow).

    CHANGED 2026-09-08 (was: only ever fires at entry_count == 1, the
    market's literal 2nd entry, using HEDGE_TRIGGER_PROBABILITY as a
    single-shot probability there). Fresh re-derivation from the full
    trader mirror found that design structurally missed most real
    hedges: only 23.5% of them land at index 1; median index is 4. Now
    checked at EVERY entry_count from 1 up through behavior_config's
    modeled hazard window, using hedge_attempt_hazard's per-attempt
    conditional probability -- rescaled so the cumulative probability
    across the whole sequence still exactly reproduces
    HEDGE_TRIGGER_PROBABILITY's calibrated overall frequency (unchanged),
    while the SHAPE of when it fires now matches the real timing
    distribution instead of concentrating everything at one point.

    CHANGED AGAIN 2026-09-09 (was: hedge_placed guarding every attempt,
    at most one hedge-shaped entry ever per market). CONFIRMED LIVE:
    even after collapsing raw trade rows into genuine decisions (merging
    same-side CLOB fragments -- 63.5% of raw rows turned out to be
    fragments, not separate decisions), only 39.8% of dual-sided markets
    actually stop at 1 hedge; the rest keep going. Once the market's
    FIRST hedge has fired (hedge_count >= 1), a further hedge-shaped
    entry is now governed by HEDGE_CONTINUATION_PROBABILITY instead of
    being blocked outright -- see its docstring in behavior_config.py for
    the calibration. hedge_attempt_hazard above still exclusively governs
    the FIRST hedge (hedge_count == 0); nothing about that path changed.

    HARDENED SAME NIGHT: confirmed live that the per-opportunity
    continuation roll runs away without a ceiling -- see
    config.MAX_HEDGE_COUNT_PER_MARKET's docstring for the exact numbers
    (real distribution vs. this bot's, and the confirmed net-negative $
    effect on the ledger). Capped as a direct mitigation while the
    underlying per-opportunity-vs-real-cadence mismatch gets a proper
    fix.
    """
    if activity.entry_count < 1 or activity.first_entry_regime is None:
        return None
    if activity.hedge_count >= config.MAX_HEDGE_COUNT_PER_MARKET:
        return None
    dominant = activity.dominant_side()
    if dominant is None:
        return None
    if activity.hedge_count == 0:
        p = bc.hedge_attempt_hazard(asset, activity.first_entry_regime, activity.entry_count)
    else:
        p = bc.hedge_continuation_probability(activity.hedge_count)
    if p > 0 and rng.random() < p:
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
    # Continuous within-band scaling (2026-09-08 finding): a real, cell-
    # verified relationship between price and size WITHIN CORE/HIGH that
    # the discrete regime bucket alone can't express. Mean-neutral by
    # construction (see WITHIN_BAND_SIZE_SLOPE's docstring) -- a no-op
    # (1.0x) everywhere it wasn't specifically verified (CHEAP/MID, the
    # three dormant assets), so this can't silently change behavior
    # outside the two regimes it was actually calibrated on.
    within_band = bc.within_band_size_multiplier(asset, regime, price)
    # config.SIZE_SCALE_FACTOR is a no-op (1.0) everywhere except a
    # deliberately small-bankroll instance -- see its docstring in
    # config.py. Applied here (not to the floor-lot branch above) so the
    # tiny, independently-calibrated probe tier is never pushed below a
    # real exchange minimum by a scale-down meant for the ordinary curve.
    return max(median * jitter * within_band * config.SIZE_SCALE_FACTOR, 0.0), False


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
        # Diagnostic (2026-09-08): this was a silent None-return -- same
        # blind spot the min-size skip had before it got a log line.
        # Investigating why HIGH-band participation is far below the real
        # trader's (3% of placements vs his 13% of actual activity in a
        # same-window comparison) needs to see whether HIGH-band
        # opportunities are being SEEN but REJECTED here (thin/wide book,
        # which is structurally more likely right at a price extreme) vs
        # never reached at all -- indistinguishable from the outside
        # without this.
        logger.info("SKIP asset=%s regime=%s: %s", market.asset, regime, reason)
        return None

    position_tier = activity.position_tier()

    min_size = market.order_min_size

    if is_hedge:
        # Hedge sizing scales with what it's protecting (the dominant
        # side's cost so far), not the position-count curve -- that's the
        # whole point: it's proportional insurance, not another ordinary
        # entry. See HEDGE_SIZE_RATIO's docstring in behavior_config.py.
        #
        # CHANGED 2026-09-09: hedge_count==0 (the market's FIRST hedge)
        # still uses HEDGE_SIZE_RATIO, unchanged. A 2nd+ hedge (hedge_count
        # >= 1, now reachable at all since decide_hedge no longer hard-
        # blocks after the first) uses HEDGE_CONTINUATION_SIZE_RATIO
        # instead -- a real, separately-calibrated decaying curve (2nd
        # hedge sizes at ~19% of the dominant side's RUNNING cost, 3rd at
        # ~13%, 4th+ at ~11% -- much flatter than the drop from 1st to
        # 2nd), not the same ratio repeated. See its docstring.
        dominant_cost = activity.cost_by_side.get(activity.dominant_side(), 0.0)
        if activity.hedge_count == 0:
            ratio = bc.hedge_size_ratio(market.asset, activity.first_entry_regime)
        else:
            ratio = bc.hedge_continuation_size_ratio(activity.hedge_count + 1)
        jitter = 1.0 + rng.uniform(-config.SIZING_JITTER_FRACTION, config.SIZING_JITTER_FRACTION)
        notional = max(dominant_cost * ratio * jitter, 0.0)
        is_floor_lot = False

        if min_size is not None and notional < min_size * price:
            # CONFIRMED LIVE (2026-09-08): hedge notional -- deliberately
            # small, proportional insurance -- falls below the exchange's
            # real minimum far more often than ordinary entries (23 of 58
            # min-size skips in one window were hedge attempts, despite
            # hedges being a small minority of total attempts). Previously
            # this meant the WHOLE tick produced nothing: decide_hedge
            # intercepts every entry_count==1 evaluation, so a rejected hedge
            # silently blocked the ORDINARY entry that would otherwise
            # have happened that same tick too -- throttling how often a
            # market's second entry landed at all, worst in exactly the
            # asset/regime cells with the highest hedge_trigger_
            # probability. Falling through to a normally-sized entry on
            # the same (still opposite-of-dominant) side instead of
            # abandoning the tick is what a real trader would do too: if
            # the insurance leg you intended is too small to place, you
            # don't skip the market entirely, you just trade normally.
            logger.info(
                "SKIP asset=%s regime=%s pos=%s hedge=True: notional $%.4f below exchange min "
                "($%.4f = %.2f shares * $%.4f) -- falling through to an ordinary entry",
                market.asset, regime, position_tier, notional, min_size * price, min_size, price,
            )
            is_hedge = False
            notional, is_floor_lot = decide_size(market.asset, regime, position_tier, price, rng)
    else:
        notional, is_floor_lot = decide_size(market.asset, regime, position_tier, price, rng)

    is_scout = False
    if not is_hedge and not is_floor_lot and activity.entry_count == 0:
        # SCOUT: a deliberately small, tentative first entry -- see
        # SCOUT_PROBABILITY/SCOUT_SIZE_RATIO's docstring in
        # behavior_config.py. Only reachable at the market's true first
        # entry (decide_hedge structurally can never fire this early --
        # dominant_side() needs at least one prior entry to exist), and
        # never stacks with a floor-lot roll (that's an orthogonal, already
        # -tiny mechanism keyed off share-count minimums, not first-entry
        # tentativeness -- scaling it down further would just inflate its
        # already-expected skip rate for no modeling benefit).
        if rng.random() < bc.scout_probability(market.asset):
            is_scout = True
            notional = notional * bc.scout_size_ratio(market.asset)

    if min_size is not None and notional < min_size * price:
        # This simulated order would be rejected by the exchange's own
        # runtime-fetched minimum order size. By this point is_hedge is
        # always False here -- a hedge that failed this same check
        # already got its own SKIP line and fell through above; this
        # only fires for an ordinary entry (or a hedge's ordinary-entry
        # fallback) that's ALSO too small.
        floor_notional = min_size * price
        if is_floor_lot:
            # Floor-lot's whole point IS a tiny, often-below-minimum
            # notional (see FLOOR_LOT_SIZE_SHARES) -- matches how his own
            # tiny partial-fill remainders look in the real trade data
            # (confirmed live 2026-09-08: the trader's per-fill share-size
            # distribution has a long near-zero tail, p10=0.02 shares,
            # almost certainly leftover fragments of larger orders rather
            # than genuine sub-minimum placements). Bumping this tier up
            # to the real minimum would defeat its entire purpose, so it
            # stays exempt and genuinely skips, unchanged from before.
            logger.info(
                "SKIP asset=%s regime=%s pos=%s hedge=%s scout=%s: notional $%.4f below exchange min "
                "($%.4f = %.2f shares * $%.4f)", market.asset, regime, position_tier, is_hedge, is_scout,
                notional, floor_notional, min_size, price,
            )
            return None
        # CONFIRMED LIVE (2026-09-08): silently skipping every ORDINARY
        # entry that fell below the minimum was a real, large problem --
        # 9,616 skips vs 5,542 successful placements in one ~8h window,
        # heavily concentrated in cells the SAME night's recalibration
        # (matching his smaller recent sizing) had shrunk toward this
        # exact floor. We replicate him by CAPITAL (ENTRY_SIZING_USD is
        # his measured dollar size, share count is only ever a derived,
        # price-dependent quantity), but the exchange's minimum is a
        # SHARE quantity -- the same dollar target needs more capital to
        # clear it at a high price than a low one, so a purely
        # dollar-calibrated median will sometimes fall short depending on
        # exactly where price sits within its band at decision time.
        # Bumping up to the real minimum (using the ACTUAL current price,
        # not a worst-case guess) instead of skipping preserves both
        # goals at once: the calibrated median still governs sizing
        # whenever it's legally placeable, and a trade that would
        # otherwise be lost entirely now happens at the smallest size the
        # exchange actually allows, instead of not happening at all.
        logger.info(
            "BUMP asset=%s regime=%s pos=%s hedge=%s scout=%s: notional $%.4f below exchange min, "
            "raised to $%.4f (%.2f shares * $%.4f) instead of skipping",
            market.asset, regime, position_tier, is_hedge, is_scout, notional, floor_notional, min_size, price,
        )
        notional = floor_notional

    size_shares = notional / price if price > 0 else 0.0
    if size_shares <= 0:
        return None

    token_id = market.token_id_up if side == "Up" else market.token_id_down

    if is_hedge:
        reason = "hedge"
    elif is_scout:
        reason = "scout_entry"
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
        is_scout=is_scout,
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
