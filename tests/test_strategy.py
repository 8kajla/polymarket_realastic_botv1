import random

import pytest

from paperbot import behavior_config as bc
from paperbot import config
from paperbot.book import BookState
from paperbot.market_discovery import Market
from paperbot.strategy import (
    MarketActivityState,
    availability_check,
    build_order_intent,
    decide_side,
    decide_size,
    timing_ok,
)


def make_market(end_time, asset="Bitcoin", condition_id="c1", order_min_size=None):
    return Market(
        condition_id=condition_id, slug="btc-updown-5m-1000", question="q",
        asset=asset, end_time=end_time, token_id_up="up", token_id_down="down",
        order_min_size=order_min_size, order_min_tick_size=0.01,
    )


def make_liquid_book(price=0.20):
    book = BookState(token_id="up")
    book.apply_snapshot(bids=[(price, 100.0)], asks=[(price + 0.01, 100.0)])
    return book


class TestTimingGate:
    def test_skips_under_90_seconds(self):
        market = make_market(end_time=100.0)
        assert timing_ok(market, now=100.0 - 89) is False

    def test_allows_at_exactly_90_seconds(self):
        market = make_market(end_time=100.0)
        assert timing_ok(market, now=100.0 - 90) is True

    def test_allows_with_plenty_of_time(self):
        market = make_market(end_time=1000.0)
        assert timing_ok(market, now=0.0) is True


class TestAvailabilityCheck:
    def test_tight_liquid_book_passes(self):
        book = make_liquid_book()
        ok, _ = availability_check(book)
        assert ok is True

    def test_wide_spread_fails(self):
        book = BookState(token_id="up")
        book.apply_snapshot(bids=[(0.20, 100.0)], asks=[(0.40, 100.0)])
        ok, reason = availability_check(book)
        assert ok is False
        assert "spread" in reason

    def test_thin_depth_fails(self):
        book = BookState(token_id="up")
        book.apply_snapshot(bids=[(0.20, 1.0)], asks=[(0.21, 1.0)])
        ok, reason = availability_check(book)
        assert ok is False
        assert "depth" in reason

    def test_one_sided_book_fails(self):
        book = BookState(token_id="up")
        book.apply_snapshot(bids=[(0.20, 100.0)], asks=[])
        ok, _ = availability_check(book)
        assert ok is False

    def test_threshold_is_uniform_not_regime_scaled(self):
        """The same MIN_BOOK_DEPTH_USD must apply regardless of what
        regime the price happens to be in -- no per-band scaling."""
        cheap_book = make_liquid_book(price=0.10)
        high_book = make_liquid_book(price=0.95)
        ok_cheap, _ = availability_check(cheap_book)
        ok_high, _ = availability_check(high_book)
        assert ok_cheap is True
        assert ok_high is True


class TestSidePersistence:
    def test_no_prior_entry_picks_a_side(self):
        rng = random.Random(0)
        activity = MarketActivityState()
        side = decide_side("Bitcoin", activity, rng)
        assert side in ("Up", "Down")

    def test_strong_bias_toward_previous_side_over_many_draws(self):
        rng = random.Random(42)
        activity = MarketActivityState(last_side="Up")
        same_count = sum(
            1 for _ in range(2000) if decide_side("BNB", activity, rng) == "Up"
        )
        expected = bc.SIDE_PERSISTENCE["BNB"]
        observed = same_count / 2000
        assert abs(observed - expected) < 0.03


class TestSizingDecision:
    def test_bitcoin_never_rolls_floor_lot(self):
        rng = random.Random(1)
        for _ in range(500):
            _, is_floor_lot = decide_size("Bitcoin", "CHEAP", "first", 0.2, rng)
            assert is_floor_lot is False

    def test_hyperliquid_floor_lot_frequency_rises_with_position(self):
        rng = random.Random(7)
        n = 4000

        def floor_rate(position_tier):
            hits = sum(
                1 for _ in range(n)
                if decide_size("Hyperliquid", "MID", position_tier, 0.5, rng)[1]
            )
            return hits / n

        first_rate = floor_rate("first")
        mid_rate = floor_rate("2nd_3rd")
        last_rate = floor_rate("4th_plus")
        assert first_rate < mid_rate < last_rate

    def test_non_floor_lot_size_is_near_the_confirmed_median(self):
        rng = random.Random(3)
        notional, is_floor_lot = decide_size("Bitcoin", "CORE", "first", 0.8, rng)
        assert is_floor_lot is False
        median = bc.median_entry_notional("Bitcoin", "CORE", "first")
        assert median * 0.8 <= notional <= median * 1.2


class TestBuildOrderIntent:
    def test_returns_none_under_timing_cutoff(self):
        market = make_market(end_time=50.0)
        book = make_liquid_book()
        activity = MarketActivityState()
        intent = build_order_intent(market, book, activity, random.Random(0), now=0.0)
        assert intent is None

    def test_returns_none_on_illiquid_book(self):
        market = make_market(end_time=1000.0)
        book = BookState(token_id="up")
        book.apply_snapshot(bids=[(0.2, 0.5)], asks=[(0.6, 0.5)])
        activity = MarketActivityState()
        intent = build_order_intent(market, book, activity, random.Random(0), now=0.0)
        assert intent is None

    def test_produces_a_valid_intent_when_everything_lines_up(self):
        market = make_market(end_time=1000.0)
        book = make_liquid_book(price=0.20)
        activity = MarketActivityState()
        intent = build_order_intent(market, book, activity, random.Random(0), now=0.0)
        assert intent is not None
        assert intent.regime == "CHEAP"
        assert intent.side in ("Up", "Down")
        assert intent.price == 0.20
        assert intent.size_shares > 0

    def test_respects_runtime_minimum_order_size(self):
        """A floor-lot-sized notional below the market's live-fetched
        minimum order size must not produce an order."""
        market = make_market(end_time=1000.0, asset="Hyperliquid",
                              order_min_size=1000.0)  # absurdly high on purpose
        book = make_liquid_book(price=0.20)
        activity = MarketActivityState()
        # Even the non-floor-lot median at this regime is far below 1000
        # shares worth, so every attempt should be rejected.
        intent = build_order_intent(market, book, activity, random.Random(0), now=0.0)
        assert intent is None
