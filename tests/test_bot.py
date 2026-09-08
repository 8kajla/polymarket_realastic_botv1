"""
Cross-module wiring tests for PaperBot (no network -- markets/books are
injected directly). These catch bugs that per-module unit tests can't see
by construction, such as state going stale across the strategy /
fill-simulation / order-tracking boundary.
"""
import asyncio
import json
import os
import signal
import time

import pytest
import requests

import paperbot.bot as botmod
from paperbot import config
from paperbot import strategy as stratmod
from paperbot.bot import PaperBot
from paperbot.book import BookState
from paperbot.fill_simulation import Fill, OrderStatus, SimulatedOrder
from paperbot.ledger import Ledger
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


def make_market_for_asset(asset, condition_id="cond-1", end_time=2000.0):
    return Market(
        condition_id=condition_id, slug=f"x-updown-5m-{condition_id}", question="q",
        asset=asset, end_time=end_time,
        token_id_up=f"{condition_id}-up", token_id_down=f"{condition_id}-down",
        order_min_size=None, order_min_tick_size=0.01,
    )


def wire_market(bot, market, bid=0.20, ask=0.21, depth=200.0):
    bot.markets_by_condition[market.condition_id] = market
    bot.activity[market.condition_id] = MarketActivityState()
    up = BookState(token_id=market.token_id_up)
    up.apply_snapshot(bids=[(bid, depth)], asks=[(ask, depth)])
    bot.book_states[market.token_id_up] = up

    # _evaluate_one_market requires both books since the fix for the
    # wrong-book regime/sizing bug -- mirror Up's price for Down (roughly
    # 1 - price, not exact, real markets aren't perfectly complementary
    # either) so tests that don't care about Down specifically still see
    # a normal two-sided liquid book.
    down = BookState(token_id=market.token_id_down)
    down.apply_snapshot(bids=[(round(1 - ask, 6), depth)], asks=[(round(1 - bid, 6), depth)])
    bot.book_states[market.token_id_down] = down
    return up


class TestSessionConnectionPoolSizing:
    """Direct regression test for a bug introduced by the onboarding
    parallelization fix itself: firing ~12 concurrent bootstrap tasks (each
    making 2 sequential requests) against one requests.Session exceeded
    the default HTTPAdapter's 10-connections-per-host pool, confirmed live
    as a burst of "Connection pool is full, discarding connection" urllib3
    warnings right after that fix shipped."""

    def test_session_pool_has_headroom_for_a_full_rollover_batch(self):
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        adapter = bot.session.get_adapter("https://clob.polymarket.com/book")
        # A full 6-market rollover batch bootstraps 12 tokens concurrently;
        # the pool must comfortably exceed that, not merely match it.
        assert adapter.poolmanager.connection_pool_kw.get("maxsize", 0) >= 12


class TestHedgeLegEndToEnd:
    """Full-pipeline test: a market's second entry becomes a deliberately-
    sized hedge leg on the opposite side, wired through decide_hedge ->
    build_order_intent -> place_order -> resting_order_ids, matching the
    confirmed historical pattern (dual-sided markets have a dramatically
    better worst-case outcome than single-sided ones)."""

    def test_second_entry_becomes_a_sized_hedge_when_triggered(self, monkeypatch):
        bot = PaperBot(assets=["BNB"], seed=1)
        market = make_market_for_asset("BNB")
        wire_market(bot, market, bid=0.75, ask=0.76, depth=200.0)  # primary lands in CORE

        # First entry: establishes the primary side and its cost.
        asyncio.run(bot.strategy_tick(now=1000.0))
        assert market.condition_id in bot.resting_order_ids
        first_order_id = next(iter(bot.resting_order_ids[market.condition_id]))
        first_order = bot.fill_sim.orders[first_order_id]
        activity = bot.activity[market.condition_id]
        assert activity.entry_count == 1
        assert activity.first_entry_regime == "CORE"

        # Free up the market for a second entry (as if the first got filled).
        first_order.fills.append(Fill(size=first_order.original_size, price=first_order.price, ts=1001.0))
        first_order.status = OrderStatus.FILLED
        del bot.resting_order_ids[market.condition_id]

        # Force the hedge roll to trigger deterministically for this test.
        monkeypatch.setattr(stratmod, "decide_hedge",
                             lambda asset, activity, rng: "Down" if activity.dominant_side() == "Up"
                             else "Up")

        asyncio.run(bot.strategy_tick(now=1001.0))

        assert market.condition_id in bot.resting_order_ids
        hedge_order_id = next(iter(bot.resting_order_ids[market.condition_id]))
        hedge_order = bot.fill_sim.orders[hedge_order_id]

        assert hedge_order.is_hedge is True
        assert hedge_order.is_floor_lot is False
        assert hedge_order.side != first_order.side
        assert activity.hedge_placed is True

        # Sized as a ratio of the dominant side's cost, not the ordinary
        # position-count curve -- confirm it's in a sane, small range
        # relative to the primary, not an ordinary full-size entry.
        primary_cost = first_order.original_size * first_order.price
        hedge_cost = hedge_order.original_size * hedge_order.price
        assert 0 < hedge_cost < primary_cost


class TestBatchedOnboarding:
    """Direct regression tests for a live-confirmed issue: onboarding a
    rollover batch (6 markets x 2 tokens = 12 sequential REST round trips,
    awaited one after another) coincided with the WebSocket getting kicked
    as a "slow consumer" a couple of times in production. Onboarding now
    bootstraps every token across the whole batch concurrently and
    subscribes once with the complete set, instead of once per market."""

    def test_multiple_new_markets_bootstrap_concurrently_and_subscribe_once(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        markets = [make_bitcoin_market(condition_id=f"cond-{i}") for i in range(3)]

        call_order = []

        def fake_poll():
            return {m.condition_id: m for m in markets}

        def fake_bootstrap(token_id, session=None):
            call_order.append(token_id)
            book = BookState(token_id=token_id)
            book.apply_snapshot(bids=[(0.2, 10)], asks=[(0.21, 10)])
            return book

        subscribe_calls = []
        orig_subscribe = bot.ws_client.subscribe

        async def spy_subscribe(token_ids):
            subscribe_calls.append(list(token_ids))
            await orig_subscribe(token_ids)

        monkeypatch.setattr(bot.discovery, "poll", fake_poll)
        monkeypatch.setattr(botmod, "bootstrap_book_state", fake_bootstrap)
        monkeypatch.setattr(bot.ws_client, "subscribe", spy_subscribe)
        bot.discovery._last_poll = 0.0  # force due_for_poll() true

        asyncio.run(bot.discovery_tick(now=1000.0))

        # All 6 tokens (3 markets x 2) bootstrapped...
        assert len(call_order) == 6
        assert set(call_order) == {t for m in markets for t in (m.token_id_up, m.token_id_down)}
        # ...and subscribed in exactly ONE call with the complete set, not
        # once per market.
        assert len(subscribe_calls) == 1
        assert set(subscribe_calls[0]) == set(call_order)
        for m in markets:
            assert m.condition_id in bot.activity

    def test_one_markets_bootstrap_failure_does_not_block_the_others(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=2)
        good_market = make_bitcoin_market(condition_id="cond-good")
        bad_market = make_bitcoin_market(condition_id="cond-bad")

        def fake_poll():
            return {good_market.condition_id: good_market, bad_market.condition_id: bad_market}

        def fake_bootstrap(token_id, session=None):
            if "bad" in token_id:
                raise RuntimeError("simulated REST failure")
            book = BookState(token_id=token_id)
            book.apply_snapshot(bids=[(0.2, 10)], asks=[(0.21, 10)])
            return book

        monkeypatch.setattr(bot.discovery, "poll", fake_poll)
        monkeypatch.setattr(botmod, "bootstrap_book_state", fake_bootstrap)
        bot.discovery._last_poll = 0.0

        asyncio.run(bot.discovery_tick(now=1000.0))

        assert good_market.token_id_up in bot.book_states
        assert good_market.token_id_down in bot.book_states
        assert bad_market.token_id_up not in bot.book_states
        assert bad_market.condition_id in bot.halted_conditions
        assert good_market.condition_id not in bot.halted_conditions


class TestOpenOrderCapPerMarketPolicy:
    """config.MAX_OPEN_ORDERS_PER_MARKET bounds how many orders the bot
    holds open in one market at once. Raised from an implicit hard 1
    (2026-09-08): confirmed live that the old cap-of-1 policy -- skip a
    market entirely in strategy_tick once it has ANY resting order -- was
    the dominant driver of the bot averaging 3.0 entries/market against the
    real trader's 18.5 average / 13.5 median in the same live 8h window.
    See config.MAX_OPEN_ORDERS_PER_MARKET's docstring for the full context
    and why the new default (3) is a deliberately conservative first step,
    not an attempt to match the trader's own ceiling directly."""

    def test_places_up_to_the_cap_then_stops(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_OPEN_ORDERS_PER_MARKET", 2)
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        market = make_bitcoin_market()
        wire_market(bot, market)

        asyncio.run(bot.strategy_tick(now=1000.0))
        assert len(bot.resting_order_ids[market.condition_id]) == 1

        # Below the cap: a second tick adds a SECOND concurrent order in
        # the same market, rather than skipping it.
        asyncio.run(bot.strategy_tick(now=1001.0))
        assert len(bot.resting_order_ids[market.condition_id]) == 2

        # At the cap: a third tick must NOT place a third order.
        asyncio.run(bot.strategy_tick(now=1002.0))
        assert len(bot.resting_order_ids[market.condition_id]) == 2

    def test_cap_of_one_reproduces_the_old_single_order_policy(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_OPEN_ORDERS_PER_MARKET", 1)
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        market = make_bitcoin_market()
        wire_market(bot, market)

        asyncio.run(bot.strategy_tick(now=1000.0))
        assert market.condition_id in bot.resting_order_ids
        first_order_id = next(iter(bot.resting_order_ids[market.condition_id]))

        asyncio.run(bot.strategy_tick(now=1001.0))
        assert bot.resting_order_ids[market.condition_id] == {first_order_id}


class TestRestingOrderIdsClearsOnFillViaTradePrint:
    """Regression test: resting_order_ids must clear when an order fills via
    a trade print consumed during manage_orders_tick's drain loop, not only
    when fill_simulation.manage_open_orders reports a change (expiry/
    reprice/cancel). An earlier version of this wiring only swept the
    latter and left filled markets permanently blocked from new entries."""

    def test_fill_via_trade_print_frees_the_market_for_a_new_order(self):
        bot = PaperBot(assets=["Bitcoin"], seed=2)
        market = make_bitcoin_market()
        book = wire_market(bot, market, bid=0.20, ask=0.21, depth=50.0)

        asyncio.run(bot.strategy_tick(now=1000.0))
        order_id = next(iter(bot.resting_order_ids[market.condition_id]))
        order = bot.fill_sim.orders[order_id]

        needed = order.queue_ahead_discounted + order.original_size
        book.apply_last_trade_price(price=order.price, size=needed, side="SELL", ts=1001.0)
        bot.manage_orders_tick(now=1001.0)

        assert order.status.value == "FILLED"
        assert market.condition_id not in bot.resting_order_ids, (
            "a filled order must not keep blocking new entries in its market"
        )

        # And a new entry can now be placed in the same market.
        asyncio.run(bot.strategy_tick(now=1002.0))
        assert market.condition_id in bot.resting_order_ids
        assert order_id not in bot.resting_order_ids[market.condition_id]


class TestTimingGateBlocksLateMarkets:
    def test_market_with_under_90s_left_gets_no_order(self):
        bot = PaperBot(assets=["Bitcoin"], seed=3)
        market = make_bitcoin_market(end_time=1030.0)  # 30s left at now=1000
        wire_market(bot, market)

        asyncio.run(bot.strategy_tick(now=1000.0))
        assert market.condition_id not in bot.resting_order_ids


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
        # A normal down book, so the up-book None-check doesn't
        # short-circuit before ever touching the exploding property.
        down = BookState(token_id=bad_market.token_id_down)
        down.apply_snapshot(bids=[(0.79, 200.0)], asks=[(0.80, 200.0)])
        bot.book_states[bad_market.token_id_down] = down

        asyncio.run(bot.strategy_tick(now=1000.0))

        assert bad_market.condition_id in bot.halted_conditions
        assert good_market.condition_id in bot.resting_order_ids
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

        monkeypatch.setattr(botmod, "fetch_market_for_resolution", fake_fetch)

        asyncio.run(bot.resolution_tick(now=1000.0))

        assert call_count["n"] == config.RESOLUTION_MAX_ATTEMPTS_PER_TICK
        assert len(bot.pending_resolution) == n_markets  # none resolved, none dropped

    def test_a_market_is_not_retried_before_its_cooldown_elapses(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=2)
        cid, market = make_pending_market(0)
        bot.pending_resolution[cid] = market

        call_count = {"n": 0}
        monkeypatch.setattr(botmod, "fetch_market_for_resolution",
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

        monkeypatch.setattr(botmod, "fetch_market_for_resolution", always_fails)

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
        monkeypatch.setattr(botmod, "fetch_market_for_resolution",
                             lambda slug, session=None, timeout=10.0: call_count.update(n=call_count["n"] + 1) or None)

        asyncio.run(bot.resolution_tick(now=config.RESOLUTION_MAX_AGE_SECONDS + 1))

        assert cid not in bot.pending_resolution
        assert call_count["n"] == 0, "should give up before even attempting a fetch past max age"

    def test_persistently_indecisive_old_markets_do_not_starve_newer_ones(self, monkeypatch):
        """Direct regression test for a live-confirmed starvation bug:
        iterating pending_resolution in insertion order meant a handful of
        persistently-indecisive OLD markets sat at the front of the dict
        forever and won the attempt budget every tick, so every market
        retired after them got zero attempts -- settled_trades froze while
        pending_resolution grew unbounded, and newer markets hit ABANDONED
        without ever having been checked once. Fixed by ordering each
        tick's candidates by least-recently-attempted instead."""
        bot = PaperBot(assets=["Bitcoin"], seed=6)
        cap = config.RESOLUTION_MAX_ATTEMPTS_PER_TICK

        # `cap` old markets that NEVER resolve (always "not decisive").
        stubborn_cids = []
        for i in range(cap):
            cid, market = make_pending_market(i)
            bot.pending_resolution[cid] = market
            stubborn_cids.append(cid)

        # One newer market, retired after the stubborn ones.
        new_cid, new_market = make_pending_market(cap)
        bot.pending_resolution[new_cid] = new_market

        def always_indecisive(slug, session=None, timeout=10.0):
            return None  # "found it, not decisive yet" -- never resolves

        monkeypatch.setattr(botmod, "fetch_market_for_resolution", always_indecisive)

        # Tick 1: with `cap` stubborn markets exactly filling the budget,
        # the insertion-order bug would let them win every single tick.
        asyncio.run(bot.resolution_tick(now=1000.0))
        for cid in stubborn_cids:
            assert bot._resolution_last_attempt[cid] == 1000.0
        assert new_cid not in bot._resolution_last_attempt, (
            "new market correctly not reached yet -- budget was full this tick"
        )

        # Tick 2, after the cooldown elapses for everyone: the new market
        # must now get a turn precisely BECAUSE it's least-recently-attempted
        # (never), even though the stubborn markets are technically eligible
        # again too.
        t2 = 1000.0 + config.RESOLUTION_RETRY_COOLDOWN_SECONDS + 1
        asyncio.run(bot.resolution_tick(now=t2))
        assert bot._resolution_last_attempt[new_cid] == t2, (
            "the previously-starved market must be attempted once its turn comes"
        )

    def test_successful_resolution_settles_filled_orders_and_logs_pnl(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=5)
        cid, market = make_pending_market(0)
        bot.pending_resolution[cid] = market
        order = make_filled_order(cid, order_id=1, side="Up", price=0.3, size=5.0)
        bot.fill_sim.orders[order.order_id] = order

        def fake_fetch(slug, session=None, timeout=10.0):
            return {"closed": True, "outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]'}

        monkeypatch.setattr(botmod, "fetch_market_for_resolution", fake_fetch)

        asyncio.run(bot.resolution_tick(now=1000.0))

        assert cid not in bot.pending_resolution
        assert bot.ledger.realized_pnl() == pytest.approx(5.0 * 1.0 - 5.0 * 0.3)

    def test_resolves_via_decisive_price_when_gamma_never_marks_closed(self, monkeypatch):
        """Direct regression test for a live-confirmed issue: Gamma's
        `closed` flag stayed False for 30+ minutes on real 5-minute
        markets while pending_resolution grew unbounded (36+, zero ever
        resolving). A market already in pending_resolution has, by our own
        state machine, definitely finished trading -- so a very decisive
        price is trusted even with closed=False."""
        bot = PaperBot(assets=["Bitcoin"], seed=7)
        cid, market = make_pending_market(0)
        bot.pending_resolution[cid] = market
        order = make_filled_order(cid, order_id=1, side="Up", price=0.3, size=5.0)
        bot.fill_sim.orders[order.order_id] = order

        def fake_fetch(slug, session=None, timeout=10.0):
            return {"closed": False, "outcomes": '["Up", "Down"]',
                    "outcomePrices": '["0.9995", "0.0005"]'}

        monkeypatch.setattr(botmod, "fetch_market_for_resolution", fake_fetch)

        asyncio.run(bot.resolution_tick(now=1000.0))

        assert cid not in bot.pending_resolution
        assert bot.ledger.realized_pnl() == pytest.approx(5.0 * 1.0 - 5.0 * 0.3)

    def test_does_not_resolve_on_an_indecisive_price_even_without_closed(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=8)
        cid, market = make_pending_market(0)
        bot.pending_resolution[cid] = market

        def fake_fetch(slug, session=None, timeout=10.0):
            return {"closed": False, "outcomes": '["Up", "Down"]',
                    "outcomePrices": '["0.6", "0.4"]'}

        monkeypatch.setattr(botmod, "fetch_market_for_resolution", fake_fetch)

        asyncio.run(bot.resolution_tick(now=1000.0))

        assert cid in bot.pending_resolution  # still waiting, correctly


class TestDriftCancelRunsBeforeTradePrintDrain:
    """
    Regression test for the CORE/HIGH win-rate gap found by comparing the
    live bot's ledger against the real trader over the same window (see
    trader_intel/README.md): bot CORE=62%/HIGH=72% vs the real trader's
    91%/98% in the identical window.

    manage_orders_tick used to drain trade prints into on_trade_print
    BEFORE calling manage_open_orders (drift/cancel). on_trade_print's
    eligibility check is deliberately permissive (`trade.price <=
    order.price`, matching real price-time priority for a resting bid),
    so a single trade print at a much lower, unrelated price level could
    fill a stale HIGH/CORE order in the same tick its price had already
    left the target regime band -- one tick before manage_open_orders
    would have cancelled it instead. Fixed by reordering: cancel-on-
    band-exit now runs first, using the book's best_bid (already current
    independent of trade-print draining -- apply_last_trade_price never
    touches best_bid), so a same-tick stale fill can no longer race ahead
    of the cancellation that should have preempted it.
    """

    def test_order_that_left_its_band_is_cancelled_not_filled_by_a_same_tick_print(self):
        bot = PaperBot(assets=["Bitcoin"], seed=7)
        market = make_bitcoin_market()
        book = wire_market(bot, market, bid=0.95, ask=0.96, depth=50.0)
        bot.markets_by_condition[market.condition_id] = market

        order = SimulatedOrder(
            order_id=1, condition_id=market.condition_id, token_id=market.token_id_up,
            asset="Bitcoin", regime="HIGH", position_tier="first", side="Up", price=0.95,
            original_size=10.0, is_floor_lot=False, placed_at=1000.0,
            remaining_size=10.0, queue_ahead_raw=0.0, queue_ahead_discounted=0.0,
        )
        bot.fill_sim.orders[order.order_id] = order
        bot.resting_order_ids[market.condition_id] = {order.order_id}

        # Price craters out of HIGH into MID before this tick -- book state
        # (best_bid/best_ask) is already current, exactly as it would be
        # from a real price_change event landing ahead of this tick.
        book.apply_snapshot(bids=[(0.50, 50.0)], asks=[(0.51, 50.0)])
        # A same-tick trade print at a price within [0.50, 0.95] that
        # WOULD have filled this order under the old (drain-first)
        # ordering, since trade.price(0.60) <= order.price(0.95).
        book.apply_last_trade_price(price=0.60, size=order.original_size, side="SELL", ts=1001.0)

        bot.manage_orders_tick(now=1001.0)

        assert order.status.value == "CANCELLED", (
            f"expected the order to be cancelled for leaving its HIGH band, "
            f"got status={order.status.value} -- a same-tick trade print at "
            f"a stale, out-of-band price must not be able to fill an order "
            f"that should have already been cancelled"
        )
        assert order.filled_size == 0.0


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


class TestBoundedMemoryForLongRunningDeployment:
    """Direct regression tests for a real unbounded-growth audit: several
    per-market dicts (activity, last_price_by_token, halted_conditions)
    and fill_sim.orders were never cleaned up, fine for a Railway deploy
    that gets restarted on every push (which accidentally masked this),
    a real problem for an AWS deployment meant to run for weeks."""

    def test_retire_cleans_up_activity_and_price_tracking_and_halted(self):
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        market = make_bitcoin_market(condition_id="cond-x")
        wire_market(bot, market)
        bot.halted_conditions.add(market.condition_id)
        bot.last_price_by_token[market.token_id_up] = 0.42
        assert market.condition_id in bot.activity

        asyncio.run(bot._retire_market(market))

        assert market.condition_id not in bot.activity
        assert market.token_id_up not in bot.last_price_by_token
        assert market.condition_id not in bot.halted_conditions
        # retiring still does its original job
        assert market.condition_id in bot.pending_resolution

    def test_resolved_market_prunes_its_orders_from_fill_sim(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=2)
        cid, market = make_pending_market(0)
        bot.pending_resolution[cid] = market
        order = make_filled_order(cid, order_id=1, side="Up", price=0.3, size=5.0)
        bot.fill_sim.orders[order.order_id] = order

        monkeypatch.setattr(
            botmod, "fetch_market_for_resolution",
            lambda slug, session=None, timeout=10.0: {
                "closed": True, "outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]'
            },
        )

        asyncio.run(bot.resolution_tick(now=1000.0))

        assert order.order_id not in bot.fill_sim.orders
        # settlement still happened before the prune
        assert bot.ledger.realized_pnl() == pytest.approx(5.0 * 1.0 - 5.0 * 0.3)

    def test_abandoned_market_also_prunes_its_orders(self, monkeypatch):
        bot = PaperBot(assets=["Bitcoin"], seed=3)
        cid, market = make_pending_market(0)
        bot.pending_resolution[cid] = market
        bot._resolution_first_seen[cid] = 0.0  # first seen at t=0, so it's already stale
        order = make_filled_order(cid, order_id=1, side="Up", price=0.3, size=5.0)
        bot.fill_sim.orders[order.order_id] = order

        monkeypatch.setattr(botmod, "fetch_market_for_resolution",
                             lambda slug, session=None, timeout=10.0: None)

        asyncio.run(bot.resolution_tick(now=config.RESOLUTION_MAX_AGE_SECONDS + 1))

        assert cid not in bot.pending_resolution
        assert order.order_id not in bot.fill_sim.orders


class TestBankrollGate:
    """config.BANKROLL_USD's optional fixed-bankroll mode (2026-09-08),
    built for a second small-capital instance meant to answer "what would
    actually happen with a real $100 deposit" -- without changing the
    strategy at all. See config.py's docstring for the full design."""

    def test_default_mode_is_unconstrained_and_available_cash_is_none(self):
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        assert config.BANKROLL_USD is None
        assert bot.available_cash() is None

    def test_available_cash_recomputes_from_starting_balance_minus_open_orders(self, monkeypatch):
        monkeypatch.setattr(config, "BANKROLL_USD", 100.0)
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        bot.ledger = Ledger()  # isolate from any real state on disk
        assert bot.available_cash() == pytest.approx(100.0)

        # A still-open (unfilled) resting order reserves its full notional --
        # a real resting limit order ties up buying power before it fills.
        order = SimulatedOrder(
            order_id=1, condition_id="cond-x", token_id="cond-x-up", asset="Bitcoin",
            regime="MID", position_tier="first", side="Up", price=0.40,
            original_size=10.0, is_floor_lot=False, placed_at=0.0, remaining_size=10.0,
        )
        bot.fill_sim.orders[order.order_id] = order
        assert bot.available_cash() == pytest.approx(100.0 - 10.0 * 0.40)

    def test_available_cash_accounts_for_filled_but_unsettled_orders(self, monkeypatch):
        monkeypatch.setattr(config, "BANKROLL_USD", 100.0)
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        bot.ledger = Ledger()  # isolate from any real state on disk
        order = make_filled_order("cond-x", order_id=1, price=0.30, size=5.0)
        bot.fill_sim.orders[order.order_id] = order
        # Filled (cost committed) but resolution_tick hasn't settled it yet.
        assert bot.available_cash() == pytest.approx(100.0 - 5.0 * 0.30)

        bot.ledger.settle_order(order, winning_side="Up")
        # Once settled, the cost drops out of "committed" and the P&L
        # (payout - cost) is already inside realized_pnl -- no double count.
        assert bot.available_cash() == pytest.approx(100.0 + bot.ledger.realized_pnl())

    def test_abandoned_filled_orders_stay_written_off_not_reclaimed(self, monkeypatch):
        """Direct regression test for the accounting leak an abandoned
        (never-resolved) market would otherwise cause: once resolution_tick
        prunes its orders from fill_sim.orders, they'd vanish from
        `committed` without ever being subtracted via realized_pnl --
        silently making that spent capital look available again."""
        monkeypatch.setattr(config, "BANKROLL_USD", 100.0)
        bot = PaperBot(assets=["Bitcoin"], seed=3)
        bot.ledger = Ledger()  # isolate from any real state on disk
        cid, market = make_pending_market(0)
        bot.pending_resolution[cid] = market
        bot._resolution_first_seen[cid] = 0.0
        order = make_filled_order(cid, order_id=1, side="Up", price=0.30, size=5.0)
        bot.fill_sim.orders[order.order_id] = order

        cash_before_abandonment = bot.available_cash()
        assert cash_before_abandonment == pytest.approx(100.0 - 5.0 * 0.30)

        monkeypatch.setattr(botmod, "fetch_market_for_resolution",
                             lambda slug, session=None, timeout=10.0: None)
        asyncio.run(bot.resolution_tick(now=config.RESOLUTION_MAX_AGE_SECONDS + 1))

        assert order.order_id not in bot.fill_sim.orders  # pruned, as before
        assert bot.ledger.realized_pnl() == 0.0            # never settled
        # ...but the spent capital must NOT have reappeared as available.
        assert bot.available_cash() == pytest.approx(cash_before_abandonment)

    def test_order_is_skipped_when_it_would_exceed_available_bankroll(self, monkeypatch):
        monkeypatch.setattr(config, "BANKROLL_USD", 0.0001)  # effectively broke
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        bot.ledger = Ledger()  # isolate from any real state on disk
        market = make_bitcoin_market(condition_id="cond-x")
        wire_market(bot, market, bid=0.20, ask=0.21, depth=200.0)

        asyncio.run(bot.strategy_tick(now=1000.0))

        assert market.condition_id not in bot.resting_order_ids
        assert len(bot.fill_sim.orders) == 0

    def test_order_still_places_normally_when_bankroll_covers_it(self, monkeypatch):
        monkeypatch.setattr(config, "BANKROLL_USD", 100.0)
        bot = PaperBot(assets=["Bitcoin"], seed=1)
        bot.ledger = Ledger()  # isolate from any real state on disk
        market = make_bitcoin_market(condition_id="cond-x")
        wire_market(bot, market, bid=0.20, ask=0.21, depth=200.0)

        asyncio.run(bot.strategy_tick(now=1000.0))

        assert market.condition_id in bot.resting_order_ids


class TestOrderIdCollisionAcrossRestarts:
    """Direct regression test for a confirmed severe live bug (2026-09-08):
    order_id was a plain per-process counter starting at 1 on every
    restart, but Ledger._settled_order_ids (settle_order()'s idempotency
    check, keyed only on this small integer) persists across restarts,
    loaded fresh from disk every time. A fresh run's own order_id range
    collided with order_ids already used by prior runs, and settle_order()
    silently refused to record the real, brand-new settlement -- measured
    live: 96.5% of one run's first 1,299 order_ids had already been used
    by an earlier run, and only 54 of 500+ genuinely filled orders ever
    made it into the ledger. Fixed by seeding the counter past the loaded
    ledger's max order_id at PaperBot construction time."""

    def test_fresh_order_ids_never_collide_with_a_loaded_ledgers_history(self):
        # Simulate a ledger from prior runs whose order_ids go far higher
        # than a fresh process's own counter would ever start at.
        old_record = {
            "order_id": 500, "condition_id": "old-cond", "asset": "Bitcoin",
            "regime": "MID", "side": "Up", "entry_price": 0.5, "filled_size": 2.0,
            "entry_cost": 1.0, "winning_side": "Up", "won": True, "payout": 2.0,
            "pnl": 1.0, "settled_at": 1000.0, "is_floor_lot": False, "is_hedge": False,
        }
        config.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.LEDGER_PATH.write_text(json.dumps({"records": [old_record]}))

        bot = PaperBot(assets=["Bitcoin"], seed=1)
        market = make_bitcoin_market(condition_id="cond-new")
        wire_market(bot, market, bid=0.20, ask=0.21, depth=200.0)

        asyncio.run(bot.strategy_tick(now=1000.0))

        assert market.condition_id in bot.resting_order_ids
        new_order_id = next(iter(bot.resting_order_ids[market.condition_id]))
        assert new_order_id > 500, (
            "a fresh order_id collided with (or fell below) the loaded ledger's "
            "historical max -- settle_order() would silently drop this order's "
            "real settlement, mistaking it for the old order_id=500 record"
        )

    def test_settlement_actually_records_after_seeding(self):
        """End-to-end: not just that the id differs, but that a real
        settlement for a fresh order actually lands in ledger.records --
        this is what was silently failing before the fix. Places through
        the real strategy_tick pipeline (not a hand-picked order_id) to
        prove the fix end-to-end."""
        old_record = {
            "order_id": 3, "condition_id": "old-cond", "asset": "Bitcoin",
            "regime": "MID", "side": "Up", "entry_price": 0.5, "filled_size": 2.0,
            "entry_cost": 1.0, "winning_side": "Up", "won": True, "payout": 2.0,
            "pnl": 1.0, "settled_at": 1000.0, "is_floor_lot": False, "is_hedge": False,
        }
        config.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.LEDGER_PATH.write_text(json.dumps({"records": [old_record]}))

        bot = PaperBot(assets=["Bitcoin"], seed=1)
        market = make_bitcoin_market(condition_id="new-cond")
        wire_market(bot, market, bid=0.20, ask=0.21, depth=200.0)
        asyncio.run(bot.strategy_tick(now=1000.0))
        placed_id = next(iter(bot.resting_order_ids[market.condition_id]))
        placed_order = bot.fill_sim.orders[placed_id]
        placed_order.fills.append(Fill(size=placed_order.original_size, price=placed_order.price, ts=1001.0))
        placed_order.status = OrderStatus.FILLED

        record = bot.ledger.settle_order(placed_order, winning_side=placed_order.side)

        assert record is not None, (
            "settle_order() silently dropped a genuinely new settlement -- "
            "the exact bug this fix closes"
        )
        assert bot.ledger.realized_pnl() == pytest.approx(1.0 + record.pnl)


class TestSizeScaleFactor:
    """config.SIZE_SCALE_FACTOR (2026-09-08): a global multiplier so a
    small-bankroll instance can run the IDENTICAL decision logic at a
    proportionally smaller dollar scale, without exhausting its bankroll
    on a handful of whale-sized entries and trading less often than the
    unscaled bot as a result."""

    def test_default_factor_is_a_no_op(self):
        assert config.SIZE_SCALE_FACTOR == 1.0

    def test_scaling_down_shrinks_notional_proportionally_not_the_strategy(self, monkeypatch):
        rng_unscaled = __import__("random").Random(42)
        rng_scaled = __import__("random").Random(42)  # same seed -> same jitter roll

        notional_unscaled, floor_unscaled = stratmod.decide_size(
            "Bitcoin", "MID", "first", 0.40, rng_unscaled)

        monkeypatch.setattr(config, "SIZE_SCALE_FACTOR", 0.2)
        notional_scaled, floor_scaled = stratmod.decide_size(
            "Bitcoin", "MID", "first", 0.40, rng_scaled)

        assert floor_unscaled == floor_scaled  # same floor-lot roll either way
        if not floor_unscaled:
            # Exactly proportional -- same relative sizing curve, just at a
            # smaller dollar scale. Not a strategy change.
            assert notional_scaled == pytest.approx(notional_unscaled * 0.2)

    def test_floor_lot_tier_is_never_scaled(self, monkeypatch):
        """The floor-lot probe tier is already tiny and independently
        calibrated -- scaling it further risks pushing it below a real
        exchange's orderMinSize and silently killing those trades outright,
        the opposite of what SIZE_SCALE_FACTOR exists for."""
        monkeypatch.setattr(config, "SIZE_SCALE_FACTOR", 0.01)
        rng = __import__("random").Random(1)
        # BNB/CHEAP/first has a nonzero floor-lot probability (position-
        # dependent table) -- force the roll to land in the floor-lot branch.
        monkeypatch.setattr(rng, "random", lambda: 0.0)
        notional, is_floor_lot = stratmod.decide_size("BNB", "CHEAP", "first", 0.10, rng)
        assert is_floor_lot is True
        assert notional == pytest.approx(config.FLOOR_LOT_SIZE_SHARES * 0.10)
