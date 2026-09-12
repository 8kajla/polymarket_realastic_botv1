"""
Per-cycle decision logic: regime classification, availability, side
persistence, sizing (including the floor-lot roll), and the 90-second
timing gate. Produces OrderIntent objects; does not touch the network or
the fill simulator directly (bot.py wires those together).
"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    # ADDED 2026-09-10, alongside ADVERSE_MOVE_SIZE_MULTIPLIER: the price
    # of the market's first entry (whichever side), needed to compute how
    # far price has since moved against the dominant side at hedge time.
    # first_entry_regime only records the BUCKET (CHEAP/MID/CORE/HIGH),
    # which is too coarse for the continuous adverse-move signal this
    # measures -- see behavior_config.adverse_move_size_multiplier's
    # docstring.
    first_entry_price: Optional[float] = None
    # ADDED 2026-09-11, alongside the win/loss-conditioned
    # CROSS_MARKET_SIDE_PERSISTENCE upgrade: the SIDE of the market's
    # first entry specifically -- distinct from last_side (the currently-
    # held side, which can change as later entries flip it) and from
    # dominant_side() (the side with more $ committed, which can differ
    # from the first entry when a hedge later outpaces it). The win/loss
    # persistence finding was measured against the FIRST entry's side
    # winning or losing specifically, so this needs its own field rather
    # than reusing either existing one.
    first_entry_side: Optional[str] = None
    # ADDED 2026-09-11, alongside CONVICTION_HEDGE_MULTIPLIER: the dollar
    # notional of the market's first entry specifically (not the
    # cumulative cost_by_side, which grows with every later entry too) --
    # needed to compute "how this market's own conviction compares to
    # this (asset, regime)'s typical first-entry size" at hedge-decision
    # time. See conviction_hedge_multiplier's docstring in
    # behavior_config.py.
    first_entry_notional: Optional[float] = None
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
    # ADDED 2026-09-12 (bug audit #5): entry_count/hedge_count above both
    # increment at PLACEMENT time, for every order placed regardless of
    # whether it ever fills -- but the calibration tables they index into
    # (ENTRY_SIZING_USD via position_tier_for_index, hedge_attempt_hazard,
    # HEDGE_CONTINUATION_PROBABILITY/SIZE_RATIO) were all built from the
    # trader's real trade log, which only contains FILLS (confirmed
    # elsewhere in this project -- his trade log has no record of an order
    # that never filled). ~36% of our own placed orders never fill at all
    # (see release_unfilled's docstring, which already fixed the identical
    # contamination for cost_by_side but never touched these two counters).
    # real_fill_count/real_hedge_fill_count track ONLY confirmed fills,
    # incremented via record_real_fill (called from bot.py's
    # manage_orders_tick the first time an order gets any fill) -- these,
    # not entry_count/hedge_count, are what every calibration-table lookup
    # keyed by "which numbered entry/hedge" should read. entry_count/
    # hedge_count themselves are left unchanged and still used for the
    # placement-time gates that are genuinely about placement, not fill
    # history (the scout eligibility check, and decide_hedge's initial
    # "has anything ever been placed" gate) -- see this project's
    # BUGS_TO_FIX.md #5 for the full reasoning.
    real_fill_count: int = 0
    real_hedge_fill_count: int = 0

    def position_tier(self) -> str:
        return bc.position_tier_for_index(self.real_fill_count)

    def dominant_side(self) -> Optional[str]:
        """The side with more cumulative $ committed so far, or None if
        nothing's been placed yet."""
        up, down = self.cost_by_side.get("Up", 0.0), self.cost_by_side.get("Down", 0.0)
        if up == 0.0 and down == 0.0:
            return None
        return "Up" if up >= down else "Down"

    def record_entry(self, side: str, notional_usd: float = 0.0, regime: Optional[str] = None,
                      is_hedge: bool = False, price: Optional[float] = None) -> None:
        if self.entry_count == 0 and regime is not None:
            self.first_entry_regime = regime
        if self.entry_count == 0 and price is not None:
            self.first_entry_price = price
        if self.entry_count == 0:
            self.first_entry_side = side
            self.first_entry_notional = notional_usd
        self.cost_by_side[side] = self.cost_by_side.get(side, 0.0) + notional_usd
        self.entry_count += 1
        self.last_side = side
        if is_hedge:
            self.hedge_count += 1

    def record_real_fill(self, is_hedge: bool) -> None:
        """Called from bot.py's manage_orders_tick the FIRST time a placed
        order receives any fill (full or partial) -- exactly once per
        order, regardless of how many separate partial fills it eventually
        gets. See real_fill_count/real_hedge_fill_count's docstring above
        for why this exists and what reads it."""
        self.real_fill_count += 1
        if is_hedge:
            self.real_hedge_fill_count += 1

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
                 held_side_price: Optional[float] = None,
                 previous_market_first_side: Optional[str] = None,
                 previous_market_won: Optional[bool] = None) -> str:
    """If the market already has an established side, keep it with
    probability side_persistence_for(asset, regime of the currently-held
    side); otherwise flip.

    If this is the first entry in the market: CHANGED 2026-09-11 (was:
    uniform random, a documented simplification -- "there's no historical
    parameter for the *initial* side"). Real, cross-asset-generalizing,
    confound-checked finding that a market's first entry isn't actually
    independent of the PREVIOUS market's first entry -- see
    bc.cross_market_side_persistence's docstring for the full derivation
    (Wald-Wolfowitz runs test, z=-12.75 pooled; per-asset conditional
    probabilities 52.65%-55.38%; confirmed NOT explained by real spot
    momentum, which runs the opposite direction). `previous_market_first_side`
    is Optional (default None -> falls back to the old uniform-random
    behavior exactly) so every existing call site keeps working unchanged;
    the caller (build_order_intent) is responsible for tracking and
    passing in the prior market's first side per asset.

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
    every regime.

    WIN/LOSS-CONDITIONED (2026-09-11): `previous_market_won` (whether the
    previous market's first-entry side actually won) refines the flat
    cross-market persistence rate above into two regimes -- much stronger
    after a loss than after a win, not hot-hand and not gambler's
    fallacy; see bc.cross_market_side_persistence's docstring for the
    full derivation. Optional (default None -> falls back to the
    win/loss-AVERAGED rate, same as before this parameter existed) so
    every existing call site keeps working unchanged; build_order_intent
    is responsible for tracking and passing in the prior market's real
    outcome per asset (only knowable once resolution_tick observes it)."""
    if activity.last_side is None:
        if previous_market_first_side is not None:
            persistence = bc.cross_market_side_persistence(asset, previous_market_won)
            if rng.random() < persistence:
                return previous_market_first_side
            return "Down" if previous_market_first_side == "Up" else "Up"
        return rng.choice(["Up", "Down"])
    held_regime = bc.classify_regime(held_side_price) if held_side_price is not None else "CHEAP"
    persistence = bc.side_persistence_for(asset, held_regime)
    if rng.random() < persistence:
        return activity.last_side
    return "Down" if activity.last_side == "Up" else "Up"


def decide_hedge(asset: str, activity: MarketActivityState, rng: random.Random,
                  liquidity: Optional[float] = None,
                  is_weekend: Optional[bool] = None,
                  dominant_current_price: Optional[float] = None,
                  prev_hedge_rate: Optional[float] = None) -> Optional[str]:
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

    LIQUIDITY-conditioned (2026-09-10): `liquidity` (Market.liquidity,
    Gamma's liquidityNum -- see its own docstring) scales the FIRST-hedge
    probability only, via bc.hedge_liquidity_multiplier -- confirmed real
    and well-powered (t=5.263) that he hedges more in higher-liquidity
    markets. Deliberately NOT applied to hedge_continuation_probability:
    that finding was measured on "did this market ever go dual-sided",
    which maps onto the first hedge specifically, not the separate
    continuation mechanism. Optional (default None -> no-op, same pattern
    as decide_size's seconds_remaining) so every existing call site keeps
    working unchanged.

    WEEKEND-conditioned (2026-09-10): `is_weekend` scales the FIRST-hedge
    probability only, via bc.weekend_hedge_multiplier -- confirmed real,
    well-powered (z=9.9), and causally explained (lower weekend
    volatility -> more MID/undecided markets -> more hedging) that his
    dual-sided rate is higher on weekends. Same scoping rationale as
    liquidity above: the underlying measurement is "did this market ever
    go dual-sided", not a continuation-shaped quantity. Optional (default
    None -> no-op) for the same reason as every other optional param here.

    ADVERSE-MOVE-conditioned TRIGGER (2026-09-10, same night as the SIZE
    versions in behavior_config.py): `dominant_current_price` (the
    dominant side's live book price, NOT the hedge side's) combines with
    activity.first_entry_price to compute adverse_move, which scales the
    FIRST-hedge probability via bc.adverse_move_hedge_trigger_multiplier
    -- confirmed real and survives regime/attempt_index/TTC confound
    checks, see that function's docstring. Distinct measurement from the
    SIZE multipliers (whether he hedges at all, not how big it is once he
    does) but the same since-origin adverse_move definition. Optional
    (default None -> no-op) for the same reason as liquidity/is_weekend.

    CROSS-MARKET HEDGE-RATE-conditioned (2026-09-11): `prev_hedge_rate`
    (the PREVIOUS market's hedge_count/entry_count for this asset) scales
    the FIRST-hedge probability only, via bc.cross_market_hedge_rate_
    multiplier -- real, cross-asset (r=0.25-0.35 all three), survives
    regime/weekend confounds, though the mechanism isn't fully pinned
    down (see that function's docstring for the honest caveat). Optional
    (default None -> no-op) for the same reason as every other optional
    param here; build_order_intent tracks and passes in the prior
    market's hedge rate per asset.

    CONVICTION-conditioned (2026-09-11): computed INTERNALLY (not a new
    parameter) from activity.first_entry_notional vs this (asset,
    first_entry_regime)'s typical first-entry size, via bc.conviction_
    hedge_multiplier -- real, regime-confound-checked finding that a
    market's own below-median first entry predicts THIS SAME market
    needing more hedging later (mechanism unexplained but the predictor
    survives regime/weekend/liquidity confounds). Computed internally
    (not threaded in from build_order_intent) because activity already
    carries everything needed (first_entry_notional, first_entry_regime)
    -- unlike prev_hedge_rate above, which is genuinely cross-market
    state this function has no other way to see.
    """
    if activity.entry_count < 1 or activity.first_entry_regime is None:
        return None
    # CHANGED 2026-09-12 (bug audit #5, extended scope): hedge_count ->
    # real_hedge_fill_count for every calibration-index/gate use below --
    # hedge_count increments at PLACEMENT time (identical contamination to
    # entry_count, see MarketActivityState's docstring), so a hedge that
    # gets placed and then cancelled/expires unfilled would otherwise still
    # trip MAX_HEDGE_COUNT_PER_MARKET early and shift every later
    # continuation lookup to the wrong index. real_hedge_fill_count only
    # counts confirmed fills, matching what HEDGE_CONTINUATION_PROBABILITY/
    # HEDGE_CONTINUATION_SIZE_RATIO/hedge_attempt_hazard were actually
    # calibrated against.
    if activity.real_hedge_fill_count >= config.MAX_HEDGE_COUNT_PER_MARKET:
        return None
    dominant = activity.dominant_side()
    if dominant is None:
        return None
    if activity.real_hedge_fill_count == 0:
        p = bc.hedge_attempt_hazard(asset, activity.first_entry_regime, activity.real_fill_count)
        if config.ENABLE_HEDGE_LIQUIDITY_MULTIPLIER:
            liq_mult = bc.hedge_liquidity_multiplier(asset, liquidity)
            p = max(0.0, min(1.0, p * liq_mult))
        if config.ENABLE_WEEKEND_HEDGE_MULTIPLIER:
            weekend_mult = bc.weekend_hedge_multiplier(asset, is_weekend)
            p = max(0.0, min(1.0, p * weekend_mult))
        if (config.ENABLE_ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER
                and activity.first_entry_price is not None and dominant_current_price is not None):
            adverse_move = activity.first_entry_price - dominant_current_price
            trigger_mult = bc.adverse_move_hedge_trigger_multiplier(asset, adverse_move)
            p = max(0.0, min(1.0, p * trigger_mult))
        if config.ENABLE_CROSS_MARKET_HEDGE_RATE_MULTIPLIER and prev_hedge_rate is not None:
            rate_mult = bc.cross_market_hedge_rate_multiplier(asset, prev_hedge_rate)
            p = max(0.0, min(1.0, p * rate_mult))
        if (config.ENABLE_CONVICTION_HEDGE_MULTIPLIER
                and activity.first_entry_notional is not None):
            try:
                regime_median = bc.median_entry_notional(asset, activity.first_entry_regime, "first")
            except bc.BehaviorLookupError:
                regime_median = 0.0
            if regime_median > 0 and activity.first_entry_notional > 0:
                conviction_log_ratio = math.log(activity.first_entry_notional / regime_median)
                conviction_mult = bc.conviction_hedge_multiplier(
                    asset, activity.first_entry_regime, conviction_log_ratio)
                p = max(0.0, min(1.0, p * conviction_mult))
    else:
        p = bc.hedge_continuation_probability(activity.real_hedge_fill_count)
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
                 rng: random.Random, seconds_remaining: Optional[float] = None,
                 hours_since_resumption: Optional[float] = None,
                 size_momentum_residual: Optional[float] = None,
                 bankroll_pnl_residual: Optional[float] = None) -> tuple[float, bool]:
    """
    Returns (notional_usd, is_floor_lot). Rolls the floor-lot tier first for
    assets where it's modeled (Part 4); falls back to the normal
    entry-count sizing curve (Part 3) with a small symmetric jitter around
    the confirmed median, since only medians -- not full distributions --
    were measured.

    seconds_remaining, hours_since_resumption, size_momentum_residual, and
    bankroll_pnl_residual are all Optional (default None -> no-op) so
    every existing call site/test that doesn't pass them keeps working
    unchanged.
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
    # CHEAP/MID gating (2026-09-11): WITHIN_BAND_SIZE_SLOPE now also has
    # real, cross-asset-validated, temporally-stable CHEAP/MID cells (see
    # cheap-mid-within-band-scaling-gap in project memory) -- but unlike
    # CORE/HIGH, those cells are gated by a dedicated flag rather than
    # being unconditionally live, so paperbot-mini's existing
    # WITHIN_BAND_SIZE_SLOPE behavior (CORE/HIGH only) stays exactly
    # unchanged when ENABLE_CHEAP_MID_WITHIN_BAND_SCALING=false, instead
    # of silently picking up two new regimes' worth of scaling the moment
    # this file's table was extended. Gated HERE (the call site), not
    # inside within_band_size_multiplier itself -- behavior_config.py
    # never imports config, same discipline as every other multiplier.
    if regime in ("CHEAP", "MID") and not config.ENABLE_CHEAP_MID_WITHIN_BAND_SCALING:
        within_band = 1.0
    # CROSS-MARKET sizing momentum (2026-09-11 finding): a market's first
    # entry echoes the PREVIOUS market's own size residual (slow EWMA
    # decay, not lag-1-only) -- see CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER's
    # docstring in behavior_config.py. Scoped to position_tier=="first"
    # only, since that's the quantity the finding was measured on (later
    # entries in the same market already have their own within-market
    # signals). size_momentum_residual is bot.py's per-asset EWMA state,
    # threaded through build_order_intent -- this file has no cross-market
    # memory of its own.
    momentum_mult = 1.0
    if (position_tier == "first" and config.ENABLE_CROSS_MARKET_SIZE_MOMENTUM
            and size_momentum_residual is not None):
        momentum_mult = bc.cross_market_size_momentum_multiplier(asset, size_momentum_residual)
    # BANKROLL-linked sizing (2026-09-12 finding): Ethereum/Solana first-
    # entry size scales DOWN after his own accumulated realized profit and
    # UP after a drawdown -- see BANKROLL_PNL_SIZE_MULTIPLIER's docstring
    # in behavior_config.py for the full validation trail (survives time-
    # detrending and a partial-correlation check against the adverse-move
    # multipliers; Bitcoin deliberately excluded, not just uncalibrated).
    # Scoped to position_tier=="first" only, same reasoning as momentum
    # above. bankroll_pnl_residual is Ledger.realized_pnl_by_asset()'s own
    # directional (no-rebate) total for this asset, recomputed from source
    # by bot.py at decision time -- this file has no bankroll memory of
    # its own.
    bankroll_mult = 1.0
    if (position_tier == "first" and config.ENABLE_BANKROLL_PNL_SIZE_MULTIPLIER
            and bankroll_pnl_residual is not None):
        bankroll_mult = bc.bankroll_pnl_size_multiplier(asset, bankroll_pnl_residual)
    # TIME-TO-CLOSE scaling (2026-09-10 finding): confirmed the trader
    # actually SIZES differently by time remaining, holding price level
    # fixed -- not just present more late-window (already known), a real
    # sizing reaction (unlike a separately-tested price-velocity signal
    # that turned out to be a passive fact he doesn't act on, and was
    # deliberately NOT implemented after that check). Mean-neutral by
    # construction, same principle as within_band above. See
    # TTC_SIZE_MULTIPLIER's docstring in behavior_config.py for the exact
    # per-asset-per-regime shapes and the real numbers behind them.
    # Scoped to THIS ordinary entry-curve path only, not
    # HEDGE_SIZE_RATIO's separate formula -- the raw trade data this was
    # measured from doesn't distinguish hedge-shaped trades from ordinary
    # ones, so extrapolating it onto the hedge formula isn't validated by
    # what was actually measured.
    ttc_mult = bc.ttc_size_multiplier(asset, regime, seconds_remaining) \
        if seconds_remaining is not None and config.ENABLE_TTC_SIZE_MULTIPLIER else 1.0
    # RESUMPTION-CAUTION scaling (2026-09-10 finding): a real, precise
    # 3-phase shape (suppressed 0-6h, overshoot 7-9h, back to baseline
    # 10h+) found from the largest known real silence this session --
    # see resumption_size_multiplier's docstring in behavior_config.py
    # for the full scope caveat (gated to gaps >= RESUMPTION_GAP_
    # THRESHOLD_HOURS; NOT extrapolated to ordinary short pauses, which
    # were separately checked and found to show no such reset). Global/
    # bot-wide, not per-market -- see bot.py's _hours_since_resumption.
    resumption_mult = bc.resumption_size_multiplier(hours_since_resumption) \
        if config.ENABLE_RESUMPTION_SIZE_MULTIPLIER else 1.0
    # COMBINED-MULTIPLIER SAFETY BOUND (2026-09-12, bug audit #4): each of
    # within_band/momentum_mult/bankroll_mult/ttc_mult/resumption_mult was
    # fit MARGINALLY -- controlling for other already-known variables
    # individually at build time -- but their PRODUCT has never been
    # validated against his real observed size distribution as a joint
    # quantity. Specific risk flagged in BUGS_TO_FIX.md #4: momentum_mult
    # (EWMA of size residual) and bankroll_mult (realized-P&L-conditioned)
    # plausibly both pick up overlapping "recent performance" signal, and
    # multiplying them risks double-counting the same real effect into a
    # swing more extreme than anything actually observed.
    #
    # This clamps the COMBINED cross-market/time signal (everything except
    # jitter and SIZE_SCALE_FACTOR, which aren't calibrated-signal
    # multipliers) to config.COMBINED_SIZE_MULTIPLIER_CAP -- a REASONED
    # safety bound, not a data-derived precise answer: the full joint-
    # distribution validation (pull real sizing data for markets where
    # multiple conditions fire simultaneously and check whether the actual
    # combined multiplier matches or overshoots the raw product) is a
    # separate, larger data effort that hasn't been done yet. This bound
    # is deliberately wider than any single multiplier's own individual
    # cap (each ~3.0x) -- real, uncorrelated effects compounding somewhat
    # is expected and legitimate; this only stops the worst-case scenario
    # of every factor aligning in the same direction at once.
    combined_signal = within_band * momentum_mult * bankroll_mult * ttc_mult * resumption_mult
    cap = config.COMBINED_SIZE_MULTIPLIER_CAP
    combined_signal = max(1.0 / cap, min(cap, combined_signal))
    # config.SIZE_SCALE_FACTOR is a no-op (1.0) everywhere except a
    # deliberately small-bankroll instance -- see its docstring in
    # config.py. Applied here (not to the floor-lot branch above) so the
    # tiny, independently-calibrated probe tier is never pushed below a
    # real exchange minimum by a scale-down meant for the ordinary curve.
    return max(median * jitter * combined_signal * config.SIZE_SCALE_FACTOR, 0.0), False


def build_order_intent(market: Market, up_book: BookState, down_book: BookState,
                        activity: MarketActivityState, rng: random.Random,
                        recent_price_delta: Optional[float] = None,
                        now: Optional[float] = None,
                        hours_since_resumption: Optional[float] = None,
                        previous_market_first_side: Optional[str] = None,
                        previous_market_won: Optional[bool] = None,
                        prev_hedge_rate: Optional[float] = None,
                        size_momentum_residual: Optional[float] = None,
                        rolling_accuracy: Optional[float] = None,
                        max_notional_usd: Optional[float] = None,
                        bankroll_pnl_residual: Optional[float] = None) -> Optional[OrderIntent]:
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

    `previous_market_won`, `prev_hedge_rate`, `size_momentum_residual`,
    `rolling_accuracy` (all added 2026-09-11), and `bankroll_pnl_residual`
    (added 2026-09-12) are per-asset
    cross-market state that only bot.py can maintain (this module has no
    memory across markets of its own) -- all Optional (default None ->
    no-op for every one of them) so every existing call site/test keeps
    working unchanged. See decide_side/decide_hedge/decide_size's own
    docstrings for what each one feeds.

    `max_notional_usd` (added 2026-09-11, $100-bankroll safety work): a
    hard per-order cap in dollars, computed by bot.py as a fraction of
    CURRENT equity (config.MAX_ORDER_PCT_OF_EQUITY) -- not a fixed
    amount, so it shrinks automatically if losses accumulate. Applied as
    a CLAMP-DOWN ONLY, below, strictly BEFORE the exchange-minimum
    bump-up check that already exists -- confirmed this can't collide
    with that check the way a rigid dollar cap could (the exchange's own
    per-share minimum doesn't shrink with SIZE_SCALE_FACTOR, so a tight
    enough fixed cap could in principle sit below it once equity drops).
    Because the clamp runs before the bump-up, a capped-then-bumped
    order still clears the exchange's real minimum exactly as it would
    without this parameter -- this can never, by itself, cause a trade
    to be skipped that would otherwise have happened. Optional (default
    None -> no-op) for the same reason as every other parameter here;
    not applied to the floor-lot tier (checked below), same scoping
    precedent as config.SIZE_SCALE_FACTOR.
    """
    if not timing_ok(market, now):
        return None

    seconds_remaining = market.seconds_remaining(now)
    # UTC weekday (>=5 is Sat/Sun) -- matches the calibration's own
    # methodology exactly (real trades.jsonl timestamps grouped by UTC
    # weekday(), not local time). `now` defaults to real wall-clock time,
    # same convention as Market.seconds_remaining's own `now` handling.
    is_weekend = datetime.fromtimestamp(now if now is not None else time.time(),
                                         tz=timezone.utc).weekday() >= 5

    # ADDED 2026-09-10: dominant side's live book price, for decide_hedge's
    # adverse-move-conditioned trigger multiplier -- must come from the
    # DOMINANT side's own book (not the eventual hedge side's), computed
    # here since decide_hedge doesn't have book access itself, same reason
    # `held_side_price` is looked up fresh below rather than cached on
    # MarketActivityState.
    dominant_side_now = activity.dominant_side()
    dominant_book = None
    if dominant_side_now == "Up":
        dominant_book = up_book
    elif dominant_side_now == "Down":
        dominant_book = down_book
    dominant_current_price = dominant_book.best_bid if dominant_book is not None else None

    hedge_side = decide_hedge(
        market.asset, activity, rng, liquidity=market.liquidity,
        is_weekend=is_weekend, dominant_current_price=dominant_current_price,
        prev_hedge_rate=prev_hedge_rate)
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
        side = decide_side(
            market.asset, activity, rng, held_side_price=held_side_price,
            previous_market_first_side=(previous_market_first_side
                                         if config.ENABLE_CROSS_MARKET_SIDE_PERSISTENCE else None),
            previous_market_won=(previous_market_won
                                  if config.ENABLE_WIN_LOSS_SIDE_PERSISTENCE else None))
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
        # CHANGED 2026-09-12 (bug audit #5): hedge_count -> real_hedge_fill_count,
        # must mirror decide_hedge's own real_hedge_fill_count==0 branch
        # exactly (that's what decided is_hedge/hedge_side in the first
        # place) -- using placement-based hedge_count here instead could
        # size a hedge decide_hedge classified as "first" using the
        # continuation ratio, or vice versa.
        if activity.real_hedge_fill_count == 0:
            ratio = bc.hedge_size_ratio(market.asset, activity.first_entry_regime)
            # ADDED 2026-09-10: real, cross-asset-confirmed finding that
            # first-hedge SIZE scales with how far price has moved against
            # the dominant side since entry, on top of (not instead of)
            # the first_entry_regime baseline above -- see
            # ADVERSE_MOVE_SIZE_MULTIPLIER's docstring in behavior_config.py.
            # `price` here is the hedge side's book price (opposite of the
            # dominant side), so 1 - price is the dominant side's current
            # implied price -- matches this multiplier's real-data
            # derivation exactly (entry price minus current price, positive
            # = moved against the dominant side).
            if config.ENABLE_ADVERSE_MOVE_SIZE_MULTIPLIER and activity.first_entry_price is not None:
                adverse_move = activity.first_entry_price - (1.0 - price)
                ratio *= bc.adverse_move_size_multiplier(market.asset, adverse_move)
            # ADDED 2026-09-12: real, confirmed-independent (via partial
            # correlation controlling for adverse_move) finding that the
            # hedge side's own ABSOLUTE price also predicts size, on top of
            # (not instead of) the delta-from-entry effect just above -- see
            # ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER's docstring in
            # behavior_config.py. Uses the same `price` (the hedge side's
            # own book price) the adverse_move calculation above already
            # reads, no new state needed.
            if config.ENABLE_ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER:
                ratio *= bc.absolute_price_hedge_size_multiplier(market.asset, price)
        else:
            ratio = bc.hedge_continuation_size_ratio(activity.real_hedge_fill_count + 1)
            # ADDED 2026-09-10: direct extension of the hedge_count==0 case
            # above to continuation hedges -- same since-ORIGIN adverse_move
            # definition (deep-research-confirmed to beat a since-last-hedge
            # alternative), see ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER's
            # docstring in behavior_config.py.
            if config.ENABLE_ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER and activity.first_entry_price is not None:
                adverse_move = activity.first_entry_price - (1.0 - price)
                ratio *= bc.adverse_move_continuation_size_multiplier(market.asset, adverse_move)
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
            notional, is_floor_lot = decide_size(market.asset, regime, position_tier, price, rng,
                                                  seconds_remaining=seconds_remaining,
                                                  hours_since_resumption=hours_since_resumption,
                                                  size_momentum_residual=size_momentum_residual,
                                                  bankroll_pnl_residual=bankroll_pnl_residual)
    else:
        notional, is_floor_lot = decide_size(market.asset, regime, position_tier, price, rng,
                                              seconds_remaining=seconds_remaining,
                                              hours_since_resumption=hours_since_resumption,
                                              size_momentum_residual=size_momentum_residual,
                                              bankroll_pnl_residual=bankroll_pnl_residual)

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
        #
        # ACCURACY-conditioned (2026-09-11): scout_probability's base rate
        # is scaled by bc.accuracy_scout_multiplier(rolling_accuracy) --
        # real, cross-asset-pooled, circularity-checked finding that worse
        # recent accuracy predicts more scouting. rolling_accuracy is
        # bot.py's per-asset rolling correctness tracker, populated inside
        # resolution_tick (see ACCURACY_SCOUT_MULTIPLIER's docstring in
        # behavior_config.py for the biggest architectural lift of this
        # implementation pass). Clamped to [0, 1] since it's still a
        # probability after scaling.
        scout_p = bc.scout_probability(market.asset)
        if config.ENABLE_ACCURACY_SCOUT_MULTIPLIER and rolling_accuracy is not None:
            scout_p = max(0.0, min(1.0, scout_p * bc.accuracy_scout_multiplier(rolling_accuracy)))
        if rng.random() < scout_p:
            is_scout = True
            notional = notional * bc.scout_size_ratio(market.asset)

    # HARD PER-ORDER CAP (2026-09-11, $100-bankroll safety work): clamp
    # DOWN only, and strictly BEFORE the exchange-minimum bump-up check
    # right below -- see max_notional_usd's docstring above for why this
    # ordering is what makes the cap unable to ever cause a skip that
    # wouldn't otherwise have happened. Never applied to the floor-lot
    # tier (already tiny and separately calibrated -- same precedent as
    # config.SIZE_SCALE_FACTOR).
    if max_notional_usd is not None and not is_floor_lot:
        notional = min(notional, max_notional_usd)

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
