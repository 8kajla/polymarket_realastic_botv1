"""
Decision logic for the standalone Coinbase-momentum experiment bot.

Deliberately NOT a trader-replica module -- separate from strategy.py/
behavior_config.py's calibration-table-driven decisions, which model the
real trader's own behavior. This module instead directly bets on a real,
validated statistical edge the real trader does NOT appear to act on
himself. See config.py's Coinbase-momentum section for the full backstory
on why this lives as its own thing rather than being wired into
paperbot/paperbot-100.

Research basis (see claude_memory.md, "real SPOT-momentum alignment
predicts win rate"): a first entry whose side matches the direction of
the real, trailing 3-minute Coinbase spot return wins meaningfully more
often than one against it -- CHEAP 40.5% vs 14.5% (z=9.78), MID 63.1% vs
36.4% (z=14.58), both regime-controlled and temporally stable. CORE/HIGH
were NOT part of the validated edge (those bands are already mostly
decided by price alone) and are excluded here. A follow-up check
confirmed the real trader does not actively hunt this signal for entry
TIMING (it's a passive calibration fact about market truth, not
something that predicts when he trades) -- irrelevant to this module,
since it isn't replicating his timing, it's a standalone directional bet
made every time a CHEAP/MID market becomes available.
"""
from __future__ import annotations

from typing import Optional

from . import behavior_config as bc
from . import config
from .book import BookState
from .market_discovery import Market
from .strategy import OrderIntent, availability_check, timing_ok


def decide_momentum_side(regime: str, trailing_return: Optional[float]) -> Optional[str]:
    """Returns "Up" or "Down" if `regime` is within the validated
    CHEAP/MID edge and `trailing_return` clears config.MOMENTUM_MIN_MOVE's
    deadband, or None otherwise (out-of-scope regime, no spot data yet,
    or too flat to be a meaningful directional signal -- in every None
    case the caller should skip the market entirely, not fall back to
    any other decision rule)."""
    if regime not in ("CHEAP", "MID"):
        return None
    if trailing_return is None or abs(trailing_return) < config.MOMENTUM_MIN_MOVE:
        return None
    return "Up" if trailing_return > 0 else "Down"


def build_momentum_order_intent(market: Market, up_book: BookState, down_book: BookState,
                                 trailing_return: Optional[float],
                                 now: Optional[float] = None) -> Optional[OrderIntent]:
    """
    One-shot, no-hedge, flat-sized order intent driven purely by real
    Coinbase spot momentum. Deliberately far simpler than
    strategy.build_order_intent: no calibration tables, no hedging, no
    cross-market state -- this bot tests whether the raw statistical edge
    is exploitable on its own, not a person's decision process. The
    caller (coinbase_bot.py) is responsible for ensuring this only ever
    runs on a market's first entry (activity.entry_count == 0) -- this
    function has no memory of its own to enforce that.
    """
    if not timing_ok(market, now):
        return None
    if trailing_return is None:
        return None

    # Momentum's sign determines the candidate side directly (unlike
    # strategy.build_order_intent, where side comes from decide_side/
    # decide_hedge and momentum would only ever be a secondary factor).
    # Regime must still come from THIS side's own book price, not a fixed
    # reference side -- same "price asymmetry" discipline
    # strategy.build_order_intent's own docstring explains (Down's price
    # isn't simply 1 - Up's once spreads are involved).
    side = "Up" if trailing_return > 0 else "Down"
    side_book = up_book if side == "Up" else down_book
    price = side_book.best_bid
    if price is None:
        return None

    # bc.classify_regime is a pure, universal price-bucketing helper
    # (fixed CHEAP/MID/CORE/HIGH boundaries) -- reused here even though
    # the rest of behavior_config.py is trader-replica calibration this
    # module otherwise deliberately avoids.
    regime = bc.classify_regime(price)
    if decide_momentum_side(regime, trailing_return) is None:
        # Either the regime is out of the validated CHEAP/MID scope, or
        # the move is inside the deadband -- decide_momentum_side is the
        # single source of truth for "is this actionable" (it always
        # agrees with `side` above when it doesn't return None, since
        # both derive the direction from trailing_return's sign the same
        # way; only the actionability gating can differ).
        return None

    ok, reason = availability_check(side_book)
    if not ok:
        return None

    min_size = market.order_min_size
    notional = config.MOMENTUM_ORDER_NOTIONAL_USD
    if min_size is not None and notional < min_size * price:
        # Same bump-up-to-exchange-minimum convention as
        # strategy.build_order_intent, simplified (no floor-lot/scout
        # tiers here to interact with -- this bot has neither).
        notional = min_size * price

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
        position_tier="first",
        price=price,
        size_shares=size_shares,
        notional_usd=notional,
        is_floor_lot=False,
        is_hedge=False,
        is_scout=False,
        reason="coinbase_momentum",
    )
