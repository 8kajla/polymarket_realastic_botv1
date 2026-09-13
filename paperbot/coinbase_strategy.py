"""
Decision logic for the standalone Coinbase-momentum bot's ONE point of
departure from the trader-replica pipeline: first-entry side selection
in CHEAP/MID markets.

CHANGED 2026-09-13 (was: a wholly separate, one-shot, flat-sized
strategy with no hedging or calibrated sizing at all). Explicit
instruction: give the Coinbase bot every other trader-replica behavior
this project has learned and shipped (hedging, the full sizing
multiplier stack, cross-market persistence, resolution feedback,
drawdown safety) -- the ONLY thing that should differ from paperbot/
paperbot-100 is HOW the market's first entry side gets chosen in
CHEAP/MID. See coinbase_bot.py's CoinbaseMomentumBot, which now inherits
PaperBot's full pipeline unchanged and overrides only the
_forced_first_entry_side hook (see its docstring in bot.py) using this
module's momentum_forced_side().

Research basis (see claude_memory.md, "real SPOT-momentum alignment
predicts win rate"): a first entry whose side matches the direction of
the real, trailing 3-minute Coinbase spot return wins meaningfully more
often than one against it -- CHEAP 40.5% vs 14.5% (z=9.78), MID 63.1% vs
36.4% (z=14.58), both regime-controlled and temporally stable. CORE/HIGH
were NOT part of the validated edge (those bands are already mostly
decided by price alone) and are excluded here -- momentum_forced_side
returns None for them, falling back to the normal trader-replica
decide_side/cross-market-persistence logic exactly as if this bot were
paperbot. A follow-up check confirmed the real trader does not actively
hunt this signal for entry TIMING (it's a passive calibration fact about
market truth, not something that predicts when he trades) -- this
module only ever touches WHICH side a first entry takes, never whether
or when one happens.
"""
from __future__ import annotations

from typing import Optional

from . import behavior_config as bc
from . import config
from .book import BookState


def decide_momentum_side(regime: str, trailing_return: Optional[float]) -> Optional[str]:
    """Returns "Up" or "Down" if `regime` is within the validated
    CHEAP/MID edge and `trailing_return` clears config.MOMENTUM_MIN_MOVE's
    deadband, or None otherwise (out-of-scope regime, no spot data yet,
    or too flat to be a meaningful directional signal)."""
    if regime not in ("CHEAP", "MID"):
        return None
    if trailing_return is None or abs(trailing_return) < config.MOMENTUM_MIN_MOVE:
        return None
    return "Up" if trailing_return > 0 else "Down"


def momentum_forced_side(up_book: BookState, down_book: BookState,
                          trailing_return: Optional[float]) -> Optional[str]:
    """Returns the side to FORCE for a market's first entry (fed into
    strategy.build_order_intent's forced_first_entry_side), or None to
    defer entirely to the normal trader-replica decide_side/cross-market-
    persistence logic -- when trailing_return is unavailable, or the
    momentum-implied side's own regime is outside the validated CHEAP/MID
    edge, or the move is inside the deadband.

    Momentum's sign determines the CANDIDATE side directly; regime must
    then come from THAT side's own book price, not a fixed reference side
    -- same "price asymmetry" discipline strategy.build_order_intent's
    own docstring explains (Down's price isn't simply 1 - Up's once
    spreads are involved). bc.classify_regime is a pure, universal
    price-bucketing helper (fixed CHEAP/MID/CORE/HIGH boundaries) --
    reused here even though the rest of behavior_config.py is trader-
    replica calibration this module otherwise doesn't touch."""
    if trailing_return is None:
        return None
    side = "Up" if trailing_return > 0 else "Down"
    side_book = up_book if side == "Up" else down_book
    price = side_book.best_bid
    if price is None:
        return None
    regime = bc.classify_regime(price)
    if decide_momentum_side(regime, trailing_return) is None:
        return None
    return side
