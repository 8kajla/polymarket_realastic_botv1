import pytest

from paperbot import config
from paperbot.book import BookState
from paperbot.coinbase_strategy import decide_momentum_side, momentum_forced_side


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


class TestMomentumForcedSide:
    def test_none_when_trailing_return_is_none(self):
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")
        assert momentum_forced_side(up_book, down_book, None) is None

    def test_positive_momentum_forces_up_in_cheap(self):
        up_book = make_liquid_book(price=0.20, token_id="up")   # CHEAP
        down_book = make_liquid_book(price=0.80, token_id="down")
        move = config.MOMENTUM_MIN_MOVE * 5
        assert momentum_forced_side(up_book, down_book, move) == "Up"

    def test_negative_momentum_forces_down_in_mid(self):
        up_book = make_liquid_book(price=0.55, token_id="up")    # MID
        down_book = make_liquid_book(price=0.45, token_id="down")  # MID
        move = -config.MOMENTUM_MIN_MOVE * 5
        assert momentum_forced_side(up_book, down_book, move) == "Down"

    def test_none_when_the_implied_sides_regime_is_core_or_high(self):
        up_book = make_liquid_book(price=0.95, token_id="up")   # HIGH
        down_book = make_liquid_book(price=0.05, token_id="down")
        move = config.MOMENTUM_MIN_MOVE * 5
        assert momentum_forced_side(up_book, down_book, move) is None

        up_book2 = make_liquid_book(price=0.75, token_id="up")  # CORE
        down_book2 = make_liquid_book(price=0.25, token_id="down")
        assert momentum_forced_side(up_book2, down_book2, move) is None

    def test_none_inside_the_deadband(self):
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")
        flat = config.MOMENTUM_MIN_MOVE * 0.1
        assert momentum_forced_side(up_book, down_book, flat) is None

    def test_regime_comes_from_the_momentum_implied_sides_own_book(self):
        # Same asymmetry discipline as strategy.build_order_intent: a
        # market can be CHEAP from Up's price and a different regime from
        # Down's -- regime must reflect whichever side momentum implies.
        up_book = make_liquid_book(price=0.20, token_id="up")     # CHEAP
        down_book = make_liquid_book(price=0.75, token_id="down")  # CORE
        move = -config.MOMENTUM_MIN_MOVE * 5  # negative -> implies Down (CORE) -> out of scope
        assert momentum_forced_side(up_book, down_book, move) is None

    def test_none_when_the_implied_sides_book_price_is_missing(self):
        up_book = BookState(token_id="up")  # never got a snapshot -- best_bid is None
        down_book = make_liquid_book(price=0.80, token_id="down")
        move = config.MOMENTUM_MIN_MOVE * 5
        assert momentum_forced_side(up_book, down_book, move) is None
