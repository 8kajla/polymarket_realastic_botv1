import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from paperbot.book import BookState, MarketWebSocketClient


class TestBookStateSnapshot:
    def test_apply_snapshot_sorts_and_filters_zero_size(self):
        book = BookState(token_id="t")
        book.apply_snapshot(
            bids=[(0.19, 5), (0.20, 10), (0.18, 0)],
            asks=[(0.25, 3), (0.22, 7)],
        )
        assert book.best_bid == 0.20
        assert book.best_ask == 0.22
        assert book.bids == [(0.20, 10.0), (0.19, 5.0)]
        assert book.asks == [(0.22, 7.0), (0.25, 3.0)]

    def test_spread_and_mid(self):
        book = BookState(token_id="t")
        book.apply_snapshot(bids=[(0.20, 10)], asks=[(0.24, 10)])
        assert book.spread == pytest.approx(0.04)
        assert book.mid() == pytest.approx(0.22)

    def test_depth_shares_at_or_better_bid(self):
        book = BookState(token_id="t")
        book.apply_snapshot(bids=[(0.20, 10), (0.19, 5), (0.18, 3)], asks=[(0.25, 1)])
        assert book.depth_shares_at_or_better("bid", 0.19) == 15.0
        assert book.depth_shares_at_or_better("bid", 0.20) == 10.0
        assert book.depth_shares_at_or_better("bid", 0.10) == 18.0

    def test_apply_snapshot_handles_real_polymarket_object_shaped_levels(self):
        """Direct regression test for a live-confirmed bug: Polymarket
        represents each level as {"price": ..., "size": ...} (often as
        strings), NOT a [price, size] pair. An earlier version did
        `for p, s in bids`, which -- since unpacking a dict binds its KEYS
        -- silently bound p="price", s="size" and then crashed on
        float("size")."""
        book = BookState(token_id="t")
        book.apply_snapshot(
            bids=[{"price": "0.20", "size": "10"}, {"price": "0.19", "size": "5"},
                  {"price": "0.18", "size": "0"}],
            asks=[{"price": "0.22", "size": "7"}, {"price": "0.25", "size": "3"}],
        )
        assert book.best_bid == 0.20
        assert book.best_ask == 0.22
        assert book.bids == [(0.20, 10.0), (0.19, 5.0)]
        assert book.asks == [(0.22, 7.0), (0.25, 3.0)]


class TestPriceChange:
    def test_new_bid_level_inserted_and_sorted(self):
        book = BookState(token_id="t")
        book.apply_snapshot(bids=[(0.20, 10)], asks=[(0.22, 10)])
        book.apply_price_change(price=0.21, size=4, side="BUY")
        assert book.bids[0] == (0.21, 4.0)
        assert book.best_bid == 0.21

    def test_zero_size_removes_level(self):
        book = BookState(token_id="t")
        book.apply_snapshot(bids=[(0.20, 10), (0.19, 5)], asks=[(0.22, 10)])
        book.apply_price_change(price=0.20, size=0, side="BUY")
        assert book.best_bid == 0.19

    def test_sell_side_updates_asks(self):
        book = BookState(token_id="t")
        book.apply_snapshot(bids=[(0.20, 10)], asks=[(0.22, 10)])
        book.apply_price_change(price=0.21, size=3, side="SELL")
        assert book.best_ask == 0.21

    def test_matching_best_price_does_not_flag_a_resync(self):
        book = BookState(token_id="t")
        book.apply_snapshot(bids=[(0.20, 10)], asks=[(0.22, 10)])
        # The message's own asserted best matches our own top-of-book level
        # exactly -- no missed update, no resync needed.
        book.apply_price_change(price=0.20, size=15, side="BUY", best_bid=0.20, best_ask=0.22)
        assert book.needs_resync is False

    def test_mismatched_best_bid_flags_a_resync(self):
        """FIXED 2026-09-12 (bug audit #6): a price_change message
        asserting a best_bid we have no matching level for (a missed
        earlier update) used to be a silent no-op -- best_bid/best_ask
        would keep reading the stale value with no bounded correction
        path. Must now flag needs_resync so bot.py can self-heal via a
        REST re-fetch."""
        book = BookState(token_id="t")
        book.apply_snapshot(bids=[(0.20, 10)], asks=[(0.22, 10)])
        # Asserts a best_bid (0.23) we have no matching level for at all.
        book.apply_price_change(price=0.21, size=5, side="BUY", best_bid=0.23, best_ask=0.22)
        assert book.needs_resync is True
        # And, just as importantly, best_bid/best_ask themselves are NOT
        # fabricated -- no sizeless synthetic level gets inserted.
        assert book.best_bid == 0.21

    def test_mismatched_best_ask_flags_a_resync(self):
        book = BookState(token_id="t")
        book.apply_snapshot(bids=[(0.20, 10)], asks=[(0.22, 10)])
        book.apply_price_change(price=0.21, size=5, side="SELL", best_bid=0.20, best_ask=0.19)
        assert book.needs_resync is True

    def test_no_best_bid_ask_in_message_never_flags_a_resync(self):
        book = BookState(token_id="t")
        book.apply_snapshot(bids=[(0.20, 10)], asks=[(0.22, 10)])
        book.apply_price_change(price=0.21, size=5, side="BUY")  # no best_bid/best_ask kwargs
        assert book.needs_resync is False

    def test_apply_snapshot_clears_a_pending_resync_flag(self):
        book = BookState(token_id="t")
        book.apply_snapshot(bids=[(0.20, 10)], asks=[(0.22, 10)])
        book.apply_price_change(price=0.21, size=5, side="BUY", best_bid=0.23, best_ask=0.22)
        assert book.needs_resync is True

        book.apply_snapshot(bids=[(0.23, 8)], asks=[(0.24, 8)])
        assert book.needs_resync is False


class TestLastTradePriceAndTickSize:
    def test_last_trade_price_queues_a_trade_print(self):
        book = BookState(token_id="t")
        book.apply_last_trade_price(price=0.20, size=5, side="SELL", ts=123.0)
        assert book.last_trade_price == 0.20
        trades = book.drain_pending_trades()
        assert len(trades) == 1
        assert trades[0].size == 5.0
        assert book.drain_pending_trades() == []  # drained, not re-delivered

    def test_tick_size_change_updates_state(self):
        book = BookState(token_id="t")
        book.apply_tick_size_change(0.001)
        assert book.tick_size == 0.001


class TestWebSocketMessageRouting:
    def test_book_message_routes_to_correct_token(self):
        states = {"tok-a": BookState(token_id="tok-a"), "tok-b": BookState(token_id="tok-b")}
        client = MarketWebSocketClient(get_book_state=states.get)
        msg = json.dumps({
            "event_type": "book", "asset_id": "tok-a",
            "bids": [[0.2, 5]], "asks": [[0.3, 5]],
        })
        client.route_message(msg)
        assert states["tok-a"].best_bid == 0.2
        assert states["tok-b"].best_bid is None

    def test_price_change_message_updates_book(self):
        states = {"tok-a": BookState(token_id="tok-a")}
        states["tok-a"].apply_snapshot(bids=[(0.2, 5)], asks=[(0.3, 5)])
        client = MarketWebSocketClient(get_book_state=states.get)
        msg = json.dumps({
            "event_type": "price_change", "asset_id": "tok-a",
            "price_changes": [{"price": 0.21, "size": 3, "side": "BUY"}],
        })
        client.route_message(msg)
        assert states["tok-a"].best_bid == 0.21

    def test_last_trade_price_message_queues_trade(self):
        states = {"tok-a": BookState(token_id="tok-a")}
        client = MarketWebSocketClient(get_book_state=states.get)
        msg = json.dumps({
            "event_type": "last_trade_price", "asset_id": "tok-a",
            "price": 0.18, "size": 2, "side": "SELL",
        })
        client.route_message(msg)
        trades = states["tok-a"].drain_pending_trades()
        assert len(trades) == 1
        assert trades[0].price == 0.18

    def test_tick_size_change_message(self):
        states = {"tok-a": BookState(token_id="tok-a")}
        client = MarketWebSocketClient(get_book_state=states.get)
        msg = json.dumps({
            "event_type": "tick_size_change", "asset_id": "tok-a",
            "old_tick_size": 0.01, "new_tick_size": 0.001,
        })
        client.route_message(msg)
        assert states["tok-a"].tick_size == 0.001

    def test_unknown_token_is_ignored_without_error(self):
        client = MarketWebSocketClient(get_book_state=lambda tid: None)
        msg = json.dumps({"event_type": "book", "asset_id": "unknown", "bids": [], "asks": []})
        client.route_message(msg)  # should not raise

    def test_pong_and_malformed_messages_are_ignored(self):
        client = MarketWebSocketClient(get_book_state=lambda tid: None)
        client.route_message("PONG")
        client.route_message("not json{{{")


class TestSubscribeNeverSendsASecondMessageOnALiveConnection:
    """Direct regression test for a live-confirmed bug: sending an
    incremental subscribe message on an already-subscribed connection got
    the connection closed by the server with a plain-text "INVALID
    OPERATION" response. subscribe() must record new ids and, if already
    connected, close the connection instead of sending anything more on
    it -- the next reconnect sends one consolidated message."""

    def test_subscribing_before_connected_just_records_ids(self):
        client = MarketWebSocketClient(get_book_state=lambda tid: None)
        asyncio.run(client.subscribe(["a", "b"]))
        assert client._subscribed_ids == {"a", "b"}
        assert client._ws is None  # nothing to send to yet, nothing sent

    def test_new_ids_after_connecting_close_instead_of_sending_again(self):
        client = MarketWebSocketClient(get_book_state=lambda tid: None)
        fake_ws = AsyncMock()
        client._ws = fake_ws
        client._subscribed_ids = {"a"}

        asyncio.run(client.subscribe(["a", "b"]))  # "b" is new

        fake_ws.close.assert_awaited_once()
        fake_ws.send.assert_not_awaited()
        assert client._subscribed_ids == {"a", "b"}

    def test_no_new_ids_does_not_touch_the_connection(self):
        client = MarketWebSocketClient(get_book_state=lambda tid: None)
        fake_ws = AsyncMock()
        client._ws = fake_ws
        client._subscribed_ids = {"a", "b"}

        asyncio.run(client.subscribe(["a", "b"]))  # nothing new

        fake_ws.close.assert_not_awaited()
        fake_ws.send.assert_not_awaited()
