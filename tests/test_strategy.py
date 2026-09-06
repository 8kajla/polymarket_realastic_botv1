import random

import pytest

from paperbot import behavior_config as bc
from paperbot import config
from paperbot import strategy as stratmod
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


def make_liquid_book(price=0.20, token_id="up"):
    book = BookState(token_id=token_id)
    book.apply_snapshot(bids=[(price, 100.0)], asks=[(round(price + 0.01, 6), 100.0)])
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
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is None

    def test_returns_none_on_illiquid_book(self):
        market = make_market(end_time=1000.0)
        up_book = BookState(token_id="up")
        up_book.apply_snapshot(bids=[(0.2, 0.5)], asks=[(0.6, 0.5)])
        down_book = BookState(token_id="down")
        down_book.apply_snapshot(bids=[(0.79, 0.5)], asks=[(0.81, 0.5)])
        activity = MarketActivityState()
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is None

    def test_produces_a_valid_intent_when_everything_lines_up(self):
        market = make_market(end_time=1000.0)
        # Both sides land in CHEAP so the assertion holds regardless of
        # which side decide_side happens to pick.
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.25, token_id="down")
        activity = MarketActivityState()
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is not None
        assert intent.regime == "CHEAP"
        assert intent.side in ("Up", "Down")
        assert intent.price in (0.20, 0.25)
        assert intent.size_shares > 0

    def test_regime_price_and_sizing_come_from_the_chosen_sides_book(self, monkeypatch):
        """Direct regression test for a live-confirmed bug: regime, price,
        and sizing were always computed from the Up token's book
        regardless of which side got chosen -- silently mis-classifying
        (and badly mis-sizing) every Down-side order whenever Up and Down
        landed in different regime bands, which is the common case since
        Down's price is roughly (1 - Up's price), not equal to it.
        Confirmed live: a HIGH-band Up price (0.95) produced a Down order
        that actually filled around Down's real ~0.03 price -- a
        HIGH-regime notional divided by a CHEAP-regime price, yielding an
        absurd 1354-share order instead of a normal small CHEAP-band one.
        """
        market = make_market(end_time=1000.0)
        up_book = make_liquid_book(price=0.95, token_id="up")      # HIGH band
        down_book = make_liquid_book(price=0.04, token_id="down")  # CHEAP band
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.side == "Down"
        assert intent.price == 0.04
        assert intent.regime == "CHEAP"  # not "HIGH", which up_book's 0.95 would wrongly give
        # sized off the CHEAP curve at Down's real price, not a HIGH notional / 0.04
        cheap_median = bc.median_entry_notional(market.asset, "CHEAP", "first")
        assert intent.notional_usd < cheap_median * 2  # generous jitter allowance

    def test_respects_runtime_minimum_order_size(self):
        """A floor-lot-sized notional below the market's live-fetched
        minimum order size must not produce an order."""
        market = make_market(end_time=1000.0, asset="Hyperliquid",
                              order_min_size=1000.0)  # absurdly high on purpose
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()
        # Even the non-floor-lot median at this regime is far below 1000
        # shares worth, so every attempt should be rejected.
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is None
