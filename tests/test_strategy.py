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
    decide_hedge,
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
    def test_skips_just_under_the_cutoff(self):
        market = make_market(end_time=100.0)
        assert timing_ok(market, now=100.0 - (config.MIN_SECONDS_BEFORE_CLOSE - 1)) is False

    def test_allows_at_exactly_the_cutoff(self):
        market = make_market(end_time=100.0)
        assert timing_ok(market, now=100.0 - config.MIN_SECONDS_BEFORE_CLOSE) is True

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
        # No held_side_price given -> falls back to the CHEAP-band rate
        # (see decide_side's docstring); BNB is dormant and has the same
        # value in all four regime cells, so this exercises that fallback
        # path directly against the one rate that applies regardless.
        rng = random.Random(42)
        activity = MarketActivityState(last_side="Up")
        same_count = sum(
            1 for _ in range(2000) if decide_side("BNB", activity, rng) == "Up"
        )
        expected = bc.SIDE_PERSISTENCE["BNB"]["CHEAP"]
        observed = same_count / 2000
        assert abs(observed - expected) < 0.03

    def test_persistence_is_regime_dependent_via_held_side_price(self):
        # Bitcoin's real per-regime rates differ meaningfully (CHEAP 88.3%
        # vs CORE 93.1%) -- confirm decide_side actually uses whichever
        # regime the CURRENTLY-HELD side's price falls in, not one fixed
        # asset-level number.
        activity = MarketActivityState(last_side="Up")
        rng_cheap, rng_core = random.Random(1), random.Random(1)

        cheap_same = sum(
            1 for _ in range(3000)
            if decide_side("Bitcoin", activity, rng_cheap, held_side_price=0.10) == "Up"
        ) / 3000
        core_same = sum(
            1 for _ in range(3000)
            if decide_side("Bitcoin", activity, rng_core, held_side_price=0.80) == "Up"
        ) / 3000

        assert abs(cheap_same - bc.SIDE_PERSISTENCE["Bitcoin"]["CHEAP"]) < 0.03
        assert abs(core_same - bc.SIDE_PERSISTENCE["Bitcoin"]["CORE"]) < 0.03
        assert core_same > cheap_same  # Bitcoin's CORE persistence is the higher of the two

    def test_missing_held_side_price_does_not_crash(self):
        # Defensive fallback: held_side_price=None (shouldn't normally
        # happen once last_side is set, but must not raise).
        rng = random.Random(7)
        activity = MarketActivityState(last_side="Down")
        side = decide_side("Solana", activity, rng, held_side_price=None)
        assert side in ("Up", "Down")


class TestCrossMarketFirstEntrySidePersistence:
    """decide_side's cross-market wiring, added 2026-09-11 -- see
    TestCrossMarketSidePersistence for the multiplier function itself in
    isolation; these confirm decide_side actually applies it, ONLY at the
    market's first entry (activity.last_side is None), and defaults to the
    old uniform-random behavior when no previous side is known."""

    def test_no_previous_side_falls_back_to_uniform_random(self):
        rng = random.Random(0)
        activity = MarketActivityState()
        side = decide_side("Bitcoin", activity, rng, previous_market_first_side=None)
        assert side in ("Up", "Down")

    def test_biases_toward_the_previous_markets_first_side(self):
        rng = random.Random(3)
        activity = MarketActivityState()  # no last_side -- this IS the first entry
        same_count = sum(
            1 for _ in range(3000)
            if decide_side("Bitcoin", activity, rng, previous_market_first_side="Up") == "Up"
        )
        observed = same_count / 3000
        expected = bc.cross_market_side_persistence("Bitcoin")
        assert abs(observed - expected) < 0.03

    def test_symmetric_for_the_opposite_previous_side(self):
        rng = random.Random(4)
        activity = MarketActivityState()
        same_count = sum(
            1 for _ in range(3000)
            if decide_side("Bitcoin", activity, rng, previous_market_first_side="Down") == "Down"
        )
        observed = same_count / 3000
        expected = bc.cross_market_side_persistence("Bitcoin")
        assert abs(observed - expected) < 0.03

    def test_does_not_apply_once_the_market_already_has_an_established_side(self):
        # Scoping check: previous_market_first_side must only affect the
        # FIRST entry -- once activity.last_side is set, ordinary
        # SIDE_PERSISTENCE governs, unaffected by this parameter.
        activity = MarketActivityState(last_side="Up")
        rng_with = random.Random(5)
        rng_without = random.Random(5)
        n = 500
        with_prev = sum(
            1 for _ in range(n)
            if decide_side("Bitcoin", activity, rng_with, held_side_price=0.10,
                            previous_market_first_side="Down") == "Up"
        )
        without_prev = sum(
            1 for _ in range(n)
            if decide_side("Bitcoin", activity, rng_without, held_side_price=0.10,
                            previous_market_first_side=None) == "Up"
        )
        assert with_prev == without_prev

    def test_unlisted_asset_is_still_a_fifty_fifty_split_not_a_crash(self):
        # BNB isn't in CROSS_MARKET_SIDE_PERSISTENCE -> 0.5, a no-op.
        rng = random.Random(6)
        activity = MarketActivityState()
        same_count = sum(
            1 for _ in range(2000)
            if decide_side("BNB", activity, rng, previous_market_first_side="Up") == "Up"
        )
        observed = same_count / 2000
        assert abs(observed - 0.5) < 0.03


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

    def test_within_band_multiplier_scales_size_with_price(self):
        # Bitcoin HIGH: band_mean_price=0.947. Average over many draws at
        # a low-in-band price should land meaningfully below the average
        # at a high-in-band price, holding regime/position_tier fixed --
        # confirms decide_size actually applies within_band_size_multiplier,
        # not just that the multiplier function itself works in isolation.
        def avg_notional(price, n=400):
            rng = random.Random(5)
            total = 0.0
            for _ in range(n):
                notional, _ = decide_size("Bitcoin", "HIGH", "4th_plus", price, rng)
                total += notional
            return total / n

        low_avg = avg_notional(0.91)
        high_avg = avg_notional(0.99)
        assert low_avg < high_avg

    def test_within_band_multiplier_scales_size_in_cheap_and_mid_too(self):
        # UPGRADED 2026-09-11 (cheap-mid-within-band-scaling-gap): CHEAP/
        # MID are now real, calibrated cells too (the strongest-validated
        # finding of that session's research pass) -- price should move
        # the average size within those regimes, same as CORE/HIGH
        # already did, whenever ENABLE_CHEAP_MID_WITHIN_BAND_SCALING is on
        # (the default).
        def avg_notional(regime, price, n=400):
            rng = random.Random(9)
            total = 0.0
            for _ in range(n):
                notional, is_floor = decide_size("Bitcoin", regime, "4th_plus", price, rng)
                if not is_floor:
                    total += notional
            return total / n

        low = avg_notional("MID", 0.31)
        high = avg_notional("MID", 0.69)
        assert low < high

    def test_within_band_multiplier_is_a_noop_in_cheap_and_mid_when_flag_off(self, monkeypatch):
        # paperbot-mini opts out via ENABLE_CHEAP_MID_WITHIN_BAND_SCALING=
        # false, preserving the original CORE/HIGH-only behavior exactly.
        monkeypatch.setattr(config, "ENABLE_CHEAP_MID_WITHIN_BAND_SCALING", False)

        def avg_notional(regime, price, n=400):
            rng = random.Random(9)
            total = 0.0
            for _ in range(n):
                notional, is_floor = decide_size("Bitcoin", regime, "4th_plus", price, rng)
                if not is_floor:
                    total += notional
            return total / n

        low = avg_notional("MID", 0.31)
        high = avg_notional("MID", 0.69)
        # Same rng seed consumed identically in both calls (Bitcoin's
        # floor-lot probability is 0 everywhere, so there's no branching
        # to desync the draw sequence) -- a true no-op multiplier means
        # these must match exactly, not just approximately.
        assert low == high

    def test_cross_market_size_momentum_defaults_to_a_noop_when_not_passed(self):
        rng_a = random.Random(37)
        rng_b = random.Random(37)
        notional_no_arg, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_a)
        notional_explicit_none, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_b,
                                                 size_momentum_residual=None)
        assert notional_no_arg == notional_explicit_none

    def test_cross_market_size_momentum_scales_first_entry_size(self):
        def avg_notional(residual, n=400):
            rng = random.Random(41)
            total = 0.0
            for _ in range(n):
                notional, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng,
                                           size_momentum_residual=residual)
                total += notional
            return total / n

        low = avg_notional(-1.3)
        high = avg_notional(1.1)
        assert low < high

    def test_cross_market_size_momentum_only_applies_to_the_first_tier(self):
        # The finding was measured on first-entry size specifically --
        # later position tiers in the same market must be unaffected.
        rng_a = random.Random(43)
        rng_b = random.Random(43)
        notional_no_residual, _ = decide_size("Bitcoin", "MID", "4th_plus", 0.5, rng_a)
        notional_with_residual, _ = decide_size("Bitcoin", "MID", "4th_plus", 0.5, rng_b,
                                                 size_momentum_residual=1.1)
        assert notional_no_residual == notional_with_residual

    def test_cross_market_size_momentum_respects_the_per_instance_feature_flag(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_CROSS_MARKET_SIZE_MOMENTUM", False)
        rng_a = random.Random(47)
        rng_b = random.Random(47)
        notional_no_residual, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_a)
        notional_with_residual, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_b,
                                                 size_momentum_residual=1.1)
        assert notional_no_residual == notional_with_residual, (
            "with the flag off, cross-market size momentum must be a strict no-op"
        )

    def test_bankroll_pnl_defaults_to_a_noop_when_not_passed(self):
        # Ethereum, not Bitcoin -- BANKROLL_PNL_SIZE_MULTIPLIER deliberately
        # excludes Bitcoin (see its docstring in behavior_config.py).
        rng_a = random.Random(53)
        rng_b = random.Random(53)
        notional_no_arg, _ = decide_size("Ethereum", "MID", "first", 0.5, rng_a)
        notional_explicit_none, _ = decide_size("Ethereum", "MID", "first", 0.5, rng_b,
                                                 bankroll_pnl_residual=None)
        assert notional_no_arg == notional_explicit_none

    def test_bankroll_pnl_scales_first_entry_size_down_as_pnl_rises(self):
        # The whole finding: sizes DOWN after accumulated profit, UP after
        # a drawdown.
        def avg_notional(pnl, n=400):
            rng = random.Random(59)
            total = 0.0
            for _ in range(n):
                notional, _ = decide_size("Ethereum", "MID", "first", 0.5, rng,
                                           bankroll_pnl_residual=pnl)
                total += notional
            return total / n

        after_drawdown = avg_notional(-200.0)
        after_profit = avg_notional(400.0)
        assert after_profit < after_drawdown

    def test_bankroll_pnl_is_a_noop_for_bitcoin(self):
        # Deliberately excluded, not just uncalibrated -- see
        # BANKROLL_PNL_SIZE_MULTIPLIER's docstring.
        rng_a = random.Random(61)
        rng_b = random.Random(61)
        notional_no_pnl, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_a)
        notional_with_pnl, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_b,
                                            bankroll_pnl_residual=400.0)
        assert notional_no_pnl == notional_with_pnl

    def test_bankroll_pnl_only_applies_to_the_first_tier(self):
        rng_a = random.Random(67)
        rng_b = random.Random(67)
        notional_no_pnl, _ = decide_size("Ethereum", "MID", "4th_plus", 0.5, rng_a)
        notional_with_pnl, _ = decide_size("Ethereum", "MID", "4th_plus", 0.5, rng_b,
                                            bankroll_pnl_residual=400.0)
        assert notional_no_pnl == notional_with_pnl

    def test_bankroll_pnl_respects_the_per_instance_feature_flag(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_BANKROLL_PNL_SIZE_MULTIPLIER", False)
        rng_a = random.Random(71)
        rng_b = random.Random(71)
        notional_no_pnl, _ = decide_size("Ethereum", "MID", "first", 0.5, rng_a)
        notional_with_pnl, _ = decide_size("Ethereum", "MID", "first", 0.5, rng_b,
                                            bankroll_pnl_residual=400.0)
        assert notional_no_pnl == notional_with_pnl, (
            "with the flag off, bankroll-linked sizing must be a strict no-op"
        )

    def test_ttc_multiplier_defaults_to_a_noop_when_not_passed(self):
        # Every pre-existing call site/test that doesn't pass
        # seconds_remaining must be completely unaffected by this feature.
        rng_a = random.Random(11)
        rng_b = random.Random(11)
        notional_no_arg, _ = decide_size("Bitcoin", "HIGH", "first", 0.95, rng_a)
        notional_explicit_none, _ = decide_size("Bitcoin", "HIGH", "first", 0.95, rng_b,
                                                  seconds_remaining=None)
        assert notional_no_arg == notional_explicit_none

    def test_ttc_multiplier_scales_size_with_time_remaining(self):
        # Bitcoin HIGH: confirmed to grow sharply toward close. Average
        # over many draws late in the window should land meaningfully
        # above early in the window, holding regime/price/position fixed.
        def avg_notional(seconds_remaining, n=400):
            rng = random.Random(13)
            total = 0.0
            for _ in range(n):
                notional, _ = decide_size("Bitcoin", "HIGH", "4th_plus", 0.95, rng,
                                           seconds_remaining=seconds_remaining)
                total += notional
            return total / n

        early_avg = avg_notional(270.0)
        late_avg = avg_notional(90.0)
        assert early_avg < late_avg

    def test_ttc_multiplier_is_a_noop_for_assets_not_calibrated(self):
        rng_a = random.Random(17)
        rng_b = random.Random(17)
        notional_early, _ = decide_size("Dogecoin", "CORE", "first", 0.85, rng_a,
                                         seconds_remaining=270.0)
        notional_late, _ = decide_size("Dogecoin", "CORE", "first", 0.85, rng_b,
                                        seconds_remaining=90.0)
        assert notional_early == notional_late

    def test_ttc_multiplier_respects_the_per_instance_feature_flag(self, monkeypatch):
        # Explicit instruction: paperbot-mini stays on the OLD behavior
        # (as a control) via ENABLE_TTC_SIZE_MULTIPLIER=false -- confirm
        # the flag actually suppresses the multiplier, not just that the
        # env var parses.
        monkeypatch.setattr(config, "ENABLE_TTC_SIZE_MULTIPLIER", False)
        rng_a = random.Random(19)
        rng_b = random.Random(19)
        notional_early, _ = decide_size("Bitcoin", "HIGH", "4th_plus", 0.95, rng_a,
                                         seconds_remaining=270.0)
        notional_late, _ = decide_size("Bitcoin", "HIGH", "4th_plus", 0.95, rng_b,
                                        seconds_remaining=90.0)
        assert notional_early == notional_late, (
            "with the flag off, ttc must be a strict no-op even for a "
            "calibrated (asset, regime) cell"
        )

    def test_resumption_multiplier_defaults_to_a_noop_when_not_passed(self):
        rng_a = random.Random(23)
        rng_b = random.Random(23)
        notional_no_arg, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_a)
        notional_explicit_none, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_b,
                                                 hours_since_resumption=None)
        assert notional_no_arg == notional_explicit_none

    def test_resumption_multiplier_suppresses_size_early_and_overshoots_later(self):
        def avg_notional(hours, n=400):
            rng = random.Random(29)
            total = 0.0
            for _ in range(n):
                notional, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng,
                                           hours_since_resumption=hours)
                total += notional
            return total / n

        no_resumption = avg_notional(None)
        suppressed = avg_notional(3.0)
        overshoot = avg_notional(8.0)
        normal_again = avg_notional(50.0)
        assert suppressed < no_resumption
        assert overshoot > no_resumption
        assert normal_again == pytest.approx(no_resumption, rel=0.05)

    def test_resumption_multiplier_respects_the_per_instance_feature_flag(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_RESUMPTION_SIZE_MULTIPLIER", False)
        rng_a = random.Random(31)
        rng_b = random.Random(31)
        notional_normal, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_a,
                                          hours_since_resumption=None)
        notional_suppressed, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_b,
                                              hours_since_resumption=3.0)
        assert notional_normal == notional_suppressed, (
            "with the flag off, resumption must be a strict no-op"
        )


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

        monkeypatch.setattr(stratmod, "decide_side",
                             lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.side == "Down"
        assert intent.price == 0.04
        assert intent.regime == "CHEAP"  # not "HIGH", which up_book's 0.95 would wrongly give
        # sized off the CHEAP curve at Down's real price, not a HIGH notional / 0.04
        cheap_median = bc.median_entry_notional(market.asset, "CHEAP", "first")
        assert intent.notional_usd < cheap_median * 2  # generous jitter allowance

    def test_seconds_remaining_is_threaded_through_to_ttc_size_multiplier(self):
        """End-to-end: build_order_intent computes seconds_remaining from
        the real market/now and passes it into decide_size, actually
        moving the resulting notional -- not just that the multiplier
        function itself works in isolation (already covered in
        TestTtcSizeMultiplier) or that decide_size applies it when called
        directly (already covered in TestSizingDecision)."""
        up_book = make_liquid_book(price=0.95, token_id="up")  # HIGH band, Bitcoin-calibrated
        down_book = make_liquid_book(price=0.04, token_id="down")

        def notional_at(seconds_remaining, seed=0):
            market = make_market(end_time=1000.0, asset="Bitcoin")
            activity = MarketActivityState()
            intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed),
                                         now=1000.0 - seconds_remaining)
            return intent.notional_usd if intent is not None else None

        early = notional_at(270.0)
        late = notional_at(90.0)
        assert early is not None and late is not None
        assert early != late, "seconds_remaining must actually reach decide_size, not be dropped"

    def test_floor_lot_still_skips_below_the_runtime_minimum(self):
        """Floor-lot's whole point is a tiny, often-below-minimum notional
        -- it must still genuinely skip (not get bumped up), unlike the
        ordinary curve below."""
        market = make_market(end_time=1000.0, asset="BNB", order_min_size=1000.0)
        up_book = make_liquid_book(price=0.20, token_id="up")  # CHEAP
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()
        rng = random.Random(0)
        monkeypatch_random = 0.0  # forces the floor-lot roll (BNB/CHEAP/first p=0.45)
        rng.random = lambda: monkeypatch_random
        intent = build_order_intent(market, up_book, down_book, activity, rng, now=0.0)
        assert intent is None

    def test_ordinary_entry_below_runtime_minimum_bumps_up_instead_of_skipping(self):
        """CHANGED 2026-09-08: an ordinary (non-floor-lot) entry below the
        exchange's real minimum now gets bumped up to exactly that
        minimum instead of being silently dropped -- confirmed live this
        was costing far more entries than it should (9,616 skips vs 5,542
        placements in one ~8h window)."""
        market = make_market(end_time=1000.0, asset="Hyperliquid",
                              order_min_size=1000.0)  # absurdly high on purpose
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()
        rng = random.Random(0)
        rng.random = lambda: 1.0  # never rolls floor-lot -- forces the ordinary curve
        intent = build_order_intent(market, up_book, down_book, activity, rng, now=0.0)

        assert intent is not None, "must bump up, not skip"
        assert intent.is_floor_lot is False
        assert intent.notional_usd == pytest.approx(1000.0 * intent.price)
        assert intent.size_shares == pytest.approx(1000.0)


class TestMarketActivityStateHedgeTracking:
    def test_cost_by_side_accumulates_per_side(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.record_entry("Up", notional_usd=5.0, regime="MID")
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP")
        assert activity.cost_by_side["Up"] == 15.0
        assert activity.cost_by_side["Down"] == 2.0

    def test_first_entry_regime_locked_on_first_call_only(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="HIGH")
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP")
        assert activity.first_entry_regime == "HIGH"

    def test_first_entry_price_locked_on_first_call_only(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="HIGH", price=0.95)
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP", price=0.05)
        assert activity.first_entry_price == 0.95

    def test_first_entry_price_defaults_to_none_when_not_passed(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="HIGH")
        assert activity.first_entry_price is None

    def test_dominant_side_is_none_before_any_entry(self):
        assert MarketActivityState().dominant_side() is None

    def test_dominant_side_picks_higher_cost_side(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP")
        assert activity.dominant_side() == "Up"

    def test_record_entry_increments_hedge_count(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        assert activity.hedge_count == 0
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP", is_hedge=True)
        assert activity.hedge_count == 1
        activity.record_entry("Down", notional_usd=1.0, regime="CHEAP", is_hedge=True)
        assert activity.hedge_count == 2

    def test_release_unfilled_reverses_a_fully_cancelled_placement(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.release_unfilled("Up", 10.0)
        assert activity.cost_by_side["Up"] == 0.0

    def test_release_unfilled_reverses_only_the_unfilled_portion(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.release_unfilled("Up", 4.0)  # e.g. 6 of 10 shares' worth filled before cancel
        assert activity.cost_by_side["Up"] == 6.0

    def test_release_unfilled_never_goes_negative(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=5.0, regime="MID")
        activity.release_unfilled("Up", 999.0)  # defensive: overshoot shouldn't corrupt state
        assert activity.cost_by_side["Up"] == 0.0

    def test_release_unfilled_does_not_touch_the_other_side(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.record_entry("Down", notional_usd=3.0, regime="CHEAP")
        activity.release_unfilled("Up", 10.0)
        assert activity.cost_by_side["Up"] == 0.0
        assert activity.cost_by_side["Down"] == 3.0


class TestDecideHedge:
    def test_no_hedge_before_a_first_entry_exists(self):
        activity = MarketActivityState()
        assert decide_hedge("Bitcoin", activity, random.Random(0)) is None

    def test_can_fire_at_entry_counts_beyond_the_second_entry(self):
        """CHANGED 2026-09-08: previously entry_count==2 was hard-gated
        off entirely. Fresh re-derivation from the real trader mirror
        found only 23.5% of real hedges land at entry_count==1 (median
        index 4) -- decide_hedge must now be able to fire at later
        entry_counts too, not just the market's literal 2nd entry."""
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")  # entry_count now 1
        activity.record_entry("Up", notional_usd=1.0, regime="MID")   # entry_count now 2
        # BNB/MID has the highest known cumulative probability (0.9004) --
        # over enough seeds, at least one must trigger at entry_count==2.
        assert any(
            decide_hedge("BNB", activity, random.Random(seed)) is not None
            for seed in range(50)
        )

    def test_returns_none_past_the_modeled_hazard_window(self):
        """hedge_attempt_hazard returns 0.0 past behavior_config's modeled
        window (10 attempts) -- decide_hedge must never fire there,
        regardless of how favorable the RNG draw is."""
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.entry_count = 11
        for seed in range(50):
            assert decide_hedge("BNB", activity, random.Random(seed)) is None

    def test_returns_the_opposite_of_the_dominant_side(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        # BNB/MID has the highest trigger probability of any (asset, regime)
        # combo (0.9004) -- a handful of seeds will trigger it reliably.
        triggered = None
        for seed in range(20):
            r = decide_hedge("BNB", activity, random.Random(seed))
            if r is not None:
                triggered = r
                break
        assert triggered == "Down"

    def test_can_refire_after_the_first_hedge_using_continuation_probability(self):
        """CHANGED 2026-09-09 (was: hedge_placed permanently blocked any
        further hedge once one had fired). CONFIRMED LIVE, even after
        correcting for CLOB-fragment inflation: only 39.8% of dual-sided
        markets stop at 1 hedge decision -- a 2nd (and 3rd, ...) must be
        reachable, governed by HEDGE_CONTINUATION_PROBABILITY instead of
        being blocked outright."""
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.record_entry("Down", notional_usd=2.0, regime="MID", is_hedge=True)
        assert activity.hedge_count == 1
        # HEDGE_CONTINUATION_PROBABILITY[1] = 0.602 -- over enough seeds,
        # at least one must trigger a 2nd hedge.
        assert any(
            decide_hedge("BNB", activity, random.Random(seed)) is not None
            for seed in range(20)
        )

    def test_never_fires_at_or_beyond_the_hard_cap(self, monkeypatch):
        """CONFIRMED LIVE (same night as the continuation fix above): the
        per-opportunity continuation roll runs away without a ceiling --
        the bot's own hedge-count-per-market distribution came back
        badly bimodal (15.1% reaching 7+ vs the real 2.2%), directly
        costing money on the ledger. config.MAX_HEDGE_COUNT_PER_MARKET
        must hard-block any further hedge once hedge_count reaches it,
        regardless of how favorable the RNG draw or the continuation
        probability is."""
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.hedge_count = config.MAX_HEDGE_COUNT_PER_MARKET
        monkeypatch.setattr(bc, "hedge_continuation_probability", lambda count: 1.0)  # would always fire otherwise
        for seed in range(20):
            assert decide_hedge("BNB", activity, random.Random(seed)) is None

    def test_still_fires_just_under_the_hard_cap(self, monkeypatch):
        """Sanity check the cap boundary is exact, not off-by-one."""
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.hedge_count = config.MAX_HEDGE_COUNT_PER_MARKET - 1
        monkeypatch.setattr(bc, "hedge_continuation_probability", lambda count: 1.0)
        assert decide_hedge("BNB", activity, random.Random(0)) is not None

    def test_continuation_uses_hedge_continuation_probability_not_the_hazard_curve(self, monkeypatch):
        """The FIRST hedge (hedge_count==0) is governed by
        hedge_attempt_hazard; a further hedge (hedge_count>=1) must use
        hedge_continuation_probability instead -- confirm decide_hedge
        actually calls the right one for each case."""
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")

        calls = []
        monkeypatch.setattr(bc, "hedge_attempt_hazard",
                             lambda asset, regime, idx: calls.append(("hazard", idx)) or 0.0)
        monkeypatch.setattr(bc, "hedge_continuation_probability",
                             lambda count: calls.append(("continuation", count)) or 0.0)

        decide_hedge("BNB", activity, random.Random(0))
        assert calls == [("hazard", 1)]

        activity.record_entry("Down", notional_usd=2.0, regime="MID", is_hedge=True)
        calls.clear()
        decide_hedge("BNB", activity, random.Random(0))
        assert calls == [("continuation", 1)]

    def test_bitcoin_high_regime_cumulative_probability_matches_calibration(self, monkeypatch):
        """CHANGED 2026-09-08: the calibrated 0.2967 for Bitcoin/HIGH is
        now a CUMULATIVE probability across the whole multi-attempt
        hazard window (hedge_attempt_hazard), not a single-shot rate at
        entry_count==1 -- simulate the real decide_hedge call pattern
        (one activity, entry_count advancing 1..10, stopping at the
        first trigger) across many independent markets and check the
        empirical hedge-at-all rate reproduces the calibrated target.

        CONVICTION_HEDGE_MULTIPLIER disabled here (added 2026-09-11): this
        test uses a fixed, arbitrary first-entry notional (10.0), not one
        representative of Bitcoin/HIGH's real distribution -- letting the
        conviction multiplier apply would test that multiplier's own bias
        at this one specific input, not whether the base cumulative-
        probability model still reproduces its calibrated target. See
        TestConvictionHedgeMultiplierIntegration below for dedicated
        coverage of the conviction mechanism itself."""
        monkeypatch.setattr(config, "ENABLE_CONVICTION_HEDGE_MULTIPLIER", False)
        n = 2000
        triggered = 0
        for seed in range(n):
            rng = random.Random(seed)
            activity = MarketActivityState()
            activity.record_entry("Up", notional_usd=10.0, regime="HIGH")
            for _ in range(10):  # entry_count 1..10, matching the modeled window
                if decide_hedge("Bitcoin", activity, rng) is not None:
                    triggered += 1
                    break
                activity.entry_count += 1
        rate = triggered / n
        assert abs(rate - 0.2967) < 0.03

    def test_first_attempt_hazard_is_lower_than_the_old_single_shot_probability(self):
        """The whole point of the fix: entry_count==1's own hazard must be
        LESS than the old design's flat 0.2967 (BNB/MID's calibrated
        cumulative, used here since it's the largest and most sensitive to
        a broken rescale), since it's now only one of several chances
        spread across the market's lifetime rather than the only one."""
        p1 = bc.hedge_attempt_hazard("BNB", "MID", 1)
        assert 0 < p1 < bc.hedge_trigger_probability("BNB", "MID")

    def test_hedge_continuation_probability_defaults_for_deep_indices(self):
        assert bc.hedge_continuation_probability(1) == pytest.approx(0.602)
        assert bc.hedge_continuation_probability(3) == pytest.approx(0.533)
        # hedge_count=10 (well past the calibrated 1-3 table) falls back
        # to the documented default rather than KeyError-ing.
        assert bc.hedge_continuation_probability(10) == bc._DEFAULT_HEDGE_CONTINUATION_PROBABILITY

    def test_hedge_continuation_size_ratio_decays_and_defaults_for_deep_indices(self):
        assert bc.hedge_continuation_size_ratio(2) == pytest.approx(0.186)
        assert bc.hedge_continuation_size_ratio(3) == pytest.approx(0.133)
        assert bc.hedge_continuation_size_ratio(10) == bc._DEFAULT_HEDGE_CONTINUATION_SIZE_RATIO
        # decaying, not flat: each successive calibrated index is smaller
        assert bc.hedge_continuation_size_ratio(2) > bc.hedge_continuation_size_ratio(3) \
            > bc._DEFAULT_HEDGE_CONTINUATION_SIZE_RATIO


class TestDecideHedgeLiquidityMultiplier:
    """decide_hedge's liquidity wiring, added 2026-09-10 -- see
    TestHedgeLiquidityMultiplier for the multiplier function itself in
    isolation; these confirm decide_hedge actually applies it (not just
    that the function works), only to the FIRST hedge, and respects the
    per-instance feature flag."""

    def test_liquidity_defaults_to_a_noop_when_not_passed(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        n_no_arg = sum(
            1 for seed in range(200)
            if decide_hedge("BNB", activity, random.Random(seed)) is not None
        )
        n_explicit_none = sum(
            1 for seed in range(200)
            if decide_hedge("BNB", activity, random.Random(seed), liquidity=None) is not None
        )
        assert n_no_arg == n_explicit_none

    def test_high_liquidity_fires_more_often_than_low_liquidity(self):
        # Bitcoin/MID -- real curve: 8590->0.9336, 19828->1.0704.
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        n = 500
        n_low = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), liquidity=8590.0) is not None
        )
        n_high = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), liquidity=19828.0) is not None
        )
        assert n_high > n_low

    def test_clamped_to_a_valid_probability_even_at_the_cap(self, monkeypatch):
        # A pathological hazard*multiplier combination must never push
        # the effective probability outside [0, 1] -- force a multiplier
        # far above what's actually calibrated and confirm no crash /
        # over-100% behavior (rng.random() < p with p>1 would always
        # fire, which is fine semantically, but p itself must stay sane).
        monkeypatch.setattr(bc, "hedge_liquidity_multiplier", lambda asset, liquidity: 100.0)
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        # must not raise, and must behave as a valid (always-fires) probability
        for seed in range(10):
            assert decide_hedge("BNB", activity, random.Random(seed), liquidity=1.0) is not None

    def test_respects_the_per_instance_feature_flag(self, monkeypatch):
        # Explicit instruction: paperbot-mini stays on the OLD behavior
        # (as a control) via ENABLE_HEDGE_LIQUIDITY_MULTIPLIER=false.
        monkeypatch.setattr(config, "ENABLE_HEDGE_LIQUIDITY_MULTIPLIER", False)
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        n = 500
        n_low = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), liquidity=8590.0) is not None
        )
        n_high = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), liquidity=19828.0) is not None
        )
        assert n_low == n_high, "with the flag off, liquidity must be a strict no-op"

    def test_does_not_affect_continuation_hedges(self):
        # The liquidity finding was measured on "did this market ever go
        # dual-sided" -- must apply ONLY to the first hedge, never to
        # HEDGE_CONTINUATION_PROBABILITY's separate mechanism. Uses
        # Bitcoin specifically (a real, differentiated curve -- unlike
        # BNB, which is already a no-op for any liquidity value since
        # it's not in HEDGE_LIQUIDITY_MULTIPLIER at all, so BNB wouldn't
        # actually exercise this scoping even if it were broken).
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.record_entry("Down", notional_usd=2.0, regime="MID", is_hedge=True)
        assert activity.hedge_count == 1
        n = 500
        n_low = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), liquidity=8590.0) is not None
        )
        n_high = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), liquidity=19828.0) is not None
        )
        assert n_low == n_high, "liquidity must not leak into the continuation-hedge path"


class TestDecideHedgeAdverseMoveTriggerMultiplier:
    """decide_hedge's adverse-move TRIGGER wiring, added 2026-09-10 -- see
    TestAdverseMoveHedgeTriggerMultiplier for the multiplier function
    itself in isolation; these confirm decide_hedge actually applies it
    (using activity.first_entry_price + dominant_current_price), only to
    the FIRST hedge, and respects the per-instance feature flag. Uses
    Bitcoin/CORE, first_entry_price=0.80 throughout -- a real,
    differentiated curve (unlike BNB, not in the table at all)."""

    def test_dominant_current_price_defaults_to_a_noop_when_not_passed(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)
        n_no_arg = sum(
            1 for seed in range(200)
            if decide_hedge("Bitcoin", activity, random.Random(seed)) is not None
        )
        n_explicit_none = sum(
            1 for seed in range(200)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=None) is not None
        )
        assert n_no_arg == n_explicit_none

    def test_larger_adverse_move_fires_more_often_than_smaller_adverse_move(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)
        n = 500
        n_small_move = sum(  # dominant_current_price=0.79 -> adverse_move=0.01
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.79) is not None
        )
        n_big_move = sum(  # dominant_current_price=0.50 -> adverse_move=0.30
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.50) is not None
        )
        assert n_big_move > n_small_move

    def test_favorable_move_is_a_strict_noop_matching_the_unmultiplied_baseline(self):
        # adverse_move < 0 (price moved IN his favor) is deliberately
        # unmodeled -- must fire at exactly the baseline rate (no
        # dominant_current_price at all -> multiplier never applied), NOT
        # at move==0.0's own rate -- the curve's first breakpoint is
        # pinned to its own MEASURED value (0.6123 for Bitcoin, a real,
        # disclosed discontinuity -- see ADVERSE_MOVE_HEDGE_TRIGGER_
        # MULTIPLIER's docstring), not forced to 1.0.
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)
        n = 500
        n_baseline = sum(  # no dominant_current_price -> multiplier never applied
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed)) is not None
        )
        n_favorable = sum(  # dominant_current_price=0.95 -> adverse_move=-0.15 (favorable)
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.95) is not None
        )
        assert n_baseline == n_favorable

    def test_move_at_zero_is_lower_than_the_unmultiplied_baseline(self):
        # The curve's own first breakpoint (move==0.0) is a REAL,
        # measured value below 1.0 (0.6123 for Bitcoin) -- not a no-op.
        # Confirms the documented discontinuity is actually wired up, not
        # silently smoothed away.
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)
        n = 500
        n_baseline = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed)) is not None
        )
        n_at_zero = sum(  # dominant_current_price=0.80 -> adverse_move=0.0
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.80) is not None
        )
        assert n_at_zero < n_baseline

    def test_respects_the_per_instance_feature_flag(self, monkeypatch):
        # Explicit instruction: paperbot-mini stays on the OLD behavior
        # (as a control) via ENABLE_ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER=false.
        monkeypatch.setattr(config, "ENABLE_ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER", False)
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)
        n = 500
        n_small_move = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.79) is not None
        )
        n_big_move = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.50) is not None
        )
        assert n_small_move == n_big_move, "with the flag off, adverse_move must be a strict no-op"

    def test_is_a_noop_when_first_entry_price_was_never_recorded(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE")  # no price= kwarg
        assert activity.first_entry_price is None
        n = 500
        n_small_move = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.79) is not None
        )
        n_big_move = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.50) is not None
        )
        assert n_small_move == n_big_move

    def test_does_not_affect_continuation_hedges(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)
        activity.record_entry("Down", notional_usd=2.0, regime="CORE", is_hedge=True)
        assert activity.hedge_count == 1
        n = 500
        n_small_move = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.79) is not None
        )
        n_big_move = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.50) is not None
        )
        assert n_small_move == n_big_move, "adverse_move must not leak into the continuation-hedge path"


class TestDecideHedgeWeekendMultiplier:
    """decide_hedge's weekend wiring, added 2026-09-10 -- see
    TestWeekendHedgeMultiplier for the multiplier function itself in
    isolation; these confirm decide_hedge actually applies it (not just
    that the function works), only to the FIRST hedge, and respects the
    per-instance feature flag."""

    def test_is_weekend_defaults_to_a_noop_when_not_passed(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        n_no_arg = sum(
            1 for seed in range(200)
            if decide_hedge("Bitcoin", activity, random.Random(seed)) is not None
        )
        n_explicit_none = sum(
            1 for seed in range(200)
            if decide_hedge("Bitcoin", activity, random.Random(seed), is_weekend=None) is not None
        )
        assert n_no_arg == n_explicit_none

    def test_weekend_fires_more_often_than_weekday(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        n = 500
        n_weekday = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), is_weekend=False) is not None
        )
        n_weekend = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), is_weekend=True) is not None
        )
        assert n_weekend > n_weekday

    def test_respects_the_per_instance_feature_flag(self, monkeypatch):
        # Explicit instruction: paperbot-mini stays on the OLD behavior
        # (as a control) via ENABLE_WEEKEND_HEDGE_MULTIPLIER=false.
        monkeypatch.setattr(config, "ENABLE_WEEKEND_HEDGE_MULTIPLIER", False)
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        n = 500
        n_weekday = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), is_weekend=False) is not None
        )
        n_weekend = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), is_weekend=True) is not None
        )
        assert n_weekday == n_weekend, "with the flag off, is_weekend must be a strict no-op"

    def test_does_not_affect_continuation_hedges(self):
        # Same scoping rule as liquidity above -- only the first hedge.
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.record_entry("Down", notional_usd=2.0, regime="MID", is_hedge=True)
        assert activity.hedge_count == 1
        n = 500
        n_weekday = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), is_weekend=False) is not None
        )
        n_weekend = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), is_weekend=True) is not None
        )
        assert n_weekday == n_weekend, "is_weekend must not leak into the continuation-hedge path"


class TestBuildOrderIntentHedge:
    def test_produces_a_hedge_intent_sized_off_dominant_cost(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="BNB")
        up_book = make_liquid_book(price=0.80, token_id="up")    # MID/CORE-ish primary
        down_book = make_liquid_book(price=0.15, token_id="down")  # cheap hedge side
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE")

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.is_hedge is True
        assert intent.is_floor_lot is False
        assert intent.side == "Down"
        assert intent.price == 0.15
        expected_ratio = bc.hedge_size_ratio("BNB", "CORE")
        # jittered by ±SIZING_JITTER_FRACTION around dominant_cost * ratio
        expected_notional = 10.0 * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01
        assert intent.reason == "hedge"

    def test_a_second_hedge_uses_continuation_size_ratio_not_hedge_size_ratio(self, monkeypatch):
        """hedge_count==1 (this would be the market's 2nd hedge-shaped
        entry) must size off HEDGE_CONTINUATION_SIZE_RATIO, a real,
        separately-calibrated (and much flatter) curve -- not repeat
        HEDGE_SIZE_RATIO, which only governs the first hedge."""
        market = make_market(end_time=1000.0, asset="BNB")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE")
        activity.record_entry("Down", notional_usd=1.0, regime="CORE", is_hedge=True)
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.is_hedge is True
        expected_ratio = bc.hedge_continuation_size_ratio(2)  # this is hedge #2
        assert expected_ratio != bc.hedge_size_ratio("BNB", "CORE")  # sanity: genuinely different curve
        dominant_cost = 10.0  # activity.cost_by_side["Up"], unaffected by the 1.0 hedge already placed
        expected_notional = dominant_cost * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_hedge_sizing_is_unaffected_by_time_to_close(self, monkeypatch):
        """TTC_SIZE_MULTIPLIER is deliberately scoped to the ordinary
        entry-curve path only (decide_size's own docstring) -- the raw
        trade data it was measured from doesn't distinguish hedge-shaped
        trades from ordinary ones, so it must never touch
        HEDGE_SIZE_RATIO's formula. Bitcoin IS calibrated in
        TTC_SIZE_MULTIPLIER (unlike BNB, used in the other hedge tests
        above), so this specifically exercises the case where a no-op
        would be silently wrong if the scoping broke."""
        market_early = make_market(end_time=1000.0, asset="Bitcoin")
        market_late = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")    # CORE primary
        down_book = make_liquid_book(price=0.15, token_id="down")  # CHEAP hedge side

        def hedge_at(now):
            activity = MarketActivityState()
            activity.record_entry("Up", notional_usd=10.0, regime="CORE")
            monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: "Down")
            return build_order_intent(market_early if now < 500 else market_late,
                                       up_book, down_book, activity, random.Random(0), now=now)

        intent_early = hedge_at(now=1000.0 - 270.0)  # 270s remaining
        intent_late = hedge_at(now=1000.0 - 90.0)    # 90s remaining -- same seed, different ttc

        assert intent_early.notional_usd == intent_late.notional_usd, (
            "hedge notional must be identical regardless of time-to-close -- "
            "the ttc multiplier must not leak into HEDGE_SIZE_RATIO's formula"
        )

    def test_no_hedge_falls_through_to_normal_flow(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: None)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is not None
        assert intent.is_hedge is False

    def test_hedge_too_small_for_exchange_minimum_falls_through_to_ordinary_entry(self, monkeypatch):
        """CONFIRMED LIVE (2026-09-08): hedge notional -- deliberately
        small, proportional insurance -- falls below the exchange's real
        minimum far more often than ordinary entries. Previously this
        meant the whole tick produced NOTHING (decide_hedge intercepts
        every entry_count==1 evaluation, so a too-small hedge silently
        blocked the ordinary entry that tick would otherwise have placed
        too). Must now fall through to a normally-sized entry on the same
        side instead of abandoning the tick entirely."""
        # Bitcoin/CORE: dominant_cost=10.0 * hedge_size_ratio(~0.1062) ~= 1.06,
        # jittered to roughly 0.9-1.22 -- vs the ordinary CORE/2nd_3rd
        # median of 10.989 (this is the market's 2nd entry, so position_
        # tier is "2nd_3rd" not "first"; jittered ~9.3-12.6). A min_size
        # that only the hedge notional can fail: 5 shares * 0.80 = $4.00.
        market = make_market(end_time=1000.0, asset="Bitcoin", order_min_size=5.0)
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")  # CORE band
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE")

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None, "must not abandon the tick -- fall through to an ordinary entry"
        assert intent.is_hedge is False
        assert intent.side == "Down"
        assert intent.reason == "entry_curve"
        # The ordinary CORE/2nd_3rd curve, not the tiny hedge-ratio sizing.
        core_median = bc.median_entry_notional("Bitcoin", "CORE", "2nd_3rd")
        assert intent.notional_usd > core_median * 0.5
        assert intent.reason in ("entry_curve", "floor_lot")

    def test_hedge_size_scales_with_adverse_move(self, monkeypatch):
        """build_order_intent's hedge-sizing block, added 2026-09-10: the
        first hedge's notional must include ADVERSE_MOVE_SIZE_MULTIPLIER on
        top of the base HEDGE_SIZE_RATIO, computed from first_entry_price
        (recorded via record_entry's new price= kwarg) vs the hedge side's
        current book price."""
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")     # CORE primary, entered at 0.80
        down_book = make_liquid_book(price=0.30, token_id="down")  # hedge side -> dominant now at 1-0.30=0.70
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.is_hedge is True
        adverse_move = 0.80 - (1.0 - 0.30)  # == 0.10
        expected_ratio = bc.hedge_size_ratio("Bitcoin", "CORE") * bc.adverse_move_size_multiplier("Bitcoin", adverse_move)
        expected_notional = 10.0 * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_adverse_move_multiplier_is_a_noop_when_first_entry_price_was_never_recorded(self, monkeypatch):
        # Same setup as test_produces_a_hedge_intent_sized_off_dominant_cost
        # but for Bitcoin (which IS in ADVERSE_MOVE_SIZE_MULTIPLIER, unlike
        # BNB) -- record_entry called WITHOUT price=, matching every
        # pre-existing caller in this file, must leave sizing untouched.
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.30, token_id="down")
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE")  # no price= kwarg
        assert activity.first_entry_price is None

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        expected_ratio = bc.hedge_size_ratio("Bitcoin", "CORE")  # no multiplier applied
        expected_notional = 10.0 * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_adverse_move_multiplier_respects_the_per_instance_feature_flag(self, monkeypatch):
        # Explicit instruction: paperbot-mini stays on the OLD behavior
        # (as a control) via ENABLE_ADVERSE_MOVE_SIZE_MULTIPLIER=false.
        monkeypatch.setattr(config, "ENABLE_ADVERSE_MOVE_SIZE_MULTIPLIER", False)
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.30, token_id="down")
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        expected_ratio = bc.hedge_size_ratio("Bitcoin", "CORE")  # multiplier must be a strict no-op
        expected_notional = 10.0 * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_adverse_move_size_multiplier_itself_does_not_leak_into_continuation_hedges(self, monkeypatch):
        # The hedge_count==0-only multiplier (ADVERSE_MOVE_SIZE_MULTIPLIER)
        # must not apply to a continuation hedge -- but ADVERSE_MOVE_
        # CONTINUATION_SIZE_MULTIPLIER (added the same night, see below)
        # DOES apply here, using the same computed adverse_move. Disable
        # the continuation multiplier via its own flag to isolate this
        # specific scoping rule from that separate mechanism.
        monkeypatch.setattr(config, "ENABLE_ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER", False)
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.30, token_id="down")
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)
        activity.record_entry("Down", notional_usd=1.0, regime="CORE", is_hedge=True)
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        expected_ratio = bc.hedge_continuation_size_ratio(2)  # neither multiplier applied
        dominant_cost = 10.0
        expected_notional = dominant_cost * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_continuation_hedge_size_scales_with_adverse_move(self, monkeypatch):
        """build_order_intent's continuation-hedge sizing, added 2026-09-10:
        hedge_count>=1 must include ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER
        on top of HEDGE_CONTINUATION_SIZE_RATIO, using the same since-
        ORIGIN adverse_move as the first-hedge case (first_entry_price vs
        the hedge side's current book price)."""
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")     # CORE primary, entered at 0.80
        down_book = make_liquid_book(price=0.30, token_id="down")  # hedge side -> dominant now at 1-0.30=0.70
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)
        activity.record_entry("Down", notional_usd=1.0, regime="CORE", is_hedge=True)
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.is_hedge is True
        adverse_move = 0.80 - (1.0 - 0.30)  # == 0.10, since-origin (first_entry_price), not since-last-hedge
        expected_ratio = bc.hedge_continuation_size_ratio(2) * bc.adverse_move_continuation_size_multiplier("Bitcoin", adverse_move)
        dominant_cost = 10.0
        expected_notional = dominant_cost * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_continuation_adverse_move_multiplier_respects_the_per_instance_feature_flag(self, monkeypatch):
        # Explicit instruction: paperbot-mini stays on the OLD behavior
        # (as a control) via ENABLE_ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER=false.
        monkeypatch.setattr(config, "ENABLE_ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER", False)
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.30, token_id="down")
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE", price=0.80)
        activity.record_entry("Down", notional_usd=1.0, regime="CORE", is_hedge=True)
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        expected_ratio = bc.hedge_continuation_size_ratio(2)  # multiplier must be a strict no-op
        dominant_cost = 10.0
        expected_notional = dominant_cost * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_continuation_adverse_move_multiplier_is_a_noop_when_first_entry_price_was_never_recorded(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.30, token_id="down")
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE")  # no price= kwarg
        activity.record_entry("Down", notional_usd=1.0, regime="CORE", is_hedge=True)
        assert activity.first_entry_price is None
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        expected_ratio = bc.hedge_continuation_size_ratio(2)  # no multiplier applied
        dominant_cost = 10.0
        expected_notional = dominant_cost * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_computes_is_weekend_correctly_from_now_and_passes_it_to_decide_hedge(self, monkeypatch):
        """build_order_intent must derive is_weekend from `now` using the
        same UTC-weekday convention the calibration itself was measured
        with (real trades.jsonl timestamps, weekday() >= 5), and pass it
        through to decide_hedge -- not leave the wiring silently unused."""
        market = make_market(end_time=2_000_000_000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE")

        captured = {}

        def spy_decide_hedge(asset, activity, rng, liquidity=None, is_weekend=None,
                              dominant_current_price=None, prev_hedge_rate=None):
            captured["is_weekend"] = is_weekend
            return "Down"

        monkeypatch.setattr(stratmod, "decide_hedge", spy_decide_hedge)

        SATURDAY_NOON_UTC = 1704542400.0  # 2024-01-06, weekday()==5
        MONDAY_NOON_UTC = 1704715200.0    # 2024-01-08, weekday()==0

        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=SATURDAY_NOON_UTC)
        assert captured["is_weekend"] is True

        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=MONDAY_NOON_UTC)
        assert captured["is_weekend"] is False

    def test_computes_dominant_current_price_from_the_dominant_sides_own_book_and_passes_it_to_decide_hedge(self, monkeypatch):
        """build_order_intent must read the DOMINANT side's own book price
        (not the eventual hedge side's) and pass it through to
        decide_hedge -- confirms the wiring, not just that
        adverse_move_hedge_trigger_multiplier works in isolation."""
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.72, token_id="up")     # dominant (Up) side's LIVE price
        down_book = make_liquid_book(price=0.15, token_id="down")  # would-be hedge side, irrelevant here
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE")  # dominant side is "Up"

        captured = {}

        def spy_decide_hedge(asset, activity, rng, liquidity=None, is_weekend=None,
                              dominant_current_price=None, prev_hedge_rate=None):
            captured["dominant_current_price"] = dominant_current_price
            return None  # no hedge -- just observing what's passed in

        monkeypatch.setattr(stratmod, "decide_hedge", spy_decide_hedge)

        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert captured["dominant_current_price"] == 0.72  # Up's own book price, not Down's

    def test_dominant_current_price_is_none_before_any_entry_exists(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.72, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()  # no entries yet -- dominant_side() is None

        captured = {}

        def spy_decide_hedge(asset, activity, rng, liquidity=None, is_weekend=None,
                              dominant_current_price=None, prev_hedge_rate=None):
            captured["dominant_current_price"] = dominant_current_price
            return None

        monkeypatch.setattr(stratmod, "decide_hedge", spy_decide_hedge)

        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert captured["dominant_current_price"] is None

    def test_passes_previous_market_first_side_through_to_decide_side(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.72, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()  # no entries yet -- this is the first entry
        monkeypatch.setattr(stratmod, "decide_hedge",
                             lambda asset, activity, rng, liquidity=None, is_weekend=None,
                             dominant_current_price=None, prev_hedge_rate=None: None)

        captured = {}

        def spy_decide_side(asset, activity, rng, held_side_price=None, previous_market_first_side=None,
                             previous_market_won=None):
            captured["previous_market_first_side"] = previous_market_first_side
            return "Up"

        monkeypatch.setattr(stratmod, "decide_side", spy_decide_side)

        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0,
                            previous_market_first_side="Down")

        assert captured["previous_market_first_side"] == "Down"

    def test_respects_the_per_instance_feature_flag(self, monkeypatch):
        # Explicit instruction: paperbot-mini stays on the OLD (uniform
        # random) behavior via ENABLE_CROSS_MARKET_SIDE_PERSISTENCE=false.
        monkeypatch.setattr(config, "ENABLE_CROSS_MARKET_SIDE_PERSISTENCE", False)
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.72, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        monkeypatch.setattr(stratmod, "decide_hedge",
                             lambda asset, activity, rng, liquidity=None, is_weekend=None,
                             dominant_current_price=None, prev_hedge_rate=None: None)

        captured = {}

        def spy_decide_side(asset, activity, rng, held_side_price=None, previous_market_first_side=None,
                             previous_market_won=None):
            captured["previous_market_first_side"] = previous_market_first_side
            return "Up"

        monkeypatch.setattr(stratmod, "decide_side", spy_decide_side)

        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0,
                            previous_market_first_side="Down")

        assert captured["previous_market_first_side"] is None, \
            "with the flag off, previous_market_first_side must never reach decide_side"


class TestBuildOrderIntentScout:
    """SCOUT: a deliberately small, tentative FIRST entry -- the gap
    decide_hedge structurally can never close (dominant_side() needs at
    least one prior entry to exist, so decide_hedge always returns None at
    entry_count==0). See SCOUT_PROBABILITY/SCOUT_SIZE_RATIO's docstring in
    behavior_config.py."""

    def test_forced_scout_shrinks_the_first_entry_below_the_ordinary_curve(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.25, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: None)
        monkeypatch.setattr(bc, "scout_probability", lambda asset: 1.0)
        monkeypatch.setattr(bc, "scout_size_ratio", lambda asset: 0.5)

        scout_intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        # Control run: same seed, same everything, but scout forced off.
        monkeypatch.setattr(bc, "scout_probability", lambda asset: 0.0)
        activity2 = MarketActivityState()
        ordinary_intent = build_order_intent(market, up_book, down_book, activity2, random.Random(0), now=0.0)

        assert scout_intent is not None and ordinary_intent is not None
        assert scout_intent.is_scout is True
        assert scout_intent.reason == "scout_entry"
        assert scout_intent.is_hedge is False
        assert scout_intent.is_floor_lot is False
        assert scout_intent.side == ordinary_intent.side  # same rng draw for side selection
        assert scout_intent.notional_usd == pytest.approx(ordinary_intent.notional_usd * 0.5)

    def test_scout_never_fires_beyond_the_first_entry(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=5.0, regime="CHEAP")  # entry_count now 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: None)
        monkeypatch.setattr(bc, "scout_probability", lambda asset: 1.0)  # would always fire if reachable

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is not None
        assert intent.is_scout is False

    def test_scout_does_not_stack_with_a_floor_lot_roll(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="BNB")  # BNB/CHEAP/first has a real floor-lot rate
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()
        rng = random.Random(0)
        rng.random = lambda: 0.0  # forces the floor-lot roll ahead of the scout check

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None: None)
        monkeypatch.setattr(bc, "scout_probability", lambda asset: 1.0)  # would always fire if reachable

        intent = build_order_intent(market, up_book, down_book, activity, rng, now=0.0)
        assert intent is not None
        assert intent.is_floor_lot is True
        assert intent.is_scout is False

    def test_scout_probability_and_ratio_default_to_no_op_for_uncalibrated_assets(self):
        assert bc.scout_probability("XRP") == 0.0
        assert bc.scout_size_ratio("XRP") == bc._DEFAULT_SCOUT_SIZE_RATIO


class TestBuildOrderIntentMaxNotionalCap:
    """Hard per-order cap, added 2026-09-11 for the $100-bankroll safety
    work. Clamp-DOWN only, applied strictly before the exchange-minimum
    bump-up check -- confirmed (before shipping) this can never itself
    cause a trade to be skipped that would otherwise have happened,
    unlike a rigid dollar cap which could collide with the exchange's
    own per-share minimum once equity drops."""

    def test_defaults_to_a_noop_when_not_passed(self):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity_a = MarketActivityState()
        activity_b = MarketActivityState()

        intent_no_arg = build_order_intent(market, up_book, down_book, activity_a,
                                            random.Random(0), now=0.0)
        intent_explicit_none = build_order_intent(market, up_book, down_book, activity_b,
                                                   random.Random(0), now=0.0, max_notional_usd=None)
        assert intent_no_arg.notional_usd == intent_explicit_none.notional_usd

    def test_clamps_an_oversized_ordinary_entry_down_to_the_cap(self):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")  # CORE
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()

        uncapped = build_order_intent(market, up_book, down_book, activity,
                                       random.Random(0), now=0.0)
        assert uncapped is not None
        tiny_cap = uncapped.notional_usd / 10.0

        capped_activity = MarketActivityState()
        capped = build_order_intent(market, up_book, down_book, capped_activity,
                                     random.Random(0), now=0.0, max_notional_usd=tiny_cap)
        assert capped is not None
        assert capped.notional_usd <= tiny_cap + 1e-9

    def test_never_leaves_the_order_below_the_exchange_minimum(self):
        """The whole point of applying this BEFORE the exchange-minimum
        bump-up check: a cap tight enough to bind must still produce a
        legally placeable order, not a skip."""
        market = make_market(end_time=1000.0, asset="Bitcoin", order_min_size=5.0)
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()

        # A cap far below the exchange minimum on either side (5 shares *
        # whichever price decide_side lands on).
        intent = build_order_intent(market, up_book, down_book, activity,
                                     random.Random(0), now=0.0, max_notional_usd=0.01)
        assert intent is not None
        assert intent.notional_usd >= 5.0 * intent.price - 1e-9

    def test_does_not_affect_the_floor_lot_tier(self):
        market = make_market(end_time=1000.0, asset="BNB")  # BNB/CHEAP/first has a real floor-lot rate
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        rng_a = random.Random(0)
        rng_a.random = lambda: 0.0  # forces the floor-lot roll
        rng_b = random.Random(0)
        rng_b.random = lambda: 0.0

        uncapped = build_order_intent(make_market(end_time=1000.0, asset="BNB"), up_book, down_book,
                                       MarketActivityState(), rng_a, now=0.0)
        capped = build_order_intent(make_market(end_time=1000.0, asset="BNB"), up_book, down_book,
                                     MarketActivityState(), rng_b, now=0.0, max_notional_usd=0.0001)
        assert uncapped is not None and capped is not None
        assert uncapped.is_floor_lot is True
        assert capped.is_floor_lot is True
        assert capped.notional_usd == uncapped.notional_usd

    def test_also_clamps_hedge_sizing(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="BNB")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=100.0, regime="CORE")

        monkeypatch.setattr(
            stratmod, "decide_hedge",
            lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None,
            prev_hedge_rate=None: "Down")

        uncapped = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert uncapped is not None
        assert uncapped.is_hedge is True

        capped_activity = MarketActivityState()
        capped_activity.record_entry("Up", notional_usd=100.0, regime="CORE")
        capped = build_order_intent(market, up_book, down_book, capped_activity, random.Random(0),
                                     now=0.0, max_notional_usd=uncapped.notional_usd / 10.0)
        assert capped is not None
        assert capped.notional_usd < uncapped.notional_usd
