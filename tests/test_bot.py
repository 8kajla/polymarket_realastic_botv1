"""
Cross-module wiring tests for PaperBot (no network -- markets/books are
injected directly). These catch bugs that per-module unit tests can't see
by construction, such as state going stale across the strategy /
fill-simulation / order-tracking boundary.
"""
import asyncio
import os
import signal
import time

import pytest
import requests

import paperbot.bot as botmod
from paperbot import config
from paperbot.bot import PaperBot
from paperbot.book import BookState
from paperbot.fill_simulation import Fill, OrderStatus, SimulatedOrder
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


class TestGracefulShutdownOnSignal:
    """Railway (and most container platforms) send SIGTERM on redeploy/
    stop, not SIGINT -- run_forever must actually stop (not hang until
    the platform SIGKILLs it) and must not blow up trying to save state."""

    def test_sigterm_stops_run_forever_promptly(self):
        bot = PaperBot(assets=["Bitcoin"], seed=5)
        bot.discovery._last_poll = time.time()  # skip a real network poll attempt
        bot.ledger.save = lambda *a, **kw: None  # avoid writing to disk in a test

        async def scenario():
            task = asyncio.create_task(bot.run_forever(tick_seconds=0.05))
            await asyncio.sleep(0.1)
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(scenario())  # raises asyncio.TimeoutError if it hangs


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


def make_pending_market(n, asset="Bitcoin"):
    cid = f"cond-{n}"
    return cid, make_bitcoin_market(condition_id=cid)


def make_filled_order(condition_id, order_id, asset="Bitcoin", side="Up", price=0.3, size=5.0):
    order = SimulatedOrder(
        order_id=order_id, condition_id=condition_id, token_id=f"{condition_id}-up",
        asset=asset, regime="MID", position_tier="first", side=side, price=price,
        original_size=size, is_floor_lot=False, placed_at=0.0, remaining_size=0.0,
    )
    order.fills.append(Fill(size=size, price=price, ts=0.0))
    order.status = OrderStatus.FILLED
    return order


class TestResolutionThrottling:
    """Direct regression tests for a live-confirmed bug: an unbounded
    resolution backlog got retried on every 2-second tick with no backoff,
    which self-inflicted a 429 storm against Gamma and flooded Railway's
    own log ingestion (dropped messages) with one traceback per market per
    tick. All of these run with the real config constants -- if someone
    loosens RESOLUTION_MAX_ATTEMPTS_PER_TICK back to "attempt everything",
    the first test here catches it."""

    def test_at_most_max_attempts_per_tick_are_made(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        n_markets = config.RESOLUTION_MAX_ATTEMPTS_PER_TICK + 5
        for i in range(n_markets):
            cid, market = make_pending_market(i)
            bot.pending_resolution[cid] = market

        call_count = {"n": 0}

        def fake_fetch(slug, session=None, timeout=10.0):
            call_count["n"] += 1
            return None  # "still open" -- no resolution yet

        monkeypatch.setattr(botmod, "fetch_market_by_slug", fake_fetch)

        asyncio.run(bot.resolution_tick(now=1000.0))

        assert call_count["n"] == config.RESOLUTION_MAX_ATTEMPTS_PER_TICK
        assert len(bot.pending_resolution) == n_markets  # none resolved, none dropped

    def test_a_market_is_not_retried_before_its_cooldown_elapses(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=2)
        cid, market = make_pending_market(0)
        bot.pending_resolution[cid] = market

        call_count = {"n": 0}
        monkeypatch.setattr(botmod, "fetch_market_by_slug",
                             lambda slug, session=None, timeout=10.0: call_count.update(n=call_count["n"] + 1) or None)

        asyncio.run(bot.resolution_tick(now=1000.0))
        assert call_count["n"] == 1

        # Immediately again, well within the cooldown window.
        asyncio.run(bot.resolution_tick(now=1000.5))
        assert call_count["n"] == 1, "must not retry before RESOLUTION_RETRY_COOLDOWN_SECONDS elapses"

        # After the cooldown, it should retry.
        asyncio.run(bot.resolution_tick(now=1000.0 + config.RESOLUTION_RETRY_COOLDOWN_SECONDS + 1))
        assert call_count["n"] == 2

    def test_repeated_failures_back_off_further_each_time(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=3)
        cid, market = make_pending_market(0)
        bot.pending_resolution[cid] = market

        def always_fails(slug, session=None, timeout=10.0):
            resp = requests.Response()
            resp.status_code = 429
            raise requests.HTTPError("429", response=resp)

        monkeypatch.setattr(botmod, "fetch_market_by_slug", always_fails)

        asyncio.run(bot.resolution_tick(now=0.0))
        assert bot._resolution_failure_count[cid] == 1

        # Cooldown after 1 failure should exceed the base cooldown.
        asyncio.run(bot.resolution_tick(now=config.RESOLUTION_RETRY_COOLDOWN_SECONDS + 1))
        assert bot._resolution_failure_count[cid] == 1, "should still be backing off, not retried yet"

        asyncio.run(bot.resolution_tick(now=config.RESOLUTION_BACKOFF_ON_FAILURE_SECONDS + 1))
        assert bot._resolution_failure_count[cid] == 2

    def test_gives_up_after_max_age_without_ever_calling_fetch(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=4)
        cid, market = make_pending_market(0)
        bot.pending_resolution[cid] = market
        bot._resolution_first_seen[cid] = 0.0  # first seen at t=0

        call_count = {"n": 0}
        monkeypatch.setattr(botmod, "fetch_market_by_slug",
                             lambda slug, session=None, timeout=10.0: call_count.update(n=call_count["n"] + 1) or None)

        asyncio.run(bot.resolution_tick(now=config.RESOLUTION_MAX_AGE_SECONDS + 1))

        assert cid not in bot.pending_resolution
        assert call_count["n"] == 0, "should give up before even attempting a fetch past max age"

    def test_successful_resolution_settles_filled_orders_and_logs_pnl(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=5)
        cid, market = make_pending_market(0)
        bot.pending_resolution[cid] = market
        order = make_filled_order(cid, order_id=1, side="Up", price=0.3, size=5.0)
        bot.fill_sim.orders[order.order_id] = order

        def fake_fetch(slug, session=None, timeout=10.0):
            return {"closed": True, "outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]'}

        monkeypatch.setattr(botmod, "fetch_market_by_slug", fake_fetch)

        asyncio.run(bot.resolution_tick(now=1000.0))

        assert cid not in bot.pending_resolution
        assert bot.ledger.realized_pnl() == pytest.approx(5.0 * 1.0 - 5.0 * 0.3)


class TestPnlSummaryLogging:
    def test_log_pnl_summary_does_not_raise_and_reports_totals(self, caplog):
        bot = PaperBot(assets=["Bitcoin"], seed=6)
        order = make_filled_order("cond-x", order_id=1, price=0.4, size=2.0)
        bot.fill_sim.orders[order.order_id] = order
        bot.ledger.settle_order(order, winning_side="Up")

        with caplog.at_level("INFO"):
            bot.log_pnl_summary(now=1000.0)

        assert any("PNL_SUMMARY" in r.message for r in caplog.records)
        assert any("realized_total=" in r.message for r in caplog.records)
