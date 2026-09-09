import pytest

from paperbot import config


class TestMakerRebateUsd:
    """This bot only ever places postOnly (maker) orders -- see
    bot.py's "postOnly order would cross spread" skip path -- so every
    fill earns config.maker_rebate_usd's rebate. See its docstring for
    sourcing (secondary aggregators; Polymarket's own docs were
    unreachable when this was written)."""

    def test_matches_the_documented_formula_at_a_known_price(self):
        # fee = shares * CRYPTO_TAKER_FEE_RATE * price * (1-price)
        # rebate = fee * CRYPTO_MAKER_REBATE_SHARE
        shares, price = 100.0, 0.50
        expected_fee = shares * config.CRYPTO_TAKER_FEE_RATE * price * (1.0 - price)
        expected_rebate = expected_fee * config.CRYPTO_MAKER_REBATE_SHARE
        assert config.maker_rebate_usd(shares, price) == pytest.approx(expected_rebate)

    def test_peaks_at_price_050_tapering_toward_the_extremes(self):
        """The p*(1-p) term is maximized at p=0.5 -- a MID-band fill earns
        more rebate per share than an equally-sized CHEAP or HIGH fill."""
        mid = config.maker_rebate_usd(100.0, 0.50)
        cheap = config.maker_rebate_usd(100.0, 0.10)
        high = config.maker_rebate_usd(100.0, 0.90)
        assert mid > cheap
        assert mid > high
        # symmetric around 0.5
        assert cheap == pytest.approx(high)

    def test_scales_linearly_with_shares(self):
        one = config.maker_rebate_usd(1.0, 0.30)
        hundred = config.maker_rebate_usd(100.0, 0.30)
        assert hundred == pytest.approx(one * 100)

    def test_zero_or_negative_shares_earns_nothing(self):
        assert config.maker_rebate_usd(0.0, 0.5) == 0.0
        assert config.maker_rebate_usd(-5.0, 0.5) == 0.0

    def test_boundary_prices_earn_nothing(self):
        """p*(1-p) is 0 at the extremes -- a near-certain fill earns
        essentially no rebate, matching the real fee curve (highest
        uncertainty = highest fee = highest rebate)."""
        assert config.maker_rebate_usd(100.0, 0.0) == 0.0
        assert config.maker_rebate_usd(100.0, 1.0) == 0.0

    def test_always_non_negative_never_a_cost(self):
        """A maker is never charged -- this must never return a negative
        value regardless of input."""
        for price in (0.01, 0.25, 0.5, 0.75, 0.99):
            assert config.maker_rebate_usd(50.0, price) >= 0.0
