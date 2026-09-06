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
    fetch_market_by_slug,
    parse_resolution,
)
from .strategy import MarketActivityState, build_order_intent, safe_postonly_price, would_cross_spread

logger = logging.getLogger("paperbot.bot")


class PaperBot:
    def __init__(self, assets: list[str] | None = None,
                 queue_safety_factor: float = config.QUEUE_SAFETY_FACTOR,
                 seed: int | None = None):
        # get_trading_mode() raises if anything but paper mode is requested;
        # calling it here means a misconfigured LIVE attempt fails at
        # construction time, before any market state is even touched.
        config.get_trading_mode()

        self.assets = assets or config.ALL_ASSETS
        self.session = requests.Session()
        self.discovery = MarketDiscovery(session=self.session)
        self.fill_sim = FillSimulator(queue_safety_factor=queue_safety_factor)
        self.ledger = Ledger.load()
        self.rng = random.Random(seed)

        self.book_states: dict[str, BookState] = {}          # token_id -> BookState
        self.activity: dict[str, MarketActivityState] = {}    # condition_id -> state
        self.markets_by_condition: dict[str, Market] = {}
        self.resting_order_id: dict[str, int] = {}            # condition_id -> order_id
        self.halted_conditions: set[str] = set()
        self.pending_resolution: dict[str, Market] = {}
        self.last_price_by_token: dict[str, float] = {}
        self._last_price_delta: dict[str, float] = {}

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

        for cid in new_conditions:
            market = active[cid]
            await self._onboard_market(market)

        for cid in dropped_conditions:
            await self._retire_market(self.markets_by_condition[cid])

        self.markets_by_condition = active

    async def _onboard_market(self, market: Market) -> None:
        logger.info("ONBOARD market=%s asset=%s condition=%s", market.slug, market.asset,
                    market.condition_id)
        self.activity[market.condition_id] = MarketActivityState()
        for token_id in (market.token_id_up, market.token_id_down):
            try:
                state = await asyncio.to_thread(bootstrap_book_state, token_id, self.session)
            except Exception:
                logger.exception("failed to bootstrap book for token %s (market %s); halting it",
                                  token_id, market.slug)
                self.halted_conditions.add(market.condition_id)
                continue
            self.book_states[token_id] = state
        await self.ws_client.subscribe([market.token_id_up, market.token_id_down])

    async def _retire_market(self, market: Market) -> None:
        logger.info("RETIRE market=%s asset=%s condition=%s", market.slug, market.asset,
                    market.condition_id)
        self.pending_resolution[market.condition_id] = market
        self.book_states.pop(market.token_id_up, None)
        self.book_states.pop(market.token_id_down, None)

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
            if cid in self.halted_conditions or cid in self.resting_order_id:
                continue
            try:
                self._evaluate_one_market(market, now)
            except Exception:
                logger.exception("unexpected error evaluating market %s; halting its activity",
                                  market.slug)
                self.halted_conditions.add(cid)

    def _evaluate_one_market(self, market: Market, now: float) -> None:
        # We evaluate against the "Up" token's book; the Down token is a
        # mirror (price_down ~= 1 - price_up) and shares the same regime
        # dynamics for our purposes, so one side is enough to decide
        # in/out-of-band and liquidity.
        book = self.book_states.get(market.token_id_up)
        if book is None or book.best_bid is None:
            return

        activity = self.activity[market.condition_id]
        delta = self._recent_price_delta(market.token_id_up, book.best_bid)

        intent = build_order_intent(market, book, activity, self.rng,
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

        order = self.fill_sim.place_order(intent, order_book, now=now)
        self.resting_order_id[market.condition_id] = order.order_id
        activity.record_entry(intent.side)

    # -- order management -------------------------------------------------

    def manage_orders_tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()

        # Drain WS-delivered trade prints into the fill simulator.
        for token_id, book in self.book_states.items():
            for trade in book.drain_pending_trades():
                try:
                    self.fill_sim.on_trade_print(token_id, trade)
                except Exception:
                    logger.exception("error consuming trade print for token %s", token_id)

        seconds_remaining = {
            cid: m.seconds_remaining(now) for cid, m in self.markets_by_condition.items()
        }
        self.fill_sim.manage_open_orders(self.book_states, seconds_remaining, now=now)

        # Unconditional sweep rather than only reacting to manage_open_orders'
        # `changed` list: an order can also stop being open because a trade
        # print filled it during the drain loop above, which manage_open_orders
        # never sees. Checking every tracked order's actual current state is
        # the only way to keep this dict from going stale either way.
        for cid, order_id in list(self.resting_order_id.items()):
            order = self.fill_sim.orders.get(order_id)
            if order is None or not order.is_open():
                del self.resting_order_id[cid]

    # -- resolution / settlement -------------------------------------------

    async def resolution_tick(self) -> None:
        for cid, market in list(self.pending_resolution.items()):
            try:
                raw = await asyncio.to_thread(fetch_market_by_slug, market.slug, self.session)
            except Exception:
                logger.exception("resolution poll failed for market %s; will retry", market.slug)
                continue
            if raw is None:
                continue
            winning_side = parse_resolution(raw)
            if winning_side is None:
                continue  # not resolved yet, try again next tick

            for order in self.fill_sim.orders.values():
                if order.condition_id == cid and order.filled_size > 0:
                    self.ledger.settle_order(order, winning_side)
            self.ledger.save()
            del self.pending_resolution[cid]

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

        ws_task = asyncio.create_task(self._ws_supervisor())
        try:
            while not stop_event.is_set():
                await self.discovery_tick()
                await self.strategy_tick()
                self.manage_orders_tick()
                await self.resolution_tick()
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            ws_task.cancel()
            self.ledger.save()
            logger.info("Shut down cleanly. Realized PnL: %.4f", self.ledger.realized_pnl())

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
    # reads cleanly in Railway's log viewer, which surfaces both but
    # orders/labels stdout more predictably for a single-process worker.
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
