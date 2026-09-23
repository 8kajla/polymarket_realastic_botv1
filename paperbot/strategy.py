"""
Per-cycle decision logic: regime classification, availability, side
persistence, sizing (including the floor-lot roll), and the 90-second
timing gate. Produces OrderIntent objects; does not touch the network or
the fill simulator directly (bot.py wires those together).
"""
from __future__ import annotations

import json
import logging
import math
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
    # "Up" or "Down" -- the standing CONVICTION side, i.e. the side an
    # ORDINARY (non-hedge) entry last landed on. FIXED 2026-09-19: a
    # hedge fill used to move this too, making it read the transient
    # hedge side instead -- see record_entry's own comment for the full
    # derivation and the confirmed-unfixable, separate problem
    # (recalibrating SIDE_PERSISTENCE's table from raw trader data) this
    # is NOT the same as.
    last_side: Optional[str] = None
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
    # ADDED 2026-09-24, alongside LATE_WINDOW_HEDGE_URGENCY_MULTIPLIER:
    # seconds from window-open to this market's first real entry -- needed
    # to (a) classify whether this market's first entry was itself EARLY
    # (t_in<120s, the only population the underlying hump was measured on)
    # and (b) compute seconds-since-first-entry at hedge-decision time. Set
    # once, same `if self.real_fill_count == 0` guard as first_entry_price/
    # first_entry_regime above (so it locks in at whichever placement
    # attempt actually fills first, same convention as those two fields).
    first_entry_t_in: Optional[float] = None
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
    # hedge_count themselves are left unchanged and still used for
    # decide_hedge's initial "has anything ever been placed" guard (line
    # ~419) -- safe to leave placement-based since it's redundant with the
    # dominant_side()-is-None check just below it: a fully-cancelled first
    # attempt zeroes cost_by_side back via release_unfilled, so
    # dominant_side() already returns None regardless of which counter
    # gates entry.
    #
    # CORRECTED 2026-09-14 (root-cause fix for the entry_count-vs-
    # real_fill_count fidelity-scorecard investigation): the scout
    # eligibility check, is_first_entry (bot.py), forced_first_entry_side's
    # gate, and record_entry's own first_entry_* capture gate below were
    # ALL still entry_count-based until this fix -- WRONGLY grouped with
    # decide_hedge's guard above in the original 2026-09-12 comment. Unlike
    # decide_hedge's guard, these have no redundant protection: a market
    # whose first placement gets cancelled/expires unfilled (which happens
    # to a large fraction of orders -- see release_unfilled's docstring)
    # permanently loses "first entry" treatment for the rest of its life
    # once entry_count leaves 0, even though nothing ever actually filled
    # and the market is, in every economic sense the trader would
    # recognize, still awaiting its true first trade. Confirmed live via
    # bot.py's own fill-rate investigation that a large share of MID/CORE
    # markets fail their first attempt by cancellation ("price left target
    # band"), not expiry -- meaning this was live-corrupting SCOUT
    # eligibility, forced_first_entry_side, CROSS_MARKET_SIDE_PERSISTENCE/
    # CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER updates, and the captured
    # first_entry_price/regime/side/notional values themselves (stale,
    # from a cancelled attempt that the trader's world has no analogue
    # for) on every affected market. All four now gate on
    # real_fill_count == 0 instead, matching the "his trade log only has
    # fills" principle already applied to the calibration-table lookups
    # above. See this project's BUGS_TO_FIX.md #5 for the original
    # reasoning this corrects.
    real_fill_count: int = 0
    real_hedge_fill_count: int = 0
    # ADDED 2026-09-13, alongside DECISIVENESS_ONSET_MULTIPLIER: the
    # timestamp each side's book price FIRST crossed
    # config.DECISIVENESS_THRESHOLD (0.70). Locks on first crossing (like
    # first_entry_price) -- never reset or overwritten, since the finding
    # this feeds measures delay from the market's FIRST snap into a
    # decisive price, not from any later moment it happens to still be
    # there. Persisted across restarts via save_activity_state/
    # load_activity_state like every other field here, so a market mid-
    # reaction-window survives a deploy without losing its crossing time.
    up_decisive_crossed_at: Optional[float] = None
    down_decisive_crossed_at: Optional[float] = None
    # ADDED 2026-09-18, alongside what's now CHEAP_JOINT_ENTRY_MULTIPLIER
    # (freshness axis): the mirror-image tracker to
    # up_decisive_crossed_at/down_decisive_crossed_at above, but for a
    # side's price dropping BELOW bc.CHEAP_JOINT_CROSSING_THRESHOLD
    # (0.30, the CHEAP regime's own upper bound) rather than snapping
    # ABOVE DECISIVENESS_THRESHOLD (0.70). Needed because
    # build_order_intent's own decisiveness_fraction computation only
    # ever reads the >=0.70 crossing fields above -- there was no
    # tracking anywhere of when a side actually BECAME cheap, which is
    # exactly what the joint dampener's freshness axis needs (see its
    # own docstring in behavior_config.py for the full derivation: the
    # real trader's willingness to take a CHEAP crossing decays sharply
    # with how long it's been sitting there, and this bot had no
    # equivalent staleness-awareness at all). Same one-shot locking
    # semantics as the
    # decisive-crossing fields (never reset once set, since the finding
    # measures delay from the FIRST moment a side became cheap, not from
    # any later moment it happens to still be there).
    up_cheap_crossed_at: Optional[float] = None
    down_cheap_crossed_at: Optional[float] = None
    # ADDED 2026-09-18, alongside MID_CONTRARIAN_BIAS_PROBABILITY: the
    # first-ever recorded book price for each side once this market
    # starts being tracked -- NOT a threshold crossing like the fields
    # above, just a plain "first observation" latch. Needed for the
    # MID-band contrarian side-selection finding: the real trader's
    # first-entry side choice in MID genuinely favors whichever side has
    # LOST value since the window opened (confirmed cross-asset,
    # magnitude-graded, survives persist/switch and time-in-window
    # confound checks -- see external-bot-strategies-loop-progress.md's
    # 2026-09-18 entries for the full derivation). This requires knowing
    # each side's price AT WINDOW OPEN as a reference point, which
    # nothing previously tracked.
    up_first_price: Optional[float] = None
    down_first_price: Optional[float] = None
    # ADDED 2026-09-21, alongside REGIME_ENGAGEMENT_MULTIPLIER: side ->
    # list of regimes that gate has already rejected an ordinary entry
    # for in THIS market. Needed because a flat per-tick skip-probability
    # roll against a long-lived regime (MID/CORE routinely persist for
    # minutes) "eventually" passes no matter how low the probability is --
    # confirmed in the historical-replay validation this table's own
    # docstring references. Latching the roll per (side, regime) EPISODE
    # instead (roll once, remember the answer until the regime genuinely
    # changes) fixes this: a real regime change produces a different
    # dict key naturally, so no explicit clearing logic is needed -- an
    # old rejection for a regime a side has since LEFT simply never
    # matches again. Stored as a dict of lists (not a set of tuples) so
    # this round-trips through save_activity_state/load_activity_state's
    # plain JSON (a Python set isn't JSON-serializable).
    rejected_regime_episodes: dict = field(default_factory=dict)

    def is_regime_episode_rejected(self, side: str, regime: str) -> bool:
        return regime in self.rejected_regime_episodes.get(side, [])

    def reject_regime_episode(self, side: str, regime: str) -> None:
        self.rejected_regime_episodes.setdefault(side, [])
        if regime not in self.rejected_regime_episodes[side]:
            self.rejected_regime_episodes[side].append(regime)

    def update_first_prices(self, up_price: Optional[float], down_price: Optional[float]) -> None:
        """Latches the first non-None price seen for each side. Called
        every tick, same discipline as update_decisive_crossings/
        update_cheap_crossings -- a no-op once a side's first price is
        already recorded."""
        if self.up_first_price is None and up_price is not None:
            self.up_first_price = up_price
        if self.down_first_price is None and down_price is not None:
            self.down_first_price = down_price

    def position_tier(self) -> str:
        return bc.position_tier_for_index(self.real_fill_count)

    def update_decisive_crossings(self, up_price: Optional[float], down_price: Optional[float],
                                   now: float, threshold: float = 0.70) -> None:
        """Records the first time each side's price reaches `threshold`,
        called every tick with the market's current book prices (mirrors
        bot.py's _recent_price_delta -- a book-state-derived per-market
        signal updated once per tick before any decision is made this
        cycle). A no-op once a side has already crossed -- see the
        crossed_at fields' own docstring for why this locks."""
        if self.up_decisive_crossed_at is None and up_price is not None and up_price >= threshold:
            self.up_decisive_crossed_at = now
        if self.down_decisive_crossed_at is None and down_price is not None and down_price >= threshold:
            self.down_decisive_crossed_at = now

    def update_cheap_crossings(self, up_price: Optional[float], down_price: Optional[float],
                                now: float, threshold: float = 0.30) -> None:
        """Mirror image of update_decisive_crossings above: records the
        first time each side's price DROPS BELOW `threshold` (the CHEAP
        band's own upper bound). Same call-every-tick, one-shot-lock
        discipline -- see up_cheap_crossed_at's own docstring for why
        this exists."""
        if self.up_cheap_crossed_at is None and up_price is not None and up_price < threshold:
            self.up_cheap_crossed_at = now
        if self.down_cheap_crossed_at is None and down_price is not None and down_price < threshold:
            self.down_cheap_crossed_at = now

    def dominant_side(self) -> Optional[str]:
        """The side with more cumulative $ committed so far, or None if
        nothing's been placed yet."""
        up, down = self.cost_by_side.get("Up", 0.0), self.cost_by_side.get("Down", 0.0)
        if up == 0.0 and down == 0.0:
            return None
        return "Up" if up >= down else "Down"

    def record_entry(self, side: str, notional_usd: float = 0.0, regime: Optional[str] = None,
                      is_hedge: bool = False, price: Optional[float] = None,
                      t_in: Optional[float] = None) -> None:
        # CHANGED 2026-09-14 (entry_count -> real_fill_count, see this
        # dataclass's docstring above): gates on whether a REAL fill has
        # happened yet, not merely a placement. Without this, a market
        # whose first placement got cancelled/expired unfilled would lock
        # in that cancelled attempt's price/regime/side/notional as
        # "first entry" forever, even once a later attempt actually fills
        # -- stale metadata the trader's world (fills-only) has no
        # analogue for.
        if self.real_fill_count == 0 and regime is not None:
            self.first_entry_regime = regime
        if self.real_fill_count == 0 and price is not None:
            self.first_entry_price = price
        if self.real_fill_count == 0 and t_in is not None:
            self.first_entry_t_in = t_in
        if self.real_fill_count == 0:
            self.first_entry_side = side
            self.first_entry_notional = notional_usd
        self.cost_by_side[side] = self.cost_by_side.get(side, 0.0) + notional_usd
        self.entry_count += 1
        # FIXED 2026-09-19 (last_side-vs-dominant_side() divergence): a
        # hedge fill used to overwrite last_side unconditionally, same as
        # any ordinary entry -- meaning decide_side's own persist-or-
        # switch roll (SIDE_PERSISTENCE) would read the TRANSIENT hedge
        # side as "the currently held side" for the market's NEXT
        # decision, not the standing conviction side SIDE_PERSISTENCE was
        # actually calibrated to measure. Quantified live (2026-09-18):
        # of 1,874 dual-sided markets, 85.5% hit at least one moment
        # where last_side and dominant_side() disagreed, and the two
        # disagreed on 35.3% of all fill events in those markets --
        # mechanically expected, since a hedge fill flips last_side to
        # the hedge side while dominant_side() (computed separately from
        # cost_by_side) usually stays on the original, larger side. This
        # is a fixable BOT-STATE bug, distinct from and NOT reopening
        # the separately-investigated, confirmed-unsolvable problem of
        # recalibrating SIDE_PERSISTENCE's own TABLE from raw trader
        # data (three independent disambiguation heuristics -- size,
        # timing, combined -- all failed to beat a naive baseline there,
        # see last-side-vs-dominant-side-divergence.md; that dead end is
        # about classifying RAW trades with no ground truth, this fix is
        # about the bot's OWN internal state, which has perfect ground
        # truth via is_hedge). A hedge fill no longer moves last_side --
        # it still updates cost_by_side/dominant_side() and hedge_count
        # exactly as before.
        if not is_hedge:
            self.last_side = side
        else:
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


# ---------------------------------------------------------------------------
# PERSISTENCE for the dict[condition_id -> MarketActivityState] every
# PaperBot instance keeps as `self.activity`. ADDED 2026-09-13, closing a
# real, confirmed-live bug found the same day: PaperBot.__init__ built
# self.activity as a plain empty dict with NO reconstruction from anything
# on disk -- meaning EVERY restart (including an ordinary graceful
# `systemctl restart` during a deploy, not just a crash) silently wiped
# real_hedge_fill_count, cost_by_side, last_side, and every other
# in-progress-market counter for any market still open at that moment.
# Confirmed impact: MAX_HEDGE_COUNT_PER_MARKET's own cap is enforced
# against real_hedge_fill_count, which resets to 0 on every restart --
# so a market already at its 6-hedge cap gets a FRESH allowance of 6 more
# the instant the bot restarts mid-market. Found via a live ledger
# analysis showing one market with 19 separate hedge fills (should have
# been capped at 6) and a broader pattern of Bitcoin hedges destroying
# ~$2,549 of value across CHEAP/MID/HIGH bands in the main paperbot's
# full history -- a large fraction of which is consistent with hedge-
# laddering runaways exactly like this.
#
# Same JSON-dict-of-dataclasses pattern as Ledger.save()/load() in
# ledger.py (dataclasses.asdict() out, dict-unpack back in via **kwargs --
# every field here is already a plain JSON-safe type: str/int/float/
# Optional/dict, no sets or custom objects). Deliberately NOT wrapped in
# Ledger itself -- this is per-market LIVE state, not an append-only
# settlement log, and mixing the two would violate ledger.py's own "never
# a running counter, always recomputed" discipline for realized_pnl().
def save_activity_state(activity: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {cid: asdict(state) for cid, state in activity.items()}
    path.write_text(json.dumps(payload, indent=2))


def load_activity_state(path: Path) -> dict:
    """Returns {} if the file doesn't exist yet (first run ever, or an
    older deploy that predates this field) -- same graceful-empty-default
    discipline as Ledger.load(). A condition_id whose market has since
    fully resolved and rotated out is harmless: strategy_tick/manage_
    orders_tick only ever look up self.activity for markets still in
    markets_by_condition, so a stale leftover entry is simply never read
    again until the next _retire_market call (or a future new market
    that coincidentally reuses the same condition_id, which cannot happen
    since condition_ids are unique on-chain identifiers) pops it."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {cid: MarketActivityState(**fields) for cid, fields in raw.items()}


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
    offset: float = 0.0  # >0: a companion resting this far BELOW the best bid (see config.ENABLE_COMPANION_ORDERS)


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
    if config.ENABLE_RECENT_SIDE_PERSISTENCE:
        persistence = bc.recent_side_persistence(asset, held_regime, persistence)
    if rng.random() < persistence:
        return activity.last_side
    return "Down" if activity.last_side == "Up" else "Up"


def decide_hedge(asset: str, activity: MarketActivityState, rng: random.Random,
                  liquidity: Optional[float] = None,
                  is_weekend: Optional[bool] = None,
                  dominant_current_price: Optional[float] = None,
                  prev_hedge_rate: Optional[float] = None,
                  after_big_loss: Optional[bool] = None,
                  seconds_remaining: Optional[float] = None,
                  seconds_since_first_entry: Optional[float] = None) -> Optional[str]:
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

    RECENT-BIG-LOSS-conditioned (2026-09-13): `after_big_loss` (whether
    the PREVIOUS resolved market for this asset was a top-decile loss,
    by this bot's own rolling loss history) scales the FIRST-hedge
    probability only, via bc.hedge_trigger_after_big_loss_multiplier --
    real loss-aversion reaction confirmed via Mantel-Haenszel stratified
    odds ratio (Ethereum/Solana only, Bitcoin null; confirmed NOT
    symmetric for a big win). Optional (default None -> no-op) for the
    same reason as every other optional param here; bot.py maintains the
    rolling per-asset loss history and passes in the flag.

    REGIME POST-SHIFT-conditioned (2026-09-19, extended same day to
    CORE/HIGH/MID): for every regime, the FIRST-hedge probability is
    also scaled by bc.regime_hedge_post_shift_multiplier(asset, regime)
    -- a real, sharp drop in the trader's hedge-trigger rate confirmed
    at a specific recent boundary in most (regime, asset) cells (see
    REGIME_HEDGE_POST_SHIFT_MULTIPLIER's docstring in
    behavior_config.py for the full derivation, which cells are and
    aren't calibrated, and the honest short-window caveat -- including
    a same-day retraction of an earlier claim that MID needed a
    different fix; it doesn't, once measured on the correct
    first-hedge-only population). Not a new parameter -- computed
    internally from `asset` and `activity.first_entry_regime`, which
    this function already has.

    LATE-WINDOW HEDGE URGENCY-conditioned (2026-09-24): `seconds_since_
    first_entry` (how long ago THIS market's own first real entry
    happened, computed by the caller) combines with
    `activity.first_entry_t_in` (how early in the window that first entry
    itself was) to scale the FIRST-hedge probability via bc.late_window_
    hedge_urgency_multiplier -- a real, cross-asset-robust survival-
    analysis finding (Kaplan-Meier hazard, right-censored) that hedge
    hazard genuinely humps 150-180s after an EARLY (t_in<120s) first
    entry, confirmed via stratification and per-asset breakdown, and
    decisively cleared of double-counting with ADVERSE_MOVE_TTC_
    INTERACTION (Ethereum has zero calibration there yet independently
    shows the hump). No-ops cleanly (1.0) for late first entries or when
    either input is unavailable -- see that function's docstring in
    behavior_config.py. Optional (default None -> no-op) for the same
    reason as every other optional param here.
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
        if config.ENABLE_REGIME_HEDGE_POST_SHIFT_DAMPENER:
            # See REGIME_HEDGE_POST_SHIFT_MULTIPLIER's docstring in
            # behavior_config.py -- the real trader's hedge rate crashed
            # to a fraction of its old level in most (regime, asset)
            # cells at a sharp boundary (~2026-09-18 03:00 UTC),
            # confirmed a pure frequency change in every regime
            # including MID (contra an earlier same-day claim -- see
            # that docstring's retraction). Applied before the other
            # conditional multipliers so it scales the same base hazard
            # they all do, not layered on top of an already-adjusted p.
            # No-op (1.0) for any uncalibrated or unaffected cell -- see
            # that docstring for which ones (e.g. Solana/HIGH genuinely
            # didn't shift).
            p = max(0.0, min(1.0, p * bc.regime_hedge_post_shift_multiplier(
                asset, activity.first_entry_regime)))
        if config.ENABLE_LATE_WINDOW_HEDGE_URGENCY:
            urgency_mult = bc.late_window_hedge_urgency_multiplier(
                activity.first_entry_t_in, seconds_since_first_entry)
            p = max(0.0, min(1.0, p * urgency_mult))
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
            # FAVORABLE-MOVE hedge trigger (2026-09-19): the mirror image
            # of the block just above, covering adverse_move < 0 in
            # CHEAP specifically -- see
            # bc.favorable_move_hedge_trigger_multiplier's docstring for
            # the full GP-IRL derivation. adverse_move_hedge_trigger_
            # multiplier already no-ops for adverse_move<0, and this
            # one already no-ops for adverse_move>=0, so applying both
            # unconditionally is safe (exactly one of the two is ever
            # non-1.0 for a given adverse_move) -- CHEAP-scoped at the
            # call site since that's this multiplier's only calibrated
            # regime, unlike its sibling which spans all four.
            if (config.ENABLE_FAVORABLE_MOVE_HEDGE_TRIGGER_MULTIPLIER
                    and activity.first_entry_regime == "CHEAP"):
                favorable_mult = bc.favorable_move_hedge_trigger_multiplier(asset, adverse_move)
                p = max(0.0, min(1.0, p * favorable_mult))
            # MID favorable-move HUMP (2026-09-20): the MID-regime sibling
            # of the CHEAP block just above -- see
            # bc.mid_favorable_hedge_trigger_multiplier's docstring for
            # the per-asset replication check (Solana only; Bitcoin null,
            # Ethereum a different unshipped shape) that closes
            # replication-checklist item #31. Same mutual-exclusivity
            # reasoning as the CHEAP block: this only ever fires for
            # adverse_move<0, adverse_move_hedge_trigger_multiplier only
            # for >=0.
            if (config.ENABLE_MID_FAVORABLE_HEDGE_HUMP_MULTIPLIER
                    and activity.first_entry_regime == "MID"):
                mid_hump_mult = bc.mid_favorable_hedge_trigger_multiplier(asset, adverse_move)
                p = max(0.0, min(1.0, p * mid_hump_mult))
            # ADVERSE-MOVE x TIME-REMAINING interaction (2026-09-20): a
            # refinement ON TOP OF adverse_move_hedge_trigger_multiplier
            # above, not instead of it -- see
            # bc.adverse_move_ttc_interaction_multiplier's docstring for
            # the full Avellaneda-Stoikov-derived rationale (his
            # sensitivity to a given adverse move genuinely scales with
            # how much time remains, real in CHEAP/MID/CORE for
            # Bitcoin/Solana, absent in HIGH and for Ethereum). No-ops
            # cleanly (1.0) outside those cells or when seconds_remaining
            # is unavailable.
            if config.ENABLE_ADVERSE_MOVE_TTC_INTERACTION:
                ttc_interaction_mult = bc.adverse_move_ttc_interaction_multiplier(
                    asset, activity.first_entry_regime, adverse_move, seconds_remaining)
                p = max(0.0, min(1.0, p * ttc_interaction_mult))
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
        if config.ENABLE_HEDGE_TRIGGER_AFTER_BIG_LOSS_MULTIPLIER:
            loss_mult = bc.hedge_trigger_after_big_loss_multiplier(asset, after_big_loss)
            p = max(0.0, min(1.0, p * loss_mult))
    else:
        p = bc.hedge_continuation_probability(activity.real_hedge_fill_count)
    if p > 0 and rng.random() < p:
        return "Down" if dominant == "Up" else "Up"
    return None


def mid_contrarian_first_entry_side(asset: str, activity: MarketActivityState,
                                     up_book: BookState, down_book: BookState,
                                     rng: random.Random) -> Optional[str]:
    """Returns the contrarian side ("Up"/"Down") if the MID-band
    first-entry contrarian override should fire this tick, or None if
    not (in which case the caller falls through to the ordinary
    decide_side flow). See bc.MID_CONTRARIAN_BIAS_PROBABILITY's
    docstring for the full derivation and confound checks.

    Only meaningful for a market's TRUE first entry (activity.last_side
    is None -- mirrors decide_side's own first-entry branch exactly,
    since this is a priority override for that same branch, not a
    replacement for the persistence logic that applies once a side is
    established) and only when both sides currently sit in MID
    (0.30 <= price < 0.70) -- checking either side is sufficient since
    Up + Down ~= 1 makes the band symmetric (if Up is in MID, Down is
    too). Returns None gracefully whenever either side's first-observed
    price hasn't been recorded yet (a market just onboarded this same
    tick), matching this file's usual defensive-None handling for a
    signal that isn't available yet rather than raising.

    BUG CAUGHT LIVE (2026-09-18, before this ever shipped): a market
    evaluated for the first time has its first_price fields latched by
    bot.py's update_first_prices call THIS SAME TICK, before
    build_order_intent runs -- meaning velocity is exactly 0.0 on that
    very first tick, not "a small move." The original version treated
    magnitude==0 as the smallest tercile and still fired at that
    tercile's full probability (~60%) with literally zero underlying
    signal. Guarded explicitly below: zero (or near-zero, to absorb
    float noise) magnitude never fires, regardless of what an asset's
    calibrated small-move tercile probability says -- there has to be
    an actual observed move to be contrarian about.
    """
    if activity.last_side is not None:
        return None
    up_price = up_book.best_bid
    down_price = down_book.best_bid
    if up_price is None or down_price is None:
        return None
    if not (0.30 <= up_price < 0.70):
        return None
    if activity.up_first_price is None or activity.down_first_price is None:
        return None
    up_velocity = up_price - activity.up_first_price
    down_velocity = down_price - activity.down_first_price
    # Whichever side has LOST value since window-open is the contrarian
    # pick -- by construction (Up + Down ~= 1 at all times) these two
    # velocities are approximately opposite in sign, so exactly one side
    # is "the dip" in the ordinary case.
    if up_velocity <= down_velocity:
        contrarian_side, magnitude = "Up", abs(up_velocity)
    else:
        contrarian_side, magnitude = "Down", abs(down_velocity)
    if magnitude < 1e-9:
        return None  # no observed move yet -- nothing to be contrarian about
    p = bc.mid_contrarian_bias_probability(asset, magnitude)
    if p > 0 and rng.random() < p:
        return contrarian_side
    return None


def cross_asset_carryover_side(asset: str, activity: MarketActivityState,
                                up_book: BookState, down_book: BookState,
                                last_first_entry_by_asset: Optional[dict],
                                now: float, rng: random.Random) -> Optional[str]:
    """Returns the carryover side ("Up"/"Down") if the cross-asset
    decision-carryover override should fire this tick, or None if not
    (in which case the caller falls through to the ordinary decide_side
    flow, or the next priority override). See
    bc.CROSS_ASSET_CARRYOVER_PROBABILITY's docstring for the full
    derivation and confound checks.

    Only meaningful for a market's TRUE first entry (activity.
    real_fill_count == 0 -- the current best-practice "first entry"
    definition, matching build_order_intent's own forced_first_entry_side
    gate; see MarketActivityState's docstring for why entry_count was
    retired in favor of this) -- mirrors this file's other first-entry
    priority overrides.

    `last_first_entry_by_asset` is bot.py-maintained cross-market state
    (this module has no memory across assets of its own), same pattern
    as `previous_market_won`/`prev_hedge_rate`/etc. threaded through
    build_order_intent: {other_asset_name: {"side": "Up"/"Down", "ts":
    <fill_time>}}, updated whenever ANY asset registers a genuine first
    fill. None or empty -> no-op (matching this file's usual defensive
    handling for a signal that isn't available yet).

    Picks the MOST RECENT other-asset entry (not all of them -- the
    measured effect is specifically about the single most recent
    cross-asset decision, not an aggregate). Regime-gated to CHEAP/MID
    on the SUGGESTED side's own book price -- confirmed zero effect in
    CORE/HIGH (those first entries are mechanically driven by that
    market's own near-certain price, leaving no room for cross-asset
    mood to intrude), so this deliberately returns None outside that
    band rather than applying a diluted probability.
    """
    if activity.real_fill_count != 0:
        return None
    if not last_first_entry_by_asset:
        return None
    candidates = [(other_asset, info) for other_asset, info in last_first_entry_by_asset.items()
                  if other_asset != asset and info.get("side") is not None and info.get("ts") is not None]
    if not candidates:
        return None
    leader_asset, leader_info = max(candidates, key=lambda kv: kv[1]["ts"])
    gap = now - leader_info["ts"]
    if gap < 0:
        return None  # defensive: a future/clock-skew timestamp, treat as no signal
    prob = bc.cross_asset_carryover_probability(leader_asset, asset, gap)
    if prob <= 0:
        return None
    candidate_side = leader_info["side"]
    candidate_book = up_book if candidate_side == "Up" else down_book
    candidate_price = candidate_book.best_bid if candidate_book is not None else None
    if candidate_price is None or bc.classify_regime(candidate_price) not in ("CHEAP", "MID"):
        return None
    if rng.random() < prob:
        return candidate_side
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
    """True iff this market's window has both STARTED and not yet hit
    its own closing cutoff.

    FIXED 2026-09-18: added the open-side check (previously only the
    close-side `seconds_remaining >= MIN_SECONDS_BEFORE_CLOSE` existed).
    market_discovery.fetch_active_markets() deliberately onboards the
    NEXT 5-minute bucket early, for readiness -- but nothing downstream
    ever checked whether a market's window had actually opened before
    build_order_intent was allowed to trade it. Confirmed live: 36.83%
    of all MID-regime orders were being placed on a market before its
    window even started (median 278s early) -- Gamma/CLOB apparently
    still returns a live, if frozen and untraded, order book for a
    not-yet-open window. Confirmed as a genuine departure from the real
    trader's behavior, not a legitimate feature: zero of his 882,355
    real trades ever land before their own window's start. See
    mid-fill-delay-anomaly-and-chase-confound.md for the full
    derivation. `seconds_until_start` returning None (slug didn't parse)
    falls back to the pre-fix behavior -- don't block an otherwise-valid
    market over a parse failure."""
    seconds_until_start = market.seconds_until_start(now)
    if seconds_until_start is not None and seconds_until_start > 0:
        return False
    return market.seconds_remaining(now) >= config.MIN_SECONDS_BEFORE_CLOSE


def decide_size(asset: str, regime: str, position_tier: str, price: float,
                 rng: random.Random, seconds_remaining: Optional[float] = None,
                 hours_since_resumption: Optional[float] = None,
                 size_momentum_residual: Optional[float] = None,
                 bankroll_pnl_residual: Optional[float] = None,
                 real_fill_count: Optional[int] = None,
                 live_hedge_count: Optional[int] = None,
                 decisiveness_fraction: Optional[float] = None) -> tuple[float, bool]:
    """
    Returns (notional_usd, is_floor_lot). Rolls the floor-lot tier first for
    assets where it's modeled (Part 4); falls back to the normal
    entry-count sizing curve (Part 3) with a small symmetric jitter around
    the confirmed median, since only medians -- not full distributions --
    were measured.

    seconds_remaining, hours_since_resumption, size_momentum_residual,
    bankroll_pnl_residual, real_fill_count, live_hedge_count, and
    decisiveness_fraction are all Optional (default None -> no-op) so
    every existing call site/test that doesn't pass them keeps working
    unchanged. live_hedge_count should only ever be passed for an
    ORDINARY (non-hedge) entry -- see
    HEDGE_COUNT_REINFORCEMENT_MULTIPLIER's docstring in behavior_config.py.
    """
    floor_p = bc.floor_lot_probability(asset, regime, position_tier)
    if floor_p > 0 and rng.random() < floor_p:
        notional = config.FLOOR_LOT_SIZE_SHARES * price
        return notional, True

    jitter = 1.0 + rng.uniform(-config.SIZING_JITTER_FRACTION, config.SIZING_JITTER_FRACTION)
    # SHARE-BASED ENTRY SIZING (2026-09-20/21, Phase 1 of share-based-
    # sizing-implementation-design.md): on-chain ground truth replaces
    # the statistically-fit median * within_band product with one
    # formula backed by exact on-chain fills. See ENABLE_SHARE_BASED_
    # SIZING's docstring in config.py for scope/rollout. Everything
    # else in this function (momentum/bankroll/ttc/resumption/reentry_
    # fatigue/hedge_count_reinforcement/decisiveness/overshoot_
    # correction below) still composes on top of this base exactly as
    # before -- Phase 1 only replaces the BASE curve, not those
    # separately-validated multipliers (see the design doc's own
    # "confirmed independent, no action needed" list).
    if config.ENABLE_SHARE_BASED_SIZING and regime in bc.SHARE_BASED_SIZING_REGIMES:
        median = bc.notional_for_price(price, position_tier)
        within_band = 1.0  # subsumed by notional_for_price's own price-shape
    else:
        median = bc.median_entry_notional(asset, regime, position_tier)
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
        # DEEP CHEAP TAIL correction (2026-09-23 finding): WITHIN_BAND_SIZE_
        # SLOPE's single whole-band CHEAP slope badly under-corrects the
        # deepest part of that band (price<0.08, ~28-30% of all CHEAP
        # entries by count) -- real sizes there run 1.4-4.2x smaller than
        # median_entry_notional * within_band_size_multiplier predicts. See
        # DEEP_CHEAP_TAIL_MULTIPLIER's docstring in behavior_config.py.
        # Composes with (doesn't replace) within_band, same layering as
        # HIGH_TAIL_SHARE_MULTIPLIER on the share-based path.
        if (regime == "CHEAP" and price < bc.DEEP_CHEAP_TAIL_PRICE_THRESHOLD
                and config.ENABLE_DEEP_CHEAP_TAIL_CORRECTION):
            within_band *= bc.deep_cheap_tail_multiplier(asset)
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
    # RE-ENTRY FATIGUE (2026-09-13 finding): unlike every multiplier above,
    # this isn't calibrated to replicate observed real sizing -- it's a
    # deliberate risk dampener once a market has already had enough
    # same-side real fills in one of 6 specific (asset, regime) cells across
    # MID/CORE/CHEAP, where real outcomes are reliably worse. See
    # REENTRY_FATIGUE_MULTIPLIER's docstring in behavior_config.py for the
    # full derivation, per-band confound checks, and why each excluded
    # (asset, regime) cell was left out.
    reentry_fatigue_mult = bc.reentry_fatigue_multiplier(asset, regime, real_fill_count) \
        if config.ENABLE_REENTRY_FATIGUE_DAMPENER else 1.0
    # HEDGE-COUNT REINFORCEMENT (2026-09-13 finding): the mirror-image
    # signal to re-entry fatigue -- needing 2+ hedges against a market's
    # original side predicts that side wins MORE, not less, for Ethereum/
    # Solana in CHEAP/MID. Verified against the LIVE hedge-count-so-far
    # (not a post-hoc final total) before building. See
    # HEDGE_COUNT_REINFORCEMENT_MULTIPLIER's docstring in behavior_config.py.
    # Only meaningful for ordinary (non-hedge) entries -- the caller must
    # not pass live_hedge_count for a hedge leg's own sizing.
    hedge_count_reinforcement_mult = bc.hedge_count_reinforcement_multiplier(
        asset, regime, live_hedge_count) if config.ENABLE_HEDGE_COUNT_REINFORCEMENT else 1.0
    # DECISIVENESS-ONSET (2026-09-13 finding): reaction speed to a market
    # snapping into CORE/HIGH scales with how much time was left when it
    # happened, not a fixed absolute delay -- see DECISIVENESS_ONSET_
    # MULTIPLIER's docstring in behavior_config.py for the ceiling-effect
    # confound check this survived before being built.
    decisiveness_mult = bc.decisiveness_onset_multiplier(
        asset, regime, decisiveness_fraction) if config.ENABLE_DECISIVENESS_ONSET_MULTIPLIER else 1.0
    # DECISIVENESS x TTC x WITHIN-BAND overshoot correction (2026-09-20):
    # closes checklist item #25 -- unlike momentum x bankroll (tested,
    # found to be calendar drift, not real), decisiveness_fraction and
    # seconds_remaining are MECHANICALLY correlated (r=-0.82) within a
    # single market, and a direct real-vs-predicted test found the three
    # multipliers above genuinely overshoot real Ethereum/Solana HIGH
    # sizing at high decisiveness_fraction -- see bc.decisiveness_ttc_
    # overshoot_correction's docstring for the split-half-confirmed
    # derivation. Bitcoin excluded (undershoots instead, a separate gap).
    overshoot_correction = bc.decisiveness_ttc_overshoot_correction(
        asset, regime, decisiveness_fraction) if config.ENABLE_DECISIVENESS_TTC_OVERSHOOT_CORRECTION else 1.0
    # DECISIVENESS x WITHIN-BAND overshoot correction (2026-09-21): a
    # SECOND, distinct double-counting bug from the on-chain share-sizing
    # audit -- decisiveness_fraction's collinearity with raw price level
    # (within_band's own axis), not TTC/seconds_remaining like the
    # correction above. Hits CORE as well as HIGH. See bc.decisiveness_
    # withinband_overshoot_correction's docstring for the split-half-
    # confirmed derivation. Bitcoin excluded (undershoots instead, same
    # reasoning as the TTC correction).
    withinband_overshoot_correction = bc.decisiveness_withinband_overshoot_correction(
        asset, regime, decisiveness_fraction) if config.ENABLE_DECISIVENESS_WITHINBAND_OVERSHOOT_CORRECTION else 1.0
    # CHEAP EARLY DEEP-TOUCH SIZE BOOST (2026-09-22): the SIZE half of
    # bc.cheap_deep_touch_edge_multiplier -- see its docstring for the full
    # derivation. A structural market fact (an early collapse to a deep
    # price tends to overshoot and partially revert), not a trader-
    # fidelity signal, so it's independent of and composes with every
    # multiplier above. Only ever a boost (>=1.0) or a no-op here -- the
    # proven-negative late-touch case is a SKIP dampener instead, applied
    # in build_order_intent before decide_size is even called (a skip
    # can't be expressed as a size multiplier). Scoped to first entries
    # only, matching what was actually measured (a touch is a first-
    # crossing event).
    cheap_deep_touch_mult = 1.0
    if (config.ENABLE_CHEAP_DEEP_TOUCH_EDGE and position_tier == "first"
            and regime == "CHEAP" and seconds_remaining is not None):
        raw = bc.cheap_deep_touch_edge_multiplier(price, 300.0 - seconds_remaining)
        if raw > 1.0:  # only the early (boost) case -- the late (skip) case is handled entirely
            cheap_deep_touch_mult = raw  # in build_order_intent's entry-eligibility gate, not here
    # MID BOUNDARY-ADJACENCY SIZE BOOST (2026-09-22): the SIZE half of
    # bc.mid_boundary_edge_multiplier -- see its docstring for the full
    # derivation. Same design as the CHEAP deep-touch boost just above:
    # only ever a boost (>=1.0) or a no-op here -- the proven-negative
    # high-MID case is a SKIP dampener in build_order_intent instead.
    mid_boundary_mult = 1.0
    if config.ENABLE_MID_BOUNDARY_EDGE:
        # not scoped to position_tier=="first" -- unlike the CHEAP
        # deep-touch edge (a first-crossing event), this was measured
        # across every decision tier pooled, matching REGIME_ENGAGEMENT_
        # MULTIPLIER's own precedent of applying to a market's whole life.
        raw_mid = bc.mid_boundary_edge_multiplier(regime, price)
        if raw_mid > 1.0:
            mid_boundary_mult = raw_mid
    # COMBINED-MULTIPLIER SAFETY BOUND (2026-09-12, bug audit #4): each of
    # within_band/momentum_mult/bankroll_mult/ttc_mult/resumption_mult/
    # reentry_fatigue_mult/hedge_count_reinforcement_mult/decisiveness_mult
    # was fit MARGINALLY -- controlling for other already-known variables
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
    combined_signal = (within_band * momentum_mult * bankroll_mult * ttc_mult
                        * resumption_mult * reentry_fatigue_mult
                        * hedge_count_reinforcement_mult * decisiveness_mult
                        * overshoot_correction * withinband_overshoot_correction
                        * cheap_deep_touch_mult * mid_boundary_mult)
    cap = config.COMBINED_SIZE_MULTIPLIER_CAP
    combined_signal = max(1.0 / cap, min(cap, combined_signal))
    # config.SIZE_SCALE_FACTOR is a no-op (1.0) everywhere except a
    # deliberately small-bankroll instance -- see its docstring in
    # config.py. Applied here (not to the floor-lot branch above) so the
    # tiny, independently-calibrated probe tier is never pushed below a
    # real exchange minimum by a scale-down meant for the ordinary curve.
    level_correction = (bc.entry_size_level_correction(asset, regime)
                         if config.ENABLE_ENTRY_SIZE_LEVEL_CORRECTION else 1.0)
    return max(median * level_correction * jitter * combined_signal * config.SIZE_SCALE_FACTOR, 0.0), False


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
                        bankroll_pnl_residual: Optional[float] = None,
                        after_big_loss: Optional[bool] = None,
                        forced_first_entry_side: Optional[str] = None,
                        last_first_entry_by_asset: Optional[dict] = None,
                        regime_engagement_state: Optional[dict] = None) -> Optional[OrderIntent]:
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
    `rolling_accuracy` (all added 2026-09-11), `bankroll_pnl_residual`
    (added 2026-09-12), and `after_big_loss` (added 2026-09-13) are
    per-asset cross-market state that only bot.py can maintain (this
    module has no memory across markets of its own) -- all Optional
    (default None -> no-op for every one of them) so every existing call
    site/test keeps working unchanged. See decide_side/decide_hedge/
    decide_size's own docstrings for what each one feeds.

    `regime_engagement_state` (added 2026-09-23): bot.py-maintained per-asset
    cumulative {"counts": {regime: n}, "total": n} of ordinary (non-hedge)
    entries that have PASSED the regime-engagement gate so far -- feeds
    bc.regime_drift_correction (see its docstring). Optional (default None
    -> no-op, same reason as every other cross-market tracker above).
    Read-only here; bot.py updates it after seeing the returned intent
    (same pattern as last_first_entry_by_asset), not mutated in this module.

    `forced_first_entry_side` (added 2026-09-13, Coinbase-momentum bot):
    when set AND this is the market's first entry (activity.real_fill_count
    == 0 -- CHANGED 2026-09-14 from entry_count, see MarketActivityState's
    docstring) AND decide_hedge didn't already fire (it structurally never
    does on a first entry -- see its own entry_count < 1 guard, so this
    never actually competes with a hedge), this side is used DIRECTLY
    instead of calling decide_side -- every downstream step (regime
    classification from that side's own book, decide_size's full
    calibrated multiplier stack, scout/floor-lot/exchange-minimum
    handling) proceeds completely unchanged. This is the ONE integration
    point between the trader-replica pipeline and coinbase_bot.py's live
    Coinbase-momentum side selection: paperbot/paperbot-100 never pass
    this (default None -> no-op, decide_side runs exactly as before);
    CoinbaseMomentumBot computes it from real spot momentum for CHEAP/MID
    first entries specifically and leaves it None otherwise (out-of-scope
    regime, no data yet, or too flat a move), falling back to this same
    trader-replica decide_side/cross-market-persistence logic in that
    case -- see coinbase_strategy.momentum_forced_side's docstring.
    Never applied to subsequent (non-first) entries in the same market --
    those keep using decide_side's normal held-side persistence, since
    the validated spot-momentum finding was measured on first entries
    specifically.

    `last_first_entry_by_asset` (added 2026-09-19, cross-asset decision-
    carryover): bot.py-maintained cross-ASSET state (this module has no
    memory across assets of its own), same category as
    previous_market_won/prev_hedge_rate above but spanning Bitcoin/
    Ethereum/Solana instead of one asset's own market history. Feeds
    cross_asset_carryover_side's priority override -- see its own
    docstring and bc.CROSS_ASSET_CARRYOVER_PROBABILITY's derivation.
    Optional (default None -> no-op) for the same reason as everything
    else here.

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

    # ADDED 2026-09-24, for LATE_WINDOW_HEDGE_URGENCY_MULTIPLIER: seconds
    # elapsed in THIS window since THIS market's own first real entry.
    # Markets are always exactly 300s (5-min) by this project's own
    # convention throughout -- `market.end_time - 300.0` is the window's
    # open time, matching how first_entry_t_in itself gets computed at
    # bot.py's record_entry call site. None whenever there's no first
    # entry recorded yet (activity.first_entry_t_in is None) -- decide_hedge
    # already no-ops cleanly on a None input.
    seconds_since_first_entry = (
        (300.0 - seconds_remaining) - activity.first_entry_t_in
        if activity.first_entry_t_in is not None and seconds_remaining is not None
        else None
    )
    hedge_side = decide_hedge(
        market.asset, activity, rng, liquidity=market.liquidity,
        is_weekend=is_weekend, dominant_current_price=dominant_current_price,
        prev_hedge_rate=prev_hedge_rate, after_big_loss=after_big_loss,
        seconds_remaining=seconds_remaining,
        seconds_since_first_entry=seconds_since_first_entry)
    is_hedge = hedge_side is not None
    is_contrarian = False
    is_cross_asset_carryover = False
    if is_hedge:
        side = hedge_side
    elif forced_first_entry_side is not None and activity.real_fill_count == 0:
        # See forced_first_entry_side's own docstring above -- bypasses
        # decide_side entirely for this one case, everything else below
        # (regime, sizing, scout, floor-lot, exchange-minimum) is
        # identical to the ordinary path. CHANGED 2026-09-14: entry_count
        # -> real_fill_count, to stay consistent with bot.py's is_first_entry
        # (which now gates forced_first_entry_side's computation the same
        # way) -- otherwise a retry after a cancelled first attempt could
        # compute a forced side here but silently ignore it.
        side = forced_first_entry_side
    elif (config.ENABLE_MID_CONTRARIAN_BIAS
          and (mid_contrarian_side := mid_contrarian_first_entry_side(
              market.asset, activity, up_book, down_book, rng)) is not None):
        # MID-BAND CONTRARIAN first-entry override (2026-09-18): see
        # bc.MID_CONTRARIAN_BIAS_PROBABILITY's docstring for the full
        # derivation. Priority override ahead of decide_side, same
        # architectural slot as forced_first_entry_side above -- fires
        # probabilistically (most of the time, for a large MID move) and
        # falls through to the ordinary decide_side path otherwise.
        side = mid_contrarian_side
        is_contrarian = True
    elif (config.ENABLE_CROSS_ASSET_CARRYOVER
          and (carryover_side := cross_asset_carryover_side(
              market.asset, activity, up_book, down_book,
              last_first_entry_by_asset, now if now is not None else time.time(),
              rng)) is not None):
        # CROSS-ASSET DECISION-CARRYOVER first-entry override (2026-09-19):
        # see bc.CROSS_ASSET_CARRYOVER_PROBABILITY's docstring for the
        # full derivation. Priority override ahead of decide_side, same
        # architectural slot as mid_contrarian/forced_first_entry_side
        # above -- deliberately placed AFTER mid_contrarian so that
        # already-shipped, already-live-confirmed MID-band mechanism
        # keeps first refusal on MID first entries; this only fires when
        # mid_contrarian didn't (wrong regime for it -- i.e. CHEAP, where
        # mid_contrarian doesn't apply at all -- or a MID case where its
        # own magnitude/probability check missed).
        side = carryover_side
        is_cross_asset_carryover = True
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

    # ADDED 2026-09-24: TICK-OFFSET PLACEMENT (see bc.TICK_OFFSET_DISTRIBUTION's
    # docstring for the full derivation). Scoped to real first entries only
    # (not hedges, not add-ons) -- exactly what the underlying MarketLens
    # measurement covered, same discipline as forced_first_entry_side above.
    # `entry_offset` feeds OrderIntent.offset below (sign-flipped: the
    # field's established convention is positive = trails BELOW best_bid,
    # so an improving/positive tick count becomes a negative offset).
    entry_offset = 0.0
    if (config.ENABLE_TICK_OFFSET_PLACEMENT and not is_hedge
            and activity.real_fill_count == 0):
        tick = max(side_book.tick_size, 1e-6)
        offset_ticks = bc.sample_tick_offset(rng)
        candidate_price = price + offset_ticks * tick
        if side_book.best_ask is not None:
            candidate_price = min(candidate_price, side_book.best_ask - tick)
        if candidate_price > 0:
            price = round(candidate_price, 6)
            entry_offset = -offset_ticks * tick
    regime = bc.classify_regime(price)

    # DECISIVENESS-ONSET fraction (2026-09-13): how much of the time
    # available when this side first snapped decisive has elapsed since.
    # None whenever there's no crossing recorded yet for this side (most
    # CHEAP/MID entries -- decisiveness_onset_multiplier no-ops on those
    # regimes anyway) or `now` wasn't supplied. Computed here, once side/
    # price/regime are all known, rather than threaded in from bot.py --
    # activity and market (for end_time) are already in scope.
    decisiveness_fraction: Optional[float] = None
    if now is not None:
        crossed_at = activity.up_decisive_crossed_at if side == "Up" else activity.down_decisive_crossed_at
        if crossed_at is not None:
            total_available = market.end_time - crossed_at
            if total_available > 0:
                decisiveness_fraction = (now - crossed_at) / total_available

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

    # REGIME ENGAGEMENT (2026-09-21): see bc.regime_engagement_multiplier's
    # docstring for the full derivation -- a historical replay found the
    # bot has no active preference for CHEAP/HIGH over MID/CORE at all
    # (unlike the freshness-gated dampeners just below, which only ever
    # suppress STALE crossings on first entries). Deliberately UNSCOPED
    # by real_fill_count -- unlike the CHEAP-joint/decisive-eligibility
    # gates below, this applies to every ordinary (non-hedge) entry
    # consideration in the market's life, since the finding it's fixing
    # (his overall regime MIX) was measured across his whole decision
    # history, not just first entries. Rolled ONCE per (side, regime)
    # EPISODE via activity.is_regime_episode_rejected/reject_regime_
    # episode, not every tick -- confirmed in the historical-replay
    # validation that a flat per-tick roll against a long-lived MID/CORE
    # episode "eventually" passes no matter how low the probability is,
    # since a real regime change naturally produces a fresh (side,
    # regime) key with no rejection recorded against it yet.
    if config.ENABLE_REGIME_ENGAGEMENT_MULTIPLIER and not is_hedge:
        if activity.is_regime_episode_rejected(side, regime):
            return None
        engagement_mult = bc.regime_engagement_multiplier(market.asset, regime)
        # PARTICIPATION_ASSET_SCALE (2026-09-22): a per-asset overlay on
        # top of the regime-mix table above -- see its docstring in
        # behavior_config.py. Scales toward 1.0 (less skip), never past
        # it, so it can only raise overall engagement, never distort the
        # regime-mix ratios the base table was calibrated for.
        if config.ENABLE_PARTICIPATION_ASSET_SCALE:
            engagement_mult = min(1.0, engagement_mult * bc.participation_asset_scale(market.asset))
        # REGIME DRIFT CORRECTION (2026-09-23): closed-loop overlay on top of
        # the two static multipliers above -- see bc.regime_drift_correction's
        # docstring for the full derivation and held-out validation. Applied
        # last so it corrects whatever the static calibration produces,
        # rather than being baked into either static table. No clamp to 1.0
        # here (unlike ASSET_SCALE just above): this one is explicitly meant
        # to be able to push the effective probability BELOW the static
        # table's own value too (e.g. dampening an over-firing HIGH anchor),
        # not just raise it -- bc.regime_drift_correction's own cap (2.0/0.5)
        # is the only bound.
        if config.ENABLE_REGIME_DRIFT_CORRECTION and regime_engagement_state is not None:
            asset_state = regime_engagement_state.get(market.asset, {})
            drift_mult = bc.regime_drift_correction(
                market.asset, regime,
                asset_state.get("counts", {}), asset_state.get("total", 0))
            engagement_mult = max(0.0, min(1.0, engagement_mult * drift_mult))
        if engagement_mult < 1.0 and rng.random() > engagement_mult:
            activity.reject_regime_episode(side, regime)
            logger.info(
                "SKIP asset=%s regime=%s price=%.4f: regime engagement dampener (mult=%.2f)",
                market.asset, regime, price, engagement_mult)
            return None

    # MID BOUNDARY-ADJACENCY SKIP dampener (2026-09-22): the SKIP half of
    # bc.mid_boundary_edge_multiplier -- see its docstring for the full
    # derivation. A structural market fact (MID prices adjacent to CORE,
    # 0.60-0.70, are systematically overpriced relative to their real win
    # rate: ROI -33.5% [-61.6,-2.8], split-half stable), independent of
    # and deliberately separate from REGIME_ENGAGEMENT_MULTIPLIER's own
    # MID rate just above (that table's overall MID edge is NOT
    # established -- this narrower sub-band is). Not scoped to real_fill_
    # count==0 or is_hedge, same reasoning as the regime-engagement gate.
    if config.ENABLE_MID_BOUNDARY_EDGE and not is_hedge:
        boundary_mult = bc.mid_boundary_edge_multiplier(regime, price)
        if boundary_mult < 1.0 and rng.random() > boundary_mult:
            logger.info(
                "SKIP asset=%s regime=%s price=%.4f: mid-boundary-adjacency dampener (mult=%.2f)",
                market.asset, regime, price, boundary_mult)
            return None

    # CHEAP-JOINT entry dampener (2026-09-18, merged into one joint
    # price-band x freshness-bucket mechanism 2026-09-19): the real
    # trader avoids the deepest-cheap sub-band of CHEAP (price <
    # bc.CHEAP_JOINT_PRICE_THRESHOLD) AND lets a first-entry crossing go
    # stale far more than a uniform model of either axis alone predicts
    # -- see bc.cheap_joint_entry_multiplier's docstring for the full
    # derivation, including why this used to be two independently-
    # multiplied dampeners and why that was proven wrong (a real
    # price x freshness interaction, not two separable effects). Gated
    # to first entries only (not hedges, whose price/side is wherever
    # the dominant side already sits, not a fresh preference) and
    # checked before sizing, same SKIP pattern as availability_check
    # just above. `now` may be None in some test/CLI call sites (same
    # pattern as decisiveness_fraction above) -- no-ops cleanly via
    # seconds_since_cheap_crossing staying None in that case.
    if (config.ENABLE_CHEAP_JOINT_DAMPENER and not is_hedge
            and activity.real_fill_count == 0 and regime == "CHEAP"):
        seconds_since_cheap_crossing: Optional[float] = None
        if now is not None:
            cheap_crossed_at = (activity.up_cheap_crossed_at if side == "Up"
                                 else activity.down_cheap_crossed_at)
            if cheap_crossed_at is not None:
                seconds_since_cheap_crossing = now - cheap_crossed_at
        joint_mult = bc.cheap_joint_entry_multiplier(market.asset, price, seconds_since_cheap_crossing)
        if joint_mult < 1.0 and rng.random() > joint_mult:
            logger.info(
                "SKIP asset=%s regime=%s price=%.4f: cheap-joint dampener "
                "(seconds_since_crossing=%s, mult=%.2f)",
                market.asset, regime, price, seconds_since_cheap_crossing, joint_mult)
            return None
        # CHEAP TTC-selectivity dampener (2026-09-20): a SEPARATE axis
        # from the joint dampener just above -- time UNTIL close, not
        # time SINCE crossing. See bc.cheap_ttc_selectivity_multiplier's
        # docstring for the full Avellaneda-Stoikov-derived rationale
        # (Ethereum/Solana only, Bitcoin showed no effect). Only
        # dampens a price that's WORSE than what's typically available
        # at this point in the window -- a good price is never skipped
        # by this mechanism.
        if config.ENABLE_CHEAP_TTC_SELECTIVITY:
            ttc_selectivity_mult = bc.cheap_ttc_selectivity_multiplier(market.asset, price, seconds_remaining)
            if ttc_selectivity_mult < 1.0 and rng.random() > ttc_selectivity_mult:
                logger.info(
                    "SKIP asset=%s regime=%s price=%.4f: cheap-ttc-selectivity dampener "
                    "(seconds_remaining=%s, mult=%.2f)",
                    market.asset, regime, price, seconds_remaining, ttc_selectivity_mult)
                return None
        # CHEAP LATE DEEP-TOUCH SKIP dampener (2026-09-22): the SKIP half
        # of bc.cheap_deep_touch_edge_multiplier -- see its docstring for
        # the full derivation. A structural market fact (a LATE collapse
        # to a deep price is closer to genuine information and already
        # priced fairly-to-generously -- buying it loses on average,
        # -2.57pp [-4.03,-0.86] window-clustered), independent of the two
        # dampeners just above. The EARLY case (proven positive) is a
        # SIZE boost in decide_size instead -- a skip-probability gate
        # can only reduce, never increase, an entry rate.
        if config.ENABLE_CHEAP_DEEP_TOUCH_EDGE and seconds_remaining is not None:
            deep_touch_mult = bc.cheap_deep_touch_edge_multiplier(price, 300.0 - seconds_remaining)
            if deep_touch_mult < 1.0 and rng.random() > deep_touch_mult:
                logger.info(
                    "SKIP asset=%s regime=%s price=%.4f: cheap-late-deep-touch dampener "
                    "(seconds_into_window=%.0f, mult=%.2f)",
                    market.asset, regime, price, 300.0 - seconds_remaining, deep_touch_mult)
                return None

    # DECISIVE-ENTRY ELIGIBILITY (2026-09-20): the CORE/HIGH sibling of
    # the CHEAP-joint dampener just above -- see
    # bc.decisive_entry_eligibility_multiplier's docstring for the full
    # derivation. Closes a gap found by checking whether the CHEAP
    # win-rate investigation generalized: CORE/HIGH never had an
    # entry-eligibility freshness gate at all (DECISIVENESS_ONSET_
    # MULTIPLIER only ever scaled size). Same skip-probability pattern,
    # first entries only, mutually exclusive regime scope with the CHEAP
    # block above so the two never both fire for the same decision.
    if (config.ENABLE_DECISIVE_ENTRY_ELIGIBILITY and not is_hedge
            and activity.real_fill_count == 0 and regime in ("CORE", "HIGH")):
        seconds_since_decisive_crossing: Optional[float] = None
        if now is not None:
            decisive_crossed_at = (activity.up_decisive_crossed_at if side == "Up"
                                    else activity.down_decisive_crossed_at)
            if decisive_crossed_at is not None:
                seconds_since_decisive_crossing = now - decisive_crossed_at
        eligibility_mult = bc.decisive_entry_eligibility_multiplier(market.asset, seconds_since_decisive_crossing)
        if eligibility_mult < 1.0 and rng.random() > eligibility_mult:
            logger.info(
                "SKIP asset=%s regime=%s price=%.4f: decisive-entry eligibility dampener "
                "(seconds_since_crossing=%s, mult=%.2f)",
                market.asset, regime, price, seconds_since_decisive_crossing, eligibility_mult)
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
        # hedge_mult accumulates every CORRECTION multiplier below,
        # separately from `ratio`'s own base value (HEDGE_SIZE_RATIO /
        # HEDGE_CONTINUATION_SIZE_RATIO, which varies legitimately by
        # asset/regime/index and isn't a 1.0-centered factor) -- so the
        # combined-pileup safety cap just below only ever bounds the
        # correction, never clips a base ratio that's intentionally > 1.0
        # (e.g. BTC/CHEAP's 2.64). See HEDGE_RATIO_MULTIPLIER_CAP's
        # docstring in config.py (code-audit pass, 2026-09-13) -- the exact
        # same class of risk COMBINED_SIZE_MULTIPLIER_CAP already bounds on
        # the ordinary entry-curve path in decide_size, never previously
        # extended to this chain even as multipliers kept being added here.
        hedge_mult = 1.0
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
                hedge_mult *= bc.adverse_move_size_multiplier(market.asset, adverse_move)
            # ADDED 2026-09-12: real, confirmed-independent (via partial
            # correlation controlling for adverse_move) finding that the
            # hedge side's own ABSOLUTE price also predicts size, on top of
            # (not instead of) the delta-from-entry effect just above -- see
            # ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER's docstring in
            # behavior_config.py. Uses the same `price` (the hedge side's
            # own book price) the adverse_move calculation above already
            # reads, no new state needed.
            if config.ENABLE_ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER:
                hedge_mult *= bc.absolute_price_hedge_size_multiplier(market.asset, price)
        else:
            ratio = bc.hedge_continuation_size_ratio(activity.real_hedge_fill_count + 1)
            # ADDED 2026-09-10: direct extension of the hedge_count==0 case
            # above to continuation hedges -- same since-ORIGIN adverse_move
            # definition (deep-research-confirmed to beat a since-last-hedge
            # alternative), see ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER's
            # docstring in behavior_config.py.
            if config.ENABLE_ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER and activity.first_entry_price is not None:
                adverse_move = activity.first_entry_price - (1.0 - price)
                hedge_mult *= bc.adverse_move_continuation_size_multiplier(market.asset, adverse_move)
        # HEDGE TTC (time-to-close) scaling (2026-09-13 finding): re-derived
        # and regime-composition-checked specifically for hedges (the
        # entry-side correction at TTC_SIZE_MULTIPLIER was never verified
        # against the original hedge data) -- real and cross-asset-
        # consistent in CHEAP/HIGH only. Applies to both the first hedge
        # and continuation hedges (calibrated on all hedge trades pooled).
        # See HEDGE_TTC_SIZE_MULTIPLIER's docstring in behavior_config.py.
        if config.ENABLE_HEDGE_TTC_SIZE_MULTIPLIER and seconds_remaining is not None:
            hedge_mult *= bc.hedge_ttc_size_multiplier(market.asset, regime, seconds_remaining)
        cap = config.HEDGE_RATIO_MULTIPLIER_CAP
        hedge_mult = max(1.0 / cap, min(cap, hedge_mult))
        ratio *= hedge_mult
        jitter = 1.0 + rng.uniform(-config.SIZING_JITTER_FRACTION, config.SIZING_JITTER_FRACTION)
        notional = max(dominant_cost * ratio * jitter, 0.0)
        is_floor_lot = False
        # PRICE-BASED HEDGE SIZING (2026-09-22, Phase 3): replaces the whole
        # dominant-cost-ratio result above (first AND continuation hedges)
        # with the price-only model -- see hedge_notional_for_price's
        # comment in behavior_config.py for the out-of-sample evidence.
        # SIZE_SCALE_FACTOR is applied explicitly here because the legacy
        # chain got it implicitly through dominant_cost (itself built from
        # scaled entries).
        if config.ENABLE_PRICE_BASED_HEDGE_SIZING:
            price_notional = bc.hedge_notional_for_price(market.asset, price)
            if price_notional is not None:
                notional = max(price_notional * config.SIZE_SCALE_FACTOR * jitter, 0.0)

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
                                                  bankroll_pnl_residual=bankroll_pnl_residual,
                                                  real_fill_count=activity.real_fill_count,
                                                  live_hedge_count=activity.real_hedge_fill_count,
                                                  decisiveness_fraction=decisiveness_fraction)
    else:
        notional, is_floor_lot = decide_size(market.asset, regime, position_tier, price, rng,
                                              seconds_remaining=seconds_remaining,
                                              hours_since_resumption=hours_since_resumption,
                                              size_momentum_residual=size_momentum_residual,
                                              bankroll_pnl_residual=bankroll_pnl_residual,
                                              real_fill_count=activity.real_fill_count,
                                              live_hedge_count=activity.real_hedge_fill_count,
                                              decisiveness_fraction=decisiveness_fraction)
        if is_contrarian and not is_floor_lot:
            # MID-CONTRARIAN size dampener (2026-09-18): a real, ~28%
            # size gap found the same day as the side-selection bias
            # above -- he treats a contrarian first entry as a
            # tentative, lower-conviction bet, not a confident
            # directional one. See MID_CONTRARIAN_SIZE_RATIO's docstring
            # in behavior_config.py. Not applied to the floor-lot tier,
            # same precedent as every other size multiplier in this
            # function (floor-lot's whole point is an already-tiny,
            # separately-calibrated notional).
            notional *= bc.MID_CONTRARIAN_SIZE_RATIO

    is_scout = False
    if (not is_hedge and not is_floor_lot and not is_contrarian
            and not is_cross_asset_carryover and activity.real_fill_count == 0):
        # SCOUT: a deliberately small, tentative first entry -- see
        # SCOUT_PROBABILITY/SCOUT_SIZE_RATIO's docstring in
        # behavior_config.py. CHANGED 2026-09-14: entry_count ->
        # real_fill_count (see MarketActivityState's docstring) -- a
        # market whose first placement attempt was cancelled/expired
        # unfilled still deserves scout eligibility on its next attempt,
        # since nothing has actually traded yet; entry_count locked this
        # out after any cancellation, silently under-applying
        # SCOUT_PROBABILITY exactly in the regimes (MID/CORE) that fail
        # their first attempt by cancellation most often. Only reachable
        # at the market's true first entry (decide_hedge structurally can
        # never fire this early -- dominant_side() needs at least one
        # prior entry to exist), and
        # never stacks with a floor-lot roll (that's an orthogonal, already
        # -tiny mechanism keyed off share-count minimums, not first-entry
        # tentativeness -- scaling it down further would just inflate its
        # already-expected skip rate for no modeling benefit).
        #
        # EXCLUDED 2026-09-18: also never stacks with a MID-contrarian
        # pick -- unlike floor-lot, this isn't structurally orthogonal
        # (SCOUT_PROBABILITY's own original calibration was measured
        # across ALL first entries, including contrarian-shaped ones,
        # before this signal was even known to exist), so applying both
        # dampeners to the same order would risk an unvalidated,
        # double-counted size reduction beyond what either finding
        # individually supports. MID_CONTRARIAN_SIZE_RATIO already
        # covers the "this is a tentative bet" adjustment for these
        # entries on its own.
        #
        # EXCLUDED 2026-09-19: also never stacks with a cross-asset-
        # carryover pick, for a DIFFERENT reason than contrarian above
        # (no dedicated size dampener exists for this one) -- without
        # this exclusion, a carryover-selected entry that also rolled
        # scout-eligible would report reason="scout_entry" (is_scout is
        # checked first below), silently hiding that carryover chose the
        # side at all. Since reason is now persisted onto the ledger
        # (see settlement-reason-persistence-and-post-deploy-puzzle.md),
        # masking one override behind another would defeat the whole
        # point of being able to isolate this mechanism's effect later.
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
    elif is_contrarian:
        reason = "mid_contrarian"
    elif is_cross_asset_carryover:
        reason = "cross_asset_carryover"
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
        offset=entry_offset,
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
