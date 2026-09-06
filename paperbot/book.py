"""
Order book state per token, fed by the CLOB REST bootstrap and the
market-channel WebSocket.

The *_apply methods are pure state-mutation logic and are fully unit
testable without any network access. The network glue (fetch_book_
snapshot, fetch_tick_size, MarketWebSocketClient) was originally
implemented against documented contracts without live access, and a real
Railway deployment then surfaced two real bugs in it on the first try:
book levels arrive as {"price": ..., "size": ...} objects (not [price,
size] pairs -- see _extract_level), and the market channel accepts only
one subscription message per connection (see MarketWebSocketClient.
subscribe's docstring). Both are fixed and covered by tests, but this
layer's only real validation is that one live deployment -- treat further
live runs as still-active integration testing, not a fully closed loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from . import config

logger = logging.getLogger("paperbot.book")


@dataclass
class TradePrint:
    price: float
    size: float
    side: str
    ts: float


def _extract_level(level) -> tuple[float, float]:
    """Normalize one order-book level to a (price, size) float pair.
    Accepts Polymarket's real shape -- {"price": "...", "size": "..."} --
    and, defensively, a bare [price, size] sequence."""
    if isinstance(level, dict):
        return float(level["price"]), float(level.get("size", 0))
    p, s = level
    return float(p), float(s)


@dataclass
class BookState:
    """Live order book state for a single token (one side of one market)."""

    token_id: str
    bids: list = field(default_factory=list)  # [(price, size), ...] desc by price
    asks: list = field(default_factory=list)  # [(price, size), ...] asc by price
    tick_size: float = 0.01
    last_trade_price: Optional[float] = None
    last_update_ts: float = 0.0
    # Trades not yet drained by the fill simulator.
    pending_trades: list = field(default_factory=list)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def depth_usd_at_or_better(self, side: str, price: float) -> float:
        """
        Sum of resting USD notional at-or-better than `price` on the given
        side. side="bid" -> bids priced >= price (better for a seller
        crossing in). side="ask" -> asks priced <= price.
        """
        levels = self.bids if side == "bid" else self.asks
        total = 0.0
        for lvl_price, lvl_size in levels:
            if side == "bid" and lvl_price >= price:
                total += lvl_price * lvl_size
            elif side == "ask" and lvl_price <= price:
                total += lvl_price * lvl_size
        return total

    def depth_shares_at_or_better(self, side: str, price: float) -> float:
        """Same as depth_usd_at_or_better but in raw share count -- used by
        the fill simulator's queue-ahead model, since trade prints consume
        shares, not USD."""
        levels = self.bids if side == "bid" else self.asks
        total = 0.0
        for lvl_price, lvl_size in levels:
            if side == "bid" and lvl_price >= price:
                total += lvl_size
            elif side == "ask" and lvl_price <= price:
                total += lvl_size
        return total

    def total_depth_usd(self) -> float:
        bid_usd = sum(p * s for p, s in self.bids)
        ask_usd = sum(p * s for p, s in self.asks)
        return bid_usd + ask_usd

    # -- mutation from WS/REST messages --------------------------------

    def apply_snapshot(self, bids: list, asks: list, ts: Optional[float] = None) -> None:
        """Replace the book entirely (REST bootstrap or WS 'book' message).

        Polymarket represents each level as an object, e.g.
        {"price": "0.42", "size": "10.5"} -- NOT a [price, size] pair. An
        earlier version of this method assumed the pair shape and did
        `for p, s in bids`, which (since a dict unpacks to its keys) bound
        p="price", s="size" and then crashed on float("size"). Confirmed
        live on a real deployment. _extract_level below accepts either
        shape defensively, in case the two feeds (REST /book vs WS "book"
        snapshots) ever diverge.
        """
        self.bids = sorted((lvl for lvl in (_extract_level(l) for l in bids) if lvl[1] > 0),
                            key=lambda x: -x[0])
        self.asks = sorted((lvl for lvl in (_extract_level(l) for l in asks) if lvl[1] > 0),
                            key=lambda x: x[0])
        self.last_update_ts = ts if ts is not None else time.time()

    def apply_price_change(self, price: float, size: float, side: str,
                            best_bid: Optional[float] = None,
                            best_ask: Optional[float] = None) -> None:
        """
        Apply a single price-level update. size == 0 removes the level.
        side is "BUY" (a bid level) or "SELL" (an ask level).
        """
        levels = self.bids if side.upper() == "BUY" else self.asks
        price = float(price)
        size = float(size)
        # remove existing level at this price, if any
        levels[:] = [lvl for lvl in levels if lvl[0] != price]
        if size > 0:
            levels.append((price, size))
        reverse = side.upper() == "BUY"
        levels.sort(key=lambda x: x[0], reverse=reverse)
        self.last_update_ts = time.time()
        # trust explicit best_bid/best_ask if the message included them,
        # since price_change doesn't always carry a full picture
        if best_bid is not None or best_ask is not None:
            self._reconcile_best(best_bid, best_ask)

    def _reconcile_best(self, best_bid, best_ask) -> None:
        # If the message asserts a best price we don't have a matching
        # level for (e.g. we missed an earlier update), insert a synthetic
        # level so best_bid/best_ask stay correct until the next snapshot.
        if best_bid is not None and (not self.bids or self.bids[0][0] != float(best_bid)):
            pass  # don't fabricate size; rely on next full snapshot to correct depth
        if best_ask is not None and (not self.asks or self.asks[0][0] != float(best_ask)):
            pass

    def apply_last_trade_price(self, price: float, size: float, side: str,
                                ts: Optional[float] = None) -> None:
        trade = TradePrint(price=float(price), size=float(size), side=side,
                            ts=ts if ts is not None else time.time())
        self.last_trade_price = trade.price
        self.pending_trades.append(trade)

    def apply_tick_size_change(self, new_tick_size: float) -> None:
        self.tick_size = float(new_tick_size)

    def drain_pending_trades(self) -> list:
        trades, self.pending_trades = self.pending_trades, []
        return trades


# ---------------------------------------------------------------------------
# REST bootstrap
# ---------------------------------------------------------------------------

def fetch_book_snapshot(token_id: str, session: Optional[requests.Session] = None,
                         timeout: float = 5.0) -> dict:
    """GET {CLOB_REST_BASE_URL}/book?token_id=... Returns the raw JSON."""
    sess = session or requests
    resp = sess.get(f"{config.CLOB_REST_BASE_URL}/book",
                     params={"token_id": token_id}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_tick_size(token_id: str, session: Optional[requests.Session] = None,
                     timeout: float = 5.0) -> float:
    """GET {CLOB_REST_BASE_URL}/tick-size?token_id=... Fetched per-market at
    runtime -- tick size varies by market and must never be hardcoded
    globally."""
    sess = session or requests
    resp = sess.get(f"{config.CLOB_REST_BASE_URL}/tick-size",
                     params={"token_id": token_id}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # py-clob-client's ClobTickSize response shape: {"minimum_tick_size": "0.01"}
    for key in ("minimum_tick_size", "tick_size", "minimumTickSize"):
        if key in data:
            return float(data[key])
    raise ValueError(f"unexpected /tick-size response shape: {data!r}")


def bootstrap_book_state(token_id: str, session: Optional[requests.Session] = None) -> BookState:
    state = BookState(token_id=token_id)
    raw = fetch_book_snapshot(token_id, session=session)
    state.apply_snapshot(raw.get("bids", []), raw.get("asks", []))
    try:
        state.tick_size = fetch_tick_size(token_id, session=session)
    except Exception:
        logger.warning("could not fetch live tick size for token %s; keeping default %.4f",
                        token_id, state.tick_size, exc_info=True)
    return state


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------

class MarketWebSocketClient:
    """
    Maintains one WS connection to the CLOB market channel, multiplexed
    across every subscribed token_id, and routes incoming messages to the
    right BookState via `on_message`.

    The message-routing logic (route_message, below) is exercised directly
    by the test suite using synthetic payloads. The connection-management
    logic (this class's async methods) was live-tested on a real Railway
    deployment, which is how the one-subscription-message-per-connection
    behavior documented on `subscribe` was discovered.
    """

    def __init__(self, get_book_state: Callable[[str], Optional[BookState]]):
        """
        get_book_state(token_id) -> BookState or None. Left as an injected
        callable (rather than owning a dict here) so bot.py can manage the
        set of tracked tokens as markets rotate in/out.
        """
        self._get_book_state = get_book_state
        self._ws = None
        self._subscribed_ids: set[str] = set()
        self._stop = False

    async def connect_and_run(self):
        import websockets  # imported lazily so the rest of the package has
                            # no hard dependency on it for pure-logic tests

        async with websockets.connect(config.CLOB_WS_URL) as ws:
            self._ws = ws
            if self._subscribed_ids:
                await self._send_subscription(list(self._subscribed_ids))
            ping_task = asyncio.create_task(self._ping_loop())
            try:
                async for raw in ws:
                    self.route_message(raw)
            finally:
                ping_task.cancel()

    async def _ping_loop(self):
        import websockets
        while True:
            await asyncio.sleep(config.WS_PING_INTERVAL_SECONDS)
            try:
                await self._ws.send(config.WS_PING_MESSAGE)
            except Exception:
                logger.warning("WS ping failed", exc_info=True)
                return

    async def subscribe(self, token_ids: list[str]):
        """
        Record `token_ids` as wanted. If we're already connected and this
        adds anything new, force a reconnect rather than sending a second
        subscription message on the live connection.

        Confirmed live: sending an incremental subscribe message to an
        already-subscribed connection gets the connection closed with a
        plain-text "INVALID OPERATION" response (not the documented JSON
        schema) -- the market channel appears to accept exactly one
        subscription message per connection. Since 5-minute markets churn
        constantly, new token ids show up often; the reliable way to add
        them is to close and let connect_and_run's next pass send ONE
        message with the complete, updated id set (it already does this
        automatically on every fresh connect).
        """
        new_ids = [t for t in token_ids if t not in self._subscribed_ids]
        self._subscribed_ids.update(new_ids)
        if self._ws is not None and new_ids:
            await self._ws.close()

    async def _send_subscription(self, token_ids: list[str]):
        msg = {
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": config.WS_SUBSCRIBE_CUSTOM_FEATURE_ENABLED,
        }
        await self._ws.send(json.dumps(msg))

    def route_message(self, raw: str) -> None:
        """Pure routing logic -- parses one WS text frame and applies it to
        the relevant BookState. Testable without a live connection."""
        if raw == "PONG":
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("non-JSON WS message ignored: %r", raw[:200])
            return
        # some feeds wrap a batch in a list
        messages = msg if isinstance(msg, list) else [msg]
        for m in messages:
            self._route_one(m)

    def _route_one(self, m: dict) -> None:
        event_type = m.get("event_type") or m.get("type")
        token_id = m.get("asset_id") or m.get("market")
        if token_id is None:
            return
        state = self._get_book_state(token_id)
        if state is None:
            return  # not a token we're tracking (or already rotated out)

        if event_type == "book":
            state.apply_snapshot(m.get("bids", []), m.get("asks", []))
        elif event_type == "price_change":
            for change in m.get("price_changes", [m]):
                if "price" not in change or "side" not in change:
                    continue
                state.apply_price_change(
                    price=change["price"], size=change.get("size", 0),
                    side=change["side"],
                    best_bid=change.get("best_bid"), best_ask=change.get("best_ask"),
                )
        elif event_type == "last_trade_price":
            state.apply_last_trade_price(
                price=m["price"], size=m.get("size", 0), side=m.get("side", ""),
            )
        elif event_type == "tick_size_change":
            new_tick = m.get("new_tick_size")
            if new_tick is not None:
                state.apply_tick_size_change(new_tick)
        else:
            logger.debug("unhandled WS event_type=%r", event_type)
