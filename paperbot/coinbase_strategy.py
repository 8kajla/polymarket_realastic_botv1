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

ORIGINAL research basis (INVALIDATED 2026-09-22, see project memory
coinbase-momentum-edge-is-lookahead-artifact.md): a first entry whose
side matches the trailing 3-minute Coinbase spot return was believed to
win far more often than one against it -- CHEAP 40.5% vs 14.5%, MID 63.1%
vs 36.4%. That result is reproduced ONLY when the momentum is computed
from the 1-min candle CONTAINING the entry (its close is up to 60s in the
future -- look-ahead). With fully-closed candles (all a live bot can know),
at every lookback tested (1/2/3/5 min) the edge vanishes: CHEAP aligned
17.6% vs against 14.6% win, and in ROI terms aligned CHEAP is WORSE than
against (e.g. 3m: +9.6% vs +35.1%; the trader's profitable pattern is the
cheap side AGAINST momentum); MID shows no edge. This override therefore
has no demonstrated causal edge; it is kept only because this bot exists
to run that experiment. CORE/HIGH are excluded (momentum_forced_side
returns None, falling back to decide_side exactly as paperbot would).
That fallthrough is NOT a defect: 72% of the real trader's first-entry
markets are ones where the momentum side is already CORE/HIGH, and his
dominant, most profitable first entry there is the cheap side against
momentum, which is exactly what the fallthrough reproduces. This module
only ever touches WHICH side a first entry takes, never whether or when
one happens.
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
