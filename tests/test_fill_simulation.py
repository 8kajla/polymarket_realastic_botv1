import pytest

from paperbot.book import BookState, TradePrint
from paperbot.fill_simulation import FillSimulator, OrderStatus
from paperbot.strategy import OrderIntent


def make_book(best_bid=0.20, bid_depth_at_best=100.0, extra_bid_levels=None,
              best_ask=0.22, tick_size=0.01):
    bids = [(best_bid, bid_depth_at_best)]
    if extra_bid_levels:
        bids.extend(extra_bid_levels)
    book = BookState(token_id="tok-up")
    book.apply_snapshot(bids=bids, asks=[(best_ask, 100.0)])
    book.tick_size = tick_size
    return book


def make_intent(price=0.20, size_shares=10.0, regime="CHEAP", position_tier="first",
                 asset="Bitcoin", is_floor_lot=False, condition_id="cond-1",
                 token_id="tok-up", side="Up"):
    return OrderIntent(
        condition_id=condition_id, token_id=token_id, asset=asset, side=side,
        regime=regime, position_tier=position_tier, price=price,
        size_shares=size_shares, notional_usd=price * size_shares,
        is_floor_lot=is_floor_lot,
    )


class TestQueueSafetyFactorDiscount:
    """Explicit regression test for the real prior bug: an un-discounted
    (factor=1.0) queue-ahead value silently produces near-zero fill rates.
    """

    def test_discounted_queue_is_less_than_raw_when_factor_below_one(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=100.0)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=10.0), book, now=0.0)
        assert order.queue_ahead_raw == pytest.approx(100.0)
        assert order.queue_ahead_discounted == pytest.approx(25.0)
        assert order.queue_ahead_discounted < order.queue_ahead_raw

    def test_default_factor_is_not_one(self):
        from paperbot import config
        assert config.QUEUE_SAFETY_FACTOR != 1.0
        assert 0.0 < config.QUEUE_SAFETY_FACTOR < 1.0

    def test_factor_of_one_reproduces_the_prior_bug_shape(self):
        """Demonstrates why 1.0 is the wrong default: with no discount, a
        single trade print equal to the visible depth doesn't even reach
        the order -- fills stay at zero indefinitely."""
        book = make_book(best_bid=0.20, bid_depth_at_best=100.0)
        sim = FillSimulator(queue_safety_factor=1.0)
        order = sim.place_order(make_intent(price=0.20, size_shares=10.0), book, now=0.0)
        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=100.0, side="SELL", ts=1.0))
        assert order.filled_size == 0.0
        assert order.status == OrderStatus.PENDING


class TestQueueConsumption:
    def test_multiple_trade_prints_drain_queue_before_filling(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=40.0)  # raw queue 40
        sim = FillSimulator(queue_safety_factor=0.25)             # discounted queue 10
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0), book, now=0.0)
        assert order.queue_ahead_discounted == pytest.approx(10.0)

        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=4.0, side="SELL", ts=1.0))
        assert order.status == OrderStatus.PENDING
        assert order.queue_ahead_discounted == pytest.approx(6.0)
        assert order.filled_size == 0.0

        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=4.0, side="SELL", ts=2.0))
        assert order.queue_ahead_discounted == pytest.approx(2.0)
        assert order.filled_size == 0.0

        # This print exhausts the remaining 2 of queue, then fills 2 shares.
        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=4.0, side="SELL", ts=3.0))
        assert order.queue_ahead_discounted == pytest.approx(0.0)
        assert order.filled_size == pytest.approx(2.0)
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.first_fill_at == 3.0

    def test_trade_print_above_order_price_does_not_consume_queue(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=10.0)
        sim = FillSimulator(queue_safety_factor=0.5)  # discounted queue = 5
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0), book, now=0.0)
        sim.on_trade_print("tok-up", TradePrint(price=0.25, size=100.0, side="SELL", ts=1.0))
        assert order.queue_ahead_discounted == pytest.approx(5.0)
        assert order.filled_size == 0.0

    def test_fill_that_exactly_exhausts_remaining_size(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=0.0)  # no queue ahead
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=6.0), book, now=0.0)
        assert order.queue_ahead_discounted == pytest.approx(0.0)

        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=6.0, side="SELL", ts=5.0))
        assert order.status == OrderStatus.FILLED
        assert order.remaining_size == pytest.approx(0.0)
        assert order.filled_size == pytest.approx(6.0)
        assert order.time_to_fill == pytest.approx(5.0)

    def test_overfill_trade_print_leaves_remainder_for_other_orders_only(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=0.0)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=3.0), book, now=0.0)
        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=10.0, side="SELL", ts=1.0))
        assert order.status == OrderStatus.FILLED
        assert order.filled_size == pytest.approx(3.0)
        # the order itself never over-fills past its own original size
        assert order.remaining_size == 0.0


class TestExpiry:
    def test_order_expires_unfilled_at_timing_cutoff(self):
        from paperbot import config
        book = make_book(best_bid=0.20, bid_depth_at_best=1000.0)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0), book, now=0.0)

        seconds_remaining = {"cond-1": config.MIN_SECONDS_BEFORE_CLOSE - 1}
        changed = sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=100.0)

        assert order.status == OrderStatus.EXPIRED_UNFILLED
        assert order in changed
        assert not order.is_open()

    def test_partially_filled_order_keeps_its_fill_on_expiry(self):
        from paperbot import config
        book = make_book(best_bid=0.20, bid_depth_at_best=0.0)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0), book, now=0.0)
        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=2.0, side="SELL", ts=1.0))
        assert order.filled_size == pytest.approx(2.0)

        seconds_remaining = {"cond-1": config.MIN_SECONDS_BEFORE_CLOSE - 1}
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=100.0)

        # Filled portion is preserved (settles later); it is NOT relabeled
        # EXPIRED_UNFILLED just because the remainder got cancelled.
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.expired_remainder is True
        assert order.filled_size == pytest.approx(2.0)
        # remaining_size keeps reporting the true unfilled amount (3.0) --
        # expiry cancels it (is_open() below reflects that) but does not
        # repurpose the field to mean "cancelled".
        assert order.remaining_size == pytest.approx(3.0)
        assert not order.is_open()

    def test_order_stays_pending_with_plenty_of_time_left(self):
        from paperbot import config
        book = make_book(best_bid=0.20, bid_depth_at_best=1000.0)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0), book, now=0.0)

        seconds_remaining = {"cond-1": config.MIN_SECONDS_BEFORE_CLOSE + 60}
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=1.0)
        assert order.status == OrderStatus.PENDING
        assert order.is_open()


class TestReprice:
    def test_drift_beyond_threshold_within_same_band_reprices(self):
        from paperbot import config
        book = make_book(best_bid=0.20, bid_depth_at_best=50.0, tick_size=0.01)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0), book, now=0.0)

        # Move price up within CHEAP band, beyond DRIFT_REPRICE_TICKS*tick_size.
        book.apply_snapshot(bids=[(0.25, 20.0)], asks=[(0.27, 20.0)])
        seconds_remaining = {"cond-1": 1000}
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=10.0)

        assert order.price == pytest.approx(0.25)
        assert order.reprice_count == 1
        assert order.status == OrderStatus.PENDING

    def test_drift_out_of_target_band_cancels_instead_of_repricing(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=50.0, tick_size=0.01)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0, regime="CHEAP"), book, now=0.0)

        # Price jumps into MID band entirely.
        book.apply_snapshot(bids=[(0.45, 20.0)], asks=[(0.47, 20.0)])
        seconds_remaining = {"cond-1": 1000}
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=10.0)

        assert order.status == OrderStatus.CANCELLED
        assert not order.is_open()


class TestPostOnlyNeverCrossesSpread:
    def test_would_cross_spread_detects_marketable_price(self):
        from paperbot.strategy import would_cross_spread
        book = make_book(best_bid=0.20, best_ask=0.22)
        assert would_cross_spread(book, 0.22) is True
        assert would_cross_spread(book, 0.23) is True
        assert would_cross_spread(book, 0.21) is False

    def test_safe_postonly_price_never_crosses(self):
        from paperbot.strategy import safe_postonly_price, would_cross_spread
        book = make_book(best_bid=0.20, best_ask=0.205, tick_size=0.001)
        # intended price is above best_ask -- would be marketable
        price = safe_postonly_price(book, intended_price=0.21)
        assert price is not None
        assert would_cross_spread(book, price) is False

    def test_safe_postonly_price_returns_none_when_no_room_below_ask(self):
        from paperbot.strategy import safe_postonly_price
        # best_ask is one tick above zero -- there is no valid price that
        # both retreats a full tick below the ask AND stays positive.
        book = make_book(best_bid=0.005, best_ask=0.005, tick_size=0.01)
        price = safe_postonly_price(book, intended_price=0.005)
        assert price is None


class TestStatsByAssetRegime:
    def test_finished_orders_are_bucketed_by_asset_and_regime(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=0.0)
        sim = FillSimulator(queue_safety_factor=0.25)
        o1 = sim.place_order(make_intent(price=0.20, size_shares=1.0, asset="Bitcoin",
                                          regime="CHEAP"), book, now=0.0)
        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=1.0, side="SELL", ts=1.0))
        assert o1.status == OrderStatus.FILLED

        stats = sim.stats_by_asset_regime()
        assert ("Bitcoin", "CHEAP") in stats
        assert stats[("Bitcoin", "CHEAP")]["fill_rate"] == 1.0
