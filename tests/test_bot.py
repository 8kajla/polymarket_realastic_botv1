"""
Cross-module wiring tests for PaperBot (no network -- markets/books are
injected directly). These catch bugs that per-module unit tests can't see
by construction, such as state going stale across the strategy /
fill-simulation / order-tracking boundary.
"""
import asyncio

from paperbot.bot import PaperBot
from paperbot.book import BookState
from paperbot.market_discovery import Market
from paperbot.strategy import MarketActivityState


def make_bitcoin_market(condition_id="cond-1", end_time=2000.0):
    # token ids are derived from condition_id so two markets in the same
    # test never collide on the same key in bot.book_states.
    return Market(
        condition_id=condition_id, slug=f"btc-updown-5m-{condition_id}", question="q",
        asset="Bitcoin", end_time=end_time,
        token_id_up=f"{condition_id}-up", token_id_down=f"{condition_id}-down",
        order_min_size=None, order_min_tick_size=0.01,
    )


def wire_market(bot, market, bid=0.20, ask=0.21, depth=200.0):
    bot.markets_by_condition[market.condition_id] = market
    bot.activity[market.condition_id] = MarketActivityState()
    up = BookState(token_id=market.token_id_up)
    up.apply_snapshot(bids=[(bid, depth)], asks=[(ask, depth)])
    bot.book_states[market.token_id_up] = up
    return up


class TestOneOrderPerMarketPolicy:
    def test_second_strategy_tick_does_not_double_place(self):
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        market = make_bitcoin_market()
        wire_market(bot, market)

        asyncio.run(bot.strategy_tick(now=1000.0))
        assert market.condition_id in bot.resting_order_id
        first_order_id = bot.resting_order_id[market.condition_id]

        asyncio.run(bot.strategy_tick(now=1001.0))
        assert bot.resting_order_id[market.condition_id] == first_order_id


class TestRestingOrderIdClearsOnFillViaTradePrint:
    """Regression test: resting_order_id must clear when an order fills via
    a trade print consumed during manage_orders_tick's drain loop, not only
    when fill_simulation.manage_open_orders reports a change (expiry/
    reprice/cancel). An earlier version of this wiring only swept the
    latter and left filled markets permanently blocked from new entries."""

    def test_fill_via_trade_print_frees_the_market_for_a_new_order(self):
        bot = PaperBot(assets=["Bitcoin"], seed=2)
        market = make_bitcoin_market()
        book = wire_market(bot, market, bid=0.20, ask=0.21, depth=50.0)

        asyncio.run(bot.strategy_tick(now=1000.0))
        order_id = bot.resting_order_id[market.condition_id]
        order = bot.fill_sim.orders[order_id]

        needed = order.queue_ahead_discounted + order.original_size
        book.apply_last_trade_price(price=order.price, size=needed, side="SELL", ts=1001.0)
        bot.manage_orders_tick(now=1001.0)

        assert order.status.value == "FILLED"
        assert market.condition_id not in bot.resting_order_id, (
            "a filled order must not keep blocking new entries in its market"
        )

        # And a new entry can now be placed in the same market.
        asyncio.run(bot.strategy_tick(now=1002.0))
        assert market.condition_id in bot.resting_order_id
        assert bot.resting_order_id[market.condition_id] != order_id


class TestTimingGateBlocksLateMarkets:
    def test_market_with_under_90s_left_gets_no_order(self):
        bot = PaperBot(assets=["Bitcoin"], seed=3)
        market = make_bitcoin_market(end_time=1030.0)  # 30s left at now=1000
        wire_market(bot, market)

        asyncio.run(bot.strategy_tick(now=1000.0))
        assert market.condition_id not in bot.resting_order_id


class TestHaltingIsolatesOnlyTheFailingMarket:
    def test_exception_in_one_market_does_not_block_others(self):
        bot = PaperBot(assets=["Bitcoin"], seed=4)
        good_market = make_bitcoin_market(condition_id="cond-good")
        bad_market = make_bitcoin_market(condition_id="cond-bad")
        wire_market(bot, good_market)
        # Deliberately leave bad_market's book state missing/broken so
        # evaluating it raises inside _evaluate_one_market.
        bot.markets_by_condition[bad_market.condition_id] = bad_market
        bot.activity[bad_market.condition_id] = MarketActivityState()

        class ExplodingBook:
            @property
            def best_bid(self):
                raise RuntimeError("simulated book corruption")

        bot.book_states[bad_market.token_id_up] = ExplodingBook()

        asyncio.run(bot.strategy_tick(now=1000.0))

        assert bad_market.condition_id in bot.halted_conditions
        assert good_market.condition_id in bot.resting_order_id
        assert good_market.condition_id not in bot.halted_conditions
