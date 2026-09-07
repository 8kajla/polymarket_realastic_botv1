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
from paperbot import strategy as stratmod
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
    build_order_intent -> place_order -> resting_order_id, matching the
    confirmed historical pattern (dual-sided markets have a dramatically
    better worst-case outcome than single-sided ones)."""

    def test_second_entry_becomes_a_sized_hedge_when_triggered(self, monkeypatch):
        bot = PaperBot(assets=["BNB"], seed=1)
        market = make_market_for_asset("BNB")
        wire_market(bot, market, bid=0.75, ask=0.76, depth=200.0)  # primary lands in CORE

        # First entry: establishes the primary side and its cost.
        asyncio.run(bot.strategy_tick(now=1000.0))
        assert market.condition_id in bot.resting_order_id
        first_order_id = bot.resting_order_id[market.condition_id]
        first_order = bot.fill_sim.orders[first_order_id]
        activity = bot.activity[market.condition_id]
        assert activity.entry_count == 1
        assert activity.first_entry_regime == "CORE"

        # Free up the market for a second entry (as if the first got filled).
        first_order.fills.append(Fill(size=first_order.original_size, price=first_order.price, ts=1001.0))
        first_order.status = OrderStatus.FILLED
        del bot.resting_order_id[market.condition_id]

        # Force the hedge roll to trigger deterministically for this test.
        monkeypatch.setattr(stratmod, "decide_hedge",
                             lambda asset, activity, rng: "Down" if activity.dominant_side() == "Up"
                             else "Up")

        asyncio.run(bot.strategy_tick(now=1001.0))

        assert market.condition_id in bot.resting_order_id
        hedge_order_id = bot.resting_order_id[market.condition_id]
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
        # A normal down book, so the up-book None-check doesn't
        # short-circuit before ever touching the exploding property.
        down = BookState(token_id=bad_market.token_id_down)
        down.apply_snapshot(bids=[(0.79, 200.0)], asks=[(0.80, 200.0)])
        bot.book_states[bad_market.token_id_down] = down

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

        monkeypatch.setattr(botmod, "fetch_market_by_slug", always_indecisive)

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

        monkeypatch.setattr(botmod, "fetch_market_by_slug", fake_fetch)

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

        monkeypatch.setattr(botmod, "fetch_market_by_slug", fake_fetch)

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

        monkeypatch.setattr(botmod, "fetch_market_by_slug", fake_fetch)

        asyncio.run(bot.resolution_tick(now=1000.0))

        assert cid in bot.pending_resolution  # still waiting, correctly


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
            botmod, "fetch_market_by_slug",
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

        monkeypatch.setattr(botmod, "fetch_market_by_slug",
                             lambda slug, session=None, timeout=10.0: None)

        asyncio.run(bot.resolution_tick(now=config.RESOLUTION_MAX_AGE_SECONDS + 1))

        assert cid not in bot.pending_resolution
        assert order.order_id not in bot.fill_sim.orders
