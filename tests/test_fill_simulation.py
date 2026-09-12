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
                 token_id="tok-up", side="Up", is_hedge=False, is_scout=False):
    return OrderIntent(
        condition_id=condition_id, token_id=token_id, asset=asset, side=side,
        regime=regime, position_tier=position_tier, price=price,
        size_shares=size_shares, notional_usd=price * size_shares,
        is_floor_lot=is_floor_lot, is_hedge=is_hedge, is_scout=is_scout,
    )


class TestRemoveOrdersForCondition:
    """Direct regression tests for a bounded-memory fix: self.orders grew
    unbounded for the lifetime of the process, fine for a Railway deploy
    restarted on every push, a real problem for an AWS deployment meant to
    stay up for weeks."""

    def test_removes_only_orders_for_the_given_condition(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=0.0)
        sim = FillSimulator(queue_safety_factor=0.25)
        o1 = sim.place_order(make_intent(condition_id="c1"), book, now=0.0)
        o2 = sim.place_order(make_intent(condition_id="c1"), book, now=0.0)
        o3 = sim.place_order(make_intent(condition_id="c2"), book, now=0.0)

        removed = sim.remove_orders_for_condition("c1")

        assert removed == 2
        assert o1.order_id not in sim.orders
        assert o2.order_id not in sim.orders
        assert o3.order_id in sim.orders

    def test_removing_a_condition_with_no_orders_is_a_safe_no_op(self):
        sim = FillSimulator(queue_safety_factor=0.25)
        assert sim.remove_orders_for_condition("nonexistent") == 0


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


class TestPerRegimeQueueSafetyFactorOverride:
    """queue_safety_factor_by_regime, added 2026-09-11 -- see
    config.QUEUE_SAFETY_FACTOR_OVERRIDE_BY_REGIME's docstring for why:
    HIGH regime expires unfilled at ~9x CORE's rate (49.4% vs 5.5%,
    measured live), with the two alternative fixes (enter earlier, quote
    more aggressively) both ruled out by real data. Backward compatibility
    (no override passed -- every pre-existing call site) is the most
    important property here: it must reproduce the flat-factor behavior
    exactly."""

    def test_no_override_behaves_identically_to_the_flat_factor(self):
        # The default (queue_safety_factor_by_regime=None) must be a
        # complete no-op -- every regime uses the single base factor,
        # exactly like before this feature existed.
        book = make_book(best_bid=0.20, bid_depth_at_best=100.0)
        sim = FillSimulator(queue_safety_factor=0.25)
        for regime in ("CHEAP", "MID", "CORE", "HIGH"):
            order = sim.place_order(make_intent(price=0.20, regime=regime), book, now=0.0)
            assert order.queue_ahead_discounted == pytest.approx(25.0)

    def test_override_applies_only_to_the_specified_regimes(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=100.0)
        sim = FillSimulator(queue_safety_factor=0.25,
                             queue_safety_factor_by_regime={"CHEAP": 0.10, "HIGH": 0.10})
        cheap_order = sim.place_order(make_intent(price=0.20, regime="CHEAP"), book, now=0.0)
        high_order = sim.place_order(make_intent(price=0.20, regime="HIGH"), book, now=0.0)
        mid_order = sim.place_order(make_intent(price=0.20, regime="MID"), book, now=0.0)
        core_order = sim.place_order(make_intent(price=0.20, regime="CORE"), book, now=0.0)

        assert cheap_order.queue_ahead_discounted == pytest.approx(10.0)  # 100 * 0.10
        assert high_order.queue_ahead_discounted == pytest.approx(10.0)
        assert mid_order.queue_ahead_discounted == pytest.approx(25.0)   # unaffected, base factor
        assert core_order.queue_ahead_discounted == pytest.approx(25.0)  # unaffected, base factor

    def test_override_lowers_effective_queue_ahead_raising_fill_likelihood(self):
        # The whole point: a smaller discounted queue means less trade
        # volume is needed before the order starts filling.
        book = make_book(best_bid=0.20, bid_depth_at_best=100.0)
        sim_flat = FillSimulator(queue_safety_factor=0.25)
        sim_override = FillSimulator(queue_safety_factor=0.25,
                                      queue_safety_factor_by_regime={"HIGH": 0.10})
        order_flat = sim_flat.place_order(make_intent(price=0.20, regime="HIGH"), book, now=0.0)
        order_override = sim_override.place_order(make_intent(price=0.20, regime="HIGH"), book, now=0.0)
        assert order_override.queue_ahead_discounted < order_flat.queue_ahead_discounted

    def test_reprice_uses_the_same_per_regime_factor(self):
        book = make_book(best_bid=0.90, bid_depth_at_best=100.0, best_ask=0.92, tick_size=0.01)
        sim = FillSimulator(queue_safety_factor=0.25,
                             queue_safety_factor_by_regime={"HIGH": 0.10})
        order = sim.place_order(make_intent(price=0.90, regime="HIGH"), book, now=0.0)
        assert order.queue_ahead_discounted == pytest.approx(10.0)

        # drift the book (still within HIGH, beyond DRIFT_REPRICE_TICKS*tick_size=0.03)
        # and force a reprice
        book.apply_snapshot(bids=[(0.95, 200.0)], asks=[(0.97, 100.0)])
        seconds_remaining = {"cond-1": 1000}
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=1.0)

        assert order.price == pytest.approx(0.95)
        assert order.queue_ahead_discounted == pytest.approx(20.0)  # 200 * 0.10, not 200 * 0.25

    def test_empty_override_dict_is_also_a_full_noop(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=100.0)
        sim = FillSimulator(queue_safety_factor=0.25, queue_safety_factor_by_regime={})
        order = sim.place_order(make_intent(price=0.20, regime="HIGH"), book, now=0.0)
        assert order.queue_ahead_discounted == pytest.approx(25.0)


class TestTradePrintSideFiltering:
    """Code-review fix (2026-09-10): on_trade_print used to have no
    trade.side check at all -- only trade.price <= o.price. This bot only
    ever places resting BUY orders, so only a SELL-side print (a taker
    selling, crossing into the bid side) can legitimately fill/consume
    queue for one; a BUY-side print (crossing into the ask side) must
    never touch it, however low its price happens to be."""

    def test_a_buy_side_print_does_not_consume_queue_or_fill(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=40.0)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0), book, now=0.0)
        assert order.queue_ahead_discounted == pytest.approx(10.0)

        # Same price, same size as a SELL print that WOULD have filled this
        # order (see TestQueueConsumption below) -- side is the only thing
        # that differs.
        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=100.0, side="BUY", ts=1.0))

        assert order.queue_ahead_discounted == pytest.approx(10.0), (
            "a BUY-side print must not drain queue meant only for SELL-side "
            "sweeps into the bid book"
        )
        assert order.filled_size == 0.0
        assert order.status == OrderStatus.PENDING

    def test_a_sell_side_print_at_the_identical_price_still_fills_normally(self):
        """Sanity check that the fix didn't break the ordinary path --
        same setup as the BUY test above, SELL side instead."""
        book = make_book(best_bid=0.20, bid_depth_at_best=40.0)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0), book, now=0.0)

        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=100.0, side="SELL", ts=1.0))

        assert order.filled_size == pytest.approx(5.0)
        assert order.status == OrderStatus.FILLED


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


class TestTradesBeforeOrderPlacementAreIgnored:
    """Direct regression test for a live-confirmed bug: pending_trades is
    drained once per main-loop tick, so a trade print timestamped BEFORE
    an order was placed can still be sitting undrained when that order
    gets placed later in the same tick. It must not be able to fill (or
    drain queue for) an order it predates -- that trade is already
    reflected in the book snapshot the order's queue_ahead came from.
    Confirmed live as a negative time_to_fill (order=9, time_to_fill=-0.1s)
    before this filter existed."""

    def test_a_trade_print_older_than_the_order_does_not_fill_it(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=0.0)  # no queue ahead
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0), book, now=10.0)

        # This trade printed BEFORE the order existed (ts=9.0 < placed_at=10.0).
        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=5.0, side="SELL", ts=9.0))

        assert order.status == OrderStatus.PENDING
        assert order.filled_size == 0.0
        assert order.first_fill_at is None

    def test_a_trade_print_older_than_the_order_does_not_drain_its_queue_either(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=20.0)  # raw queue 20
        sim = FillSimulator(queue_safety_factor=0.5)              # discounted queue 10
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0), book, now=10.0)
        assert order.queue_ahead_discounted == pytest.approx(10.0)

        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=8.0, side="SELL", ts=9.0))

        assert order.queue_ahead_discounted == pytest.approx(10.0)  # untouched

    def test_a_trade_print_at_or_after_placement_still_fills_normally(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=0.0)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0), book, now=10.0)

        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=5.0, side="SELL", ts=10.0))

        assert order.status == OrderStatus.FILLED
        assert order.time_to_fill == pytest.approx(0.0)
        assert order.time_to_fill >= 0


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

    def test_drift_out_of_band_after_a_partial_fill_preserves_settlement_eligibility(self):
        """Direct regression test for a confirmed live bug (2026-09-08,
        found via the RESOLUTION_GAP diagnostic in bot.py): overwriting
        status to CANCELLED unconditionally -- even for an order with real
        fills already recorded -- silently dropped those fills from ever
        settling, since CANCELLED isn't in settle_order()'s allowed-status
        set (only FILLED/PARTIALLY_FILLED are). The already-filled portion
        must stay just as settleable as one that survives the 90-second
        timing cutoff (see the EXPIRED/expired_remainder case just above)."""
        book = make_book(best_bid=0.20, bid_depth_at_best=4.0, tick_size=0.01)
        sim = FillSimulator(queue_safety_factor=0.25)  # queue_ahead_discounted = 1.0
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0, regime="CHEAP"), book, now=0.0)

        # Partially fill it first: 1.0 drains the queue, 2.0 fills the order.
        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=3.0, side="SELL", ts=1.0))
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.filled_size == pytest.approx(2.0)

        # Then price jumps into MID band entirely.
        book.apply_snapshot(bids=[(0.45, 20.0)], asks=[(0.47, 20.0)])
        seconds_remaining = {"cond-1": 1000}
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=10.0)

        assert not order.is_open(), "must stop being open (no more fills against a stale price)"
        assert order.status == OrderStatus.PARTIALLY_FILLED, (
            "status must NOT be overwritten to CANCELLED -- that would make "
            "settle_order() silently refuse to settle this order's real 2.0-share fill"
        )
        assert order.filled_size == pytest.approx(2.0), "the real fill must survive intact"


class TestCheapRepriceCap:
    """Confirmed live (2026-09-12, cheap-fill-calibration-gap): CHEAP
    orders that chase the book 3+ times land in a confirmed, badly-
    negative-edge bucket (z=-4.64) -- orders that fill on the first quote
    are perfectly fair. MAX_CHEAP_REPRICES (default 2) cancels a CHEAP
    order instead of repricing it again once it's already used up its
    cap, rather than let it walk further into that bucket."""

    def test_cheap_order_cancels_once_reprice_cap_is_reached(self):
        book = make_book(best_bid=0.20, bid_depth_at_best=50.0, tick_size=0.01)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0, regime="CHEAP"), book, now=0.0)
        seconds_remaining = {"cond-1": 1000}

        # First two drifts: still within the cap (default MAX_CHEAP_REPRICES=2),
        # reprice normally.
        book.apply_snapshot(bids=[(0.24, 20.0)], asks=[(0.26, 20.0)])
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=10.0)
        assert order.reprice_count == 1
        assert order.status == OrderStatus.PENDING

        book.apply_snapshot(bids=[(0.20, 20.0)], asks=[(0.22, 20.0)])
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=20.0)
        assert order.reprice_count == 2
        assert order.status == OrderStatus.PENDING

        # Third drift: cap reached -- cancel instead of repricing again.
        book.apply_snapshot(bids=[(0.24, 20.0)], asks=[(0.26, 20.0)])
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=30.0)
        assert order.status == OrderStatus.CANCELLED
        assert not order.is_open()
        assert order.reprice_count == 2, "cap-cancel must not itself count as another reprice"

    def test_non_cheap_regime_is_never_capped(self):
        book = make_book(best_bid=0.60, bid_depth_at_best=50.0, tick_size=0.01)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.60, size_shares=5.0, regime="MID"), book, now=0.0)
        seconds_remaining = {"cond-1": 1000}

        # Drift three times, well past the CHEAP cap -- MID must keep
        # repricing indefinitely, never cancel for this reason.
        prices = [0.64, 0.60, 0.64, 0.60]
        for i, p in enumerate(prices):
            book.apply_snapshot(bids=[(p, 20.0)], asks=[(p + 0.02, 20.0)])
            sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=float(10 * (i + 1)))

        assert order.reprice_count == 4
        assert order.status == OrderStatus.PENDING

    def test_flag_disabled_reverts_to_uncapped_chasing(self, monkeypatch):
        from paperbot import config
        monkeypatch.setattr(config, "ENABLE_CHEAP_REPRICE_CAP", False)

        book = make_book(best_bid=0.20, bid_depth_at_best=50.0, tick_size=0.01)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0, regime="CHEAP"), book, now=0.0)
        seconds_remaining = {"cond-1": 1000}

        prices = [0.24, 0.20, 0.24, 0.20]
        for i, p in enumerate(prices):
            book.apply_snapshot(bids=[(p, 20.0)], asks=[(p + 0.02, 20.0)])
            sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=float(10 * (i + 1)))

        assert order.reprice_count == 4, "with the flag off, CHEAP must keep chasing past the default cap"
        assert order.status == OrderStatus.PENDING

    def test_cap_cancel_preserves_an_existing_partial_fill(self):
        """Same discipline as the drift-out-of-band cancel path: a real
        partial fill must survive as PARTIALLY_FILLED (settle-eligible),
        never silently overwritten to CANCELLED."""
        book = make_book(best_bid=0.20, bid_depth_at_best=4.0, tick_size=0.01)
        sim = FillSimulator(queue_safety_factor=0.25)  # queue_ahead_discounted = 1.0
        order = sim.place_order(make_intent(price=0.20, size_shares=5.0, regime="CHEAP"), book, now=0.0)
        seconds_remaining = {"cond-1": 1000}

        sim.on_trade_print("tok-up", TradePrint(price=0.20, size=3.0, side="SELL", ts=1.0))
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.filled_size == pytest.approx(2.0)

        # Reach the reprice cap.
        book.apply_snapshot(bids=[(0.24, 20.0)], asks=[(0.26, 20.0)])
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=10.0)
        book.apply_snapshot(bids=[(0.20, 20.0)], asks=[(0.22, 20.0)])
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=20.0)
        assert order.reprice_count == 2

        # Third drift triggers the cap-cancel.
        book.apply_snapshot(bids=[(0.24, 20.0)], asks=[(0.26, 20.0)])
        sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=30.0)

        assert not order.is_open()
        assert order.status == OrderStatus.PARTIALLY_FILLED, (
            "the real 2.0-share fill must remain settle-eligible, not overwritten to CANCELLED"
        )
        assert order.filled_size == pytest.approx(2.0)
        assert order.cancelled_remainder is True

    def test_hedge_is_never_cancelled_by_the_cheap_reprice_cap(self):
        """FIXED 2026-09-12 (bug audit #3): the real-data finding behind
        this cap was measured explicitly excluding hedges and scouts (not
        directional edge bets) -- a CHEAP-band hedge must keep chasing
        indefinitely, same as a non-CHEAP order, never cancel for this
        reason. Exactly the scenario ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER
        deliberately sizes UP (near the CHEAP extreme)."""
        book = make_book(best_bid=0.20, bid_depth_at_best=50.0, tick_size=0.01)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(
            make_intent(price=0.20, size_shares=5.0, regime="CHEAP", is_hedge=True),
            book, now=0.0,
        )
        seconds_remaining = {"cond-1": 1000}

        prices = [0.24, 0.20, 0.24, 0.20]
        for i, p in enumerate(prices):
            book.apply_snapshot(bids=[(p, 20.0)], asks=[(p + 0.02, 20.0)])
            sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=float(10 * (i + 1)))

        assert order.reprice_count == 4, "a CHEAP hedge must keep chasing past the cap, never cancel for this reason"
        assert order.status == OrderStatus.PENDING

    def test_scout_is_never_cancelled_by_the_cheap_reprice_cap(self):
        """Same fix as the hedge case above -- scouts were also explicitly
        excluded from the real-data finding this cap was calibrated on."""
        book = make_book(best_bid=0.20, bid_depth_at_best=50.0, tick_size=0.01)
        sim = FillSimulator(queue_safety_factor=0.25)
        order = sim.place_order(
            make_intent(price=0.20, size_shares=5.0, regime="CHEAP", is_scout=True),
            book, now=0.0,
        )
        seconds_remaining = {"cond-1": 1000}

        prices = [0.24, 0.20, 0.24, 0.20]
        for i, p in enumerate(prices):
            book.apply_snapshot(bids=[(p, 20.0)], asks=[(p + 0.02, 20.0)])
            sim.manage_open_orders({"tok-up": book}, seconds_remaining, now=float(10 * (i + 1)))

        assert order.reprice_count == 4, "a CHEAP scout must keep chasing past the cap, never cancel for this reason"
        assert order.status == OrderStatus.PENDING


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
