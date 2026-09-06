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
        if self.expired_remainder:
            return False
        return self.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)


class FillSimulator:
    """
    Owns all SimulatedOrders. bot.py calls:
      - place_order() when strategy produces a fresh OrderIntent
      - on_trade_print() whenever the book emits a last_trade_price
      - manage_open_orders() every tick to handle drift-reprice and expiry
    """

    def __init__(self, queue_safety_factor: float = config.QUEUE_SAFETY_FACTOR):
        self.queue_safety_factor = queue_safety_factor
        self.orders: dict[int, SimulatedOrder] = {}

    # -- placement -------------------------------------------------------

    def place_order(self, intent: OrderIntent, book: BookState,
                     now: Optional[float] = None) -> SimulatedOrder:
        now = now if now is not None else time.time()
        raw_queue = book.depth_shares_at_or_better("bid", intent.price)
        discounted_queue = raw_queue * self.queue_safety_factor

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
            placed_at=now,
            remaining_size=intent.size_shares,
            queue_ahead_raw=raw_queue,
            queue_ahead_discounted=discounted_queue,
        )
        self.orders[order.order_id] = order
        logger.info(
            "PLACE order=%d asset=%s regime=%s pos=%s side=%s price=%.4f size=%.6f "
            "floor_lot=%s queue_ahead_raw=%.4f queue_ahead_discounted=%.4f (factor=%.3f)",
            order.order_id, order.asset, order.regime, order.position_tier, order.side,
            order.price, order.original_size, order.is_floor_lot,
            raw_queue, discounted_queue, self.queue_safety_factor,
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
        """
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
                    self._reprice(order, book, now)
                    changed.append(order)
                else:
                    # price left our target band entirely -- not our trade anymore
                    order.status = OrderStatus.CANCELLED
                    order.final_at = now
                    logger.info(
                        "CANCELLED order=%d asset=%s regime=%s: price left target band",
                        order.order_id, order.asset, order.regime,
                    )
                    changed.append(order)

        return changed

    def _reprice(self, order: SimulatedOrder, book: BookState, now: float) -> None:
        old_price = order.price
        new_raw_queue = book.depth_shares_at_or_better("bid", book.best_bid)
        order.price = book.best_bid
        order.queue_ahead_raw = new_raw_queue
        order.queue_ahead_discounted = new_raw_queue * self.queue_safety_factor
        order.reprice_count += 1
        order.placed_at = now  # time-to-fill measured from the latest resting price
        order.first_fill_at = None
        logger.info(
            "REPRICE order=%d asset=%s %.4f -> %.4f queue_ahead_raw=%.4f queue_ahead_discounted=%.4f",
            order.order_id, order.asset, old_price, order.price,
            new_raw_queue, order.queue_ahead_discounted,
        )

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
            partial = [o for o in orders if o.status == OrderStatus.PARTIALLY_FILLED and o.expired_remainder]
            times = [o.time_to_fill for o in filled if o.time_to_fill is not None]
            out[(asset, regime)] = {
                "n": total,
                "fill_rate": len(filled) / total if total else None,
                "partial_fill_rate": len(partial) / total if total else None,
                "expiry_rate": len(expired) / total if total else None,
                "median_time_to_fill": sorted(times)[len(times) // 2] if times else None,
            }
        return out
