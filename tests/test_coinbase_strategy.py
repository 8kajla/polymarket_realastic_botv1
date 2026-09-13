import pytest

from paperbot import config
from paperbot.book import BookState
from paperbot.coinbase_strategy import build_momentum_order_intent, decide_momentum_side
from paperbot.market_discovery import Market


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


class TestDecideMomentumSide:
    def test_noop_outside_cheap_mid(self):
        assert decide_momentum_side("CORE", 0.01) is None
        assert decide_momentum_side("HIGH", 0.01) is None

    def test_noop_when_return_is_none(self):
        assert decide_momentum_side("CHEAP", None) is None
        assert decide_momentum_side("MID", None) is None

    def test_noop_inside_the_deadband(self):
        just_inside = config.MOMENTUM_MIN_MOVE * 0.5
        assert decide_momentum_side("CHEAP", just_inside) is None
        assert decide_momentum_side("MID", -just_inside) is None

    def test_noop_exactly_at_the_deadband_boundary(self):
        # Strict inequality (abs(ret) < MIN_MOVE is a no-op) -- exactly at
        # the boundary should be actionable, not excluded.
        assert decide_momentum_side("CHEAP", config.MOMENTUM_MIN_MOVE) == "Up"
        assert decide_momentum_side("MID", -config.MOMENTUM_MIN_MOVE) == "Down"

    def test_positive_return_picks_up_in_cheap_and_mid(self):
        move = config.MOMENTUM_MIN_MOVE * 5
        assert decide_momentum_side("CHEAP", move) == "Up"
        assert decide_momentum_side("MID", move) == "Up"

    def test_negative_return_picks_down_in_cheap_and_mid(self):
        move = -config.MOMENTUM_MIN_MOVE * 5
        assert decide_momentum_side("CHEAP", move) == "Down"
        assert decide_momentum_side("MID", move) == "Down"


class TestBuildMomentumOrderIntent:
    def test_returns_none_before_the_timing_gate(self):
        market = make_market(end_time=config.MIN_SECONDS_BEFORE_CLOSE - 1.0)  # already too close
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")
        intent = build_momentum_order_intent(market, up_book, down_book, 0.01, now=0.0)
        assert intent is None

    def test_returns_none_when_trailing_return_is_none(self):
        market = make_market(end_time=1000.0)
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")
        intent = build_momentum_order_intent(market, up_book, down_book, None, now=0.0)
        assert intent is None

    def test_positive_momentum_buys_up_in_cheap(self):
        market = make_market(end_time=1000.0)
        up_book = make_liquid_book(price=0.20, token_id="up")   # CHEAP
        down_book = make_liquid_book(price=0.80, token_id="down")
        move = config.MOMENTUM_MIN_MOVE * 5
        intent = build_momentum_order_intent(market, up_book, down_book, move, now=0.0)
        assert intent is not None
        assert intent.side == "Up"
        assert intent.regime == "CHEAP"
        assert intent.price == 0.20
        assert intent.is_hedge is False
        assert intent.is_floor_lot is False
        assert intent.reason == "coinbase_momentum"
        assert intent.notional_usd == pytest.approx(config.MOMENTUM_ORDER_NOTIONAL_USD)

    def test_negative_momentum_buys_down(self):
        market = make_market(end_time=1000.0)
        up_book = make_liquid_book(price=0.55, token_id="up")   # MID
        down_book = make_liquid_book(price=0.45, token_id="down")  # MID
        move = -config.MOMENTUM_MIN_MOVE * 5
        intent = build_momentum_order_intent(market, up_book, down_book, move, now=0.0)
        assert intent is not None
        assert intent.side == "Down"
        assert intent.regime == "MID"
        assert intent.price == 0.45

    def test_skips_core_and_high_even_with_a_real_move(self):
        market = make_market(end_time=1000.0)
        up_book = make_liquid_book(price=0.95, token_id="up")   # HIGH
        down_book = make_liquid_book(price=0.05, token_id="down")
        move = config.MOMENTUM_MIN_MOVE * 5
        intent = build_momentum_order_intent(market, up_book, down_book, move, now=0.0)
        assert intent is None

        up_book2 = make_liquid_book(price=0.75, token_id="up")  # CORE
        down_book2 = make_liquid_book(price=0.25, token_id="down")
        intent2 = build_momentum_order_intent(market, up_book2, down_book2, move, now=0.0)
        assert intent2 is None

    def test_skips_inside_the_deadband(self):
        market = make_market(end_time=1000.0)
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")
        flat = config.MOMENTUM_MIN_MOVE * 0.1
        intent = build_momentum_order_intent(market, up_book, down_book, flat, now=0.0)
        assert intent is None

    def test_bumps_up_to_exchange_minimum_when_flat_size_is_too_small(self):
        market = make_market(end_time=1000.0, order_min_size=100.0)  # forces a bump
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")
        move = config.MOMENTUM_MIN_MOVE * 5
        intent = build_momentum_order_intent(market, up_book, down_book, move, now=0.0)
        assert intent is not None
        assert intent.size_shares == pytest.approx(100.0)
        assert intent.notional_usd == pytest.approx(100.0 * 0.20)

    def test_skips_when_availability_check_fails(self, monkeypatch):
        market = make_market(end_time=1000.0)
        up_book = BookState(token_id="up")
        up_book.apply_snapshot(bids=[(0.20, 1.0)], asks=[(0.21, 1.0)])  # thin -- below MIN_BOOK_DEPTH_USD
        down_book = make_liquid_book(price=0.80, token_id="down")
        move = config.MOMENTUM_MIN_MOVE * 5
        intent = build_momentum_order_intent(market, up_book, down_book, move, now=0.0)
        assert intent is None

    def test_regime_comes_from_the_actual_traded_sides_own_book(self):
        # Same asymmetry discipline as strategy.build_order_intent: a
        # market can be CHEAP from Up's price and a different regime from
        # Down's -- regime must reflect whichever side momentum picks.
        market = make_market(end_time=1000.0)
        up_book = make_liquid_book(price=0.20, token_id="up")     # CHEAP
        down_book = make_liquid_book(price=0.75, token_id="down")  # CORE
        move = -config.MOMENTUM_MIN_MOVE * 5  # negative -> picks Down (CORE) -> out of scope
        intent = build_momentum_order_intent(market, up_book, down_book, move, now=0.0)
        assert intent is None
