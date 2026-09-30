"""
Consolidated decision engine, 2026-09-24.

Origin: after consolidating the hedge decision stack (13 trigger factors
+ 7 size factors -> 2 functions, see behavior_config.hedge_probability's
docstring), a full validation pass of every OTHER decision-making
mechanism in strategy.py/behavior_config.py found the exact same
"illusion of complexity" pattern outside hedging too -- e.g. 3 more
calibration tables (ACCURACY_SCOUT_MULTIPLIER, BANKROLL_PNL_SIZE_
MULTIPLIER, CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER) had quietly decayed
to empty dicts while their ENABLE_* flags still defaulted to true, and
side selection / entry sizing / entry eligibility were each split across
several independently-named, independently-flagged entry points the way
hedging used to be.

Direct instruction: "create a completely new bot with exactly one
function for everything including yesterdays findings ... first
validate everything" -- this module is that bot's decision core.
Replaces coinbase-bot (whose own differentiator, a Coinbase-momentum
forced first-entry side, was a separate live-momentum EXPERIMENT, not a
verified trader-replication finding, so it's deliberately not carried
over here).

Exactly FOUR decision functions, matching the architecture precedent set
by decide_hedge/hedge_probability -- one entry point per decision TYPE,
each folding in every real, currently-verified finding for that type
(not one literal function for the whole bot: side/size/eligibility/hedge
need different inputs and fire in a different order, so collapsing them
into one call would hide that they're four different questions, not
one):

  1. decide_entry_side  -- WHICH side (folds in: mid-contrarian override,
     cross-asset-carryover override, cross-market first-entry
     persistence incl. win/loss-conditioning, regime-conditioned
     held-side persistence)
  2. entry_eligible     -- WHETHER to trade this tick at all (folds in:
     timing window, book liquidity, regime-engagement mix targeting +
     participation-asset-scale + drift-correction, mid-boundary-
     adjacency dampener, cheap-joint price x freshness dampener, cheap
     TTC-selectivity dampener, cheap late-deep-touch dampener,
     decisive-entry CORE/HIGH freshness dampener, add-on-continuation
     dampener [2026-09-26])
  3. decide_size        -- HOW MUCH (folds in: floor-lot roll,
     within-band scaling + deep-cheap-tail correction, TTC scaling,
     resumption-caution, reentry-fatigue, hedge-count-reinforcement,
     decisiveness-onset + both overshoot corrections, cheap-deep-touch
     boost, mid-boundary boost, entry-size-level correction)
  4. decide_hedge       -- IF/WHEN to hedge (reused unchanged from
     strategy.py -- already a single function as of the 2026-09-24
     hedge consolidation)

DELIBERATELY DROPPED versus the old chain (confirmed dead, zero
behavioral effect either way):
  - cross_market_size_momentum_multiplier (CROSS_MARKET_SIZE_MOMENTUM_
    MULTIPLIER is now an empty table)
  - bankroll_pnl_size_multiplier (BANKROLL_PNL_SIZE_MULTIPLIER is now an
    empty table)
  - accuracy_scout_multiplier (ACCURACY_SCOUT_MULTIPLIER is now an empty
    table)
See the 2026-09-24 validation-pass memory note for the full audit.
(2026-09-26: bot.py's own tracking for all three, plus prev_hedge_rate/
after_big_loss -- also dead, cut in the 2026-09-24 hedge consolidation --
was removed entirely rather than left computed-and-ignored; see
strategy.py's build_order_intent/decide_size docstrings.)

No ENABLE_* feature flags anywhere in this module -- every mechanism
folded in here has already been verified real; a bot meant to copy the
trader shouldn't carry a pile of togglable maybes. build_order_intent's
own **_unused absorbs any extra keyword arguments bot.py's shared
_evaluate_one_market call site passes for the OTHER bots' benefit
(forced_first_entry_side) so this engine can be swapped in via
PaperBot(build_order_intent_fn=...) without
forking _evaluate_one_market itself.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Optional

from . import behavior_config as bc
from . import config
from .book import BookState
from .market_discovery import Market
from .strategy import (
    MarketActivityState,
    OrderIntent,
    availability_check,
    cross_asset_carryover_side,
    decide_hedge,
    mid_contrarian_first_entry_side,
    streak_fade_side,
    timing_ok,
)

logger = logging.getLogger("paperbot.consolidated_engine")


def decide_entry_side(asset: str, activity: MarketActivityState,
                       up_book: BookState, down_book: BookState, rng: random.Random,
                       previous_market_first_side: Optional[str] = None,
                       previous_market_won: Optional[bool] = None,
                       last_first_entry_by_asset: Optional[dict] = None,
                       now: Optional[float] = None,
                       streak_side: Optional[str] = None,
                       streak_length: int = 0,
                       seconds_since_window_open: Optional[float] = None) -> tuple[str, str]:
    """ONE function for the entire side-selection decision. Returns
    (side, tag) where tag records which mechanism actually picked it
    ("contrarian" / "cross_asset_carryover" / "streak_fade" /
    "persistence"), needed downstream for the contrarian size dampener
    and the scout/order reason. Reuses mid_contrarian_first_entry_side,
    cross_asset_carryover_side, and streak_fade_side as-is (already
    single, tested functions, not reimplemented here) -- the
    consolidation is having ONE place that owns their priority order,
    not re-deriving their math.

    streak_side/streak_length/seconds_since_window_open (added
    2026-09-25, see bc.streak_fade_probability's docstring): same
    bot.py-maintained state as strategy.py's own build_order_intent
    wiring -- this module has no cross-window memory of its own either.
    No ENABLE_* flag guarding this call, consistent with this module's
    own no-flags-for-validated-findings design philosophy (see the
    module docstring) -- unlike strategy.py's ENABLE_STREAK_FADE, which
    exists there only because that file guards every mechanism with a
    flag as a matter of its own established convention."""
    if activity.last_side is None:
        contrarian = mid_contrarian_first_entry_side(asset, activity, up_book, down_book, rng)
        if contrarian is not None:
            return contrarian, "contrarian"
        carryover = cross_asset_carryover_side(
            asset, activity, up_book, down_book, last_first_entry_by_asset,
            now if now is not None else time.time(), rng)
        if carryover is not None:
            return carryover, "cross_asset_carryover"
        fade = streak_fade_side(asset, activity, streak_side, streak_length,
                                 seconds_since_window_open, rng)
        if fade is not None:
            return fade, "streak_fade"
        if previous_market_first_side is not None:
            persistence = bc.cross_market_side_persistence(asset, previous_market_won)
            if rng.random() < persistence:
                return previous_market_first_side, "persistence"
            return ("Down" if previous_market_first_side == "Up" else "Up"), "persistence"
        return rng.choice(["Up", "Down"]), "persistence"

    held_book = up_book if activity.last_side == "Up" else down_book
    held_price = held_book.best_bid if held_book is not None else None
    held_regime = bc.classify_regime(held_price) if held_price is not None else "CHEAP"
    persistence = bc.side_persistence(asset, held_regime)
    if rng.random() < persistence:
        return activity.last_side, "persistence"
    return ("Down" if activity.last_side == "Up" else "Up"), "persistence"


def entry_eligible(market: Market, activity: MarketActivityState, side: str,
                    side_book: BookState, price: float, regime: str,
                    now: Optional[float], seconds_remaining: Optional[float],
                    is_hedge: bool, is_first_entry: bool,
                    regime_engagement_state: Optional[dict],
                    rng: random.Random) -> tuple[bool, str]:
    """ONE function for the entire entry-eligibility decision. Returns
    (eligible, reason_if_not). A hedge leg only goes through the timing/
    availability checks -- every dampener below it is specifically about
    ORDINARY entry selectivity, same scoping as the original per-gate
    checks in strategy.build_order_intent."""
    if not timing_ok(market, now):
        return False, "timing"
    ok, reason = availability_check(side_book)
    if not ok:
        return False, reason
    if is_hedge:
        return True, ""

    if activity.is_episode_rejected("regime", side, regime):
        return False, "regime_engagement_episode_already_rejected"
    engagement_mult = bc.regime_engagement_multiplier(market.asset, regime)
    engagement_mult = min(1.0, engagement_mult * bc.participation_asset_scale(market.asset))
    if regime_engagement_state is not None:
        asset_state = regime_engagement_state.get(market.asset, {})
        drift_mult = bc.regime_drift_correction(
            market.asset, regime, asset_state.get("counts", {}), asset_state.get("total", 0))
        engagement_mult = max(0.0, min(1.0, engagement_mult * drift_mult))
    # CLOSE-URGENCY boost (2026-09-25 research-loop capstone finding, see
    # its docstring in behavior_config.py) -- no flag, consistent with
    # this module's own no-flags-for-validated-findings design.
    if seconds_remaining is not None:
        engagement_mult = min(1.0, engagement_mult * bc.urgency_multiplier("entry", seconds_remaining))
    # CHEAP DEEP-TAIL PARTICIPATION BOOST (2026-09-28, see its docstring
    # in behavior_config.py) -- no flag, same reasoning as CLOSE-URGENCY
    # above.
    if regime == "CHEAP":
        engagement_mult = min(1.0, engagement_mult
                              * bc.cheap_deep_tail_participation_multiplier(market.asset, price))
    if engagement_mult < 1.0 and rng.random() > engagement_mult:
        activity.reject_episode("regime", side, regime)
        return False, "regime_engagement"

    boundary_mult = bc.mid_boundary_edge_multiplier(regime, price)
    if boundary_mult < 1.0 and rng.random() > boundary_mult:
        return False, "mid_boundary"

    if is_first_entry and regime == "CHEAP":
        seconds_since_cheap_crossing: Optional[float] = None
        if now is not None:
            cheap_crossed_at = (activity.up_cheap_crossed_at if side == "Up"
                                 else activity.down_cheap_crossed_at)
            if cheap_crossed_at is not None:
                seconds_since_cheap_crossing = now - cheap_crossed_at
        joint_mult = bc.cheap_joint_entry_multiplier(market.asset, price, seconds_since_cheap_crossing)
        if joint_mult < 1.0 and rng.random() > joint_mult:
            return False, "cheap_joint"

        ttc_mult = bc.cheap_ttc_selectivity_multiplier(market.asset, price, seconds_remaining)
        if ttc_mult < 1.0 and rng.random() > ttc_mult:
            return False, "cheap_ttc_selectivity"

        if seconds_remaining is not None:
            deep_touch_mult = bc.cheap_deep_touch_edge_multiplier(price, 300.0 - seconds_remaining)
            if deep_touch_mult < 1.0 and rng.random() > deep_touch_mult:
                return False, "cheap_late_deep_touch"

    if is_first_entry and regime in ("CORE", "HIGH"):
        seconds_since_decisive_crossing: Optional[float] = None
        if now is not None:
            decisive_crossed_at = (activity.up_decisive_crossed_at if side == "Up"
                                    else activity.down_decisive_crossed_at)
            if decisive_crossed_at is not None:
                seconds_since_decisive_crossing = now - decisive_crossed_at
        elig_mult = bc.decisive_entry_eligibility_multiplier(market.asset, seconds_since_decisive_crossing)
        if elig_mult < 1.0 and rng.random() > elig_mult:
            return False, "decisive_entry_eligibility"

    # ADD-ON CONTINUATION (2026-09-26): see bc.addon_continuation_
    # probability's docstring -- the complementary case to the two
    # is_first_entry-scoped blocks just above. No flag, consistent with
    # this module's own no-flags-for-validated-findings design.
    if not is_first_entry and activity.real_fill_count > 0:
        same_side_fill_count = activity.real_fill_count - activity.real_hedge_fill_count
        k = same_side_fill_count - 1
        if k >= 0:
            if activity.is_episode_rejected("addon", side, k):
                return False, "addon_continuation_episode_already_rejected"
            continuation_mult = bc.addon_continuation_probability(market.asset, regime)
            if continuation_mult < 1.0 and rng.random() > continuation_mult:
                activity.reject_episode("addon", side, k)
                return False, "addon_continuation"

    return True, ""


def decide_size(asset: str, regime: str, position_tier: str, price: float,
                 rng: random.Random, seconds_remaining: Optional[float] = None,
                 hours_since_resumption: Optional[float] = None,
                 real_fill_count: Optional[int] = None,
                 live_hedge_count: Optional[int] = None,
                 decisiveness_fraction: Optional[float] = None,
                 hour_of_day: Optional[int] = None) -> tuple[float, bool]:
    """ONE function for the entire ordinary-entry sizing decision.
    Returns (notional_usd, is_floor_lot). See module docstring for
    exactly which two multipliers (cross-market size momentum,
    bankroll-linked sizing) were dropped as confirmed-dead and why.

    hour_of_day (added 2026-09-25, see bc.hour_size_multiplier's
    docstring): no ENABLE_* flag, consistent with this module's own
    no-flags-for-validated-findings design."""
    floor_p = bc.floor_lot_probability(asset, regime, position_tier)
    if floor_p > 0 and rng.random() < floor_p:
        return config.FLOOR_LOT_SIZE_SHARES * price, True

    jitter = 1.0 + rng.uniform(-config.SIZING_JITTER_FRACTION, config.SIZING_JITTER_FRACTION)
    median = bc.median_entry_notional(asset, regime, position_tier)
    within_band = bc.within_band_size_multiplier(asset, regime, price)
    if regime == "CHEAP" and price < bc.DEEP_CHEAP_TAIL_PRICE_THRESHOLD:
        within_band *= bc.table_lookup(bc.DEEP_CHEAP_TAIL_MULTIPLIER, asset)

    ttc_mult = (bc.ttc_size_multiplier(asset, regime, seconds_remaining)
                if seconds_remaining is not None else 1.0)
    resumption_mult = bc.resumption_size_multiplier(hours_since_resumption)
    reentry_fatigue_mult = bc.reentry_fatigue_multiplier(asset, regime, real_fill_count)
    hedge_count_reinforcement_mult = bc.hedge_count_reinforcement_multiplier(asset, regime, live_hedge_count)
    decisiveness_mult = bc.decisiveness_onset_multiplier(asset, regime, decisiveness_fraction)
    overshoot_correction = bc.decisiveness_ttc_overshoot_correction(asset, regime, decisiveness_fraction)
    withinband_overshoot_correction = bc.decisiveness_withinband_overshoot_correction(
        asset, regime, decisiveness_fraction)

    cheap_deep_touch_mult = 1.0
    if position_tier == "first" and regime == "CHEAP" and seconds_remaining is not None:
        raw = bc.cheap_deep_touch_edge_multiplier(price, 300.0 - seconds_remaining)
        if raw > 1.0:
            cheap_deep_touch_mult = raw

    mid_boundary_mult = 1.0
    raw_mid = bc.mid_boundary_edge_multiplier(regime, price)
    if raw_mid > 1.0:
        mid_boundary_mult = raw_mid

    # HOUR-CONDITIONED SIZE BOOST (2026-09-25 finding): see
    # bc.hour_size_multiplier's docstring. Scoped to position_tier==
    # "first" only, matching what was actually measured -- same
    # discipline as cheap_deep_touch_mult above.
    hour_boost_mult = bc.hour_size_multiplier(hour_of_day) if position_tier == "first" else 1.0

    combined_signal = (within_band * ttc_mult * resumption_mult * reentry_fatigue_mult
                        * hedge_count_reinforcement_mult * decisiveness_mult * overshoot_correction
                        * withinband_overshoot_correction * cheap_deep_touch_mult * mid_boundary_mult
                        * hour_boost_mult)
    cap = config.COMBINED_SIZE_MULTIPLIER_CAP
    combined_signal = max(1.0 / cap, min(cap, combined_signal))

    level_correction = bc.table_lookup(bc.ENTRY_SIZE_LEVEL_CORRECTION, asset, regime)
    notional = median * level_correction * jitter * combined_signal * config.SIZE_SCALE_FACTOR
    return max(notional, 0.0), False


def build_order_intent(market: Market, up_book: BookState, down_book: BookState,
                        activity: MarketActivityState, rng: random.Random,
                        now: Optional[float] = None,
                        hours_since_resumption: Optional[float] = None,
                        previous_market_first_side: Optional[str] = None,
                        previous_market_won: Optional[bool] = None,
                        last_first_entry_by_asset: Optional[dict] = None,
                        regime_engagement_state: Optional[dict] = None,
                        max_notional_usd: Optional[float] = None,
                        streak_side: Optional[str] = None,
                        streak_length: int = 0,
                        **_unused) -> Optional[OrderIntent]:
    """The thin orchestrator wiring the four decision functions together
    in the only order they CAN run (side needs to be known before price/
    regime; price/regime before eligibility; eligibility before sizing).
    This is not itself "a decision function" any more than decide_hedge
    calling hedge_probability is -- it makes zero independent calibrated
    judgements of its own. **_unused absorbs the extra keyword arguments
    bot.py's shared _evaluate_one_market call site passes for the other
    (non-consolidated) bots' benefit -- see module docstring."""
    if not timing_ok(market, now):
        return None
    seconds_remaining = market.seconds_remaining(now)
    # HOUR-CONDITIONED SIZE BOOST (2026-09-25): see
    # bc.hour_size_multiplier's docstring.
    hour_of_day = datetime.fromtimestamp(now, timezone.utc).hour if now is not None else None

    dominant_side_now = activity.dominant_side()
    dominant_book = None
    hedge_side_book = None  # opposite of dominant -- where a hedge, if any, would price
    if dominant_side_now == "Up":
        dominant_book = up_book
        hedge_side_book = down_book
    elif dominant_side_now == "Down":
        dominant_book = down_book
        hedge_side_book = up_book
    dominant_current_price = dominant_book.best_bid if dominant_book is not None else None
    hedge_side_price = hedge_side_book.best_bid if hedge_side_book is not None else None

    # CONSOLIDATED 2026-09-26: decide_hedge now returns (side, notional)
    # together -- see its own docstring in strategy.py -- so hedge_notional
    # is already final (jitter/SIZE_SCALE_FACTOR applied) the moment this
    # call returns. (streak-size-boost removed 2026-09-26/28 -- see
    # decide_hedge's own docstring in strategy.py.)
    hedge_decision = decide_hedge(market.asset, activity, rng, price=hedge_side_price,
                                   dominant_current_price=dominant_current_price,
                                   seconds_remaining=seconds_remaining)
    hedge_side, hedge_notional = hedge_decision if hedge_decision is not None else (None, None)
    is_hedge = hedge_side is not None
    if is_hedge:
        side, side_tag = hedge_side, "hedge"
    else:
        side, side_tag = decide_entry_side(
            market.asset, activity, up_book, down_book, rng,
            previous_market_first_side=previous_market_first_side,
            previous_market_won=previous_market_won,
            last_first_entry_by_asset=last_first_entry_by_asset, now=now,
            streak_side=streak_side, streak_length=streak_length,
            seconds_since_window_open=(300.0 - seconds_remaining) if seconds_remaining is not None else None)

    side_book = up_book if side == "Up" else down_book
    price = side_book.best_bid
    if price is None:
        return None

    is_first_entry = activity.real_fill_count == 0

    # TICK-OFFSET PLACEMENT (2026-09-24 finding, real first entries only):
    # see bc.TICK_OFFSET_DISTRIBUTION's docstring.
    entry_offset = 0.0
    if not is_hedge and is_first_entry:
        tick = max(side_book.tick_size, 1e-6)
        offset_ticks = bc.sample_tick_offset(rng)
        candidate_price = price + offset_ticks * tick
        if side_book.best_ask is not None:
            candidate_price = min(candidate_price, side_book.best_ask - tick)
        if candidate_price > 0:
            price = round(candidate_price, 6)
            entry_offset = -offset_ticks * tick

    regime = bc.classify_regime(price)

    decisiveness_fraction: Optional[float] = None
    if now is not None:
        crossed_at = activity.up_decisive_crossed_at if side == "Up" else activity.down_decisive_crossed_at
        if crossed_at is not None:
            total_available = market.end_time - crossed_at
            if total_available > 0:
                decisiveness_fraction = (now - crossed_at) / total_available

    eligible, reason = entry_eligible(
        market, activity, side, side_book, price, regime, now, seconds_remaining,
        is_hedge, is_first_entry, regime_engagement_state, rng)
    if not eligible:
        logger.info("SKIP asset=%s regime=%s price=%.4f: %s", market.asset, regime, price, reason)
        return None

    position_tier = activity.position_tier()
    min_size = market.order_min_size

    if is_hedge:
        # Hedge SIZE: already fully computed by decide_hedge's own
        # consolidated (side, notional) return above -- see its
        # docstring in strategy.py. `price` here is the same
        # hedge_side_price already passed into that call (same book,
        # same tick).
        notional = hedge_notional if hedge_notional is not None else 0.0
        is_floor_lot = False
        if min_size is not None and notional < min_size * price:
            logger.info(
                "SKIP asset=%s regime=%s pos=%s hedge=True: notional $%.4f below exchange min "
                "($%.4f = %.2f shares * $%.4f) -- falling through to an ordinary entry",
                market.asset, regime, position_tier, notional, min_size * price, min_size, price)
            is_hedge = False
            notional, is_floor_lot = decide_size(
                market.asset, regime, position_tier, price, rng,
                seconds_remaining=seconds_remaining, hours_since_resumption=hours_since_resumption,
                real_fill_count=activity.real_fill_count, live_hedge_count=activity.real_hedge_fill_count,
                decisiveness_fraction=decisiveness_fraction, hour_of_day=hour_of_day)
    else:
        notional, is_floor_lot = decide_size(
            market.asset, regime, position_tier, price, rng,
            seconds_remaining=seconds_remaining, hours_since_resumption=hours_since_resumption,
            real_fill_count=activity.real_fill_count, live_hedge_count=activity.real_hedge_fill_count,
            decisiveness_fraction=decisiveness_fraction, hour_of_day=hour_of_day)
        if side_tag == "contrarian" and not is_floor_lot:
            notional *= bc.MID_CONTRARIAN_SIZE_RATIO

    is_scout = False
    # "streak_fade" excluded (2026-09-25) same reasoning as contrarian/
    # cross_asset_carryover: scout is for the no-real-signal default
    # path, not for layering a tentative-size treatment on top of a
    # dedicated side-selection override that already fired.
    if (not is_hedge and not is_floor_lot
            and side_tag not in ("contrarian", "cross_asset_carryover", "streak_fade")
            and is_first_entry):
        scout_p, scout_size = bc.scout_params(market.asset)
        if rng.random() < scout_p:
            is_scout = True
            notional *= scout_size

    if max_notional_usd is not None and not is_floor_lot:
        notional = min(notional, max_notional_usd)

    if min_size is not None and notional < min_size * price:
        floor_notional = min_size * price
        if is_floor_lot:
            logger.info(
                "SKIP asset=%s regime=%s pos=%s hedge=%s scout=%s: notional $%.4f below exchange min "
                "($%.4f = %.2f shares * $%.4f)", market.asset, regime, position_tier, is_hedge, is_scout,
                notional, floor_notional, min_size, price)
            return None
        logger.info(
            "BUMP asset=%s regime=%s pos=%s hedge=%s scout=%s: notional $%.4f below exchange min, "
            "raised to $%.4f (%.2f shares * $%.4f) instead of skipping",
            market.asset, regime, position_tier, is_hedge, is_scout, notional, floor_notional, min_size, price)
        notional = floor_notional

    size_shares = notional / price if price > 0 else 0.0
    if size_shares <= 0:
        return None

    token_id = market.token_id_up if side == "Up" else market.token_id_down

    if is_hedge:
        order_reason = "hedge"
    elif is_scout:
        order_reason = "scout_entry"
    elif is_floor_lot:
        order_reason = "floor_lot"
    elif side_tag == "contrarian":
        order_reason = "mid_contrarian"
    elif side_tag == "cross_asset_carryover":
        order_reason = "cross_asset_carryover"
    else:
        order_reason = "entry_curve"

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
        reason=order_reason,
        offset=entry_offset,
    )
