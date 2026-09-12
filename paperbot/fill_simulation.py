"""
The paper fill model. This is the part explicitly called out as most
important to get right: orders do NOT fill instantly. Each resting order
tracks how much depth is genuinely "ahead" of it in the book, and that
queue is worked down only by real last_trade_price prints -- exactly the
mechanism a real maker order would have to wait through.
"""
from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from . import config
from .book import BookState, TradePrint
from .strategy import OrderIntent

logger = logging.getLogger("paperbot.fill_simulation")

_order_id_counter = itertools.count(1)


def seed_order_id_counter(start_at: int) -> None:
    """
    Reset the module-level order_id counter to begin at `start_at`. Call
    once at process startup, AFTER loading the persisted ledger -- see
    PaperBot.__init__, which seeds this past every order_id its loaded
    ledger has ever recorded a settlement for.

    CONFIRMED LIVE (2026-09-08) as the root cause of a severe, silent bug:
    order_id was a plain per-process counter starting at 1 on every
    restart, but Ledger._settled_order_ids (used by settle_order() as an
    idempotency check, keyed ONLY on this small integer -- no per-run
    namespace) persists across restarts, loaded fresh from disk every
    time. On a long-running ledger restarted many times, a fresh
    process's own order_id range collided almost entirely with order_ids
    already used by PRIOR runs: settle_order() saw `order_id in
    self._settled_order_ids` as True (matching some unrelated OLD order
    that happened to reuse the same small integer) and silently refused
    to record the real, brand-new settlement -- not an error, not a log
    line, just a missing record. Measured live: of one run's first 1,299
    order_ids, 1,253 (96.5%) had already been used by an earlier run,
    and only 54 of over 500 genuinely filled orders ever made it into the
    ledger. This was the dominant cause of the bot's settled-trade counts
    (and therefore entries/market, win rate, and PNL) looking far lower
    than what was actually happening.
    """
    global _order_id_counter
    _order_id_counter = itertools.count(start_at)


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    EXPIRED_UNFILLED = "EXPIRED_UNFILLED"
    CANCELLED = "CANCELLED"


@dataclass
class Fill:
    size: float
    price: float
    ts: float


@dataclass
class SimulatedOrder:
    order_id: int
    condition_id: str
    token_id: str
    asset: str
    regime: str
    position_tier: str
    side: str
    price: float
    original_size: float
    is_floor_lot: bool
    placed_at: float
    is_hedge: bool = False
    is_scout: bool = False

    # None means "not yet initialized -- default to original_size" (see
    # __post_init__). This is deliberately NOT 0.0: a caller must be able
    # to construct an order that is already fully filled (remaining_size
    # explicitly 0.0) without that value being mistaken for "unset". An
    # earlier version of this file used 0.0 as the sentinel and that
    # collision silently zeroed out real fills -- see test_ledger.py.
    remaining_size: Optional[float] = None
    queue_ahead_raw: float = 0.0
    queue_ahead_discounted: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    fills: list = field(default_factory=list)
    first_fill_at: Optional[float] = None
    final_at: Optional[float] = None
    reprice_count: int = 0
    expired_remainder: bool = False  # cutoff hit after a partial fill; the
                                      # unfilled remainder was cancelled but
                                      # the filled portion still settles.
    cancelled_remainder: bool = False  # price left the target band after a
                                        # partial fill; same idea as
                                        # expired_remainder above -- status
                                        # stays PARTIALLY_FILLED (not
                                        # overwritten to CANCELLED) so the
                                        # already-filled portion still
                                        # settles. See manage_open_orders'
                                        # drift-cancel branch.

    def __post_init__(self):
        if self.remaining_size is None:
            self.remaining_size = self.original_size

    @property
    def filled_size(self) -> float:
        # Derived from the fills log, not from original_size -
        # remaining_size: those two can be constructed independently in
        # tests/fixtures, and the fills log is the actual source of truth
        # for "what filled".
        return sum(f.size for f in self.fills)

    @property
    def time_to_fill(self) -> Optional[float]:
        if self.first_fill_at is None:
            return None
        return self.first_fill_at - self.placed_at

    def is_open(self) -> bool:
        if self.expired_remainder or self.cancelled_remainder:
            return False
        return self.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)


class FillSimulator:
    """
    Owns all SimulatedOrders. bot.py calls:
      - place_order() when strategy produces a fresh OrderIntent
      - on_trade_print() whenever the book emits a last_trade_price
      - manage_open_orders() every tick to handle drift-reprice and expiry
    """

    def __init__(self, queue_safety_factor: float = config.QUEUE_SAFETY_FACTOR,
                 queue_safety_factor_by_regime: Optional[dict] = None):
        self.queue_safety_factor = queue_safety_factor
        # ADDED 2026-09-11: optional per-regime override -- see
        # config.QUEUE_SAFETY_FACTOR_OVERRIDE_BY_REGIME's docstring.
        # Defaults to None (every existing call site -- CLI, tests, every
        # PaperBot() construction before tonight -- keeps behaving exactly
        # as before: one flat factor for every regime).
        self.queue_safety_factor_by_regime = queue_safety_factor_by_regime or {}
        self.orders: dict[int, SimulatedOrder] = {}

    def _queue_safety_factor_for(self, regime: str) -> float:
        return self.queue_safety_factor_by_regime.get(regime, self.queue_safety_factor)

    # -- placement -------------------------------------------------------

    def place_order(self, intent: OrderIntent, book: BookState,
                     now: Optional[float] = None) -> SimulatedOrder:
        now = now if now is not None else time.time()
        factor = self._queue_safety_factor_for(intent.regime)
        raw_queue = book.depth_shares_at_or_better("bid", intent.price)
        discounted_queue = raw_queue * factor

        order = SimulatedOrder(
            order_id=next(_order_id_counter),
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            asset=intent.asset,
            regime=intent.regime,
            position_tier=intent.position_tier,
            side=intent.side,
            price=intent.price,
            original_size=intent.size_shares,
            is_floor_lot=intent.is_floor_lot,
            is_hedge=intent.is_hedge,
            is_scout=intent.is_scout,
            placed_at=now,
            remaining_size=intent.size_shares,
            queue_ahead_raw=raw_queue,
            queue_ahead_discounted=discounted_queue,
        )
        self.orders[order.order_id] = order
        logger.info(
            "PLACE order=%d asset=%s regime=%s pos=%s side=%s price=%.4f size=%.6f "
            "floor_lot=%s hedge=%s scout=%s queue_ahead_raw=%.4f queue_ahead_discounted=%.4f (factor=%.3f)",
            order.order_id, order.asset, order.regime, order.position_tier, order.side,
            order.price, order.original_size, order.is_floor_lot, order.is_hedge, order.is_scout,
            raw_queue, discounted_queue, factor,
        )
        return order

    # -- consumption -------------------------------------------------------

    def on_trade_print(self, token_id: str, trade: TradePrint) -> None:
        """
        A real trade printed at trade.price. Any resting order on this
        token whose price is at-or-better than the trade price has its
        queue worked down by trade.size; once a given order's discounted
        queue is exhausted, further size fills that order directly.
        Consumption is applied price-then-time priority across our own
        open orders on this token (best price first, then earliest
        placement), matching real CLOB matching priority.

        `trade.ts < order.placed_at` is excluded on purpose: pending_trades
        is drained once per main-loop tick, so a trade that printed before
        an order existed can still be sitting undrained when that order
        gets placed later in the same tick. That trade is already
        reflected in the book snapshot the order's queue_ahead was measured
        against -- letting it also retroactively fill (or drain queue for)
        an order it predates produces impossible negative time-to-fill and
        inflates the fill rate. Confirmed live: order=9 in a real deploy
        logged `time_to_fill=-0.1s` before this filter was added.

        `trade.side` is filtered to SELL only -- also on purpose, and
        FIXED 2026-09-10 (code-review pass): this bot only ever places
        resting BUY (bid) orders, so only a SELL-side trade print (a
        taker selling, crossing INTO the bid side) can ever legitimately
        consume queue or fill one -- a BUY-side print (a taker buying,
        crossing into the ASK side) never touches the bid book at all.
        The eligibility check used to be `trade.price <= o.price` alone,
        with no side check whatsoever -- confirmed by grep that every
        single existing test used side="SELL", so this gap was never
        exercised. Usually harmless (a BUY print's price sits at/above
        the ask, normally well above any resting bid), but under a tight
        spread combined with a resting order that's drifted up to
        DRIFT_REPRICE_TICKS behind the live best_bid before its next
        reprice, a BUY-side print's price can fall at or below a stale
        bid price and would have been misattributed as filling it.
        """
        if trade.side.upper() != "SELL":
            return
        remaining_trade_size = trade.size
        candidates = [
            o for o in self.orders.values()
            if o.token_id == token_id and o.is_open() and trade.price <= o.price
            and trade.ts >= o.placed_at
        ]
        candidates.sort(key=lambda o: (-o.price, o.placed_at))

        for order in candidates:
            if remaining_trade_size <= 0:
                break
            remaining_trade_size = self._consume_one(order, remaining_trade_size, trade.ts)

    def _consume_one(self, order: SimulatedOrder, trade_size: float, ts: float) -> float:
        """Applies up to `trade_size` shares of consumption to `order`.
        Returns whatever's left of trade_size after this order took its
        share (of queue-draining and/or an actual fill)."""
        remaining = trade_size

        if order.queue_ahead_discounted > 0:
            consumed = min(remaining, order.queue_ahead_discounted)
            order.queue_ahead_discounted -= consumed
            remaining -= consumed

        if remaining > 0 and order.remaining_size > 0:
            fill_size = min(remaining, order.remaining_size)
            order.remaining_size -= fill_size
            order.fills.append(Fill(size=fill_size, price=order.price, ts=ts))
            remaining -= fill_size
            if order.first_fill_at is None:
                order.first_fill_at = ts
            if order.remaining_size <= 1e-9:
                order.status = OrderStatus.FILLED
                order.final_at = ts
                logger.info(
                    "FILLED order=%d asset=%s regime=%s time_to_fill=%.1fs",
                    order.order_id, order.asset, order.regime,
                    order.time_to_fill if order.time_to_fill is not None else -1,
                )
            else:
                order.status = OrderStatus.PARTIALLY_FILLED
                logger.info(
                    "PARTIAL_FILL order=%d asset=%s filled=%.6f/%.6f",
                    order.order_id, order.asset, order.filled_size, order.original_size,
                )

        return remaining

    # -- lifecycle management ---------------------------------------------

    def manage_open_orders(self, book_by_token: dict, seconds_remaining_by_condition: dict,
                            now: Optional[float] = None) -> list:
        """
        Call every tick. Handles:
          - expiry: cancel + mark EXPIRED_UNFILLED once < MIN_SECONDS_BEFORE_CLOSE
            remain for that market, whether or not the order has drifted.
          - drift repricing: if the book's best_bid has moved more than
            DRIFT_REPRICE_TICKS * tick_size away from the order's resting
            price (while price is still in-band), cancel and re-place at
            the new price with a freshly computed queue-ahead.
        Returns the list of orders that changed status this tick (for
        logging/inspection).
        """
        now = now if now is not None else time.time()
        changed = []

        for order in list(self.orders.values()):
            if not order.is_open():
                continue

            secs_remaining = seconds_remaining_by_condition.get(order.condition_id)
            if secs_remaining is not None and secs_remaining < config.MIN_SECONDS_BEFORE_CLOSE:
                if order.filled_size > 0:
                    # Partial fill survives the cutoff and will settle on
                    # its filled portion; only the unfilled remainder is
                    # cancelled (is_open() below excludes it from further
                    # consumption once expired_remainder is set). Status
                    # stays PARTIALLY_FILLED; remaining_size is left as-is
                    # for reporting -- it's not repurposed to mean
                    # "cancelled amount".
                    order.expired_remainder = True
                else:
                    order.status = OrderStatus.EXPIRED_UNFILLED
                order.final_at = now
                logger.info(
                    "EXPIRED order=%d asset=%s regime=%s filled=%.6f/%.6f (timing cutoff)",
                    order.order_id, order.asset, order.regime,
                    order.filled_size, order.original_size,
                )
                changed.append(order)
                continue

            book = book_by_token.get(order.token_id)
            if book is None or book.best_bid is None:
                continue

            drift = abs(book.best_bid - order.price)
            if drift > config.DRIFT_REPRICE_TICKS * max(book.tick_size, 1e-6):
                from . import behavior_config as bc
                same_band = bc.classify_regime(book.best_bid) == order.regime
                if same_band:
                    # CONFIRMED LIVE (2026-09-12, cheap-fill-calibration-gap):
                    # CHEAP orders that chase the book 3+ times land in a
                    # confirmed, badly-negative-edge bucket (z=-4.64) --
                    # adverse selection from repeatedly re-quoting into a
                    # book that keeps moving away. Cancel instead of
                    # repricing again once an order has already used up its
                    # cap, rather than let it walk further into that bucket.
                    if (config.ENABLE_CHEAP_REPRICE_CAP and order.regime == "CHEAP"
                            and order.reprice_count >= config.MAX_CHEAP_REPRICES):
                        if order.filled_size > 0:
                            order.cancelled_remainder = True
                        else:
                            order.status = OrderStatus.CANCELLED
                        order.final_at = now
                        logger.info(
                            "CANCELLED order=%d asset=%s regime=%s filled=%.6f/%.6f: "
                            "reprice cap reached (%d >= %d), stopped chasing",
                            order.order_id, order.asset, order.regime,
                            order.filled_size, order.original_size,
                            order.reprice_count, config.MAX_CHEAP_REPRICES,
                        )
                        changed.append(order)
                        continue
                    self._reprice(order, book, now)
                    changed.append(order)
                else:
                    # price left our target band entirely -- not our trade
                    # anymore. CONFIRMED LIVE (2026-09-08, via the
                    # RESOLUTION_GAP diagnostic in bot.py): unconditionally
                    # overwriting status to CANCELLED here -- even for an
                    # order with real fills already recorded -- silently
                    # dropped those fills from ever settling, since
                    # CANCELLED isn't in settle_order()'s allowed-status
                    # set (only FILLED/PARTIALLY_FILLED are). A partial
                    # fill's already-filled portion is exactly as real and
                    # exactly as settleable as one that survives the
                    # 90-second timing cutoff (see expired_remainder right
                    # above) -- only the STILL-UNFILLED remainder is what's
                    # actually being cancelled here.
                    if order.filled_size > 0:
                        order.cancelled_remainder = True
                    else:
                        order.status = OrderStatus.CANCELLED
                    order.final_at = now
                    logger.info(
                        "CANCELLED order=%d asset=%s regime=%s filled=%.6f/%.6f: "
                        "price left target band",
                        order.order_id, order.asset, order.regime,
                        order.filled_size, order.original_size,
                    )
                    changed.append(order)

        return changed

    def _reprice(self, order: SimulatedOrder, book: BookState, now: float) -> None:
        old_price = order.price
        new_raw_queue = book.depth_shares_at_or_better("bid", book.best_bid)
        order.price = book.best_bid
        order.queue_ahead_raw = new_raw_queue
        order.queue_ahead_discounted = new_raw_queue * self._queue_safety_factor_for(order.regime)
        order.reprice_count += 1
        order.placed_at = now  # time-to-fill measured from the latest resting price
        order.first_fill_at = None
        logger.info(
            "REPRICE order=%d asset=%s %.4f -> %.4f queue_ahead_raw=%.4f queue_ahead_discounted=%.4f",
            order.order_id, order.asset, old_price, order.price,
            new_raw_queue, order.queue_ahead_discounted,
        )

    # -- lifecycle cleanup ---------------------------------------------------

    def remove_orders_for_condition(self, condition_id: str) -> int:
        """
        Prune every order belonging to one market. Without this, self.orders
        grows unbounded for the lifetime of the process -- fine for a
        Railway deploy that gets restarted every push, but a real problem
        for a long-running AWS deployment meant to stay up for weeks.

        Only call this once bot.py's resolution_tick is DONE with a
        condition_id (settled or abandoned) -- by the time a market
        retires, every one of its orders is already terminal (this bot's
        90-second expiry cutoff always runs before a market can retire),
        so there's nothing further fill simulation could still do with
        them, and settlement has already happened by this point.

        Trade-off, documented rather than silent: stats_by_asset_regime()
        below becomes a rolling/recent view instead of a lifetime one,
        since pruned orders no longer contribute to it. It isn't wired
        into any live logging today (only exercised directly in tests),
        so this is a reasonable trade for bounded memory; if lifetime
        fill-rate/expiry-rate tracking is wanted later, aggregate into a
        small persistent counter at settlement time instead of relying on
        raw order objects staying around forever.
        """
        ids_to_remove = [oid for oid, o in self.orders.items() if o.condition_id == condition_id]
        for oid in ids_to_remove:
            del self.orders[oid]
        return len(ids_to_remove)

    # -- reporting ---------------------------------------------------------

    def stats_by_asset_regime(self) -> dict:
        """Fill rate, time-to-fill, and expiry rate broken out by
        (asset, regime) -- the real signal for whether QUEUE_SAFETY_FACTOR
        is calibrated reasonably."""
        buckets: dict[tuple, list] = {}
        for order in self.orders.values():
            if order.is_open():
                continue  # only finished orders count toward these rates
            buckets.setdefault((order.asset, order.regime), []).append(order)

        out = {}
        for (asset, regime), orders in buckets.items():
            total = len(orders)
            filled = [o for o in orders if o.status == OrderStatus.FILLED]
            expired = [o for o in orders if o.status == OrderStatus.EXPIRED_UNFILLED]
            partial = [o for o in orders if o.status == OrderStatus.PARTIALLY_FILLED
                       and (o.expired_remainder or o.cancelled_remainder)]
            times = [o.time_to_fill for o in filled if o.time_to_fill is not None]
            out[(asset, regime)] = {
                "n": total,
                "fill_rate": len(filled) / total if total else None,
                "partial_fill_rate": len(partial) / total if total else None,
                "expiry_rate": len(expired) / total if total else None,
                "median_time_to_fill": sorted(times)[len(times) // 2] if times else None,
            }
        return out
