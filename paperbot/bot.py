"""
Main loop: wires market discovery, live book state, strategy, the fill
simulator, and the ledger together.

FAIL-CLOSED: any unexpected exception while processing a given market is
caught, logged clearly, and that market is added to `halted_conditions` --
its activity stops rather than continuing in an unknown state. Other
markets are unaffected.

This orchestrator's network-facing pieces (WS connect, Gamma polling)
could not be live-tested from the build environment (no outbound network
to polymarket.com hosts here). The decision logic it calls into
(strategy, fill_simulation, ledger) is fully unit tested; treat a live
run of this file as the remaining integration smoke test before trusting
it unattended.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import signal
import sys
import time

import requests

from . import behavior_config as bc
from . import config
from .book import BookState, MarketWebSocketClient, bootstrap_book_state
from .fill_simulation import FillSimulator
from .ledger import Ledger
from .market_discovery import (
    Market,
    MarketDiscovery,
    fetch_market_for_resolution,
    infer_resolution_from_price,
    parse_resolution,
)
from .strategy import MarketActivityState, build_order_intent, safe_postonly_price, would_cross_spread

logger = logging.getLogger("paperbot.bot")

# _onboard_markets bootstraps every token in a rollover batch concurrently
# (up to ~12 tokens at once, per market rollover) via asyncio.gather over
# asyncio.to_thread, all against one shared requests.Session. requests'
# default HTTPAdapter pools only 10 connections per host, which isn't
# enough for that -- confirmed live as a burst of "Connection pool is
# full, discarding connection: clob.polymarket.com" warnings right after
# the onboarding-parallelization fix shipped (harmless -- urllib3 just
# opens a fresh connection instead of reusing one -- but noisy and
# avoidable). Sized with headroom above the current worst case.
SESSION_POOL_SIZE = 30


def _make_session() -> requests.Session:
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=SESSION_POOL_SIZE, pool_maxsize=SESSION_POOL_SIZE,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class PaperBot:
    def __init__(self, assets: list[str] | None = None,
                 queue_safety_factor: float = config.QUEUE_SAFETY_FACTOR,
                 seed: int | None = None):
        # get_trading_mode() raises if anything but paper mode is requested;
        # calling it here means a misconfigured LIVE attempt fails at
        # construction time, before any market state is even touched.
        config.get_trading_mode()

        self.assets = assets or config.ALL_ASSETS
        self.session = _make_session()
        self.discovery = MarketDiscovery(session=self.session, assets=self.assets)
        self.fill_sim = FillSimulator(queue_safety_factor=queue_safety_factor)
        self.ledger = Ledger.load()
        self.rng = random.Random(seed)

        self.book_states: dict[str, BookState] = {}          # token_id -> BookState
        self.activity: dict[str, MarketActivityState] = {}    # condition_id -> state
        self.markets_by_condition: dict[str, Market] = {}
        self.resting_order_ids: dict[str, set[int]] = {}       # condition_id -> {order_id, ...}
        self.halted_conditions: set[str] = set()
        self.pending_resolution: dict[str, Market] = {}
        self.last_price_by_token: dict[str, float] = {}
        self._resolution_last_attempt: dict[str, float] = {}
        self._resolution_failure_count: dict[str, int] = {}
        self._resolution_first_seen: dict[str, float] = {}
        self._last_pnl_summary_at: float = 0.0
        # Append-only, like ledger.records -- never an incrementally
        # mutated single number. Only ever grows when config.BANKROLL_USD
        # is set; see resolution_tick's ABANDONED branch and
        # available_cash()'s docstring for why this exists.
        self._abandoned_filled_costs: list[float] = []

        self.ws_client = MarketWebSocketClient(get_book_state=self.book_states.get)

    # -- discovery ---------------------------------------------------------

    async def discovery_tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if not self.discovery.due_for_poll(now):
            return
        try:
            active = await asyncio.to_thread(self.discovery.poll)
        except Exception:
            logger.exception("market discovery poll failed; keeping previous active set")
            return

        active = {cid: m for cid, m in active.items() if m.asset in self.assets}
        new_conditions = set(active) - set(self.markets_by_condition)
        dropped_conditions = set(self.markets_by_condition) - set(active)

        new_markets = [active[cid] for cid in new_conditions]
        if new_markets:
            await self._onboard_markets(new_markets)

        for cid in dropped_conditions:
            await self._retire_market(self.markets_by_condition[cid])

        self.markets_by_condition = active

    async def _onboard_markets(self, markets: list[Market]) -> None:
        """
        Onboard a batch of newly-discovered markets. Every token's REST
        book-bootstrap across the WHOLE batch runs concurrently (asyncio.
        gather over asyncio.to_thread) rather than one market -- and one
        token within it -- at a time.

        This isn't just a speed optimization: confirmed live that
        sequentially awaiting up to 12 blocking REST round trips (6 markets
        x 2 tokens, the normal size of a 5-minute rollover batch) coincided
        with the WebSocket getting kicked as a "slow consumer" (websockets
        1013) a couple of times. The event loop can technically interleave
        other coroutines while any single await is pending, but a long
        CHAIN of sequential awaits in this one coroutine gives the WS
        reader far more, and far more frequent, opportunities to fall
        behind than one batched gather does. The existing reconnect-with-
        backoff in _ws_supervisor already recovered cleanly every time this
        happened, but reducing how often it happens at all is worth doing
        now that there's a clear, reproducible trigger (rollover batches).
        """
        for market in markets:
            logger.info("ONBOARD market=%s asset=%s condition=%s", market.slug, market.asset,
                        market.condition_id)
            self.activity[market.condition_id] = MarketActivityState()

        token_jobs = [
            (market, token_id)
            for market in markets
            for token_id in (market.token_id_up, market.token_id_down)
        ]

        async def _bootstrap_one(market: Market, token_id: str):
            try:
                return await asyncio.to_thread(bootstrap_book_state, token_id, self.session)
            except Exception:
                logger.exception("failed to bootstrap book for token %s (market %s); halting it",
                                  token_id, market.slug)
                self.halted_conditions.add(market.condition_id)
                return None

        results = await asyncio.gather(*(_bootstrap_one(m, t) for m, t in token_jobs))

        subscribed_ids = []
        for (market, token_id), state in zip(token_jobs, results):
            if state is not None:
                self.book_states[token_id] = state
                subscribed_ids.append(token_id)

        if subscribed_ids:
            await self.ws_client.subscribe(subscribed_ids)

    async def _retire_market(self, market: Market) -> None:
        logger.info("RETIRE market=%s asset=%s condition=%s", market.slug, market.asset,
                    market.condition_id)
        self.pending_resolution[market.condition_id] = market
        self.book_states.pop(market.token_id_up, None)
        self.book_states.pop(market.token_id_down, None)
        # Bounded-memory cleanup for a long-running process: none of this
        # per-market state is needed again once a market is no longer in
        # markets_by_condition -- strategy_tick will never evaluate it
        # again. (fill_sim.orders for this market is pruned separately, in
        # resolution_tick, once settlement/abandonment is actually done
        # with them -- not here, since some may still be open at retire
        # time for a few ticks while expiry catches up.)
        self.activity.pop(market.condition_id, None)
        self.last_price_by_token.pop(market.token_id_up, None)
        self.halted_conditions.discard(market.condition_id)

    # -- strategy ------------------------------------------------------

    def _recent_price_delta(self, token_id: str, current_price: float) -> float | None:
        prev = self.last_price_by_token.get(token_id)
        self.last_price_by_token[token_id] = current_price
        if prev is None:
            return None
        return current_price - prev

    async def strategy_tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        for cid, market in self.markets_by_condition.items():
            if cid in self.halted_conditions:
                continue
            open_here = self.resting_order_ids.get(cid)
            if open_here and len(open_here) >= config.MAX_OPEN_ORDERS_PER_MARKET:
                continue
            try:
                self._evaluate_one_market(market, now)
            except Exception:
                logger.exception("unexpected error evaluating market %s; halting its activity",
                                  market.slug)
                self.halted_conditions.add(cid)

    def _evaluate_one_market(self, market: Market, now: float) -> None:
        # Both books are required: Down's price is (roughly) 1 - Up's
        # price, not the same value, so build_order_intent needs whichever
        # one actually corresponds to the side it decides to trade. See
        # build_order_intent's docstring for the live bug this fixed.
        up_book = self.book_states.get(market.token_id_up)
        down_book = self.book_states.get(market.token_id_down)
        if up_book is None or down_book is None or up_book.best_bid is None \
                or down_book.best_bid is None:
            return

        activity = self.activity[market.condition_id]
        delta = self._recent_price_delta(market.token_id_up, up_book.best_bid)

        intent = build_order_intent(market, up_book, down_book, activity, self.rng,
                                     recent_price_delta=delta, now=now)
        if intent is None:
            return

        order_book = self.book_states.get(intent.token_id)
        if order_book is None:
            return

        price = intent.price
        if would_cross_spread(order_book, price):
            # Per Part 1: retry once against fresh book data, else skip.
            price = safe_postonly_price(order_book, price)
            if price is None:
                logger.info("SKIP market=%s: postOnly order would cross spread even after retry",
                            market.slug)
                return
            intent.price = price
            intent.size_shares = intent.notional_usd / price if price > 0 else 0.0

        cash = self.available_cash()
        if cash is not None and intent.notional_usd > cash:
            # Real "insufficient buying power" rejection, not a silent
            # downsize -- see available_cash()'s docstring. Only reachable
            # when config.BANKROLL_USD is set; a None return short-
            # circuits this for the main (unconstrained) bot.
            logger.info(
                "SKIP market=%s: insufficient bankroll (need $%.4f, have $%.4f)",
                market.slug, intent.notional_usd, cash,
            )
            return

        order = self.fill_sim.place_order(intent, order_book, now=now)
        self.resting_order_ids.setdefault(market.condition_id, set()).add(order.order_id)
        activity.record_entry(intent.side, intent.notional_usd, intent.regime, is_hedge=intent.is_hedge)

    def available_cash(self) -> float | None:
        """
        Returns None in the default (unconstrained) mode -- see
        config.BANKROLL_USD's docstring; the main bot must always get None
        here so this whole feature is a no-op for it.

        Otherwise, RECOMPUTED FROM SOURCE every call (never a running
        total that gets +=/-= mutated -- the same hard rule this project
        already enforces for Ledger.realized_pnl(), for the same reason:
        an incremental counter drifts from reality the first time an edge
        case updates state without also touching it, and a stale bankroll
        number is exactly the kind of bug that would go unnoticed for a
        long time on an unattended box):

            available = BANKROLL_USD + realized_pnl - committed - written_off

        `committed` covers every dollar not yet reflected in realized_pnl:
        the unfilled portion of any still-open resting order (remaining_
        size * price -- a real resting limit order ties up buying power
        even before it fills), plus the filled-but-not-yet-settled portion
        of any order still waiting on resolution_tick. Once an order
        actually settles, it drops out of `committed` (is_open() is False
        and ledger.is_settled() is True) and its P&L is already inside
        realized_pnl -- no double-counting either way.

        `written_off` covers the one lifecycle path that skips settlement
        entirely: an ABANDONED market (Gamma never reported a decisive
        outcome within RESOLUTION_MAX_AGE_SECONDS). Those filled orders
        are deliberately excluded from realized_pnl (see resolution_tick's
        docstring) -- but without also removing that capital here, it
        would silently reappear as available once remove_orders_for_
        condition() drops the order from fill_sim.orders, understating
        what's actually still spent. Treated as a full write-off (the
        conservative assumption when we genuinely can't confirm the
        outcome) rather than invented as a settlement this bot has no real
        confidence in.
        """
        if config.BANKROLL_USD is None:
            return None
        committed = 0.0
        for order in self.fill_sim.orders.values():
            if order.filled_size > 0 and not self.ledger.is_settled(order.order_id):
                committed += order.filled_size * order.price
            if order.is_open():
                committed += order.remaining_size * order.price
        written_off = sum(self._abandoned_filled_costs)
        return config.BANKROLL_USD + self.ledger.realized_pnl() - committed - written_off

    # -- order management -------------------------------------------------

    def manage_orders_tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()

        # Drift/cancel-out-of-band FIRST, trade-print draining SECOND --
        # deliberately, not incidentally. book.best_bid is updated
        # immediately by book/price_change WS events, independent of
        # pending_trades (apply_last_trade_price never touches best_bid),
        # so by the time this tick runs, best_bid already reflects the
        # latest book state either way. Running manage_open_orders first
        # means an order whose price has already left its target regime
        # band gets CANCELLED before this same tick's trade prints get a
        # chance to fill it. The old order (drain first, manage second)
        # let a fast intra-tick price move do real damage: on a CORE/HIGH
        # order (resting at e.g. 0.95), on_trade_print's eligibility check
        # is only `trade.price <= order.price` -- deliberately permissive,
        # since a real resting bid legitimately fills against any trade at
        # or below it (price-time priority) -- but that means a single
        # trade print reflecting a much LOWER, unrelated price level
        # (the market having already moved on, not a continuous sweep
        # through our level) could fill a stale HIGH-confidence order
        # using a price that no longer reflects anything close to
        # certainty, one tick before that same order would have been
        # cancelled for having left its band. Confirmed as the likely
        # driver of the CORE win rate=62%/HIGH win rate=72% found live
        # against the real trader's 91%/98% in the same window (see
        # trader_intel/README.md status log) -- those regimes' entire
        # edge depends on NOT holding a stale resting order into a
        # reversal, and cancelling before draining closes that specific
        # same-tick race.
        seconds_remaining = {
            cid: m.seconds_remaining(now) for cid, m in self.markets_by_condition.items()
        }
        self.fill_sim.manage_open_orders(self.book_states, seconds_remaining, now=now)

        for token_id, book in self.book_states.items():
            for trade in book.drain_pending_trades():
                try:
                    self.fill_sim.on_trade_print(token_id, trade)
                except Exception:
                    logger.exception("error consuming trade print for token %s", token_id)

        # Unconditional sweep rather than only reacting to manage_open_orders'
        # `changed` list: an order can also stop being open because a trade
        # print filled it during the drain loop above, which manage_open_orders
        # never sees. Checking every tracked order's actual current state is
        # the only way to keep this dict from going stale either way. Per-cid
        # SET (not a single id) since MAX_OPEN_ORDERS_PER_MARKET can hold more
        # than one order open in the same market at once -- drop only the
        # ones that actually closed, keep the cid entry as long as at least
        # one order in it is still open.
        for cid, order_ids in list(self.resting_order_ids.items()):
            still_open = {oid for oid in order_ids
                          if (order := self.fill_sim.orders.get(oid)) is not None and order.is_open()}
            if still_open:
                self.resting_order_ids[cid] = still_open
            else:
                del self.resting_order_ids[cid]

    # -- resolution / settlement -------------------------------------------

    def _forget_resolution_tracking(self, cid: str) -> None:
        self._resolution_last_attempt.pop(cid, None)
        self._resolution_failure_count.pop(cid, None)
        self._resolution_first_seen.pop(cid, None)

    async def resolution_tick(self, now: float | None = None) -> None:
        """
        Poll Gamma for outcomes on retired (closed) markets, and settle any
        filled orders once a winning side is decisive.

        Rate-limited and bounded on purpose -- confirmed live that without
        this, a growing backlog gets retried on every 2-second tick with no
        backoff, which both self-inflicts a 429 storm against Gamma and (via
        one full traceback logged per market per tick) floods Railway's own
        log ingestion badly enough that Railway starts dropping messages.
        Per market: at most one attempt per RESOLUTION_RETRY_COOLDOWN_SECONDS,
        growing on repeated failure; at most RESOLUTION_MAX_ATTEMPTS_PER_TICK
        markets attempted per call; and a hard give-up after
        RESOLUTION_MAX_AGE_SECONDS so a persistently-broken lookup can't grow
        the backlog (and therefore the request rate) forever.

        Iterates in least-recently-attempted order, NOT dict/insertion
        order. Confirmed live: with insertion-order iteration, a handful of
        persistently-indecisive OLD markets sit at the front of the dict
        forever (nothing ever removes them except eventual resolution or
        the 2h abandon) and win the attempt budget every single tick,
        starving every market retired after them of ANY attempts at all --
        settled_trades sat frozen for 20+ minutes while pending_resolution
        grew unbounded and a cluster of newer markets hit ABANDONED without
        ever having been checked. Sorting by last-attempt time each tick
        means every market gets fair rotation through the budget regardless
        of backlog size or how long any one entry has been stuck.
        """
        now = now if now is not None else time.time()
        attempted = 0

        ordered = sorted(
            self.pending_resolution.items(),
            key=lambda kv: self._resolution_last_attempt.get(kv[0], 0.0),
        )
        for cid, market in ordered:
            if attempted >= config.RESOLUTION_MAX_ATTEMPTS_PER_TICK:
                break

            first_seen = self._resolution_first_seen.setdefault(cid, now)
            if now - first_seen > config.RESOLUTION_MAX_AGE_SECONDS:
                logger.warning(
                    "ABANDONED resolution for market=%s asset=%s after %.0fs -- Gamma never "
                    "reported a decisive outcome; any filled orders on this market remain "
                    "unsettled and are excluded from realized P&L",
                    market.slug, market.asset, now - first_seen,
                )
                if config.BANKROLL_USD is not None:
                    # See available_cash()'s docstring: without this, the
                    # capital spent on this market's filled orders would
                    # silently reappear as available once remove_orders_
                    # for_condition() below drops them from fill_sim.
                    # orders, understating what's actually still spent. A
                    # no-op list append when unset, so the main bot is
                    # completely unaffected.
                    for order in self.fill_sim.orders.values():
                        if (order.condition_id == cid and order.filled_size > 0
                                and not self.ledger.is_settled(order.order_id)):
                            self._abandoned_filled_costs.append(order.filled_size * order.price)
                del self.pending_resolution[cid]
                self._forget_resolution_tracking(cid)
                self.fill_sim.remove_orders_for_condition(cid)
                continue

            fails = self._resolution_failure_count.get(cid, 0)
            cooldown = config.RESOLUTION_RETRY_COOLDOWN_SECONDS
            if fails > 0:
                cooldown = max(cooldown, config.RESOLUTION_BACKOFF_ON_FAILURE_SECONDS *
                               min(fails, config.RESOLUTION_MAX_BACKOFF_MULTIPLIER))
            # None (not 0.0) means "never attempted" -- do NOT use 0.0 as
            # that sentinel here, the same class of bug already bit
            # SimulatedOrder.remaining_size once (see fill_simulation.py):
            # a 0.0 "unset" default is indistinguishable from a real
            # elapsed-time-since-epoch-zero value in an edge case.
            last_attempt = self._resolution_last_attempt.get(cid)
            if last_attempt is not None and now - last_attempt < cooldown:
                continue

            attempted += 1
            self._resolution_last_attempt[cid] = now
            try:
                raw = await asyncio.to_thread(fetch_market_for_resolution, market.slug, self.session)
            except Exception as exc:
                self._resolution_failure_count[cid] = fails + 1
                status = getattr(getattr(exc, "response", None), "status_code", None)
                logger.warning("resolution poll failed for market=%s (attempt %d, http_status=%s): %s",
                                market.slug, fails + 1, status, exc)
                continue

            self._resolution_failure_count.pop(cid, None)
            if raw is None:
                # Distinct from "found it, not decisive yet" -- Gamma
                # returned nothing for this slug at all (possibly archived
                # out of this query once fully settled). Logged explicitly
                # since this was previously silent and indistinguishable
                # from ordinary pending-resolution noise.
                logger.info("resolution check market=%s: no market returned by Gamma "
                            "(possibly archived)", market.slug)
                continue

            winning_side = parse_resolution(raw)
            used_price_fallback = False
            if winning_side is None:
                # Confirmed live: `closed` can stay False for 30+ minutes
                # on these markets even once the price is fully decisive
                # and has been stable across many checks. This market is
                # already in pending_resolution -- our own state machine
                # has confirmed its trading window is over -- so trust a
                # very decisive (>=99%) price as a fallback signal rather
                # than waiting indefinitely on `closed`.
                winning_side = infer_resolution_from_price(raw)
                used_price_fallback = winning_side is not None

            if winning_side is None:
                logger.info("resolution check market=%s closed=%s outcomePrices=%s -- not decisive yet",
                            market.slug, raw.get("closed"), raw.get("outcomePrices"))
                continue

            if used_price_fallback:
                logger.info("resolution inferred from decisive price (Gamma closed=False) market=%s "
                            "outcomePrices=%s", market.slug, raw.get("outcomePrices"))

            with_fills = [
                order for order in self.fill_sim.orders.values()
                if order.condition_id == cid and order.filled_size > 0
            ]
            settled = [self.ledger.settle_order(order, winning_side) for order in with_fills]
            settled = [r for r in settled if r is not None]
            self.ledger.save()
            # Diagnostic (2026-09-08): settle_order() silently returns None
            # for a filled order whose status isn't FILLED/PARTIALLY_FILLED
            # (e.g. CANCELLED after a partial fill, if that ever happens --
            # see manage_open_orders' drift-cancel path) -- surfacing the
            # status breakdown here makes that gap visible instead of just
            # a settled_orders count lower than expected with no way to
            # tell why from the logs alone.
            if len(with_fills) != len(settled):
                status_counts: dict[str, int] = {}
                for order in with_fills:
                    status_counts[order.status.value] = status_counts.get(order.status.value, 0) + 1
                logger.warning(
                    "RESOLUTION_GAP market=%s: %d orders had fills but only %d settled -- "
                    "status breakdown of the %d with fills: %s",
                    market.slug, len(with_fills), len(settled), len(with_fills), status_counts,
                )
            logger.info(
                "RESOLVED market=%s asset=%s winning_side=%s settled_orders=%d "
                "realized_pnl_total=%.4f",
                market.slug, market.asset, winning_side, len(settled), self.ledger.realized_pnl(),
            )
            del self.pending_resolution[cid]
            self._forget_resolution_tracking(cid)
            self.fill_sim.remove_orders_for_condition(cid)

    def log_pnl_summary(self, now: float | None = None) -> None:
        """Periodic heartbeat, independent of any individual settlement, so
        realized P&L is visible in the logs even during a quiet stretch."""
        now = now if now is not None else time.time()
        by_asset = {a: round(v, 4) for a, v in self.ledger.realized_pnl_by_asset().items()}
        open_orders = sum(1 for o in self.fill_sim.orders.values() if o.is_open())
        hedge = self.ledger.hedge_summary()
        logger.info(
            "PNL_SUMMARY realized_total=%.4f settled_trades=%d open_orders=%d "
            "pending_resolution=%d by_asset=%s hedge_trades=%d hedge_pnl=%.4f "
            "normal_trades=%d normal_pnl=%.4f",
            self.ledger.realized_pnl(), len(self.ledger.records), open_orders,
            len(self.pending_resolution), by_asset,
            hedge["hedge_trades"], hedge["hedge_pnl"], hedge["normal_trades"], hedge["normal_pnl"],
        )

    # -- main loop -----------------------------------------------------

    async def run_forever(self, tick_seconds: float = 2.0) -> None:
        """
        Runs until asked to stop via SIGTERM/SIGINT (both handled the same
        way here, since Railway -- and most container platforms -- send
        SIGTERM on redeploy/stop, not SIGINT). Shutdown is prompt (interrupts
        the tick sleep immediately) and always saves the ledger before
        returning, so a redeploy doesn't lose recent settlements that
        haven't hit a natural save point yet.
        """
        stop_event = asyncio.Event()

        def _request_stop(sig_name: str) -> None:
            logger.info("Received %s; shutting down gracefully.", sig_name)
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_stop, sig.name)
            except NotImplementedError:
                pass  # e.g. Windows -- SIGINT still surfaces as KeyboardInterrupt there

        # Run one discovery pass BEFORE opening the WebSocket, so the
        # connection has real token ids to subscribe with from the moment
        # it opens rather than sitting connected with an empty
        # subscription while the first poll is still in flight. Without
        # this there's a race where the WS connects, sends nothing (no
        # markets discovered yet), and the server closes it as an invalid/
        # empty subscription -- which then repeats every reconnect until
        # discovery happens to win the race.
        await self.discovery_tick()

        ws_task = asyncio.create_task(self._ws_supervisor())
        try:
            while not stop_event.is_set():
                now = time.time()
                await self.discovery_tick(now)
                await self.strategy_tick(now)
                self.manage_orders_tick(now)
                await self.resolution_tick(now)
                if now - self._last_pnl_summary_at >= config.PNL_SUMMARY_INTERVAL_SECONDS:
                    self.log_pnl_summary(now)
                    self._last_pnl_summary_at = now
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            ws_task.cancel()
            self.ledger.save()
            self.log_pnl_summary()
            logger.info("Shut down cleanly.")

    async def _ws_supervisor(self) -> None:
        """Reconnects the WebSocket with backoff if it drops. A dropped
        connection halts nothing by itself -- book state simply goes stale,
        which the availability check (spread/depth) will naturally reject."""
        backoff = 1.0
        while True:
            try:
                await self.ws_client.connect_and_run()
                backoff = 1.0
            except Exception:
                logger.exception("WS connection dropped; reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", nargs="*", default=None,
                         help="restrict to a subset of assets, e.g. --assets Bitcoin BNB")
    parser.add_argument("--queue-safety-factor", type=float, default=config.QUEUE_SAFETY_FACTOR)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    # stream=sys.stdout (rather than logging's default stderr) so this
    # reads cleanly in whatever's capturing it -- Railway's log viewer,
    # or journald under a systemd unit (StandardOutput=journal /
    # StandardError=journal captures both either way, but stdout orders
    # more predictably for a single-process worker on both).
    logging.basicConfig(level=args.log_level, stream=sys.stdout,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bot = PaperBot(assets=args.assets, queue_safety_factor=args.queue_safety_factor,
                    seed=args.seed)
    logger.info("Starting paperbot in PAPER mode (assets=%s, queue_safety_factor=%.3f)",
                bot.assets, args.queue_safety_factor)
    try:
        asyncio.run(bot.run_forever())
    except KeyboardInterrupt:
        # Fallback path only -- run_forever installs its own SIGINT/SIGTERM
        # handlers and normally returns cleanly (ledger already saved)
        # before this would ever fire.
        logger.info("Shutting down. Realized PnL: %.4f", bot.ledger.realized_pnl())


if __name__ == "__main__":
    main()
