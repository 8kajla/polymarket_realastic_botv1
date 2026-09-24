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
    cross_asset_carryover_side,
    decide_hedge,
    decide_side,
    decide_size,
    load_activity_state,
    mid_contrarian_first_entry_side,
    save_activity_state,
    timing_ok,
)


def make_market(end_time, asset="Bitcoin", condition_id="c1", order_min_size=None, slug=None):
    # slug's embedded window_start defaults to 0 -- every existing test's
    # `now` is >= 0, so the window always reads as "already started",
    # preserving pre-2026-09-18 behavior. Pass an explicit slug to test
    # the open-side timing_ok check itself (see TestTimingOkWindowStart).
    return Market(
        condition_id=condition_id, slug=slug if slug is not None else "btc-updown-5m-0",
        question="q", asset=asset, end_time=end_time, token_id_up="up", token_id_down="down",
        order_min_size=order_min_size, order_min_tick_size=0.01,
    )


def make_liquid_book(price=0.20, token_id="up"):
    book = BookState(token_id=token_id)
    book.apply_snapshot(bids=[(price, 100.0)], asks=[(round(price + 0.01, 6), 100.0)])
    return book


def record_filled_entry(activity, side, notional_usd=0.0, regime=None, is_hedge=False,
                         price=None):
    """Test helper (bug audit #5): record_entry() alone only simulates a
    PLACEMENT -- decide_hedge/decide_size now read real_fill_count/
    real_hedge_fill_count (only incremented by record_real_fill, which in
    production is called from bot.py's manage_orders_tick once an order
    actually fills) for every calibration-index lookup. Most of this test
    file's scenarios are about calibration LOGIC and intend "this entry
    represents a genuine, filled trade" -- this helper does both calls
    together so those tests keep expressing that intent directly, without
    every call site needing to know about the placement/fill distinction
    itself."""
    activity.record_entry(side, notional_usd=notional_usd, regime=regime,
                           is_hedge=is_hedge, price=price)
    activity.record_real_fill(is_hedge=is_hedge)


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

    # -- open-side check, added 2026-09-18 (pre-window order-placement fix) --

    def test_skips_before_the_window_has_opened(self):
        # slug's embedded window_start is 1789668000 -- now is 300s before it
        market = make_market(end_time=1789668900.0, slug="btc-updown-5m-1789668000")
        assert timing_ok(market, now=1789667700.0) is False

    def test_allows_exactly_at_window_open(self):
        market = make_market(end_time=1789668900.0, slug="btc-updown-5m-1789668000")
        assert timing_ok(market, now=1789668000.0) is True

    def test_allows_after_window_open_with_plenty_of_time_left(self):
        market = make_market(end_time=1789668900.0, slug="btc-updown-5m-1789668000")
        assert timing_ok(market, now=1789668100.0) is True

    def test_still_respects_the_close_side_cutoff_after_a_valid_open(self):
        market = make_market(end_time=1789668900.0, slug="btc-updown-5m-1789668000")
        just_under_cutoff = 1789668900.0 - (config.MIN_SECONDS_BEFORE_CLOSE - 1)
        assert timing_ok(market, now=just_under_cutoff) is False

    def test_falls_back_to_old_behavior_when_slug_does_not_parse(self):
        # A slug that doesn't end in a bare integer -- window_start() is
        # None, so the open-side check must no-op rather than block.
        market = make_market(end_time=1000.0, slug="not-a-real-slug")
        assert timing_ok(market, now=0.0) is True


class TestMarketWindowStart:
    """Market.window_start()/seconds_until_start() (2026-09-18): parses
    the window's own open time directly from the slug, the same
    convention market_discovery.BUCKET_SECONDS documents."""

    def test_window_start_parses_the_slug_suffix(self):
        market = make_market(end_time=1000.0, slug="btc-updown-5m-1789668000")
        assert market.window_start() == 1789668000.0

    def test_window_start_is_none_for_a_non_numeric_slug(self):
        market = make_market(end_time=1000.0, slug="not-a-real-slug")
        assert market.window_start() is None

    def test_seconds_until_start_is_positive_before_open(self):
        market = make_market(end_time=1000.0, slug="btc-updown-5m-1789668000")
        assert market.seconds_until_start(now=1789667700.0) == pytest.approx(300.0)

    def test_seconds_until_start_is_non_positive_after_open(self):
        market = make_market(end_time=1000.0, slug="btc-updown-5m-1789668000")
        assert market.seconds_until_start(now=1789668100.0) == pytest.approx(-100.0)

    def test_seconds_until_start_is_none_when_slug_does_not_parse(self):
        market = make_market(end_time=1000.0, slug="not-a-real-slug")
        assert market.seconds_until_start(now=0.0) is None


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
    @pytest.fixture(autouse=True)
    def _isolate_recent_override(self, monkeypatch):
        # These tests pin the raw SIDE_PERSISTENCE table; the recent override has its own class.
        monkeypatch.setattr(config, "ENABLE_RECENT_SIDE_PERSISTENCE", False)

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
    @pytest.fixture(autouse=True)
    def _isolate_level_correction(self, monkeypatch):
        # These tests pin the raw base curve; the Bitcoin level correction
        # has its own TestEntrySizeLevelCorrection class.
        monkeypatch.setattr(config, "ENABLE_ENTRY_SIZE_LEVEL_CORRECTION", False)

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

    def test_deep_cheap_tail_correction_shrinks_entries_below_threshold(self):
        # ADDED 2026-09-23: price<0.08 in CHEAP should be shrunk relative
        # to what within_band_size_multiplier alone would give -- confirms
        # decide_size actually applies deep_cheap_tail_multiplier, not just
        # that the function itself works in isolation.
        rng_a = random.Random(13)
        rng_b = random.Random(13)
        notional_deep, _ = decide_size("Ethereum", "CHEAP", "4th_plus", 0.05, rng_a)
        median = bc.median_entry_notional("Ethereum", "CHEAP", "4th_plus")
        within_band = bc.within_band_size_multiplier("Ethereum", "CHEAP", 0.05)
        uncorrected = median * within_band
        # notional_deep should be roughly uncorrected * DEEP_CHEAP_TAIL_
        # MULTIPLIER["Ethereum"], within jitter bounds.
        expected = uncorrected * bc.DEEP_CHEAP_TAIL_MULTIPLIER["Ethereum"]
        assert expected * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= notional_deep <= \
            expected * (1 + config.SIZING_JITTER_FRACTION) * 1.01
        assert notional_deep < uncorrected * (1 + config.SIZING_JITTER_FRACTION)

    def test_deep_cheap_tail_correction_does_not_apply_above_threshold_or_outside_cheap(self):
        rng_a = random.Random(17)
        rng_b = random.Random(17)
        # at price=0.08 exactly (not < threshold): no correction
        notional_at, _ = decide_size("Ethereum", "CHEAP", "4th_plus", 0.08, rng_a)
        median = bc.median_entry_notional("Ethereum", "CHEAP", "4th_plus")
        within_band = bc.within_band_size_multiplier("Ethereum", "CHEAP", 0.08)
        expected = median * within_band
        assert expected * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= notional_at <= \
            expected * (1 + config.SIZING_JITTER_FRACTION) * 1.01
        # MID regime, even at a deep price, must not be touched (correction
        # is CHEAP-only)
        rng_c = random.Random(19)
        notional_mid, _ = decide_size("Ethereum", "MID", "4th_plus", 0.05, rng_c)
        assert notional_mid > 0  # sanity -- just confirms no exception/zeroing

    def test_deep_cheap_tail_correction_respects_its_own_flag(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_DEEP_CHEAP_TAIL_CORRECTION", False)
        rng_a = random.Random(13)
        rng_b = random.Random(13)
        notional_deep, _ = decide_size("Ethereum", "CHEAP", "4th_plus", 0.05, rng_a)
        median = bc.median_entry_notional("Ethereum", "CHEAP", "4th_plus")
        within_band = bc.within_band_size_multiplier("Ethereum", "CHEAP", 0.05)
        expected = median * within_band
        assert expected * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= notional_deep <= \
            expected * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_share_based_sizing_off_by_default_preserves_old_behavior(self):
        assert config.ENABLE_SHARE_BASED_SIZING is False
        rng_a = random.Random(11)
        rng_b = random.Random(11)
        notional_default, _ = decide_size("Bitcoin", "CORE", "first", 0.8, rng_a)
        median = bc.median_entry_notional("Bitcoin", "CORE", "first")
        within_band = bc.within_band_size_multiplier("Bitcoin", "CORE", 0.8)
        assert median * within_band * 0.8 <= notional_default <= median * within_band * 1.2
        # sanity: explicitly toggling the flag off (matching the real
        # default) must produce the identical draw sequence/result.
        with_flag_off = decide_size("Bitcoin", "CORE", "first", 0.8, rng_b)
        assert notional_default == with_flag_off[0]

    def test_share_based_sizing_on_uses_notional_for_price(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_SHARE_BASED_SIZING", True)
        rng = random.Random(1)
        notional, is_floor_lot = decide_size("Ethereum", "MID", "first", 0.35, rng)
        assert is_floor_lot is False
        expected = bc.notional_for_price(0.35, "first")
        # within jitter bounds (SIZING_JITTER_FRACTION), not exact --
        # other multipliers (momentum/bankroll/etc.) all default to 1.0
        # when their optional args aren't passed, so only jitter should
        # move this away from the bare notional_for_price value.
        assert expected * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= notional <= \
            expected * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_share_based_sizing_on_ignores_the_old_median_and_within_band_tables(self, monkeypatch):
        # Solana MID has real WITHIN_BAND_SIZE_SLOPE/ENTRY_SIZING_USD
        # entries -- with the flag on, decide_size must stop reading them
        # entirely, not just override their product's magnitude.
        monkeypatch.setattr(config, "ENABLE_SHARE_BASED_SIZING", True)
        monkeypatch.setattr(bc, "median_entry_notional",
                             lambda *a, **kw: (_ for _ in ()).throw(AssertionError(
                                 "median_entry_notional must not be called when "
                                 "ENABLE_SHARE_BASED_SIZING is on")))
        monkeypatch.setattr(bc, "within_band_size_multiplier",
                             lambda *a, **kw: (_ for _ in ()).throw(AssertionError(
                                 "within_band_size_multiplier must not be called when "
                                 "ENABLE_SHARE_BASED_SIZING is on")))
        rng = random.Random(2)
        notional, is_floor_lot = decide_size("Solana", "MID", "2nd_3rd", 0.45, rng)
        assert is_floor_lot is False
        assert notional > 0

    def test_share_based_sizing_position_tier_is_now_a_noop(self, monkeypatch):
        # RETIRED 2026-09-23: POSITION_TIER_SHARE_MULTIPLIER's tier
        # escalation was found to be a 5s-merge-convention artifact (see
        # its docstring / [[five-second-merge-convention-decisively-wrong]]),
        # not a real effect -- all tiers now size the same on average.
        monkeypatch.setattr(config, "ENABLE_SHARE_BASED_SIZING", True)

        def avg_notional(position_tier, n=300):
            rng = random.Random(4)
            total = 0.0
            for _ in range(n):
                notional, _ = decide_size("Bitcoin", "MID", position_tier, 0.4, rng)
                total += notional
            return total / n

        first = avg_notional("first")
        second = avg_notional("2nd_3rd")
        fourth = avg_notional("4th_plus")
        assert first == pytest.approx(second, rel=0.1)
        assert first == pytest.approx(fourth, rel=0.1)

    def test_share_based_sizing_is_scoped_to_mid_core_and_high(self, monkeypatch):
        # 2026-09-22: CHEAP stays on the old path even with the flag on
        # (share base fits worse there -- see SHARE_BASED_SIZING_REGIMES).
        # 2026-09-23: HIGH was added once HIGH_TAIL_SHARE_MULTIPLIER made
        # the share base competitive with the old path there too.
        assert bc.SHARE_BASED_SIZING_REGIMES == ("MID", "CORE", "HIGH")
        monkeypatch.setattr(config, "ENABLE_SHARE_BASED_SIZING", True)
        calls = []
        real_median = bc.median_entry_notional
        monkeypatch.setattr(bc, "median_entry_notional",
                             lambda *a, **kw: (calls.append(a[1]), real_median(*a, **kw))[1])
        decide_size("Ethereum", "CHEAP", "first", 0.15, random.Random(3))
        assert calls == ["CHEAP"]                  # old median path used
        calls.clear()
        for regime, price in (("MID", 0.45), ("CORE", 0.80), ("HIGH", 0.95)):
            decide_size("Ethereum", regime, "first", price, random.Random(3))
        assert calls == []                         # share path: old median never read

    def test_cross_market_size_momentum_defaults_to_a_noop_when_not_passed(self):
        rng_a = random.Random(37)
        rng_b = random.Random(37)
        notional_no_arg, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_a)
        notional_explicit_none, _ = decide_size("Bitcoin", "MID", "first", 0.5, rng_b,
                                                 size_momentum_residual=None)
        assert notional_no_arg == notional_explicit_none

    def test_cross_market_size_momentum_is_now_a_noop_for_every_asset(self):
        # Bitcoin/Ethereum were REMOVED at the 2026-09-13 post-halt
        # recalibration; Solana (the last survivor) RETIRED 2026-09-23
        # (systematic post-Sep-18 audit) -- a proper regime-normalized
        # re-derivation found the effect has genuinely vanished (r=0.0531,
        # t=1.27, null in both split halves). Table is now empty for
        # every asset -- see CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER's
        # docstring in behavior_config.py.
        def avg_notional(residual, n=400):
            rng = random.Random(41)
            total = 0.0
            for _ in range(n):
                notional, _ = decide_size("Solana", "MID", "first", 0.5, rng,
                                           size_momentum_residual=residual)
                total += notional
            return total / n

        low = avg_notional(-5.0)
        high = avg_notional(5.0)
        assert low == high

    def test_cross_market_size_momentum_only_applies_to_the_first_tier(self):
        # The finding was measured on first-entry size specifically --
        # later position tiers in the same market must be unaffected.
        # Uses Solana for the same reason as the test just above.
        rng_a = random.Random(43)
        rng_b = random.Random(43)
        notional_no_residual, _ = decide_size("Solana", "MID", "4th_plus", 0.5, rng_a)
        notional_with_residual, _ = decide_size("Solana", "MID", "4th_plus", 0.5, rng_b,
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

    def test_bankroll_pnl_is_now_a_noop_regardless_of_pnl(self):
        # RETIRED 2026-09-23 (systematic post-Sep-18 audit): the original
        # finding (sizes DOWN after profit, UP after a drawdown) failed a
        # fresh, leak-free, rolling-window re-derivation for both
        # Ethereum and Solana -- see BANKROLL_PNL_SIZE_MULTIPLIER's
        # docstring in behavior_config.py. Table emptied; pnl no longer
        # moves size at all.
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
        assert after_profit == after_drawdown

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

    def test_reentry_fatigue_defaults_to_a_noop_when_not_passed(self):
        rng_a = random.Random(83)
        rng_b = random.Random(83)
        notional_no_arg, _ = decide_size("Ethereum", "MID", "4th_plus", 0.5, rng_a)
        notional_explicit_none, _ = decide_size("Ethereum", "MID", "4th_plus", 0.5, rng_b,
                                                 real_fill_count=None)
        assert notional_no_arg == notional_explicit_none

    def test_reentry_fatigue_dampens_size_at_and_beyond_threshold(self):
        # Exercise every calibrated (asset, regime) cell, not just MID --
        # this generalized 2026-09-13 from an originally MID-only feature.
        # Price must be a realistic value FOR EACH regime (not one fixed
        # 0.5 for all of them, which real strategy.py code could never
        # produce -- regime and price always come from the same
        # classify_regime(price) call in production) -- a fixed 0.5 for
        # CORE/HIGH regimes started hitting WITHIN_BAND_SIZE_SLOPE's own
        # extrapolation cap after the 2026-09-13 post-halt recalibration
        # steepened some slopes, saturating COMBINED_SIZE_MULTIPLIER_CAP
        # and masking the very effect this test means to isolate.
        REALISTIC_PRICE = {"CHEAP": 0.15, "MID": 0.5, "CORE": 0.80, "HIGH": 0.95}
        for (asset, regime), threshold in bc.REENTRY_FATIGUE_THRESHOLD.items():
            price = REALISTIC_PRICE[regime]

            def avg_notional(real_fill_count, n=400):
                rng = random.Random(89)
                total = 0.0
                for _ in range(n):
                    notional, _ = decide_size(asset, regime, "4th_plus", price, rng,
                                               real_fill_count=real_fill_count)
                    total += notional
                return total / n

            before_threshold = avg_notional(threshold - 1)
            at_threshold = avg_notional(threshold)
            assert at_threshold < before_threshold, f"{asset}/{regime} did not dampen at threshold"

    def test_reentry_fatigue_is_a_noop_for_excluded_cells(self):
        # Deliberately excluded, not just uncalibrated -- see
        # REENTRY_FATIGUE_MULTIPLIER's docstring in behavior_config.py.
        for asset, regime in [("Bitcoin", "MID"), ("Solana", "CORE"), ("Bitcoin", "CHEAP")]:
            rng_a = random.Random(97)
            rng_b = random.Random(97)
            notional_low, _ = decide_size(asset, regime, "4th_plus", 0.5, rng_a,
                                           real_fill_count=1)
            notional_high, _ = decide_size(asset, regime, "4th_plus", 0.5, rng_b,
                                            real_fill_count=20)
            assert notional_low == notional_high, f"{asset}/{regime} should be a strict no-op"

    def test_reentry_fatigue_respects_the_per_instance_feature_flag(self, monkeypatch):
        # Ethereum/CHEAP (not MID -- Ethereum/MID's own fatigue effect
        # vanished post-halt and was removed from the table entirely in
        # the 2026-09-13 /loop cycle 10 recalibration, which would make
        # this test trivially pass even with the flag ON and not actually
        # exercise the flag's gating).
        monkeypatch.setattr(config, "ENABLE_REENTRY_FATIGUE_DAMPENER", False)
        rng_a = random.Random(101)
        rng_b = random.Random(101)
        notional_low, _ = decide_size("Ethereum", "CHEAP", "4th_plus", 0.15, rng_a,
                                       real_fill_count=1)
        notional_high, _ = decide_size("Ethereum", "CHEAP", "4th_plus", 0.15, rng_b,
                                        real_fill_count=20)
        assert notional_low == notional_high, (
            "with the flag off, re-entry fatigue must be a strict no-op"
        )

    def test_hedge_count_reinforcement_defaults_to_a_noop_when_not_passed(self):
        rng_a = random.Random(103)
        rng_b = random.Random(103)
        notional_no_arg, _ = decide_size("Ethereum", "MID", "4th_plus", 0.5, rng_a)
        notional_explicit_none, _ = decide_size("Ethereum", "MID", "4th_plus", 0.5, rng_b,
                                                 live_hedge_count=None)
        assert notional_no_arg == notional_explicit_none

    def test_hedge_count_reinforcement_boosts_size_at_and_beyond_threshold(self):
        price_for_regime = {"CHEAP": 0.2, "MID": 0.5, "CORE": 0.8, "HIGH": 0.95}
        for (asset, regime), threshold in bc.HEDGE_COUNT_REINFORCEMENT_THRESHOLD.items():
            price = price_for_regime[regime]

            def avg_notional(live_hedge_count, n=400):
                rng = random.Random(107)
                total = 0.0
                for _ in range(n):
                    notional, _ = decide_size(asset, regime, "4th_plus", price, rng,
                                               live_hedge_count=live_hedge_count)
                    total += notional
                return total / n

            before_threshold = avg_notional(threshold - 1)
            at_threshold = avg_notional(threshold)
            assert at_threshold > before_threshold, f"{asset}/{regime} did not boost at threshold"

    def test_hedge_count_reinforcement_is_a_noop_for_excluded_cells(self):
        for asset, regime in [("Bitcoin", "MID"), ("Ethereum", "CORE"), ("Solana", "HIGH")]:
            rng_a = random.Random(109)
            rng_b = random.Random(109)
            notional_low, _ = decide_size(asset, regime, "4th_plus", 0.5, rng_a,
                                           live_hedge_count=1)
            notional_high, _ = decide_size(asset, regime, "4th_plus", 0.5, rng_b,
                                            live_hedge_count=20)
            assert notional_low == notional_high, f"{asset}/{regime} should be a strict no-op"

    def test_hedge_count_reinforcement_respects_the_per_instance_feature_flag(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_HEDGE_COUNT_REINFORCEMENT", False)
        rng_a = random.Random(113)
        rng_b = random.Random(113)
        notional_low, _ = decide_size("Ethereum", "CHEAP", "4th_plus", 0.2, rng_a,
                                       live_hedge_count=1)
        notional_high, _ = decide_size("Ethereum", "CHEAP", "4th_plus", 0.2, rng_b,
                                        live_hedge_count=20)
        assert notional_low == notional_high, (
            "with the flag off, hedge-count reinforcement must be a strict no-op"
        )

    def test_reentry_fatigue_and_hedge_count_reinforcement_dont_compound_pathologically(self):
        """/loop iter 139 due-diligence check: originally 4 (asset, regime)
        cells -- Ethereum/Solana x CHEAP/MID -- were calibrated for BOTH
        REENTRY_FATIGUE (keyed on same-side real_fill_count) and
        HEDGE_COUNT_REINFORCEMENT (keyed on opposite-side live_hedge_count)
        simultaneously, since a market can plausibly have both a long
        same-side ladder AND multiple hedges at once. Verifies the two
        real, shipped multipliers combine to a near-neutral product in
        every overlapping cell (a market worked hard on both sides reads
        as genuinely uncertain, not a directional compounding signal) --
        not just that some generic worst-case mock stays under the cap.

        UPDATED (same day): HEDGE_COUNT_REINFORCEMENT gained a second,
        bigger tier -- re-checked the WORST case (reentry fatigue x the
        higher tier-2 multiplier) explicitly, not just tier 1. Widened the
        band to 1.4 to match (max real combination is 1.314x, still a very
        modest adjustment relative to COMBINED_SIZE_MULTIPLIER_CAP=4.0).

        UPDATED 2026-09-13 (/loop cycle 10, post-halt audit): Ethereum/MID
        and Solana/MID's REENTRY_FATIGUE effect genuinely vanished
        post-halt and was removed from that table entirely -- only
        Ethereum/CHEAP and Solana/CHEAP still overlap with
        HEDGE_COUNT_REINFORCEMENT now, 2 cells not 4.

        UPDATED AGAIN 2026-09-13 (/loop cycle 22, same day): cycle 10's
        REENTRY_FATIGUE re-check itself used the wrong fill-index
        semantics (same-side-only, not the true combined real_fill_count
        -- see ENTRY_SIZING_USD's cycle-21 fix for the same bug class).
        Redone correctly: Solana/CHEAP's earlier "real" verdict was
        itself an artifact of that mis-indexing and is now removed
        (genuinely flat, z=-0.106); Ethereum/CORE, previously removed,
        is restored (z=-2.561, borderline but consistent with Bitcoin/
        CORE's confirmed-real magnitude). Only Ethereum/CHEAP now
        overlaps with HEDGE_COUNT_REINFORCEMENT -- 1 cell, not 2. The
        overlap set is still computed dynamically below rather than
        hardcoded, so this test adapts on its own regardless of how many
        cells end up overlapping; the loop assertion still exercises
        real values either way."""
        overlap_cells = set(bc.REENTRY_FATIGUE_MULTIPLIER) & set(bc.HEDGE_COUNT_REINFORCEMENT_MULTIPLIER)
        assert overlap_cells, "expected at least one overlapping cell to actually exercise this"
        for asset, regime in overlap_cells:
            reentry_threshold = bc.REENTRY_FATIGUE_THRESHOLD[(asset, regime)]
            reentry_mult = bc.reentry_fatigue_multiplier(asset, regime, reentry_threshold)
            for tier_threshold, hedge_mult in bc.HEDGE_COUNT_REINFORCEMENT_TIERS[(asset, regime)]:
                got = bc.hedge_count_reinforcement_multiplier(asset, regime, tier_threshold)
                assert got == hedge_mult
                product = reentry_mult * hedge_mult
                assert 0.7 <= product <= 1.4, (
                    f"{asset}/{regime}@hedge_tier={tier_threshold}: combined product {product:.4f} "
                    "outside the intended near-neutral band -- re-examine the calibrated values"
                )

    def test_combined_multiplier_cap_bounds_worst_case_compounding(self, monkeypatch):
        """FIXED 2026-09-12 (bug audit #4): each cross-market/time
        multiplier was fit MARGINALLY and none of them, individually,
        reaches anywhere near its own defensive cap under the real
        calibration tables (they're all "flat beyond the measured range"
        at modest values) -- so this test monkeypatches the underlying
        behavior_config functions directly to force a pathological
        all-aligned-in-the-same-direction case, the exact scenario
        BUGS_TO_FIX.md #4 flagged as unvalidated (e.g. momentum_mult and
        bankroll_mult both plausibly picking up overlapping "recent
        performance" signal). Confirms decide_size's COMBINED_SIZE_
        MULTIPLIER_CAP clamps the joint product, not just each factor's
        own individual (much higher) cap."""
        monkeypatch.setattr(config, "SIZING_JITTER_FRACTION", 0.0)  # deterministic
        monkeypatch.setattr(config, "ENABLE_RESUMPTION_SIZE_MULTIPLIER", True)
        monkeypatch.setattr(bc, "within_band_size_multiplier", lambda asset, regime, price: 3.0)
        monkeypatch.setattr(bc, "cross_market_size_momentum_multiplier", lambda asset, r: 3.0)
        monkeypatch.setattr(bc, "bankroll_pnl_size_multiplier", lambda asset, r: 3.0)
        monkeypatch.setattr(bc, "ttc_size_multiplier", lambda asset, regime, secs: 3.0)
        monkeypatch.setattr(bc, "resumption_size_multiplier", lambda hours: 3.0)

        median = bc.median_entry_notional("Ethereum", "MID", "first")
        notional, _ = decide_size(
            "Ethereum", "MID", "first", 0.5, random.Random(0),
            seconds_remaining=200.0, hours_since_resumption=5.0,
            size_momentum_residual=1.0, bankroll_pnl_residual=1.0,
        )
        # Raw (uncapped) product would be median * 3.0**5 = median * 243 --
        # the combined cap must clamp this down to median * COMBINED_SIZE_
        # MULTIPLIER_CAP instead.
        assert notional == pytest.approx(median * config.COMBINED_SIZE_MULTIPLIER_CAP)

    def test_combined_multiplier_cap_also_bounds_the_downside(self, monkeypatch):
        """Same mechanism, opposite direction -- every factor aligned
        SUPPRESSING size must be clamped at 1/COMBINED_SIZE_MULTIPLIER_CAP,
        not multiplied down toward zero."""
        monkeypatch.setattr(config, "SIZING_JITTER_FRACTION", 0.0)
        monkeypatch.setattr(config, "ENABLE_RESUMPTION_SIZE_MULTIPLIER", True)
        small = 1.0 / 3.0
        monkeypatch.setattr(bc, "within_band_size_multiplier", lambda asset, regime, price: small)
        monkeypatch.setattr(bc, "cross_market_size_momentum_multiplier", lambda asset, r: small)
        monkeypatch.setattr(bc, "bankroll_pnl_size_multiplier", lambda asset, r: small)
        monkeypatch.setattr(bc, "ttc_size_multiplier", lambda asset, regime, secs: small)
        monkeypatch.setattr(bc, "resumption_size_multiplier", lambda hours: small)

        median = bc.median_entry_notional("Ethereum", "MID", "first")
        notional, _ = decide_size(
            "Ethereum", "MID", "first", 0.5, random.Random(0),
            seconds_remaining=200.0, hours_since_resumption=5.0,
            size_momentum_residual=1.0, bankroll_pnl_residual=1.0,
        )
        assert notional == pytest.approx(median / config.COMBINED_SIZE_MULTIPLIER_CAP)

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

    def test_resumption_multiplier_suppresses_size_early_and_overshoots_later(self, monkeypatch):
        # Explicit instruction (bug audit #7): ENABLE_RESUMPTION_SIZE_MULTIPLIER
        # now defaults to False (the trigger that decided WHEN to apply this
        # multiplier in production was measuring the wrong event -- see
        # BUGS_TO_FIX.md #7) -- this test is still valid coverage of the
        # multiplier FUNCTION's own behavior in isolation, so it opts back in
        # explicitly rather than relying on the (now-disabled) default.
        monkeypatch.setattr(config, "ENABLE_RESUMPTION_SIZE_MULTIPLIER", True)

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

    def test_returns_none_before_the_window_has_opened(self):
        # Reproduces the confirmed live bug (2026-09-18): market_discovery
        # onboards the NEXT window early, and this market's window hasn't
        # opened yet even though there's plenty of time before it closes.
        market = make_market(end_time=1789668900.0, slug="btc-updown-5m-1789668000")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0),
                                     now=1789667700.0)  # 300s before window_start
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
        # 0.04 is below CHEAP_JOINT_PRICE_THRESHOLD -- not what this test
        # is about, disable the dampener's randomness so it can't skip.
        monkeypatch.setattr(bc, "cheap_joint_entry_multiplier", lambda asset, price, elapsed: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.side == "Down"
        assert intent.price == 0.04
        assert intent.regime == "CHEAP"  # not "HIGH", which up_book's 0.95 would wrongly give
        # sized off the CHEAP curve at Down's real price, not a HIGH notional / 0.04
        cheap_median = bc.median_entry_notional(market.asset, "CHEAP", "first")
        assert intent.notional_usd < cheap_median * 2  # generous jitter allowance

    def test_seconds_remaining_is_threaded_through_to_ttc_size_multiplier(self, monkeypatch):
        """End-to-end: build_order_intent computes seconds_remaining from
        the real market/now and passes it into decide_size, actually
        moving the resulting notional -- not just that the multiplier
        function itself works in isolation (already covered in
        TestTtcSizeMultiplier) or that decide_size applies it when called
        directly (already covered in TestSizingDecision)."""
        up_book = make_liquid_book(price=0.95, token_id="up")  # HIGH band, Bitcoin-calibrated
        down_book = make_liquid_book(price=0.04, token_id="down")
        # 0.04 is below CHEAP_JOINT_PRICE_THRESHOLD -- not what this test
        # is about, disable the dampener's randomness so it can't skip.
        monkeypatch.setattr(bc, "cheap_joint_entry_multiplier", lambda asset, price, elapsed: 1.0)

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

    def test_decisiveness_fraction_is_computed_from_activitys_crossing_and_threaded_through(self, monkeypatch):
        """End-to-end: build_order_intent must compute decisiveness_fraction
        from activity.up_decisive_crossed_at/market.end_time/now itself
        (strategy.py has no other way to learn a crossing happened) and
        pass it into decide_size, actually moving the resulting notional --
        not just that the multiplier function works in isolation (already
        covered in TestDecisivenessOnsetMultiplier). decide_side is forced
        to "Up" -- an unforced first entry isn't deterministic (see
        test_regime_price_and_sizing_come_from_the_chosen_sides_book's own
        use of the same pattern), and which side gets traded is not what
        this test is checking."""
        monkeypatch.setattr(stratmod, "decide_side",
                             lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        # Neutralize the separate decisive-entry ELIGIBILITY gate (added
        # 2026-09-20, after this test) -- it also keys off elapsed time
        # since the same crossing and would otherwise probabilistically
        # skip the "slow_reaction" case here, which is testing
        # DECISIVENESS_ONSET_MULTIPLIER's SIZE wiring specifically, not
        # this newer, separate eligibility mechanism.
        monkeypatch.setattr(bc, "decisive_entry_eligibility_multiplier", lambda asset, elapsed: 1.0)
        up_book = make_liquid_book(price=0.75, token_id="up")  # CORE band, Bitcoin-calibrated
        down_book = make_liquid_book(price=0.20, token_id="down")

        def notional_at(now, crossed_at, seed=0):
            market = make_market(end_time=1000.0, asset="Bitcoin")
            activity = MarketActivityState()
            activity.up_decisive_crossed_at = crossed_at
            intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=now)
            return intent.notional_usd if intent is not None else None

        # crossed at t=600 (400s available to close): fraction at t=620 is
        # 20/400=0.05 (the 1.74x bucket); at t=920 it's 320/400=0.80 (the
        # 0.71x bucket) -- a clean contrast, both still clearing
        # MIN_SECONDS_BEFORE_CLOSE (75s) so neither call returns None for
        # an unrelated reason.
        tight_reaction = notional_at(now=620.0, crossed_at=600.0)     # fraction=0.05 -> 1.74x
        slow_reaction = notional_at(now=920.0, crossed_at=600.0)      # fraction=0.80 -> 0.71x
        no_crossing = notional_at(now=620.0, crossed_at=None)         # fraction=None -> 1.0x (no-op)

        assert tight_reaction is not None and slow_reaction is not None and no_crossing is not None
        assert tight_reaction != slow_reaction, \
            "decisiveness_fraction must actually reach decide_size, not be dropped"
        assert tight_reaction > slow_reaction  # 1.74x bucket must size larger than 0.71x

    def test_decisiveness_fraction_uses_the_traded_sides_own_crossing_time(self, monkeypatch):
        """The fraction must come from whichever side actually gets traded
        (forced to Up here), not the other side's -- an unset Down
        crossing must not leak in and force a no-op."""
        monkeypatch.setattr(stratmod, "decide_side",
                             lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        up_book = make_liquid_book(price=0.75, token_id="up")   # CORE -- this side gets traded
        down_book = make_liquid_book(price=0.20, token_id="down")  # CHEAP
        market = make_market(end_time=1000.0, asset="Bitcoin")
        activity = MarketActivityState()
        activity.up_decisive_crossed_at = 700.0
        activity.down_decisive_crossed_at = None  # must be irrelevant here

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=715.0)
        assert intent is not None
        assert intent.side == "Up"
        # Compare against a no-crossing baseline to confirm the boost fired.
        activity_baseline = MarketActivityState()
        intent_baseline = build_order_intent(market, up_book, down_book, activity_baseline,
                                              random.Random(0), now=715.0)
        assert intent.notional_usd != intent_baseline.notional_usd

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

    # CHANGED 2026-09-14: record_entry's first_entry_* capture gate moved
    # from entry_count == 0 to real_fill_count == 0 (see MarketActivityState's
    # docstring) -- a market whose first placement never actually fills has
    # no analogue in the trader's own trade log, so a second record_entry()
    # call with no real fill in between now correctly RE-captures rather
    # than locking onto the never-filled attempt. Locking now happens once a
    # real fill occurs (record_real_fill), not merely once a second
    # placement is attempted -- see the two tests below for both halves of
    # that behavior.
    def test_first_entry_regime_recaptures_until_a_real_fill_happens(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="HIGH")  # placed, never filled
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP")  # retry after cancellation
        assert activity.first_entry_regime == "CHEAP"

    def test_first_entry_regime_locked_once_a_real_fill_happens(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="HIGH")
        activity.record_real_fill(is_hedge=False)  # the HIGH entry actually filled
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP", is_hedge=True)
        assert activity.first_entry_regime == "HIGH"

    def test_first_entry_price_recaptures_until_a_real_fill_happens(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="HIGH", price=0.95)  # never fills
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP", price=0.05)  # retry
        assert activity.first_entry_price == 0.05

    def test_first_entry_price_locked_once_a_real_fill_happens(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="HIGH", price=0.95)
        activity.record_real_fill(is_hedge=False)
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP", price=0.05, is_hedge=True)
        assert activity.first_entry_price == 0.95

    def test_first_entry_price_defaults_to_none_when_not_passed(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="HIGH")
        assert activity.first_entry_price is None

    def test_first_entry_t_in_recaptures_until_a_real_fill_happens(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="HIGH", t_in=12.0)  # never fills
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP", t_in=45.0)  # retry
        assert activity.first_entry_t_in == 45.0

    def test_first_entry_t_in_locked_once_a_real_fill_happens(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="HIGH", t_in=12.0)
        activity.record_real_fill(is_hedge=False)
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP", t_in=45.0, is_hedge=True)
        assert activity.first_entry_t_in == 12.0

    def test_first_entry_t_in_defaults_to_none_when_not_passed(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="HIGH")
        assert activity.first_entry_t_in is None

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

    def test_ordinary_entry_updates_last_side(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        assert activity.last_side == "Up"

    def test_hedge_does_not_move_last_side(self):
        # FIXED 2026-09-19 (last-side-vs-dominant-side-divergence.md): a
        # hedge fill must NOT overwrite last_side -- decide_side's own
        # persist-or-switch roll should keep reading the standing
        # conviction side across a hedge, not the transient hedge side.
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP", is_hedge=True)
        assert activity.last_side == "Up"
        # dominant_side() is unaffected either way -- it's computed
        # separately from cost_by_side, not from last_side.
        assert activity.dominant_side() == "Up"

    def test_last_side_still_updates_after_a_hedge_on_the_next_ordinary_entry(self):
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID")
        activity.record_entry("Down", notional_usd=2.0, regime="CHEAP", is_hedge=True)
        activity.record_entry("Down", notional_usd=5.0, regime="MID", is_hedge=False)
        assert activity.last_side == "Down"

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


class TestDecisiveCrossingTracking:
    """ADDED 2026-09-13, alongside DECISIVENESS_ONSET_MULTIPLIER: real,
    ceiling-effect-confound-checked finding that reaction speed to a
    market snapping into CORE/HIGH scales with time remaining at the
    crossing -- see the multiplier's own docstring in behavior_config.py."""

    def test_records_up_crossing_the_first_time_it_reaches_threshold(self):
        activity = MarketActivityState()
        activity.update_decisive_crossings(up_price=0.75, down_price=0.25, now=1000.0, threshold=0.70)
        assert activity.up_decisive_crossed_at == 1000.0
        assert activity.down_decisive_crossed_at is None

    def test_records_down_crossing_independently(self):
        activity = MarketActivityState()
        activity.update_decisive_crossings(up_price=0.25, down_price=0.75, now=1000.0, threshold=0.70)
        assert activity.down_decisive_crossed_at == 1000.0
        assert activity.up_decisive_crossed_at is None

    def test_both_sides_can_cross_independently_at_different_times(self):
        activity = MarketActivityState()
        activity.update_decisive_crossings(up_price=0.75, down_price=0.25, now=1000.0, threshold=0.70)
        activity.update_decisive_crossings(up_price=0.80, down_price=0.20, now=1010.0, threshold=0.70)
        assert activity.up_decisive_crossed_at == 1000.0  # locked, not overwritten by the later tick

    def test_locks_on_first_crossing_does_not_update_on_later_ticks(self):
        activity = MarketActivityState()
        activity.update_decisive_crossings(up_price=0.71, down_price=0.29, now=1000.0, threshold=0.70)
        activity.update_decisive_crossings(up_price=0.95, down_price=0.05, now=1050.0, threshold=0.70)
        assert activity.up_decisive_crossed_at == 1000.0

    def test_no_crossing_recorded_below_threshold(self):
        activity = MarketActivityState()
        activity.update_decisive_crossings(up_price=0.65, down_price=0.35, now=1000.0, threshold=0.70)
        assert activity.up_decisive_crossed_at is None
        assert activity.down_decisive_crossed_at is None

    def test_none_price_does_not_crash_or_record(self):
        activity = MarketActivityState()
        activity.update_decisive_crossings(up_price=None, down_price=None, now=1000.0, threshold=0.70)
        assert activity.up_decisive_crossed_at is None
        assert activity.down_decisive_crossed_at is None

    def test_exact_threshold_price_counts_as_crossed(self):
        activity = MarketActivityState()
        activity.update_decisive_crossings(up_price=0.70, down_price=0.30, now=1000.0, threshold=0.70)
        assert activity.up_decisive_crossed_at == 1000.0

    def test_defaults_to_a_fresh_none_none_for_a_new_instance(self):
        activity = MarketActivityState()
        assert activity.up_decisive_crossed_at is None
        assert activity.down_decisive_crossed_at is None


class TestDecisivenessOnsetMultiplier:
    def test_noop_for_cheap_regime(self):
        assert bc.decisiveness_onset_multiplier("Bitcoin", "CHEAP", 0.1) == 1.0

    def test_noop_for_mid_regime(self):
        assert bc.decisiveness_onset_multiplier("Bitcoin", "MID", 0.1) == 1.0

    def test_noop_when_fraction_is_none(self):
        assert bc.decisiveness_onset_multiplier("Bitcoin", "CORE", None) == 1.0

    def test_noop_for_an_asset_with_no_table_entry(self):
        assert bc.decisiveness_onset_multiplier("Dogecoin", "HIGH", 0.1) == 1.0

    def test_looks_up_the_correct_bucket_for_core(self):
        # Bitcoin's [0.00, 0.15) bucket is 1.74 -- see DECISIVENESS_ONSET_MULTIPLIER.
        assert bc.decisiveness_onset_multiplier("Bitcoin", "CORE", 0.05) == 1.74

    def test_looks_up_the_correct_bucket_for_high(self):
        assert bc.decisiveness_onset_multiplier("Bitcoin", "HIGH", 0.05) == 1.74

    def test_different_buckets_return_different_values(self):
        low = bc.decisiveness_onset_multiplier("Bitcoin", "CORE", 0.05)
        high = bc.decisiveness_onset_multiplier("Bitcoin", "CORE", 0.50)
        assert low != high

    def test_fraction_at_or_beyond_one_falls_back_to_neutral(self):
        # The empty tail bucket (no observations at ANY sample size reached
        # so far) -- deliberately left at 1.0, not extrapolated.
        assert bc.decisiveness_onset_multiplier("Bitcoin", "CORE", 1.2) == 1.0

    def test_negative_fraction_defensively_falls_back_to_neutral(self):
        assert bc.decisiveness_onset_multiplier("Bitcoin", "CORE", -0.5) == 1.0

    def test_every_table_value_is_within_the_documented_safety_cap(self):
        for asset, buckets in bc.DECISIVENESS_ONSET_MULTIPLIER.items():
            for lo, hi, mult in buckets:
                assert 0.5 <= mult <= 2.0, f"{asset} bucket [{lo},{hi}) = {mult} exceeds the cap"


class TestDecideSizeDecisivenessIntegration:
    def test_decide_size_is_unaffected_when_fraction_not_passed(self):
        rng = random.Random(1)
        notional, _ = decide_size("Bitcoin", "CORE", "first", 0.75, rng)
        assert notional > 0  # no crash, no-op multiplier applied internally

    def test_decisiveness_boost_changes_core_sizing_when_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_ONSET_MULTIPLIER", True)
        rng_a = random.Random(7)
        rng_b = random.Random(7)
        notional_noboost, _ = decide_size("Bitcoin", "CORE", "first", 0.75, rng_a,
                                           decisiveness_fraction=None)
        notional_boosted, _ = decide_size("Bitcoin", "CORE", "first", 0.75, rng_b,
                                           decisiveness_fraction=0.05)  # 1.74x bucket
        assert notional_boosted > notional_noboost

    def test_flag_disabled_makes_decide_size_ignore_the_fraction(self, monkeypatch):
        # decide_size itself reads config.ENABLE_DECISIVENESS_ONSET_MULTIPLIER
        # (same gating pattern/location as hedge_count_reinforcement_mult
        # just above it) -- with the flag off, passing a fraction that would
        # otherwise land in a boosted bucket must produce the SAME notional
        # as passing no fraction at all, not merely "still positive."
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_ONSET_MULTIPLIER", False)
        rng_a = random.Random(9)
        rng_b = random.Random(9)
        notional_flagged_off, _ = decide_size("Bitcoin", "CORE", "first", 0.75, rng_a,
                                               decisiveness_fraction=0.05)  # would be a 1.74x bucket
        notional_no_fraction, _ = decide_size("Bitcoin", "CORE", "first", 0.75, rng_b,
                                               decisiveness_fraction=None)
        assert notional_flagged_off == notional_no_fraction


class TestDecisivenessTtcOvershootCorrection:
    """Item #25 chase, round 2: unlike momentum x bankroll (found to be
    calendar drift, not real -- see the item 25 project memory), this
    overlap between decisiveness_fraction and seconds_remaining is
    mechanically correlated (r=-0.82 within a market) and a direct
    real-vs-predicted test found a genuine overshoot for Ethereum/Solana
    HIGH at high decisiveness_fraction, split-half-confirmed stable
    across the Sep-18 regime boundary."""

    def test_noop_for_bitcoin_uncalibrated_fraction(self):
        # Bitcoin's overshoot correction was ADDED 2026-09-22 for the
        # well-powered [0.00,0.45) bucket, then RETIRED 2026-09-23 (same
        # day re-check found the drift didn't just continue, it reversed
        # sign entirely -- see test_bitcoin_is_now_fully_uncalibrated).
        # Bitcoin is a no-op for any fraction now, not just 0.50.
        assert bc.decisiveness_ttc_overshoot_correction("Bitcoin", "HIGH", 0.50) == 1.0

    def test_noop_for_core(self):
        # CORE's predicted/real ratios stayed <=1 in the joint test.
        assert bc.decisiveness_ttc_overshoot_correction("Ethereum", "CORE", 0.50) == 1.0

    def test_noop_below_the_confirmed_overshoot_threshold(self):
        # Ethereum/Solana only -- Bitcoin's own low-fraction range IS
        # calibrated now (see test_bitcoin_low_fraction_is_a_boost).
        assert bc.decisiveness_ttc_overshoot_correction("Ethereum", "HIGH", 0.30) == 1.0
        assert bc.decisiveness_ttc_overshoot_correction("Solana", "HIGH", 0.00) == 1.0

    def test_noop_when_fraction_is_none(self):
        assert bc.decisiveness_ttc_overshoot_correction("Ethereum", "HIGH", None) == 1.0

    def test_exact_bucket_values(self):
        assert bc.decisiveness_ttc_overshoot_correction("Ethereum", "HIGH", 0.50) == 0.567
        assert bc.decisiveness_ttc_overshoot_correction("Ethereum", "HIGH", 0.80) == 0.878
        assert bc.decisiveness_ttc_overshoot_correction("Solana", "HIGH", 0.50) == 0.516
        assert bc.decisiveness_ttc_overshoot_correction("Solana", "HIGH", 0.80) == 0.461

    def test_bitcoin_is_now_fully_uncalibrated(self):
        # RETIRED 2026-09-23 (systematic post-Sep-18 audit, same-day
        # re-check of the 2026-09-22 addition): the shipped 1.567 boost
        # was flagged unstable at build time ("RE-CHECK WITHIN DAYS") --
        # the re-check found the drift had REVERSED sign entirely (fresh
        # median real/pred(uncorrected) = 0.821, both split halves below
        # 1.0, i.e. now wants a dampener, not a boost). Reverted to a
        # full no-op rather than keep a correction pointing the wrong
        # direction -- see DECISIVENESS_TTC_OVERSHOOT_CORRECTION's
        # docstring in behavior_config.py.
        assert bc.decisiveness_ttc_overshoot_correction("Bitcoin", "HIGH", 0.00) == 1.0
        assert bc.decisiveness_ttc_overshoot_correction("Bitcoin", "HIGH", 0.44) == 1.0
        assert "Bitcoin" not in bc.DECISIVENESS_TTC_OVERSHOOT_CORRECTION

    def test_ethereum_and_solana_corrections_are_dampeners(self):
        # Ethereum/Solana's calibrated buckets correct DOWN an overshoot
        # (< 1.0); Bitcoin's is the opposite (undershoot -> boost, tested
        # separately above) so it's excluded from this check.
        for asset in ("Ethereum", "Solana"):
            for lo, hi, mult in bc.DECISIVENESS_TTC_OVERSHOOT_CORRECTION[asset]:
                assert mult < 1.0, f"{asset} bucket [{lo},{hi}) = {mult} is not a dampener"


class TestDecideSizeDecisivenessTtcOvershootIntegration:
    def test_dampens_ethereum_high_sizing_at_late_fraction_when_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_TTC_OVERSHOOT_CORRECTION", True)
        rng_a = random.Random(3)
        rng_b = random.Random(3)
        notional_no_late, _ = decide_size("Ethereum", "HIGH", "first", 0.95, rng_a,
                                           decisiveness_fraction=0.10)  # below threshold, no-op
        notional_late, _ = decide_size("Ethereum", "HIGH", "first", 0.95, rng_b,
                                        decisiveness_fraction=0.50)  # 0.567x correction
        assert notional_late < notional_no_late

    def test_flag_disabled_makes_decide_size_ignore_the_correction(self, monkeypatch):
        # decisiveness_fraction also feeds the separate, still-enabled
        # DECISIVENESS_ONSET_MULTIPLIER -- isolate this flag's own effect
        # via the ratio between two fractions, not an absolute value.
        # Solana HIGH is ALSO affected by DECISIVENESS_WITHINBAND_
        # OVERSHOOT_CORRECTION (added 2026-09-21) -- disable that too so
        # this stays an isolated test of the TTC correction alone.
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_TTC_OVERSHOOT_CORRECTION", False)
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_WITHINBAND_OVERSHOOT_CORRECTION", False)
        rng_a = random.Random(5)
        rng_b = random.Random(5)
        notional_low, _ = decide_size("Solana", "HIGH", "first", 0.95, rng_a,
                                       decisiveness_fraction=0.10)
        notional_high, _ = decide_size("Solana", "HIGH", "first", 0.95, rng_b,
                                        decisiveness_fraction=0.80)  # would be 0.461x if enabled
        raw_decisiveness_ratio = (bc.decisiveness_onset_multiplier("Solana", "HIGH", 0.80)
                                   / bc.decisiveness_onset_multiplier("Solana", "HIGH", 0.10))
        assert abs((notional_high / notional_low) - raw_decisiveness_ratio) < 1e-6

    def test_bitcoin_is_now_fully_uncalibrated_everywhere(self, monkeypatch):
        # RETIRED 2026-09-23 (systematic post-Sep-18 audit): Bitcoin's
        # 2026-09-22 [0.00,0.45) boost was explicitly flagged unstable at
        # build time and re-checked the same day it was added -- the
        # drift had reversed sign entirely (fresh data now wants a
        # dampener, not a boost). Reverted to a full no-op rather than
        # keep a correction pointing the wrong direction -- see
        # DECISIVENESS_TTC_OVERSHOOT_CORRECTION's docstring in
        # behavior_config.py. Bitcoin is now a strict no-op at every
        # fraction, low or high.
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_TTC_OVERSHOOT_CORRECTION", True)
        rng_a = random.Random(11)
        rng_b = random.Random(11)
        notional_a, _ = decide_size("Bitcoin", "HIGH", "first", 0.95, rng_a,
                                     decisiveness_fraction=0.10)
        notional_b, _ = decide_size("Bitcoin", "HIGH", "first", 0.95, rng_b,
                                     decisiveness_fraction=0.80)
        raw_decisiveness_ratio = (bc.decisiveness_onset_multiplier("Bitcoin", "HIGH", 0.10)
                                   / bc.decisiveness_onset_multiplier("Bitcoin", "HIGH", 0.80))
        assert abs((notional_a / notional_b) - raw_decisiveness_ratio) < 1e-6


class TestDecisivenessWithinbandOvershootCorrection:
    """2026-09-21: a SECOND, distinct double-counting bug from the
    on-chain share-sizing audit -- decisiveness_fraction's collinearity
    with raw price level (within_band's own axis), not TTC/seconds_
    remaining like TestDecisivenessTtcOvershootCorrection above. Unlike
    that one, this hits CORE as well as HIGH, confirmed via a real-vs-
    predicted joint test holding price fixed and comparing early-vs-late
    decisiveness_fraction, split-half-stable in direction across the
    Sep-18 boundary."""

    def test_noop_for_bitcoin(self):
        # Bitcoin UNDERSHOOTS in the same joint test -- a separate,
        # unexplained gap, not this correction's job.
        assert bc.decisiveness_withinband_overshoot_correction("Bitcoin", "HIGH", 0.50) == 1.0
        assert bc.decisiveness_withinband_overshoot_correction("Bitcoin", "CORE", 0.50) == 1.0

    def test_noop_below_the_confirmed_overshoot_threshold(self):
        assert bc.decisiveness_withinband_overshoot_correction("Ethereum", "HIGH", 0.30) == 1.0
        assert bc.decisiveness_withinband_overshoot_correction("Solana", "CORE", 0.00) == 1.0

    def test_noop_when_fraction_is_none(self):
        assert bc.decisiveness_withinband_overshoot_correction("Ethereum", "HIGH", None) == 1.0

    def test_noop_for_cheap_and_mid(self):
        assert bc.decisiveness_withinband_overshoot_correction("Ethereum", "CHEAP", 0.50) == 1.0
        assert bc.decisiveness_withinband_overshoot_correction("Solana", "MID", 0.50) == 1.0

    def test_high_is_noop_because_ttc_correction_already_covers_it(self):
        # 2026-09-22: HIGH values double-corrected on top of
        # DECISIVENESS_TTC_OVERSHOOT_CORRECTION and were removed.
        assert bc.decisiveness_withinband_overshoot_correction("Ethereum", "HIGH", 0.50) == 1.0
        assert bc.decisiveness_withinband_overshoot_correction("Solana", "HIGH", 0.80) == 1.0

    def test_exact_values(self):
        assert bc.decisiveness_withinband_overshoot_correction("Ethereum", "CORE", 0.50) == 0.87
        assert bc.decisiveness_withinband_overshoot_correction("Solana", "CORE", 0.50) == 0.497

    def test_flat_across_the_whole_late_bucket(self):
        # Unlike the TTC correction's two sub-buckets, this uses one
        # combined [0.45,1.00) bucket -- confirm it doesn't vary within it.
        assert (bc.decisiveness_withinband_overshoot_correction("Solana", "CORE", 0.46)
                == bc.decisiveness_withinband_overshoot_correction("Solana", "CORE", 0.99))

    def test_correction_is_a_dampener_not_a_boost(self):
        for asset, regimes in bc.DECISIVENESS_WITHINBAND_OVERSHOOT_CORRECTION.items():
            for regime, mult in regimes.items():
                assert mult < 1.0, f"{asset} {regime} = {mult} is not a dampener"


class TestDecideSizeDecisivenessWithinbandOvershootIntegration:
    def test_dampens_ethereum_core_sizing_at_late_fraction_when_enabled(self, monkeypatch):
        # decisiveness_onset_multiplier's OWN raw ratio (1.06->1.77 for
        # Ethereum CORE's early->late buckets) is a bigger boost than this
        # correction's 0.793x dampens away, so late can still net out
        # larger than early in absolute terms -- isolate the correction's
        # own effect via the ratio against the raw decisiveness ratio,
        # same pattern as the sibling TTC-correction tests above, rather
        # than asserting an absolute early-vs-late direction.
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_TTC_OVERSHOOT_CORRECTION", False)
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_WITHINBAND_OVERSHOOT_CORRECTION", True)
        rng_a = random.Random(7)
        rng_b = random.Random(7)
        notional_early, _ = decide_size("Ethereum", "CORE", "first", 0.80, rng_a,
                                         decisiveness_fraction=0.10)  # below threshold, no-op
        notional_late, _ = decide_size("Ethereum", "CORE", "first", 0.80, rng_b,
                                        decisiveness_fraction=0.50)  # 0.87x correction
        raw_decisiveness_ratio = (bc.decisiveness_onset_multiplier("Ethereum", "CORE", 0.50)
                                   / bc.decisiveness_onset_multiplier("Ethereum", "CORE", 0.10))
        actual_ratio = notional_late / notional_early
        assert actual_ratio < raw_decisiveness_ratio
        assert abs(actual_ratio - raw_decisiveness_ratio * 0.87) < 1e-6

    def test_flag_disabled_makes_decide_size_ignore_the_correction(self, monkeypatch):
        # Solana HIGH is ALSO affected by DECISIVENESS_TTC_OVERSHOOT_
        # CORRECTION -- disable that too so this stays an isolated test
        # of the withinband correction alone.
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_TTC_OVERSHOOT_CORRECTION", False)
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_WITHINBAND_OVERSHOOT_CORRECTION", False)
        rng_a = random.Random(9)
        rng_b = random.Random(9)
        notional_low, _ = decide_size("Solana", "CORE", "first", 0.80, rng_a,
                                       decisiveness_fraction=0.10)
        notional_high, _ = decide_size("Solana", "CORE", "first", 0.80, rng_b,
                                        decisiveness_fraction=0.80)  # would be 0.497x if enabled
        raw_decisiveness_ratio = (bc.decisiveness_onset_multiplier("Solana", "CORE", 0.80)
                                   / bc.decisiveness_onset_multiplier("Solana", "CORE", 0.10))
        assert abs((notional_high / notional_low) - raw_decisiveness_ratio) < 1e-6

    def test_does_not_affect_bitcoin(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_WITHINBAND_OVERSHOOT_CORRECTION", True)
        rng_a = random.Random(13)
        rng_b = random.Random(13)
        notional_a, _ = decide_size("Bitcoin", "CORE", "first", 0.80, rng_a,
                                     decisiveness_fraction=0.10)
        notional_b, _ = decide_size("Bitcoin", "CORE", "first", 0.80, rng_b,
                                     decisiveness_fraction=0.80)
        raw_decisiveness_ratio = (bc.decisiveness_onset_multiplier("Bitcoin", "CORE", 0.10)
                                   / bc.decisiveness_onset_multiplier("Bitcoin", "CORE", 0.80))
        assert abs((notional_a / notional_b) - raw_decisiveness_ratio) < 1e-6

    def test_does_not_stack_on_the_ttc_correction_for_ethereum_high(self, monkeypatch):
        # 2026-09-22: HIGH is covered by the TTC correction alone -- this
        # correction must NOT further change HIGH sizing (it double-corrected
        # before being removed for HIGH).
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_TTC_OVERSHOOT_CORRECTION", True)
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_WITHINBAND_OVERSHOOT_CORRECTION", True)
        rng_a = random.Random(17)
        rng_b = random.Random(17)
        notional_both, _ = decide_size("Ethereum", "HIGH", "first", 0.95, rng_a,
                                        decisiveness_fraction=0.50)
        monkeypatch.setattr(config, "ENABLE_DECISIVENESS_WITHINBAND_OVERSHOOT_CORRECTION", False)
        notional_ttc_only, _ = decide_size("Ethereum", "HIGH", "first", 0.95, rng_b,
                                            decisiveness_fraction=0.50)
        assert notional_both == notional_ttc_only


class TestActivityStatePersistence:
    """ADDED 2026-09-13: closes a real, confirmed-live bug -- PaperBot's
    self.activity used to exist ONLY in memory, so every restart (an
    ordinary graceful `systemctl restart` during a deploy, not just a
    crash) silently reset every in-progress market's real_hedge_fill_
    count/cost_by_side/last_side to zero, including MAX_HEDGE_COUNT_
    PER_MARKET's own cap -- confirmed live via a ledger showing one
    market with 19 separate hedge fills (should have been capped at 6)."""

    def test_round_trips_a_populated_activity_dict(self, tmp_path):
        path = tmp_path / "activity_state.json"
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="MID", price=0.55)
        activity.record_entry("Down", notional_usd=3.0, regime="CHEAP", is_hedge=True)
        activity.record_real_fill(is_hedge=False)
        activity.record_real_fill(is_hedge=True)
        original = {"cond-1": activity}

        save_activity_state(original, path)
        reloaded = load_activity_state(path)

        assert set(reloaded) == {"cond-1"}
        restored = reloaded["cond-1"]
        assert restored.entry_count == activity.entry_count
        assert restored.cost_by_side == activity.cost_by_side
        assert restored.last_side == activity.last_side
        assert restored.first_entry_regime == activity.first_entry_regime
        assert restored.first_entry_price == activity.first_entry_price
        assert restored.hedge_count == activity.hedge_count
        assert restored.real_fill_count == activity.real_fill_count
        assert restored.real_hedge_fill_count == activity.real_hedge_fill_count

    def test_restored_state_still_enforces_the_hedge_count_cap(self, tmp_path):
        # The exact bug this closes: real_hedge_fill_count must survive a
        # save/load round-trip so a market already at its cap doesn't get
        # a fresh allowance after a restart.
        path = tmp_path / "activity_state.json"
        activity = MarketActivityState()
        for _ in range(6):
            activity.record_real_fill(is_hedge=True)
        assert activity.real_hedge_fill_count == 6

        save_activity_state({"cond-1": activity}, path)
        restored = load_activity_state(path)["cond-1"]
        assert restored.real_hedge_fill_count == 6, (
            "a restart must not reset the counter MAX_HEDGE_COUNT_PER_MARKET enforces against"
        )

    def test_multiple_markets_round_trip_independently(self, tmp_path):
        path = tmp_path / "activity_state.json"
        a1 = MarketActivityState()
        a1.record_entry("Up", notional_usd=10.0, regime="MID")
        a2 = MarketActivityState()
        a2.record_entry("Down", notional_usd=4.0, regime="HIGH")

        save_activity_state({"cond-1": a1, "cond-2": a2}, path)
        reloaded = load_activity_state(path)

        assert set(reloaded) == {"cond-1", "cond-2"}
        assert reloaded["cond-1"].first_entry_regime == "MID"
        assert reloaded["cond-2"].first_entry_regime == "HIGH"

    def test_load_returns_empty_dict_when_file_does_not_exist(self, tmp_path):
        # First-ever run, or an older deploy that predates this field --
        # same graceful-empty-default discipline as Ledger.load().
        missing_path = tmp_path / "does_not_exist.json"
        assert load_activity_state(missing_path) == {}

    def test_save_creates_parent_directory_if_missing(self, tmp_path):
        nested_path = tmp_path / "nested" / "dir" / "activity_state.json"
        save_activity_state({"cond-1": MarketActivityState()}, nested_path)
        assert nested_path.exists()

    def test_empty_activity_dict_round_trips_to_empty(self, tmp_path):
        path = tmp_path / "activity_state.json"
        save_activity_state({}, path)
        assert load_activity_state(path) == {}


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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")  # entry_count now 1
        record_filled_entry(activity, "Up", notional_usd=1.0, regime="MID")   # entry_count now 2
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
        activity.entry_count = 11
        activity.real_fill_count = 11
        for seed in range(50):
            assert decide_hedge("BNB", activity, random.Random(seed)) is None

    def test_returns_the_opposite_of_the_dominant_side(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
        record_filled_entry(activity, "Down", notional_usd=2.0, regime="MID", is_hedge=True)
        assert activity.hedge_count == 1
        assert activity.real_hedge_fill_count == 1
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
        activity.hedge_count = config.MAX_HEDGE_COUNT_PER_MARKET
        activity.real_hedge_fill_count = config.MAX_HEDGE_COUNT_PER_MARKET
        monkeypatch.setattr(bc, "hedge_continuation_probability", lambda count: 1.0)  # would always fire otherwise
        for seed in range(20):
            assert decide_hedge("BNB", activity, random.Random(seed)) is None

    def test_still_fires_just_under_the_hard_cap(self, monkeypatch):
        """Sanity check the cap boundary is exact, not off-by-one."""
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
        activity.hedge_count = config.MAX_HEDGE_COUNT_PER_MARKET - 1
        activity.real_hedge_fill_count = config.MAX_HEDGE_COUNT_PER_MARKET - 1
        monkeypatch.setattr(bc, "hedge_continuation_probability", lambda count: 1.0)
        assert decide_hedge("BNB", activity, random.Random(0)) is not None

    def test_continuation_uses_hedge_continuation_probability_not_the_hazard_curve(self, monkeypatch):
        """The FIRST hedge (hedge_count==0) is governed by
        hedge_attempt_hazard; a further hedge (hedge_count>=1) must use
        hedge_continuation_probability instead -- confirm decide_hedge
        actually calls the right one for each case."""
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")

        calls = []
        monkeypatch.setattr(bc, "hedge_attempt_hazard",
                             lambda asset, regime, idx: calls.append(("hazard", idx)) or 0.0)
        monkeypatch.setattr(bc, "hedge_continuation_probability",
                             lambda count: calls.append(("continuation", count)) or 0.0)

        decide_hedge("BNB", activity, random.Random(0))
        assert calls == [("hazard", 1)]

        record_filled_entry(activity, "Down", notional_usd=2.0, regime="MID", is_hedge=True)
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
        coverage of the conviction mechanism itself.

        REGIME_HEDGE_POST_SHIFT_MULTIPLIER disabled here (added
        2026-09-19): Bitcoin/HIGH is one of the newly-calibrated cells
        (ratio 0.378) -- this test is about the base cumulative-
        probability model reproducing HEDGE_TRIGGER_PROBABILITY's
        target, not the post-shift dampener layered on top of it."""
        monkeypatch.setattr(config, "ENABLE_CONVICTION_HEDGE_MULTIPLIER", False)
        monkeypatch.setattr(config, "ENABLE_REGIME_HEDGE_POST_SHIFT_DAMPENER", False)
        n = 2000
        triggered = 0
        for seed in range(n):
            rng = random.Random(seed)
            activity = MarketActivityState()
            record_filled_entry(activity, "Up", notional_usd=10.0, regime="HIGH")
            for _ in range(10):  # real_fill_count 1..10, matching the modeled window
                if decide_hedge("Bitcoin", activity, rng) is not None:
                    triggered += 1
                    break
                activity.entry_count += 1
                activity.real_fill_count += 1
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
        # RECALIBRATED 2026-09-13 (post-halt, /loop cycle 16) -- derive
        # expected values from the table itself rather than hardcoding,
        # since the exact numbers have since changed.
        assert bc.hedge_continuation_size_ratio(2) == pytest.approx(bc.HEDGE_CONTINUATION_SIZE_RATIO[2])
        assert bc.hedge_continuation_size_ratio(3) == pytest.approx(bc.HEDGE_CONTINUATION_SIZE_RATIO[3])
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
        n_no_arg = sum(
            1 for seed in range(200)
            if decide_hedge("BNB", activity, random.Random(seed)) is not None
        )
        n_explicit_none = sum(
            1 for seed in range(200)
            if decide_hedge("BNB", activity, random.Random(seed), liquidity=None) is not None
        )
        assert n_no_arg == n_explicit_none

    def test_high_liquidity_fires_more_often_than_low_liquidity(self, monkeypatch):
        # HEDGE_LIQUIDITY_MULTIPLIER is empty as of 2026-09-13 (the
        # underlying correlation vanished post-halt for all three live
        # assets -- see TestHedgeLiquidityMultiplier's docstring), so
        # this mocks the multiplier function directly to exercise
        # decide_hedge's WIRING (does it apply whatever
        # hedge_liquidity_multiplier returns to the trigger probability)
        # independent of whether any asset currently has real calibration
        # data for it.
        monkeypatch.setattr(bc, "hedge_liquidity_multiplier",
                             lambda asset, liquidity: 0.5 if liquidity < 10000 else 1.5)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
        # must not raise, and must behave as a valid (always-fires) probability
        for seed in range(10):
            assert decide_hedge("BNB", activity, random.Random(seed), liquidity=1.0) is not None

    def test_respects_the_per_instance_feature_flag(self, monkeypatch):
        # Explicit instruction: paperbot-mini stays on the OLD behavior
        # (as a control) via ENABLE_HEDGE_LIQUIDITY_MULTIPLIER=false.
        monkeypatch.setattr(config, "ENABLE_HEDGE_LIQUIDITY_MULTIPLIER", False)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
        record_filled_entry(activity, "Down", notional_usd=2.0, regime="MID", is_hedge=True)
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
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
        # pinned to its own MEASURED value (below 1.0 for every asset, a
        # real, disclosed discontinuity -- see ADVERSE_MOVE_HEDGE_TRIGGER_
        # MULTIPLIER's docstring), not forced to 1.0.
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
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

    def test_move_at_zero_is_lower_than_the_unmultiplied_baseline(self, monkeypatch):
        # The curve's own first breakpoint (move==0.0) is a REAL,
        # measured value below 1.0 for Bitcoin -- not a no-op. Confirms
        # the documented discontinuity is actually wired up, not silently
        # smoothed away.
        #
        # REGIME_HEDGE_POST_SHIFT_MULTIPLIER disabled here (added
        # 2026-09-19): Bitcoin/CORE is a newly-calibrated cell (ratio
        # 0.270) -- without disabling it, both counts shrink toward the
        # same small absolute values and this test loses the power to
        # distinguish the (much smaller) adverse-move effect it's
        # actually testing.
        monkeypatch.setattr(config, "ENABLE_REGIME_HEDGE_POST_SHIFT_DAMPENER", False)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
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


class TestDecideHedgeFavorableMoveTriggerMultiplier:
    """decide_hedge's favorable-move TRIGGER wiring, added 2026-09-19 --
    see TestFavorableMoveHedgeTriggerMultiplier for the multiplier
    function itself in isolation; these confirm decide_hedge applies it
    ONLY in CHEAP (its only calibrated regime), only to the FIRST hedge,
    and respects its own feature flag independently of its sibling's."""

    def test_favorable_move_is_no_longer_a_noop_in_cheap(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CHEAP", price=0.10)
        n = 500
        n_baseline = sum(  # no dominant_current_price -> neither multiplier applied
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed)) is not None
        )
        n_deep_favorable = sum(  # dominant_current_price=0.95 -> adverse_move=-0.85 (deep favorable)
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.95) is not None
        )
        assert n_deep_favorable != n_baseline, "CHEAP's favorable-move multiplier must actually move the rate"

    def test_valley_near_zero_fires_less_often_than_deep_favorable_in_cheap(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CHEAP", price=0.10)
        n = 500
        n_near_zero = sum(  # dominant_current_price=0.10 -> adverse_move=0.0 (approx, valley)
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.10) is not None
        )
        n_deep_favorable = sum(  # dominant_current_price=0.95 -> adverse_move=-0.85 (deep favorable, elevated)
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.95) is not None
        )
        assert n_deep_favorable > n_near_zero

    def test_does_not_apply_outside_cheap(self):
        # Same favorable adverse_move, but CORE first-entry regime --
        # this multiplier is CHEAP-only by construction, must stay a
        # strict no-op here (matching the sibling's own CORE behavior).
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
        n = 500
        n_baseline = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed)) is not None
        )
        n_favorable = sum(  # dominant_current_price=0.95 -> adverse_move=-0.15 (favorable)
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.95) is not None
        )
        assert n_baseline == n_favorable

    def test_respects_its_own_feature_flag_independently(self, monkeypatch):
        # FIXED 2026-09-22: was comparing dominant_current_price=0.10
        # (== first_entry_price -> adverse_move EXACTLY 0.0) against
        # dominant_current_price=0.95 (adverse_move=-0.85). That doesn't
        # isolate the favorable-move flag -- adverse_move_hedge_trigger_
        # multiplier (a SEPARATE, still-enabled multiplier) returns 0.892
        # at exactly adverse_move=0.0, not 1.0, so the two branches were
        # never expected to match exactly; the ~11% per-seed gap was
        # simply too small to show up in 500 seeds until the regime
        # dampener's 2026-09-22 refresh raised the baseline probability
        # enough to expose it. Use the no-dominant_current_price baseline
        # instead (same pattern as this class's other tests, e.g.
        # test_favorable_move_is_no_longer_a_noop_in_cheap above), which
        # skips the adverse-move multiplier entirely and correctly
        # isolates ONLY the favorable-move flag being tested here.
        monkeypatch.setattr(config, "ENABLE_FAVORABLE_MOVE_HEDGE_TRIGGER_MULTIPLIER", False)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CHEAP", price=0.10)
        n = 500
        n_baseline = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed)) is not None
        )
        n_deep_favorable = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.95) is not None
        )
        assert n_baseline == n_deep_favorable, "with the flag off, favorable adverse_move must be a strict no-op"


class TestDecideHedgeMidFavorableHedgeHumpMultiplier:
    """decide_hedge's MID favorable-move HUMP wiring, added 2026-09-20 to
    close replication-checklist item #31 -- see
    TestMidFavorableHedgeTriggerMultiplier for the multiplier function
    itself. Solana/MID only; confirms decide_hedge applies it there and
    nowhere else."""

    def test_favorable_move_is_no_longer_a_noop_in_mid_for_solana(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID", price=0.50)
        n = 500
        n_baseline = sum(
            1 for seed in range(n)
            if decide_hedge("Solana", activity, random.Random(seed)) is not None
        )
        n_hump_peak = sum(  # dominant_current_price=0.791 -> adverse_move=-0.291 (the hump peak)
            1 for seed in range(n)
            if decide_hedge("Solana", activity, random.Random(seed), dominant_current_price=0.791) is not None
        )
        assert n_hump_peak != n_baseline, "MID's favorable-move hump must actually move the rate for Solana"

    def test_hump_peak_fires_more_often_than_near_zero(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID", price=0.50)
        n = 500
        n_near_zero = sum(  # dominant_current_price=0.502 -> adverse_move~=-0.002 (valley)
            1 for seed in range(n)
            if decide_hedge("Solana", activity, random.Random(seed), dominant_current_price=0.502) is not None
        )
        n_hump_peak = sum(  # dominant_current_price=0.791 -> adverse_move=-0.291 (the hump peak)
            1 for seed in range(n)
            if decide_hedge("Solana", activity, random.Random(seed), dominant_current_price=0.791) is not None
        )
        assert n_hump_peak > n_near_zero

    def test_does_not_apply_outside_mid(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
        n = 500
        n_baseline = sum(
            1 for seed in range(n)
            if decide_hedge("Solana", activity, random.Random(seed)) is not None
        )
        n_favorable = sum(  # same-shaped favorable move, but CORE first-entry regime
            1 for seed in range(n)
            if decide_hedge("Solana", activity, random.Random(seed), dominant_current_price=1.09) is not None
        )
        assert n_baseline == n_favorable

    def test_does_not_apply_outside_solana(self):
        # Bitcoin/Ethereum found null/different-shaped in the per-asset
        # check -- must stay a strict no-op for them even in MID.
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID", price=0.50)
        n = 500
        n_baseline = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed)) is not None
        )
        n_hump_peak = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.791) is not None
        )
        assert n_baseline == n_hump_peak

    def test_respects_its_own_feature_flag_independently(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_MID_FAVORABLE_HEDGE_HUMP_MULTIPLIER", False)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID", price=0.50)
        n = 500
        n_near_zero = sum(
            1 for seed in range(n)
            if decide_hedge("Solana", activity, random.Random(seed), dominant_current_price=0.502) is not None
        )
        n_hump_peak = sum(
            1 for seed in range(n)
            if decide_hedge("Solana", activity, random.Random(seed), dominant_current_price=0.791) is not None
        )
        assert n_near_zero == n_hump_peak, "with the flag off, MID favorable adverse_move must be a strict no-op"


class TestDecideHedgeAdverseMoveTtcInteraction:
    """decide_hedge's adverse_move x time-remaining wiring, added
    2026-09-20 -- see TestAdverseMoveTtcInteractionMultiplier for the
    multiplier function itself; these confirm decide_hedge actually
    threads seconds_remaining through and applies it. Bitcoin/CORE,
    a confirmed cell, first_entry_price=0.80 throughout."""

    def test_more_time_remaining_fires_more_often_for_the_same_adverse_move(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
        n = 500
        # dominant_current_price=0.60 -> adverse_move=0.20 in both cases,
        # only seconds_remaining differs.
        n_high_ttc = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed),
                             dominant_current_price=0.60, seconds_remaining=240.0) is not None
        )
        n_low_ttc = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed),
                             dominant_current_price=0.60, seconds_remaining=60.0) is not None
        )
        assert n_high_ttc > n_low_ttc

    def test_no_effect_without_seconds_remaining(self):
        # seconds_remaining defaults to None -> the interaction multiplier
        # no-ops, matching its own unit-tested behavior.
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
        n = 500
        n_no_ttc = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed), dominant_current_price=0.60) is not None
        )
        n_center_ttc = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed),
                             dominant_current_price=0.60, seconds_remaining=150.0) is not None
        )
        assert n_no_ttc == n_center_ttc, "no seconds_remaining should match the mean-neutral center (150s)"

    def test_does_not_apply_outside_confirmed_regimes(self):
        # HIGH is confirmed ABSENT -- seconds_remaining must not move the rate.
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="HIGH", price=0.95)
        n = 500
        n_high_ttc = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed),
                             dominant_current_price=0.80, seconds_remaining=240.0) is not None
        )
        n_low_ttc = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed),
                             dominant_current_price=0.80, seconds_remaining=60.0) is not None
        )
        assert n_high_ttc == n_low_ttc

    def test_respects_its_own_feature_flag(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_ADVERSE_MOVE_TTC_INTERACTION", False)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
        n = 500
        n_high_ttc = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed),
                             dominant_current_price=0.60, seconds_remaining=240.0) is not None
        )
        n_low_ttc = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed),
                             dominant_current_price=0.60, seconds_remaining=60.0) is not None
        )
        assert n_high_ttc == n_low_ttc, "with the flag off, seconds_remaining must not move the rate"

    def test_is_a_noop_when_first_entry_price_was_never_recorded(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")  # no price= kwarg
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
        record_filled_entry(activity, "Down", notional_usd=2.0, regime="CORE", is_hedge=True)
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


class TestDecideHedgeLateWindowUrgency:
    """LATE_WINDOW_HEDGE_URGENCY_MULTIPLIER wiring into decide_hedge, added
    2026-09-24. Default OFF (config.ENABLE_LATE_WINDOW_HEDGE_URGENCY)."""

    def _early_activity(self, t_in=50.0):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
        activity.first_entry_t_in = t_in  # BNB/MID has the highest known trigger prob -- easy to measure
        return activity

    def test_flag_off_ignores_seconds_since_first_entry_entirely(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_LATE_WINDOW_HEDGE_URGENCY", False)
        activity = self._early_activity()
        n = 800
        n_peak = sum(
            1 for seed in range(n)
            if decide_hedge("BNB", activity, random.Random(seed), seconds_since_first_entry=160.0) is not None
        )
        n_trough = sum(
            1 for seed in range(n)
            if decide_hedge("BNB", activity, random.Random(seed), seconds_since_first_entry=10.0) is not None
        )
        assert n_peak == n_trough, "with the flag off, seconds_since_first_entry must not move the rate"

    def test_flag_on_raises_the_rate_in_the_peak_bucket_vs_a_low_bucket(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_LATE_WINDOW_HEDGE_URGENCY", True)
        activity = self._early_activity(t_in=50.0)  # early first entry -- the hump applies
        n = 800
        n_peak = sum(  # 150-180s bucket, multiplier 2.381
            1 for seed in range(n)
            if decide_hedge("BNB", activity, random.Random(seed), seconds_since_first_entry=160.0) is not None
        )
        n_trough = sum(  # 0-30s bucket, multiplier 0.575
            1 for seed in range(n)
            if decide_hedge("BNB", activity, random.Random(seed), seconds_since_first_entry=10.0) is not None
        )
        assert n_peak > n_trough, "the 150-180s peak bucket must trigger more often than the 0-30s trough"

    def test_flag_on_but_late_first_entry_is_still_a_noop(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_LATE_WINDOW_HEDGE_URGENCY", True)
        activity = self._early_activity(t_in=200.0)  # LATE first entry -- the hump must not apply
        n = 800
        n_peak = sum(
            1 for seed in range(n)
            if decide_hedge("BNB", activity, random.Random(seed), seconds_since_first_entry=160.0) is not None
        )
        n_trough = sum(
            1 for seed in range(n)
            if decide_hedge("BNB", activity, random.Random(seed), seconds_since_first_entry=10.0) is not None
        )
        assert n_peak == n_trough, "a late first entry must not get the early-entry hump"

    def test_flag_on_but_missing_seconds_since_first_entry_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_LATE_WINDOW_HEDGE_URGENCY", True)
        activity = self._early_activity(t_in=50.0)
        n = 800
        n_no_arg = sum(
            1 for seed in range(n)
            if decide_hedge("BNB", activity, random.Random(seed)) is not None
        )
        n_explicit_peak = sum(
            1 for seed in range(n)
            if decide_hedge("BNB", activity, random.Random(seed), seconds_since_first_entry=160.0) is not None
        )
        assert n_no_arg < n_explicit_peak, "missing seconds_since_first_entry must fall back to no-op, not the peak"

    def test_does_not_affect_continuation_hedges(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_LATE_WINDOW_HEDGE_URGENCY", True)
        activity = self._early_activity(t_in=50.0)
        record_filled_entry(activity, "Down", notional_usd=2.0, regime="MID", is_hedge=True)
        assert activity.hedge_count == 1
        n = 500
        n_peak = sum(
            1 for seed in range(n)
            if decide_hedge("BNB", activity, random.Random(seed), seconds_since_first_entry=160.0) is not None
        )
        n_trough = sum(
            1 for seed in range(n)
            if decide_hedge("BNB", activity, random.Random(seed), seconds_since_first_entry=10.0) is not None
        )
        assert n_peak == n_trough, "the urgency multiplier must not leak into the continuation-hedge path"


class TestDecideHedgeWeekendMultiplier:
    """decide_hedge's weekend wiring, added 2026-09-10 -- see
    TestWeekendHedgeMultiplier for the multiplier function itself in
    isolation; these confirm decide_hedge actually applies it (not just
    that the function works), only to the FIRST hedge, and respects the
    per-instance feature flag."""

    def test_is_weekend_defaults_to_a_noop_when_not_passed(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
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

    def test_regime_hedge_dampener_lookup(self):
        # REFRESHED 2026-09-22 -- see REGIME_HEDGE_POST_SHIFT_MULTIPLIER's
        # docstring for the full recalibration (now every cell covered,
        # based on the full ~4.5-day post-boundary window instead of the
        # original ~33 hours).
        # CHEAP/Bitcoin RE-RECALIBRATED 2026-09-24: 0.275 -> 0.075, see the
        # table's own docstring for the cross-bot-validated derivation.
        assert bc.regime_hedge_post_shift_multiplier("Bitcoin", "CHEAP") == pytest.approx(0.075)
        assert bc.regime_hedge_post_shift_multiplier("Ethereum", "CHEAP") == pytest.approx(0.393)
        assert bc.regime_hedge_post_shift_multiplier("Solana", "CHEAP") == pytest.approx(0.270)
        assert bc.regime_hedge_post_shift_multiplier("Bitcoin", "MID") == pytest.approx(0.320)
        assert bc.regime_hedge_post_shift_multiplier("Ethereum", "MID") == pytest.approx(0.191)
        assert bc.regime_hedge_post_shift_multiplier("Solana", "MID") == pytest.approx(0.176)
        assert bc.regime_hedge_post_shift_multiplier("Bitcoin", "CORE") == pytest.approx(0.370)
        assert bc.regime_hedge_post_shift_multiplier("Ethereum", "CORE") == pytest.approx(0.122)
        assert bc.regime_hedge_post_shift_multiplier("Solana", "CORE") == pytest.approx(0.214)
        assert bc.regime_hedge_post_shift_multiplier("Bitcoin", "HIGH") == pytest.approx(0.292)
        assert bc.regime_hedge_post_shift_multiplier("Ethereum", "HIGH") == pytest.approx(0.455)
        assert bc.regime_hedge_post_shift_multiplier("Solana", "HIGH") == pytest.approx(0.484)

    def test_cheap_bitcoin_dampener_is_a_real_further_cut_not_a_typo(self):
        # 2026-09-24 recalibration: cross-bot-validated (paperbot 61.9%,
        # paperbot-100 56.7%, coinbase-bot 75.4% CHEAP-Bitcoin hedge rate,
        # all vs the real trader's ~16.8%) as needing roughly a further
        # 3.7x cut on top of the already-shipped CHEAP/Bitcoin dampener --
        # not a small nudge. Guards against a future accidental near-
        # revert (e.g. a merge conflict silently restoring 0.275).
        cheap_btc = bc.regime_hedge_post_shift_multiplier("Bitcoin", "CHEAP")
        assert cheap_btc < 0.15, "CHEAP/Bitcoin dampener should reflect the 2026-09-24 recalibration, not the stale 0.275"
        # sibling CHEAP cells (Ethereum/Solana) were validated as only mildly
        # off (1.4-1.5x) and deliberately left untouched -- must stay put.
        assert bc.regime_hedge_post_shift_multiplier("Ethereum", "CHEAP") == pytest.approx(0.393)
        assert bc.regime_hedge_post_shift_multiplier("Solana", "CHEAP") == pytest.approx(0.270)

    def test_regime_hedge_dampener_is_noop_for_uncalibrated_cells(self):
        # As of the 2026-09-22 refresh every real (asset, regime) cell is
        # calibrated -- the no-op fallback now only matters for an asset
        # this table has never heard of.
        assert bc.regime_hedge_post_shift_multiplier("Dogecoin", "CHEAP") == 1.0
        assert bc.regime_hedge_post_shift_multiplier("Dogecoin", "CORE") == 1.0

    def test_regime_hedge_dampener_fires_less_often_than_mid(self):
        # Same asset, same attempt index -- CHEAP should hedge less
        # often than MID for Bitcoin specifically (0.275 vs MID's 0.320
        # -- still true after the 2026-09-22 refresh, though the margin
        # is much closer than the original 0.151 vs 0.462; this no
        # longer holds for Ethereum/Solana, where MID is now the MORE
        # dampened regime, so this test is deliberately Bitcoin-only).
        activity_cheap = MarketActivityState()
        record_filled_entry(activity_cheap, "Up", notional_usd=10.0, regime="CHEAP")
        activity_mid = MarketActivityState()
        record_filled_entry(activity_mid, "Up", notional_usd=10.0, regime="MID")
        n = 1000
        n_cheap = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity_cheap, random.Random(seed)) is not None
        )
        n_mid = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity_mid, random.Random(seed)) is not None
        )
        assert n_cheap < n_mid

    def test_regime_hedge_dampener_fires_less_often_in_core_and_high_too(self, monkeypatch):
        activity_core = MarketActivityState()
        record_filled_entry(activity_core, "Up", notional_usd=10.0, regime="CORE")
        activity_high = MarketActivityState()
        record_filled_entry(activity_high, "Up", notional_usd=10.0, regime="HIGH")
        n = 1000
        with_dampener_core = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity_core, random.Random(seed)) is not None
        )
        with_dampener_high = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity_high, random.Random(seed)) is not None
        )
        monkeypatch.setattr(bc, "regime_hedge_post_shift_multiplier", lambda asset, regime: 1.0)
        without_dampener_core = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity_core, random.Random(seed)) is not None
        )
        without_dampener_high = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity_high, random.Random(seed)) is not None
        )
        assert with_dampener_core < without_dampener_core
        assert with_dampener_high < without_dampener_high

    def test_regime_hedge_dampener_respects_feature_flag(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_REGIME_HEDGE_POST_SHIFT_DAMPENER", False)
        monkeypatch.setattr(bc, "regime_hedge_post_shift_multiplier", lambda asset, regime: 0.0)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CHEAP")
        n = 200
        n_fired = sum(
            1 for seed in range(n)
            if decide_hedge("Bitcoin", activity, random.Random(seed)) is not None
        )
        # With the flag off, a multiplier of 0.0 must be ignored entirely
        # -- some hedges should still fire from the base hazard alone.
        assert n_fired > 0

    def test_does_not_affect_continuation_hedges(self):
        # Same scoping rule as liquidity above -- only the first hedge.
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID")
        record_filled_entry(activity, "Down", notional_usd=2.0, regime="MID", is_hedge=True)
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


class TestDecideHedgeAfterBigLossMultiplier:
    """decide_hedge's after_big_loss wiring, added 2026-09-13 -- see
    TestHedgeTriggerAfterBigLossMultiplier for the multiplier function
    itself in isolation; these confirm decide_hedge actually applies it
    (not just that the function works), only to the FIRST hedge, and
    respects the per-instance feature flag."""

    def test_defaults_to_a_noop_when_not_passed(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CHEAP")
        n_no_arg = sum(
            1 for seed in range(200)
            if decide_hedge("Ethereum", activity, random.Random(seed)) is not None
        )
        n_explicit_none = sum(
            1 for seed in range(200)
            if decide_hedge("Ethereum", activity, random.Random(seed), after_big_loss=None) is not None
        )
        assert n_no_arg == n_explicit_none

    def test_noop_for_all_assets_now_the_table_is_retired(self):
        # RETIRED 2026-09-13 (post-halt, /loop cycle 11): the effect
        # this multiplier replicated is no longer detectable post-halt
        # for either Ethereum or Solana -- see
        # HEDGE_TRIGGER_AFTER_BIG_LOSS_MULTIPLIER's docstring in
        # behavior_config.py. This test used to assert Ethereum fires
        # MORE after a big loss; now every asset (including the two that
        # used to have a real boost) must be a strict no-op.
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            activity = MarketActivityState()
            record_filled_entry(activity, "Up", notional_usd=10.0, regime="CHEAP")
            n = 500
            n_normal = sum(
                1 for seed in range(n)
                if decide_hedge(asset, activity, random.Random(seed), after_big_loss=False) is not None
            )
            n_after_loss = sum(
                1 for seed in range(n)
                if decide_hedge(asset, activity, random.Random(seed), after_big_loss=True) is not None
            )
            assert n_normal == n_after_loss, f"{asset}: after_big_loss must be a strict no-op now the table is empty"

    def test_respects_the_per_instance_feature_flag(self, monkeypatch):
        # Mocks the underlying multiplier directly rather than relying on
        # a currently-calibrated asset -- HEDGE_TRIGGER_AFTER_BIG_LOSS_
        # MULTIPLIER is now empty (retired post-halt, cycle 11), so no
        # real asset exercises the flag's gating any more. This tests the
        # WIRING (the flag actually suppresses the multiplier's effect)
        # independent of whatever the table currently holds -- the same
        # pattern used for hedge_liquidity_multiplier's own flag test
        # after ITS retirement.
        monkeypatch.setattr(bc, "hedge_trigger_after_big_loss_multiplier",
                             lambda asset, after_big_loss: 3.0 if after_big_loss else 1.0)
        monkeypatch.setattr(config, "ENABLE_HEDGE_TRIGGER_AFTER_BIG_LOSS_MULTIPLIER", False)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CHEAP")
        n = 500
        n_normal = sum(
            1 for seed in range(n)
            if decide_hedge("Ethereum", activity, random.Random(seed), after_big_loss=False) is not None
        )
        n_after_loss = sum(
            1 for seed in range(n)
            if decide_hedge("Ethereum", activity, random.Random(seed), after_big_loss=True) is not None
        )
        assert n_normal == n_after_loss, "with the flag off, after_big_loss must be a strict no-op"

    def test_does_not_affect_continuation_hedges(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CHEAP")
        record_filled_entry(activity, "Down", notional_usd=2.0, regime="CHEAP", is_hedge=True)
        assert activity.hedge_count == 1
        n = 500
        n_normal = sum(
            1 for seed in range(n)
            if decide_hedge("Ethereum", activity, random.Random(seed), after_big_loss=False) is not None
        )
        n_after_loss = sum(
            1 for seed in range(n)
            if decide_hedge("Ethereum", activity, random.Random(seed), after_big_loss=True) is not None
        )
        assert n_normal == n_after_loss, "after_big_loss must not leak into the continuation-hedge path"


class TestBuildOrderIntentHedge:
    def test_produces_a_hedge_intent_sized_off_dominant_cost(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="BNB")
        up_book = make_liquid_book(price=0.80, token_id="up")    # MID/CORE-ish primary
        down_book = make_liquid_book(price=0.15, token_id="down")  # cheap hedge side
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

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

    def test_price_based_hedge_sizing_is_off_by_default(self):
        assert config.ENABLE_PRICE_BASED_HEDGE_SIZING is False

    def test_price_based_hedge_sizing_ignores_dominant_cost_and_uses_the_price_model(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_PRICE_BASED_HEDGE_SIZING", True)
        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")
        sizes = []
        for dominant in (10.0, 400.0):   # 40x different dominant cost, same hedge price
            market = make_market(end_time=1000.0, asset="Bitcoin")
            up_book = make_liquid_book(price=0.80, token_id="up")
            down_book = make_liquid_book(price=0.15, token_id="down")
            activity = MarketActivityState()
            record_filled_entry(activity, "Up", notional_usd=dominant, regime="CORE")
            intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
            assert intent is not None and intent.is_hedge is True
            sizes.append(intent.notional_usd)
        expected = bc.hedge_notional_for_price("Bitcoin", 0.15) * config.SIZE_SCALE_FACTOR
        for n in sizes:
            assert expected * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= n \
                <= expected * (1 + config.SIZING_JITTER_FRACTION) * 1.01
        assert sizes[0] == sizes[1]   # same seed, no dominant-cost dependence at all

    def test_price_based_hedge_sizing_falls_back_to_legacy_for_an_uncalibrated_asset(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_PRICE_BASED_HEDGE_SIZING", True)
        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")
        market = make_market(end_time=1000.0, asset="BNB")   # not in HEDGE_PRICE_SIZING_SCALE
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")
        intent = build_order_intent(market, make_liquid_book(price=0.80, token_id="up"),
                                     make_liquid_book(price=0.15, token_id="down"), activity, random.Random(0), now=0.0)
        expected = 10.0 * bc.hedge_size_ratio("BNB", "CORE")
        assert expected * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_a_second_hedge_uses_continuation_size_ratio_not_hedge_size_ratio(self, monkeypatch):
        """hedge_count==1 (this would be the market's 2nd hedge-shaped
        entry) must size off HEDGE_CONTINUATION_SIZE_RATIO, a real,
        separately-calibrated (and much flatter) curve -- not repeat
        HEDGE_SIZE_RATIO, which only governs the first hedge."""
        market = make_market(end_time=1000.0, asset="BNB")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")
        record_filled_entry(activity, "Down", notional_usd=1.0, regime="CORE", is_hedge=True)
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.is_hedge is True
        expected_ratio = bc.hedge_continuation_size_ratio(2)  # this is hedge #2
        assert expected_ratio != bc.hedge_size_ratio("BNB", "CORE")  # sanity: genuinely different curve
        dominant_cost = 10.0  # activity.cost_by_side["Up"], unaffected by the 1.0 hedge already placed
        expected_notional = dominant_cost * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_hedge_sizing_is_unaffected_by_the_ENTRY_side_ttc_multiplier(self, monkeypatch):
        """TTC_SIZE_MULTIPLIER (the entry-only one) is deliberately scoped
        to the ordinary entry-curve path only (decide_size's own
        docstring) -- the raw trade data it was measured from doesn't
        distinguish hedge-shaped trades from ordinary ones, so it must
        never touch HEDGE_SIZE_RATIO's formula. Bitcoin IS calibrated in
        TTC_SIZE_MULTIPLIER (unlike BNB, used in the other hedge tests
        above), so this specifically exercises the case where a no-op
        would be silently wrong if the scoping broke.

        UPDATED 2026-09-13: hedge notional is no longer TTC-invariant --
        HEDGE_TTC_SIZE_MULTIPLIER (a separate, hedge-specific curve, see
        its own docstring) now legitimately makes it vary with
        time-to-close. This test now confirms the observed variation
        matches THAT curve exactly, not the entry-side one (which would
        imply the old scoping bug had crept back in disguised as the new
        feature)."""
        market_early = make_market(end_time=1000.0, asset="Bitcoin")
        market_late = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")    # CORE primary
        down_book = make_liquid_book(price=0.15, token_id="down")  # CHEAP hedge side

        def hedge_at(now):
            activity = MarketActivityState()
            record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")
            monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")
            return build_order_intent(market_early if now < 500 else market_late,
                                       up_book, down_book, activity, random.Random(0), now=now)

        intent_early = hedge_at(now=1000.0 - 270.0)  # 270s remaining
        intent_late = hedge_at(now=1000.0 - 90.0)    # 90s remaining -- same seed, different ttc

        expected_ratio = bc.hedge_ttc_size_multiplier("Bitcoin", "CHEAP", 90.0) / \
            bc.hedge_ttc_size_multiplier("Bitcoin", "CHEAP", 270.0)
        got_ratio = intent_late.notional_usd / intent_early.notional_usd
        assert abs(got_ratio - expected_ratio) < 1e-9, (
            "the late/early notional ratio must match HEDGE_TTC_SIZE_MULTIPLIER exactly, "
            "not the entry-side TTC_SIZE_MULTIPLIER (which would be a scoping regression)"
        )

    def test_no_hedge_falls_through_to_normal_flow(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)

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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.is_hedge is True
        adverse_move = 0.80 - (1.0 - 0.30)  # == 0.10
        expected_ratio = (bc.hedge_size_ratio("Bitcoin", "CORE")
                           * bc.adverse_move_size_multiplier("Bitcoin", adverse_move)
                           * bc.absolute_price_hedge_size_multiplier("Bitcoin", 0.30))
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")  # no price= kwarg
        assert activity.first_entry_price is None

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        # ADVERSE_MOVE_SIZE_MULTIPLIER is skipped (no first_entry_price), but
        # ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER doesn't depend on it and still applies.
        expected_ratio = bc.hedge_size_ratio("Bitcoin", "CORE") * bc.absolute_price_hedge_size_multiplier("Bitcoin", 0.30)
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        # This flag alone doesn't disable ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER,
        # a separate, independently-flagged multiplier -- it still applies.
        expected_ratio = bc.hedge_size_ratio("Bitcoin", "CORE") * bc.absolute_price_hedge_size_multiplier("Bitcoin", 0.30)
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
        record_filled_entry(activity, "Down", notional_usd=1.0, regime="CORE", is_hedge=True)
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        expected_ratio = bc.hedge_continuation_size_ratio(2)  # neither multiplier applied
        dominant_cost = 10.0
        expected_notional = dominant_cost * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_hedge_size_scales_with_ttc_in_cheap(self, monkeypatch):
        """build_order_intent's hedge-sizing block, added 2026-09-13:
        the hedge's notional must include HEDGE_TTC_SIZE_MULTIPLIER,
        computed from the hedge side's own regime and seconds_remaining --
        re-derived specifically for hedges (CHEAP shrinks toward close,
        matching the same direction already shipped for entries). No
        price= kwarg on record_filled_entry -- first_entry_price stays
        unset so ADVERSE_MOVE_SIZE_MULTIPLIER is skipped, isolating the
        TTC multiplier being tested here (same precedent as
        test_adverse_move_multiplier_is_a_noop_when_first_entry_price_was_never_recorded)."""
        market = make_market(end_time=210.0, asset="Bitcoin")  # seconds_remaining=210 at now=0.0
        up_book = make_liquid_book(price=0.80, token_id="up")     # dominant
        down_book = make_liquid_book(price=0.15, token_id="down")  # hedge side -> CHEAP
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.is_hedge is True
        expected_ratio = (bc.hedge_size_ratio("Bitcoin", "CORE")
                           * bc.absolute_price_hedge_size_multiplier("Bitcoin", 0.15)
                           * bc.hedge_ttc_size_multiplier("Bitcoin", "CHEAP", 210.0))
        expected_notional = 10.0 * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_hedge_ttc_multiplier_respects_the_per_instance_feature_flag(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_HEDGE_TTC_SIZE_MULTIPLIER", False)
        market = make_market(end_time=210.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        expected_ratio = (bc.hedge_size_ratio("Bitcoin", "CORE")
                           * bc.absolute_price_hedge_size_multiplier("Bitcoin", 0.15))
        expected_notional = 10.0 * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_hedge_ttc_multiplier_also_applies_to_continuation_hedges(self, monkeypatch):
        # Calibrated on all hedge trades pooled (first + continuation), so
        # it must apply to both, unlike the hedge_count==0-only multipliers.
        # No price= kwarg on the first entry -- first_entry_price stays
        # unset so ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER is skipped,
        # isolating the TTC multiplier being tested here.
        market = make_market(end_time=210.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")
        record_filled_entry(activity, "Down", notional_usd=1.0, regime="CHEAP", is_hedge=True)
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        expected_ratio = bc.hedge_continuation_size_ratio(2) * bc.hedge_ttc_size_multiplier("Bitcoin", "CHEAP", 210.0)
        dominant_cost = 10.0
        expected_notional = dominant_cost * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_hedge_size_also_scales_with_absolute_hedge_price(self, monkeypatch):
        """build_order_intent's hedge-sizing block, added 2026-09-12:
        the first hedge's notional must also include
        ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER, on top of (not instead of)
        ADVERSE_MOVE_SIZE_MULTIPLIER -- a confirmed-independent effect
        of the hedge side's own current price."""
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.11, token_id="down")  # a calibrated point (Bitcoin CHEAP extreme)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        adverse_move = 0.80 - (1.0 - 0.11)
        expected_ratio = (bc.hedge_size_ratio("Bitcoin", "CORE")
                           * bc.adverse_move_size_multiplier("Bitcoin", adverse_move)
                           * bc.absolute_price_hedge_size_multiplier("Bitcoin", 0.11)
                           * bc.hedge_ttc_size_multiplier("Bitcoin", "CHEAP", 1000.0))
        expected_notional = 10.0 * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01
        # sanity: the multiplier isn't a silent no-op in this scenario
        assert bc.absolute_price_hedge_size_multiplier("Bitcoin", 0.11) != 1.0

    def test_absolute_price_hedge_multiplier_respects_the_per_instance_feature_flag(self, monkeypatch):
        # Explicit instruction: paperbot-mini stays on the OLD behavior
        # (as a control) via ENABLE_ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER=false.
        monkeypatch.setattr(config, "ENABLE_ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER", False)
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.11, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        adverse_move = 0.80 - (1.0 - 0.11)
        expected_ratio = (bc.hedge_size_ratio("Bitcoin", "CORE")
                           * bc.adverse_move_size_multiplier("Bitcoin", adverse_move)
                           * bc.hedge_ttc_size_multiplier("Bitcoin", "CHEAP", 1000.0))
        expected_notional = 10.0 * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_absolute_price_hedge_size_multiplier_itself_does_not_leak_into_continuation_hedges(self, monkeypatch):
        # Scoped to hedge_count==0 only, same rule as ADVERSE_MOVE_SIZE_MULTIPLIER.
        # Uses the same price (0.30, MID) as the sibling adverse-move test --
        # CHEAP has its own separate size-scaling that would otherwise
        # confound this test, which isn't about regime-specific sizing at all.
        # Also disables ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER, same as
        # that sibling test, to isolate just this multiplier's scoping.
        monkeypatch.setattr(config, "ENABLE_ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER", False)
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.30, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
        record_filled_entry(activity, "Down", notional_usd=1.0, regime="CORE", is_hedge=True)
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        expected_ratio = bc.hedge_continuation_size_ratio(2)  # not touched by this multiplier
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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
        record_filled_entry(activity, "Down", notional_usd=1.0, regime="CORE", is_hedge=True)
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)
        record_filled_entry(activity, "Down", notional_usd=1.0, regime="CORE", is_hedge=True)
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")  # no price= kwarg
        record_filled_entry(activity, "Down", notional_usd=1.0, regime="CORE", is_hedge=True)
        assert activity.first_entry_price is None
        assert activity.hedge_count == 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

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
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")

        captured = {}

        def spy_decide_hedge(asset, activity, rng, liquidity=None, is_weekend=None,
                              dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None,
                              seconds_remaining=None, seconds_since_first_entry=None):
            captured["is_weekend"] = is_weekend
            return "Down"

        monkeypatch.setattr(stratmod, "decide_hedge", spy_decide_hedge)

        SATURDAY_NOON_UTC = 1704542400.0  # 2024-01-06, weekday()==5
        MONDAY_NOON_UTC = 1704715200.0    # 2024-01-08, weekday()==0

        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=SATURDAY_NOON_UTC)
        assert captured["is_weekend"] is True

        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=MONDAY_NOON_UTC)
        assert captured["is_weekend"] is False

    def test_computes_seconds_since_first_entry_from_t_in_and_passes_it_to_decide_hedge(self, monkeypatch):
        """build_order_intent must derive seconds_since_first_entry as
        (elapsed-in-window) - activity.first_entry_t_in, using the same
        'markets are always exactly 300s' convention record_entry's own
        t_in was computed with -- and pass it through to decide_hedge."""
        market = make_market(end_time=1000.0, asset="Bitcoin")  # window opened at t=700
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")
        activity.first_entry_t_in = 40.0  # entered 40s into the window

        captured = {}

        def spy_decide_hedge(asset, activity, rng, liquidity=None, is_weekend=None,
                              dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None,
                              seconds_remaining=None, seconds_since_first_entry=None):
            captured["seconds_since_first_entry"] = seconds_since_first_entry
            return "Down"

        monkeypatch.setattr(stratmod, "decide_hedge", spy_decide_hedge)

        # now=800.0 -> 100s elapsed in the window (700 + 100 = 800) -> 100 - 40 = 60s since first entry
        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=800.0)
        assert captured["seconds_since_first_entry"] == pytest.approx(60.0)

    def test_seconds_since_first_entry_is_none_when_first_entry_t_in_was_never_recorded(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")  # no t_in ever set
        assert activity.first_entry_t_in is None

        captured = {}

        def spy_decide_hedge(asset, activity, rng, liquidity=None, is_weekend=None,
                              dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None,
                              seconds_remaining=None, seconds_since_first_entry=None):
            captured["seconds_since_first_entry"] = seconds_since_first_entry
            return "Down"

        monkeypatch.setattr(stratmod, "decide_hedge", spy_decide_hedge)
        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=800.0)
        assert captured["seconds_since_first_entry"] is None

    def test_passes_after_big_loss_through_to_decide_hedge(self, monkeypatch):
        """build_order_intent has no memory of its own across markets --
        after_big_loss must simply pass through, unmodified, straight from
        the caller (bot.py's after_big_loss_by_asset) to decide_hedge."""
        market = make_market(end_time=1000.0, asset="Ethereum")
        up_book = make_liquid_book(price=0.72, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")

        captured = {}

        def spy_decide_hedge(asset, activity, rng, liquidity=None, is_weekend=None,
                              dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None,
                              seconds_remaining=None, seconds_since_first_entry=None):
            captured["after_big_loss"] = after_big_loss
            return None

        monkeypatch.setattr(stratmod, "decide_hedge", spy_decide_hedge)

        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0,
                            after_big_loss=True)
        assert captured["after_big_loss"] is True

        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0,
                            after_big_loss=False)
        assert captured["after_big_loss"] is False

    def test_forced_first_entry_side_bypasses_decide_side_on_a_first_entry(self, monkeypatch):
        """forced_first_entry_side (added 2026-09-13, Coinbase-momentum
        bot integration point) must be used directly instead of calling
        decide_side, on a genuine first entry."""
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")
        activity = MarketActivityState()  # no entries yet

        def spy_decide_side(*args, **kwargs):
            raise AssertionError("decide_side must not be called when forced_first_entry_side is set")

        monkeypatch.setattr(stratmod, "decide_side", spy_decide_side)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0,
                                     forced_first_entry_side="Down")

        assert intent is not None
        assert intent.side == "Down"
        assert intent.price == 0.80

    def test_forced_first_entry_side_is_ignored_past_the_first_entry(self, monkeypatch):
        """Only ever applies to the market's FIRST entry -- a 2nd,
        ordinary entry must fall through to decide_side's normal held-
        side persistence, exactly as if forced_first_entry_side were
        never passed."""
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CHEAP", price=0.20)
        monkeypatch.setattr(stratmod, "decide_hedge",
                             lambda asset, activity, rng, liquidity=None, is_weekend=None,
                             dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)

        captured = {}

        def spy_decide_side(asset, activity, rng, held_side_price=None, previous_market_first_side=None,
                             previous_market_won=None):
            captured["called"] = True
            return "Up"

        monkeypatch.setattr(stratmod, "decide_side", spy_decide_side)

        build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0,
                            forced_first_entry_side="Down")

        assert captured.get("called") is True

    def test_forced_first_entry_side_never_overrides_a_real_hedge_decision(self, monkeypatch):
        """Defensive/documentation test: even though decide_hedge
        structurally never fires on a first entry (its own entry_count<1
        guard), the is_hedge branch must still take precedence in code if
        that were ever to change."""
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")
        activity = MarketActivityState()
        # Not a real entry_count==0 scenario (decide_hedge's own guard
        # means this could never happen for real) -- gives the forced
        # hedge a real dominant cost to size off, so a non-zero intent
        # actually confirms which branch won rather than both paths
        # coincidentally producing a zero-size None.
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)

        monkeypatch.setattr(stratmod, "decide_hedge",
                             lambda asset, activity, rng, liquidity=None, is_weekend=None,
                             dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0,
                                     forced_first_entry_side="Up")

        assert intent is not None
        assert intent.is_hedge is True
        assert intent.side == "Down"  # the hedge decision, not the forced side

    def test_forced_first_entry_side_defaults_to_a_noop(self):
        """Every existing call site/test that never passes
        forced_first_entry_side must be completely unaffected -- default
        None means decide_side runs exactly as before."""
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")
        activity_a = MarketActivityState()
        activity_b = MarketActivityState()

        intent_a = build_order_intent(market, up_book, down_book, activity_a, random.Random(7), now=0.0)
        intent_b = build_order_intent(market, up_book, down_book, activity_b, random.Random(7),
                                       now=0.0, forced_first_entry_side=None)

        assert intent_a is not None and intent_b is not None
        assert intent_a.side == intent_b.side

    def test_forced_first_entry_sides_regime_comes_from_its_own_book(self):
        """Same price-asymmetry discipline as everywhere else in this
        function -- regime/price must come from the FORCED side's own
        book, not a fixed reference side."""
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.85, token_id="up")    # CORE
        down_book = make_liquid_book(price=0.10, token_id="down")  # CHEAP
        activity = MarketActivityState()

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0,
                                     forced_first_entry_side="Down")

        assert intent is not None
        assert intent.side == "Down"
        assert intent.regime == "CHEAP"
        assert intent.price == 0.10

    def test_computes_dominant_current_price_from_the_dominant_sides_own_book_and_passes_it_to_decide_hedge(self, monkeypatch):
        """build_order_intent must read the DOMINANT side's own book price
        (not the eventual hedge side's) and pass it through to
        decide_hedge -- confirms the wiring, not just that
        adverse_move_hedge_trigger_multiplier works in isolation."""
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.72, token_id="up")     # dominant (Up) side's LIVE price
        down_book = make_liquid_book(price=0.15, token_id="down")  # would-be hedge side, irrelevant here
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE")  # dominant side is "Up"

        captured = {}

        def spy_decide_hedge(asset, activity, rng, liquidity=None, is_weekend=None,
                              dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None,
                              seconds_remaining=None, seconds_since_first_entry=None):
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
                              dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None,
                              seconds_remaining=None, seconds_since_first_entry=None):
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
                             dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)

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
                             dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)

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

    def test_hedge_ratio_multiplier_cap_bounds_a_pathological_pileup(self, monkeypatch):
        """Full code-audit pass (2026-09-13, 'build everything' item 5):
        the exact same risk COMBINED_SIZE_MULTIPLIER_CAP already bounds on
        the ordinary entry-curve path (decide_size) also applies to the
        hedge-ratio chain here -- ADVERSE_MOVE_SIZE_MULTIPLIER,
        ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER, and HEDGE_TTC_SIZE_MULTIPLIER
        all multiply into the same `ratio`, and nothing bounded their joint
        product until now. None of them individually reaches anywhere near
        its own cap under the real calibration tables in ordinary use, so
        this monkeypatches the underlying behavior_config functions
        directly to force a pathological all-aligned case, the same
        technique test_reentry_fatigue_and_hedge_count_reinforcement_
        dont_compound_pathologically uses for the entry-curve side."""
        monkeypatch.setattr(config, "SIZING_JITTER_FRACTION", 0.0)  # deterministic
        monkeypatch.setattr(bc, "adverse_move_size_multiplier", lambda asset, move: 6.0)
        monkeypatch.setattr(bc, "absolute_price_hedge_size_multiplier", lambda asset, price: 3.0)
        monkeypatch.setattr(bc, "hedge_ttc_size_multiplier", lambda asset, regime, secs: 1.4)

        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)

        monkeypatch.setattr(stratmod, "decide_hedge",
                             lambda asset, activity, rng, liquidity=None, is_weekend=None,
                             dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        base_ratio = bc.hedge_size_ratio("Bitcoin", "CORE")
        # Raw (uncapped) correction would be 6.0 * 3.0 * 1.4 = 25.2 -- the
        # cap must clamp it down to HEDGE_RATIO_MULTIPLIER_CAP instead.
        expected_notional = 10.0 * base_ratio * config.HEDGE_RATIO_MULTIPLIER_CAP
        assert intent.notional_usd == pytest.approx(expected_notional)

    def test_hedge_ratio_multiplier_cap_bounds_the_downside_too(self, monkeypatch):
        monkeypatch.setattr(config, "SIZING_JITTER_FRACTION", 0.0)
        small = 1.0 / 6.0
        monkeypatch.setattr(bc, "adverse_move_size_multiplier", lambda asset, move: small)
        monkeypatch.setattr(bc, "absolute_price_hedge_size_multiplier", lambda asset, price: small)
        monkeypatch.setattr(bc, "hedge_ttc_size_multiplier", lambda asset, regime, secs: small)

        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.80, token_id="up")
        down_book = make_liquid_book(price=0.15, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CORE", price=0.80)

        monkeypatch.setattr(stratmod, "decide_hedge",
                             lambda asset, activity, rng, liquidity=None, is_weekend=None,
                             dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        base_ratio = bc.hedge_size_ratio("Bitcoin", "CORE")
        expected_notional = 10.0 * base_ratio / config.HEDGE_RATIO_MULTIPLIER_CAP
        assert intent.notional_usd == pytest.approx(expected_notional)

class TestBuildOrderIntentScout:
    """SCOUT: a deliberately small, tentative FIRST entry -- the gap
    decide_hedge structurally can never close (dominant_side() needs at
    least one prior entry to exist, so decide_hedge always returns None at
    real_fill_count==0). See SCOUT_PROBABILITY/SCOUT_SIZE_RATIO's docstring
    in behavior_config.py."""

    def test_forced_scout_shrinks_the_first_entry_below_the_ordinary_curve(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.25, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
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
        # CHANGED 2026-09-14: a REAL fill, not just a placement (see
        # MarketActivityState's docstring -- the scout gate now reads
        # real_fill_count, not entry_count). record_filled_entry mirrors
        # bot.py calling record_real_fill once an order actually fills.
        record_filled_entry(activity, "Up", notional_usd=5.0, regime="CHEAP")  # real_fill_count now 1

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(bc, "scout_probability", lambda asset: 1.0)  # would always fire if reachable

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is not None
        assert intent.is_scout is False

    def test_scout_still_reachable_after_a_placement_that_never_filled(self, monkeypatch):
        """CHANGED 2026-09-14 (root-cause fix, see MarketActivityState's
        docstring): a market whose first order was placed and then
        cancelled/expired unfilled has real_fill_count still 0 -- the
        trader's own trade log has no record of that attempt at all, so a
        retry is his true first entry and remains scout-eligible. Under the
        old entry_count-based gate this retry permanently lost scout
        eligibility the moment ANYTHING was placed, filled or not."""
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=5.0, regime="CHEAP")  # placed, never filled
        activity.release_unfilled("Up", 5.0)  # cancelled before any fill (entry_count stays 1)

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(bc, "scout_probability", lambda asset: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is not None
        assert intent.is_scout is True

    def test_scout_does_not_stack_with_a_floor_lot_roll(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="BNB")  # BNB/CHEAP/first has a real floor-lot rate
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()
        rng = random.Random(0)
        rng.random = lambda: 0.0  # forces the floor-lot roll ahead of the scout check

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None, is_weekend=None, dominant_current_price=None, prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
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
            prev_hedge_rate=None, after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")

        uncapped = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert uncapped is not None
        assert uncapped.is_hedge is True

        capped_activity = MarketActivityState()
        capped_activity.record_entry("Up", notional_usd=100.0, regime="CORE")
        capped = build_order_intent(market, up_book, down_book, capped_activity, random.Random(0),
                                     now=0.0, max_notional_usd=uncapped.notional_usd / 10.0)
        assert capped is not None
        assert capped.notional_usd < uncapped.notional_usd


class TestCheapJointDampener:
    """CHEAP_JOINT_ENTRY_MULTIPLIER (2026-09-18, MERGED 2026-09-19 from
    two separate mechanisms -- DEEP_LONGSHOT_ENTRY_MULTIPLIER for price
    and CHEAP_FRESHNESS_MULTIPLIER for time-since-crossing -- after a
    joint model proved the two axes interact and independent
    multiplication mis-suppressed the joint (deep, stale) cells by up to
    7x). A first CHEAP entry gets probabilistically skipped based on its
    (is_deep, seconds-since-crossing) cell.

    RECALIBRATED 2026-09-21: the two is_deep rows were previously
    identical placeholders (DEEP_LONGSHOT itself was disabled), which
    turned out to be a real bug -- the shared freshness clock (measured
    from the original ~0.30 CHEAP crossing, not from reaching whatever
    price the episode is currently at) meant deep-price entries were
    getting killed by staleness accumulated while price was still
    shallow. Fixed by differentiating is_deep=True/False using fresh,
    well-powered post-boundary hazard data (not the clock itself -- two
    clock-redefinition attempts were tried and rejected in favor of this
    price-axis fix, see CHEAP_JOINT_ENTRY_MULTIPLIER's docstring in
    behavior_config.py for why). These tests cover the mechanism's
    gating, exclusions and feature flag against the new calibration."""

    def test_multiplier_is_a_noop_within_the_first_bucket(self):
        assert bc.cheap_joint_entry_multiplier("Bitcoin", 0.10, 0.0) == 1.0
        assert bc.cheap_joint_entry_multiplier("Bitcoin", 0.10, 59.9) == 1.0

    def test_multiplier_applies_the_calibrated_value_in_later_buckets(self):
        assert bc.cheap_joint_entry_multiplier("Bitcoin", 0.10, 90.0) == pytest.approx(0.7225)
        assert bc.cheap_joint_entry_multiplier("Bitcoin", 0.10, 150.0) == pytest.approx(0.7997)
        assert bc.cheap_joint_entry_multiplier("Bitcoin", 0.10, 300.0) == pytest.approx(0.1002)
        assert bc.cheap_joint_entry_multiplier("Ethereum", 0.10, 90.0) == pytest.approx(0.8059)
        assert bc.cheap_joint_entry_multiplier("Solana", 0.10, 90.0) == pytest.approx(0.5222)

    def test_deep_prices_get_meaningfully_more_room_than_shallow_at_the_harshest_bucket(self):
        # The 180s+ bucket is where the old identical-rows placeholder did
        # the most damage (crushed both rows to 0.02-0.03x). Now deep
        # (<0.05) must be meaningfully LESS suppressed than shallow there,
        # on every asset -- this is the core bug this recalibration fixes.
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            deep = bc.cheap_joint_entry_multiplier(asset, 0.03, 300.0)
            shallow = bc.cheap_joint_entry_multiplier(asset, 0.10, 300.0)
            assert deep > shallow

    def test_multiplier_defaults_to_noop_for_an_uncalibrated_asset(self):
        assert bc.cheap_joint_entry_multiplier("Dogecoin", 0.10, 300.0) == 1.0

    def test_multiplier_is_a_noop_when_no_crossing_recorded(self):
        assert bc.cheap_joint_entry_multiplier("Bitcoin", 0.10, None) == 1.0

    def test_multiplier_treats_a_negative_elapsed_as_the_first_bucket(self):
        # Defensive: a crossing timestamp somehow after `now` shouldn't crash.
        assert bc.cheap_joint_entry_multiplier("Bitcoin", 0.10, -5.0) == 1.0

    def test_every_calibrated_value_is_a_valid_probability(self):
        for asset, rows in bc.CHEAP_JOINT_ENTRY_MULTIPLIER.items():
            for is_deep, buckets in rows.items():
                for lo, hi, mult in buckets:
                    assert 0.0 <= mult <= 1.0, (asset, is_deep, lo, hi, mult)

    def test_dampener_can_skip_a_stale_cheap_first_entry(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.10, token_id="up")
        down_book = make_liquid_book(price=0.90, token_id="down")
        activity = MarketActivityState()
        activity.up_cheap_crossed_at = 0.0  # crossed at t=0

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "cheap_joint_entry_multiplier", lambda asset, price, elapsed: 0.0)  # always skip

        # now=200.0 -- 200s after the crossing, well into the stale tail
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is None

    def test_dampener_can_skip_a_deep_longshot_first_entry(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.03, token_id="up")
        down_book = make_liquid_book(price=0.97, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "cheap_joint_entry_multiplier", lambda asset, price, elapsed: 0.0)  # always skip

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is None

    def test_dampener_never_fires_when_freshly_crossed(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.10, token_id="up")
        down_book = make_liquid_book(price=0.90, token_id="down")
        activity = MarketActivityState()
        activity.up_cheap_crossed_at = 0.0

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")

        # now=1.0 -- only 1s since the crossing, squarely in the no-op first bucket
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=1.0)
        assert intent is not None

    def test_dampener_never_fires_with_no_recorded_crossing(self, monkeypatch):
        # activity.up_cheap_crossed_at left at its default None (e.g. a
        # market that opened already-cheap with no pre-crossing tick
        # captured) -- must not skip.
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.10, token_id="up")
        down_book = make_liquid_book(price=0.90, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        # Deliberately NOT monkeypatching cheap_joint_entry_multiplier here --
        # the real function's own None-handling (elapsed=None -> 1.0 no-op)
        # is exactly what this test verifies.

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is not None  # no crossing recorded -- multiplier no-ops on None, never skips

    def test_dampener_never_fires_above_the_threshold_and_freshly_crossed(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is not None  # 0.20 is above the deep threshold, freshly crossed -- no-op cell

    def test_dampener_does_not_apply_to_hedges(self, monkeypatch):
        # A hedge landing below the threshold, or stale, must NOT be
        # skipped -- the finding was measured on first entries only.
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.97, token_id="up")
        down_book = make_liquid_book(price=0.03, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=100.0, regime="HIGH", price=0.97)
        activity.down_cheap_crossed_at = 0.0

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")
        monkeypatch.setattr(bc, "cheap_joint_entry_multiplier", lambda asset, price, elapsed: 0.0)  # would always skip a first entry

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is not None
        assert intent.is_hedge is True

    def test_dampener_does_not_apply_once_a_real_fill_has_happened(self, monkeypatch):
        # Not a first entry (real_fill_count > 0) -- must not be gated.
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.03, token_id="up")
        down_book = make_liquid_book(price=0.97, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=5.0, regime="CHEAP", price=0.05)
        activity.up_cheap_crossed_at = 0.0

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "cheap_joint_entry_multiplier", lambda asset, price, elapsed: 0.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is not None  # real_fill_count > 0 -- dampener is scoped to first entries only

    def test_dampener_does_not_apply_outside_cheap_regime(self, monkeypatch):
        # Even if a (misconfigured) crossing timestamp were present, a
        # MID-band price must never trigger this dampener.
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()
        activity.up_cheap_crossed_at = 0.0

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "cheap_joint_entry_multiplier", lambda asset, price, elapsed: 0.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is not None  # regime is MID, not CHEAP -- dampener call site is never reached

    def test_feature_flag_disables_the_dampener(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.03, token_id="up")
        down_book = make_liquid_book(price=0.97, token_id="down")
        activity = MarketActivityState()
        activity.up_cheap_crossed_at = 0.0

        monkeypatch.setattr(config, "ENABLE_CHEAP_JOINT_DAMPENER", False)
        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "cheap_joint_entry_multiplier", lambda asset, price, elapsed: 0.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is not None  # flag off -- must not skip even though the multiplier says to


class TestDecisiveEntryEligibilityDampener:
    """decisive_entry_eligibility_multiplier wiring, added 2026-09-20 --
    the CORE/HIGH sibling of TestCheapJointDampener above. Mirrors that
    class's test structure exactly, using up_decisive_crossed_at/
    down_decisive_crossed_at instead of the cheap crossing fields."""

    def test_dampener_can_skip_a_stale_decisive_first_entry(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.95, token_id="up")
        down_book = make_liquid_book(price=0.05, token_id="down")
        activity = MarketActivityState()
        activity.up_decisive_crossed_at = 0.0  # crossed at t=0

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "decisive_entry_eligibility_multiplier", lambda asset, elapsed: 0.0)  # always skip

        # now=200.0 -- 200s after the crossing, well into the stale tail
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is None

    def test_dampener_never_fires_when_freshly_crossed(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.95, token_id="up")
        down_book = make_liquid_book(price=0.05, token_id="down")
        activity = MarketActivityState()
        activity.up_decisive_crossed_at = 0.0

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")

        # now=1.0 -- only 1s since the crossing, squarely in the no-op first bucket
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=1.0)
        assert intent is not None

    def test_dampener_never_fires_with_no_recorded_crossing(self, monkeypatch):
        # up_decisive_crossed_at left at its default None -- must not skip.
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.95, token_id="up")
        down_book = make_liquid_book(price=0.05, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        # Deliberately NOT monkeypatching decisive_entry_eligibility_multiplier --
        # the real function's own None-handling (elapsed=None -> 1.0 no-op) is
        # exactly what this test verifies.

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is not None

    def test_dampener_does_not_apply_to_hedges(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.05, token_id="up")
        down_book = make_liquid_book(price=0.95, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=100.0, regime="CHEAP", price=0.05)
        activity.down_decisive_crossed_at = 0.0

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")
        monkeypatch.setattr(bc, "decisive_entry_eligibility_multiplier", lambda asset, elapsed: 0.0)  # would always skip a first entry

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is not None
        assert intent.is_hedge is True

    def test_dampener_does_not_apply_once_a_real_fill_has_happened(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.95, token_id="up")
        down_book = make_liquid_book(price=0.05, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=5.0, regime="HIGH", price=0.95)
        activity.up_decisive_crossed_at = 0.0

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "decisive_entry_eligibility_multiplier", lambda asset, elapsed: 0.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is not None  # real_fill_count > 0 -- dampener is scoped to first entries only

    def test_dampener_does_not_apply_outside_core_high_regime(self, monkeypatch):
        # Even if a (misconfigured) crossing timestamp were present, a
        # MID-band price must never trigger this dampener.
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()
        activity.up_decisive_crossed_at = 0.0

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "decisive_entry_eligibility_multiplier", lambda asset, elapsed: 0.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is not None  # regime is MID, not CORE/HIGH -- dampener call site is never reached

    def test_feature_flag_disables_the_dampener(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.95, token_id="up")
        down_book = make_liquid_book(price=0.05, token_id="down")
        activity = MarketActivityState()
        activity.up_decisive_crossed_at = 0.0

        monkeypatch.setattr(config, "ENABLE_DECISIVE_ENTRY_ELIGIBILITY", False)
        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "decisive_entry_eligibility_multiplier", lambda asset, elapsed: 0.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=200.0)
        assert intent is not None  # flag off -- must not skip even though the multiplier says to


class TestRegimeDriftCorrection:
    """regime_engagement_state/bc.regime_drift_correction wiring, added
    2026-09-23 -- a closed-loop overlay on top of
    TestRegimeEngagementDampener's static mechanism just below. Default OFF
    (config.ENABLE_REGIME_DRIFT_CORRECTION), so every test elsewhere in the
    suite is unaffected without needing a conftest.py neutralizer (unlike
    regime_engagement_multiplier, this one doesn't reach into tests that
    weren't written with it in mind, since it requires BOTH the flag on AND
    a non-None regime_engagement_state to do anything)."""

    def _base_mocks(self, monkeypatch, side="Up"):
        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: side)
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 1.0)  # static: no-op

    def test_flag_off_ignores_state_even_if_it_would_dampen_to_zero(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()
        self._base_mocks(monkeypatch)
        monkeypatch.setattr(config, "ENABLE_REGIME_DRIFT_CORRECTION", False)
        monkeypatch.setattr(bc, "regime_drift_correction", lambda asset, regime, counts, total: 0.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0,
                                     regime_engagement_state={"Bitcoin": {"counts": {"MID": 100}, "total": 100}})
        assert intent is not None  # flag off -- correction must not apply

    def test_none_state_is_a_noop_even_with_flag_on(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()
        self._base_mocks(monkeypatch)
        monkeypatch.setattr(config, "ENABLE_REGIME_DRIFT_CORRECTION", True)
        monkeypatch.setattr(bc, "regime_drift_correction", lambda asset, regime, counts, total: 0.0)

        # regime_engagement_state defaults to None -- must not error, and
        # since the docstring promises None -> no-op, must not skip either.
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0)
        assert intent is not None

    def test_correction_can_skip_an_entry_the_static_multiplier_alone_would_allow(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()
        self._base_mocks(monkeypatch)  # static multiplier is a no-op (1.0)
        monkeypatch.setattr(config, "ENABLE_REGIME_DRIFT_CORRECTION", True)
        monkeypatch.setattr(bc, "regime_drift_correction", lambda asset, regime, counts, total: 0.0)  # always dampen to zero

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0,
                                     regime_engagement_state={"Bitcoin": {"counts": {"MID": 100}, "total": 100}})
        assert intent is None

    def test_correction_never_fires_for_hedges(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=100.0, regime="MID", price=0.50)
        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")
        monkeypatch.setattr(config, "ENABLE_REGIME_DRIFT_CORRECTION", True)
        monkeypatch.setattr(bc, "regime_drift_correction", lambda asset, regime, counts, total: 0.0)  # would always skip an ordinary entry

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0,
                                     regime_engagement_state={"Bitcoin": {"counts": {"MID": 100}, "total": 100}})
        assert intent is not None
        assert intent.is_hedge is True

    def test_reads_the_specific_assets_state_not_a_different_asset(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()
        self._base_mocks(monkeypatch)
        monkeypatch.setattr(config, "ENABLE_REGIME_DRIFT_CORRECTION", True)

        seen = {}

        def fake_correction(asset, regime, counts, total):
            seen["asset"] = asset
            seen["counts"] = counts
            seen["total"] = total
            return 1.0

        monkeypatch.setattr(bc, "regime_drift_correction", fake_correction)
        intent = build_order_intent(
            market, up_book, down_book, activity, random.Random(0), now=100.0,
            regime_engagement_state={
                "Bitcoin": {"counts": {"MID": 3}, "total": 7},
                "Ethereum": {"counts": {"MID": 999}, "total": 999},
            })
        assert intent is not None
        assert seen["asset"] == "Bitcoin"
        assert seen["counts"] == {"MID": 3}
        assert seen["total"] == 7

    def test_missing_asset_in_state_dict_defaults_to_cold_start(self, monkeypatch):
        # An asset with no entry yet in regime_engagement_state (e.g. right
        # after a restart, before its first non-hedge entry) must not error.
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()
        self._base_mocks(monkeypatch)
        monkeypatch.setattr(config, "ENABLE_REGIME_DRIFT_CORRECTION", True)

        intent = build_order_intent(
            market, up_book, down_book, activity, random.Random(0), now=100.0,
            regime_engagement_state={"Ethereum": {"counts": {"MID": 5}, "total": 5}})
        assert intent is not None  # real bc.regime_drift_correction(total=0) -> 1.0, no-op


class TestRegimeEngagementDampener:
    """regime_engagement_multiplier wiring, added 2026-09-21 -- unlike
    TestCheapJointDampener/TestDecisiveEntryEligibilityDampener above,
    this fires in EVERY regime and is NOT scoped to real_fill_count==0
    (see build_order_intent's own comment for why). conftest.py's
    autouse neutralize_regime_engagement_multiplier fixture defaults this
    to a no-op for every other test in the suite -- these tests
    explicitly re-enable it."""

    def test_dampener_can_skip_an_entry_in_a_suppressed_regime(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")  # MID
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 0.0)  # always skip

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0)
        assert intent is None

    def test_dampener_never_fires_for_a_no_op_multiplier(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0)
        assert intent is not None

    def test_dampener_does_not_apply_to_hedges(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=100.0, regime="MID", price=0.50)

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 0.0)  # would always skip an ordinary entry

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0)
        assert intent is not None
        assert intent.is_hedge is True

    def test_feature_flag_disables_the_dampener(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(config, "ENABLE_REGIME_ENGAGEMENT_MULTIPLIER", False)
        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 0.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0)
        assert intent is not None  # flag off -- must not skip even though the multiplier says to

    def test_dampener_applies_even_after_a_real_fill_unlike_the_freshness_gates(self, monkeypatch):
        # Deliberately DIFFERENT from TestDecisiveEntryEligibilityDampener's
        # equivalent test -- this gate is NOT scoped to first entries only,
        # since the finding it fixes (his overall regime MIX) was measured
        # across his whole decision history, not just first entries.
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=5.0, regime="MID", price=0.50)

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 0.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0)
        assert intent is None

    def test_episode_latch_prevents_re_rolling_on_a_second_call(self, monkeypatch):
        # The v3->v4 historical-replay finding this fixes: a flat per-tick
        # roll against a long-lived regime "eventually" passes no matter
        # how low the probability is. Verify a SECOND call for the same
        # unchanged (side, regime) is rejected WITHOUT consuming another
        # rng draw -- simulated here with a Random subclass that raises if
        # asked for more than one draw.
        class OneShotRandom(random.Random):
            def __init__(self, seed):
                super().__init__(seed)
                self._draws = 0

            def random(self):
                self._draws += 1
                if self._draws > 1:
                    raise AssertionError("regime engagement gate re-rolled an already-rejected episode")
                return 0.999  # guarantees a reject against any mult < 1.0

        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 0.5)

        rng = OneShotRandom(0)
        first = build_order_intent(market, up_book, down_book, activity, rng, now=100.0)
        second = build_order_intent(market, up_book, down_book, activity, rng, now=112.0)
        assert first is None
        assert second is None
        assert activity.is_regime_episode_rejected("Up", "MID") is True

    def test_a_regime_change_produces_a_fresh_episode_not_blocked_by_the_old_rejection(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        activity = MarketActivityState()
        activity.reject_regime_episode("Up", "MID")  # simulates an earlier rejection

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 1.0)

        # price has since moved to CHEAP -- a different (side, regime) key,
        # so the old MID rejection must not block it.
        up_book = make_liquid_book(price=0.10, token_id="up")
        down_book = make_liquid_book(price=0.90, token_id="down")
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0)
        assert intent is not None


class TestCheapTtcSelectivityDampenerWiring:
    """build_order_intent's cheap_ttc_selectivity_multiplier wiring,
    added 2026-09-20 -- see TestCheapTtcSelectivityMultiplier for the
    function itself; these confirm the call site actually threads
    seconds_remaining through. Ethereum, end_time=1000.0 -- a worse-
    than-every-bucket price (0.29) so the effect is present regardless
    of which time-to-close bucket applies."""

    def test_skips_more_often_with_more_time_remaining(self):
        market = make_market(end_time=1000.0, asset="Ethereum")
        up_book = make_liquid_book(price=0.29, token_id="up")
        down_book = make_liquid_book(price=0.71, token_id="down")

        def n_placed(now):
            n = 0
            for seed in range(300):
                activity = MarketActivityState()
                intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=now)
                if intent is not None:
                    n += 1
            return n

        n_high_ttc = n_placed(now=710.0)  # seconds_remaining=290 -- lots of time left
        n_low_ttc = n_placed(now=920.0)   # seconds_remaining=80 -- little time left (just above MIN_SECONDS_BEFORE_CLOSE=75)
        assert n_high_ttc < n_low_ttc, "more time remaining should skip a bad price more often"

    def test_does_not_apply_outside_confirmed_assets(self):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.29, token_id="up")
        down_book = make_liquid_book(price=0.71, token_id="down")

        def n_placed(now):
            n = 0
            for seed in range(300):
                activity = MarketActivityState()
                intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=now)
                if intent is not None:
                    n += 1
            return n

        assert n_placed(now=710.0) == n_placed(now=920.0)

    def test_respects_its_own_feature_flag(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_CHEAP_TTC_SELECTIVITY", False)
        market = make_market(end_time=1000.0, asset="Ethereum")
        up_book = make_liquid_book(price=0.29, token_id="up")
        down_book = make_liquid_book(price=0.71, token_id="down")

        def n_placed(now):
            n = 0
            for seed in range(300):
                activity = MarketActivityState()
                intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=now)
                if intent is not None:
                    n += 1
            return n

        assert n_placed(now=710.0) == n_placed(now=920.0), "with the flag off, seconds_remaining must not move the rate"


class TestUpdateCheapCrossings:
    """MarketActivityState.update_cheap_crossings (2026-09-18): mirror
    image of update_decisive_crossings, latching the first time each
    side's price drops below the CHEAP threshold."""

    def test_latches_up_side_below_threshold(self):
        activity = MarketActivityState()
        activity.update_cheap_crossings(up_price=0.25, down_price=0.75, now=100.0)
        assert activity.up_cheap_crossed_at == 100.0
        assert activity.down_cheap_crossed_at is None

    def test_latches_down_side_below_threshold(self):
        activity = MarketActivityState()
        activity.update_cheap_crossings(up_price=0.75, down_price=0.25, now=100.0)
        assert activity.down_cheap_crossed_at == 100.0
        assert activity.up_cheap_crossed_at is None

    def test_does_not_latch_at_or_above_threshold(self):
        activity = MarketActivityState()
        activity.update_cheap_crossings(up_price=0.30, down_price=0.70, now=100.0)
        assert activity.up_cheap_crossed_at is None
        assert activity.down_cheap_crossed_at is None

    def test_is_a_one_shot_lock_not_overwritten_on_later_ticks(self):
        activity = MarketActivityState()
        activity.update_cheap_crossings(up_price=0.25, down_price=0.75, now=100.0)
        activity.update_cheap_crossings(up_price=0.10, down_price=0.90, now=200.0)
        assert activity.up_cheap_crossed_at == 100.0  # unchanged, not reset to 200.0

    def test_handles_none_prices_without_crashing(self):
        activity = MarketActivityState()
        activity.update_cheap_crossings(up_price=None, down_price=None, now=100.0)
        assert activity.up_cheap_crossed_at is None
        assert activity.down_cheap_crossed_at is None

    def test_respects_a_custom_threshold(self):
        activity = MarketActivityState()
        activity.update_cheap_crossings(up_price=0.45, down_price=0.55, now=100.0, threshold=0.50)
        assert activity.up_cheap_crossed_at == 100.0
        assert activity.down_cheap_crossed_at is None


class TestUpdateFirstPrices:
    """MarketActivityState.update_first_prices (2026-09-18): plain
    first-observation latch, no threshold, for the MID contrarian
    side-selection signal."""

    def test_latches_the_first_observed_price_each_side(self):
        activity = MarketActivityState()
        activity.update_first_prices(up_price=0.45, down_price=0.55)
        assert activity.up_first_price == 0.45
        assert activity.down_first_price == 0.55

    def test_is_a_one_shot_lock(self):
        activity = MarketActivityState()
        activity.update_first_prices(up_price=0.45, down_price=0.55)
        activity.update_first_prices(up_price=0.30, down_price=0.70)
        assert activity.up_first_price == 0.45
        assert activity.down_first_price == 0.55

    def test_handles_none_prices_without_crashing(self):
        activity = MarketActivityState()
        activity.update_first_prices(up_price=None, down_price=None)
        assert activity.up_first_price is None
        assert activity.down_first_price is None

    def test_can_latch_one_side_before_the_other(self):
        activity = MarketActivityState()
        activity.update_first_prices(up_price=0.45, down_price=None)
        activity.update_first_prices(up_price=0.30, down_price=0.55)
        assert activity.up_first_price == 0.45  # unchanged
        assert activity.down_first_price == 0.55  # first time it was available


class TestRegimeEpisodeRejection:
    """MarketActivityState.is_regime_episode_rejected/reject_regime_episode
    (2026-09-21), the episode-latch state REGIME_ENGAGEMENT_MULTIPLIER
    needs -- see its own docstring for why a flat per-tick roll against a
    long-lived regime isn't enough on its own."""

    def test_nothing_rejected_by_default(self):
        activity = MarketActivityState()
        assert activity.is_regime_episode_rejected("Up", "MID") is False
        assert activity.is_regime_episode_rejected("Down", "CHEAP") is False

    def test_reject_then_check_round_trips(self):
        activity = MarketActivityState()
        activity.reject_regime_episode("Up", "MID")
        assert activity.is_regime_episode_rejected("Up", "MID") is True

    def test_rejection_is_scoped_to_the_exact_side_and_regime(self):
        activity = MarketActivityState()
        activity.reject_regime_episode("Up", "MID")
        assert activity.is_regime_episode_rejected("Down", "MID") is False
        assert activity.is_regime_episode_rejected("Up", "CORE") is False

    def test_rejecting_the_same_episode_twice_is_a_safe_no_op(self):
        activity = MarketActivityState()
        activity.reject_regime_episode("Up", "MID")
        activity.reject_regime_episode("Up", "MID")
        assert activity.rejected_regime_episodes["Up"] == ["MID"]

    def test_both_sides_can_independently_reject_different_regimes(self):
        activity = MarketActivityState()
        activity.reject_regime_episode("Up", "MID")
        activity.reject_regime_episode("Down", "CORE")
        assert activity.is_regime_episode_rejected("Up", "MID") is True
        assert activity.is_regime_episode_rejected("Down", "CORE") is True
        assert activity.is_regime_episode_rejected("Up", "CORE") is False
        assert activity.is_regime_episode_rejected("Down", "MID") is False

    def test_round_trips_through_save_and_load(self, tmp_path):
        activity = MarketActivityState()
        activity.reject_regime_episode("Up", "MID")
        path = tmp_path / "activity.json"
        stratmod.save_activity_state({"cond-1": activity}, path)
        loaded = stratmod.load_activity_state(path)
        assert loaded["cond-1"].is_regime_episode_rejected("Up", "MID") is True


class TestMidContrarianFirstEntrySide:
    """bc.MID_CONTRARIAN_BIAS_PROBABILITY / mid_contrarian_first_entry_side
    (2026-09-18): the real trader's MID first-entry side genuinely favors
    whichever side has lost value since window-open. See
    behavior_config.py's docstring for the full derivation."""

    def test_bias_probability_lookup_by_tercile(self):
        assert bc.mid_contrarian_bias_probability("Bitcoin", 0.01) == pytest.approx(0.5946)
        assert bc.mid_contrarian_bias_probability("Bitcoin", 0.08) == pytest.approx(0.6596)
        assert bc.mid_contrarian_bias_probability("Bitcoin", 0.20) == pytest.approx(0.7037)

    def test_bias_probability_is_zero_for_an_uncalibrated_asset(self):
        assert bc.mid_contrarian_bias_probability("Dogecoin", 0.10) == 0.0

    def test_no_signal_when_a_side_already_established(self):
        activity = MarketActivityState()
        activity.up_first_price = 0.50
        activity.down_first_price = 0.50
        activity.last_side = "Up"  # not a true first entry anymore
        up_book = make_liquid_book(price=0.30, token_id="up")
        down_book = make_liquid_book(price=0.70, token_id="down")
        result = mid_contrarian_first_entry_side("Bitcoin", activity, up_book, down_book, random.Random(0))
        assert result is None

    def test_no_signal_outside_mid_band(self):
        activity = MarketActivityState()
        activity.up_first_price = 0.50
        activity.down_first_price = 0.50
        up_book = make_liquid_book(price=0.20, token_id="up")  # CHEAP, not MID
        down_book = make_liquid_book(price=0.80, token_id="down")
        result = mid_contrarian_first_entry_side("Bitcoin", activity, up_book, down_book, random.Random(0))
        assert result is None

    def test_no_signal_when_first_price_not_yet_recorded(self):
        activity = MarketActivityState()  # up_first_price/down_first_price both None
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        result = mid_contrarian_first_entry_side("Bitcoin", activity, up_book, down_book, random.Random(0))
        assert result is None

    def test_no_signal_at_zero_magnitude(self):
        # Regression test for the bug caught before shipping: a market's
        # very first tick has first_price == current price (both latched
        # the same tick), giving velocity exactly 0 -- must NOT be
        # treated as "smallest tercile" and fire anyway.
        activity = MarketActivityState()
        activity.up_first_price = 0.40
        activity.down_first_price = 0.60
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        result = mid_contrarian_first_entry_side("Bitcoin", activity, up_book, down_book, random.Random(0))
        assert result is None

    def test_fires_and_picks_the_side_that_lost_value(self):
        activity = MarketActivityState()
        activity.up_first_price = 0.60   # Up started at 0.60
        activity.down_first_price = 0.40  # Down started at 0.40
        up_book = make_liquid_book(price=0.40, token_id="up")   # Up dropped 0.20 -- the "dip"
        down_book = make_liquid_book(price=0.60, token_id="down")  # Down rose 0.20
        # rng.random() always returns < any real probability -> forces a fire
        result = mid_contrarian_first_entry_side(
            "Bitcoin", activity, up_book, down_book,
            type("AlwaysFire", (), {"random": staticmethod(lambda: 0.0)})())
        assert result == "Up"

    def test_can_pick_down_when_down_lost_value(self):
        activity = MarketActivityState()
        activity.up_first_price = 0.40
        activity.down_first_price = 0.60
        up_book = make_liquid_book(price=0.60, token_id="up")     # Up rose
        down_book = make_liquid_book(price=0.40, token_id="down")  # Down dropped -- the "dip"
        result = mid_contrarian_first_entry_side(
            "Bitcoin", activity, up_book, down_book,
            type("AlwaysFire", (), {"random": staticmethod(lambda: 0.0)})())
        assert result == "Down"

    def test_does_not_fire_when_roll_exceeds_probability(self):
        activity = MarketActivityState()
        activity.up_first_price = 0.60
        activity.down_first_price = 0.40
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        result = mid_contrarian_first_entry_side(
            "Bitcoin", activity, up_book, down_book,
            type("NeverFire", (), {"random": staticmethod(lambda: 0.9999)})())
        assert result is None


class TestBuildOrderIntentMidContrarian:
    """Integration tests: the contrarian override wired into
    build_order_intent's decision chain."""

    def test_contrarian_override_picks_the_losing_side(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        activity = MarketActivityState()
        activity.up_first_price = 0.60
        activity.down_first_price = 0.40

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(bc, "mid_contrarian_bias_probability", lambda asset, magnitude: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is not None
        assert intent.side == "Up"
        assert intent.reason == "mid_contrarian"

    def test_does_not_override_a_hedge(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Down", notional_usd=100.0, regime="MID", price=0.60)
        activity.up_first_price = 0.60
        activity.down_first_price = 0.40

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Up")
        monkeypatch.setattr(bc, "mid_contrarian_bias_probability", lambda asset, magnitude: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is not None
        assert intent.is_hedge is True

    def test_does_not_override_forced_first_entry_side(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        activity = MarketActivityState()
        activity.up_first_price = 0.60
        activity.down_first_price = 0.40

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(bc, "mid_contrarian_bias_probability", lambda asset, magnitude: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0,
                                     forced_first_entry_side="Down")
        assert intent is not None
        assert intent.side == "Down"
        assert intent.reason != "mid_contrarian"

    def test_size_dampener_applies_to_contrarian_entries(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        activity_contrarian = MarketActivityState()
        activity_contrarian.up_first_price = 0.60
        activity_contrarian.down_first_price = 0.40
        activity_ordinary = MarketActivityState()  # no first_price set -- can't trigger contrarian

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "mid_contrarian_bias_probability", lambda asset, magnitude: 1.0)
        monkeypatch.setattr(config, "ENABLE_ACCURACY_SCOUT_MULTIPLIER", False)

        contrarian_intent = build_order_intent(market, up_book, down_book, activity_contrarian,
                                                random.Random(0), now=0.0)
        ordinary_intent = build_order_intent(market, up_book, down_book, activity_ordinary,
                                              random.Random(0), now=0.0)
        assert contrarian_intent is not None and ordinary_intent is not None
        assert contrarian_intent.reason == "mid_contrarian"
        assert ordinary_intent.reason != "mid_contrarian"
        # Same price/regime -- the only difference should be the ~0.72x dampener
        assert contrarian_intent.notional_usd < ordinary_intent.notional_usd

    def test_feature_flag_disables_the_override(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        activity = MarketActivityState()
        activity.up_first_price = 0.60
        activity.down_first_price = 0.40

        monkeypatch.setattr(config, "ENABLE_MID_CONTRARIAN_BIAS", False)
        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Down")
        monkeypatch.setattr(bc, "mid_contrarian_bias_probability", lambda asset, magnitude: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)
        assert intent is not None
        assert intent.side == "Down"  # decide_side's answer, not the contrarian override
        assert intent.reason != "mid_contrarian"


class TestCrossAssetCarryoverProbability:
    """bc.CROSS_ASSET_CARRYOVER_PROBABILITY / cross_asset_carryover_probability
    (2026-09-19): the real trader's first-entry side on one asset
    correlates with his most recent first-entry side on a DIFFERENT
    asset if that decision landed within the last ~2-5 minutes. See
    behavior_config.py's docstring for the full derivation."""

    def test_lookup_by_gap_bucket(self):
        assert bc.cross_asset_carryover_probability("Bitcoin", "Ethereum", 5) == pytest.approx(0.697)
        assert bc.cross_asset_carryover_probability("Bitcoin", "Ethereum", 20) == pytest.approx(0.618)
        assert bc.cross_asset_carryover_probability("Bitcoin", "Ethereum", 250) == pytest.approx(0.515)

    def test_zero_beyond_max_gap(self):
        assert bc.cross_asset_carryover_probability("Bitcoin", "Ethereum", 300) == 0.0
        assert bc.cross_asset_carryover_probability("Bitcoin", "Ethereum", 1000) == 0.0

    def test_zero_for_negative_gap(self):
        assert bc.cross_asset_carryover_probability("Bitcoin", "Ethereum", -1) == 0.0

    def test_zero_for_uncalibrated_pair(self):
        assert bc.cross_asset_carryover_probability("Bitcoin", "Dogecoin", 5) == 0.0

    def test_asymmetric_by_direction(self):
        # Bitcoin is the stronger leader -- BTC->ETH should differ from
        # ETH->BTC at the same gap, not be symmetric.
        btc_leads = bc.cross_asset_carryover_probability("Bitcoin", "Ethereum", 5)
        eth_leads = bc.cross_asset_carryover_probability("Ethereum", "Bitcoin", 5)
        assert btc_leads != eth_leads


class TestCrossAssetCarryoverSide:
    """cross_asset_carryover_side: the priority-override function itself."""

    def test_no_signal_when_not_a_first_entry(self):
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="MID", price=0.40)
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        last = {"Ethereum": {"side": "Up", "ts": 95.0}}
        result = cross_asset_carryover_side("Bitcoin", activity, up_book, down_book, last,
                                             now=100.0, rng=random.Random(0))
        assert result is None

    def test_no_signal_when_no_other_asset_state(self):
        activity = MarketActivityState()
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        result = cross_asset_carryover_side("Bitcoin", activity, up_book, down_book, None,
                                             now=100.0, rng=random.Random(0))
        assert result is None
        result = cross_asset_carryover_side("Bitcoin", activity, up_book, down_book, {},
                                             now=100.0, rng=random.Random(0))
        assert result is None

    def test_ignores_own_asset_entry(self):
        # Only OTHER assets' state should count, never the same asset's.
        activity = MarketActivityState()
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        last = {"Bitcoin": {"side": "Down", "ts": 95.0}}
        result = cross_asset_carryover_side("Bitcoin", activity, up_book, down_book, last,
                                             now=100.0, rng=random.Random(0))
        assert result is None

    def test_no_signal_beyond_max_gap(self):
        activity = MarketActivityState()
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        last = {"Ethereum": {"side": "Up", "ts": 0.0}}
        result = cross_asset_carryover_side(
            "Bitcoin", activity, up_book, down_book, last,
            now=bc.CROSS_ASSET_CARRYOVER_MAX_GAP_S,
            rng=type("AlwaysFire", (), {"random": staticmethod(lambda: 0.0)})())
        assert result is None

    def test_no_signal_outside_cheap_mid(self):
        # Suggested side (Up, from Ethereum) sits at 0.95 -- HIGH band,
        # confirmed-zero-effect regime -- must not fire even with an
        # always-fire rng.
        activity = MarketActivityState()
        up_book = make_liquid_book(price=0.95, token_id="up")
        down_book = make_liquid_book(price=0.05, token_id="down")
        last = {"Ethereum": {"side": "Up", "ts": 95.0}}
        result = cross_asset_carryover_side(
            "Bitcoin", activity, up_book, down_book, last, now=100.0,
            rng=type("AlwaysFire", (), {"random": staticmethod(lambda: 0.0)})())
        assert result is None

    def test_fires_with_the_leaders_side_in_mid(self):
        activity = MarketActivityState()
        up_book = make_liquid_book(price=0.40, token_id="up")  # MID
        down_book = make_liquid_book(price=0.60, token_id="down")
        last = {"Ethereum": {"side": "Up", "ts": 95.0}}
        result = cross_asset_carryover_side(
            "Bitcoin", activity, up_book, down_book, last, now=100.0,
            rng=type("AlwaysFire", (), {"random": staticmethod(lambda: 0.0)})())
        assert result == "Up"

    def test_does_not_fire_when_roll_exceeds_probability(self):
        activity = MarketActivityState()
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        last = {"Ethereum": {"side": "Up", "ts": 95.0}}
        result = cross_asset_carryover_side(
            "Bitcoin", activity, up_book, down_book, last, now=100.0,
            rng=type("NeverFire", (), {"random": staticmethod(lambda: 0.9999)})())
        assert result is None

    def test_picks_the_most_recent_other_asset(self):
        # Ethereum's decision is stale (well past the max gap); Solana's
        # is fresh -- must use Solana's side, not Ethereum's.
        activity = MarketActivityState()
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        last = {
            "Ethereum": {"side": "Down", "ts": -1000.0},
            "Solana": {"side": "Up", "ts": 95.0},
        }
        result = cross_asset_carryover_side(
            "Bitcoin", activity, up_book, down_book, last, now=100.0,
            rng=type("AlwaysFire", (), {"random": staticmethod(lambda: 0.0)})())
        assert result == "Up"

    def test_no_signal_for_future_timestamp(self):
        # Defensive: a leader timestamp after `now` (clock skew) must not
        # produce a negative gap that accidentally matches a bucket.
        activity = MarketActivityState()
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        last = {"Ethereum": {"side": "Up", "ts": 200.0}}
        result = cross_asset_carryover_side(
            "Bitcoin", activity, up_book, down_book, last, now=100.0,
            rng=type("AlwaysFire", (), {"random": staticmethod(lambda: 0.0)})())
        assert result is None


class TestBuildOrderIntentCrossAssetCarryover:
    """Integration tests: the cross-asset carryover override wired into
    build_order_intent's decision chain."""

    def test_carryover_override_picks_the_leaders_side(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        activity = MarketActivityState()
        last = {"Ethereum": {"side": "Up", "ts": 95.0}}

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(bc, "cross_asset_carryover_probability",
                             lambda leader, follower, gap: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0,
                                     last_first_entry_by_asset=last)
        assert intent is not None
        assert intent.side == "Up"
        assert intent.reason == "cross_asset_carryover"

    def test_mid_contrarian_keeps_priority(self, monkeypatch):
        # Mid-contrarian should fire FIRST when both could apply --
        # cross-asset carryover only kicks in when mid-contrarian didn't.
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        activity = MarketActivityState()
        activity.up_first_price = 0.60
        activity.down_first_price = 0.40
        last = {"Ethereum": {"side": "Down", "ts": 95.0}}  # would suggest the OPPOSITE side

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(bc, "mid_contrarian_bias_probability", lambda asset, magnitude: 1.0)
        monkeypatch.setattr(bc, "cross_asset_carryover_probability",
                             lambda leader, follower, gap: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0,
                                     last_first_entry_by_asset=last)
        assert intent is not None
        assert intent.reason == "mid_contrarian"
        assert intent.side == "Up"  # mid_contrarian's pick, not carryover's "Down"

    def test_does_not_override_a_hedge(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Down", notional_usd=100.0, regime="MID", price=0.60)
        last = {"Ethereum": {"side": "Up", "ts": 95.0}}

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Up")
        monkeypatch.setattr(bc, "cross_asset_carryover_probability",
                             lambda leader, follower, gap: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0,
                                     last_first_entry_by_asset=last)
        assert intent is not None
        assert intent.is_hedge is True

    def test_feature_flag_disables_the_override(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        activity = MarketActivityState()
        last = {"Ethereum": {"side": "Up", "ts": 95.0}}

        monkeypatch.setattr(config, "ENABLE_CROSS_ASSET_CARRYOVER", False)
        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Down")
        monkeypatch.setattr(bc, "cross_asset_carryover_probability",
                             lambda leader, follower, gap: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0,
                                     last_first_entry_by_asset=last)
        assert intent is not None
        assert intent.side == "Down"  # decide_side's answer, not the carryover override
        assert intent.reason != "cross_asset_carryover"

    def test_carryover_never_masked_by_scout(self, monkeypatch):
        # Regression: is_scout used to be checked before
        # is_cross_asset_carryover in the reason chain, so a carryover
        # pick that also rolled scout-eligible would silently report
        # reason="scout_entry", hiding that carryover chose the side --
        # exactly defeating the point of persisting `reason` for later
        # isolation. Force both SCOUT_PROBABILITY and the carryover roll
        # to "always fire" and confirm carryover still wins the reason.
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        activity = MarketActivityState()
        last = {"Ethereum": {"side": "Up", "ts": 95.0}}

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(bc, "cross_asset_carryover_probability",
                             lambda leader, follower, gap: 1.0)
        monkeypatch.setattr(bc, "scout_probability", lambda *a, **k: 1.0)

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0,
                                     last_first_entry_by_asset=last)
        assert intent is not None
        assert intent.side == "Up"
        assert intent.reason == "cross_asset_carryover"
        assert intent.is_scout is False

    def test_no_carryover_state_falls_through_unchanged(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.40, token_id="up")
        down_book = make_liquid_book(price=0.60, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Down")

        # last_first_entry_by_asset defaults to None -- every existing
        # call site (paperbot/paperbot-100, every prior test) must keep
        # working unchanged.
        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0)
        assert intent is not None
        assert intent.side == "Down"
        assert intent.reason != "cross_asset_carryover"


class TestHedgeNotionalForPrice:
    def test_none_for_uncalibrated_asset_or_bad_price(self):
        assert bc.hedge_notional_for_price("BNB", 0.5) is None
        assert bc.hedge_notional_for_price("Bitcoin", None) is None
        assert bc.hedge_notional_for_price("Bitcoin", 0.0) is None
        assert bc.hedge_notional_for_price("Bitcoin", 1.0) is None

    def test_matches_the_documented_formula(self):
        import math
        for asset, scale in bc.HEDGE_PRICE_SIZING_SCALE.items():
            for p in (0.05, 0.3, 0.5, 0.9, 0.97):
                expected = scale * (bc.shares_for_price(p) * p) ** bc.HEDGE_PRICE_SIZING_BETA
                assert bc.hedge_notional_for_price(asset, p) == pytest.approx(expected)

    def test_asset_ordering_and_price_growth_toward_the_extremes(self):
        assert (bc.hedge_notional_for_price("Bitcoin", 0.9) > bc.hedge_notional_for_price("Ethereum", 0.9)
                > bc.hedge_notional_for_price("Solana", 0.9))
        assert bc.hedge_notional_for_price("Bitcoin", 0.97) > bc.hedge_notional_for_price("Bitcoin", 0.5) \
            > bc.hedge_notional_for_price("Bitcoin", 0.05)


class TestEntrySizeLevelCorrection:
    """Keyed by (asset, regime) as of the 2026-09-22 redesign -- a flat
    per-tier factor was over/under-correcting because the real gap moves
    with regime, not tier (see the table's own docstring)."""

    def test_scaled_per_regime_when_enabled_for_every_covered_asset(self, monkeypatch):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            for regime in ("CHEAP", "MID", "CORE", "HIGH"):
                monkeypatch.setattr(config, "ENABLE_ENTRY_SIZE_LEVEL_CORRECTION", True)
                on, _ = decide_size(asset, regime, "first", 0.5, random.Random(3))
                monkeypatch.setattr(config, "ENABLE_ENTRY_SIZE_LEVEL_CORRECTION", False)
                off, _ = decide_size(asset, regime, "first", 0.5, random.Random(3))
                assert abs(on / off - bc.entry_size_level_correction(asset, regime)) < 1e-6

    def test_uniform_across_tiers_within_a_regime(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_ENTRY_SIZE_LEVEL_CORRECTION", True)
        seen = set()
        for tier in ("first", "2nd_3rd", "4th_plus"):
            on, _ = decide_size("Ethereum", "CORE", tier, 0.8, random.Random(7))
            monkeypatch.setattr(config, "ENABLE_ENTRY_SIZE_LEVEL_CORRECTION", False)
            off, _ = decide_size("Ethereum", "CORE", tier, 0.8, random.Random(7))
            monkeypatch.setattr(config, "ENABLE_ENTRY_SIZE_LEVEL_CORRECTION", True)
            seen.add(round(on / off, 6))
        assert len(seen) == 1  # same ratio regardless of tier

    def test_lookup_defaults_to_one(self):
        assert bc.entry_size_level_correction("Ethereum", "no_such_regime") == 1.0
        assert bc.entry_size_level_correction("Dogecoin", "CHEAP") == 1.0


class TestRecentSidePersistence:
    def _rate(self, asset, held_price, monkeypatch, enabled, n=6000):
        monkeypatch.setattr(config, "ENABLE_RECENT_SIDE_PERSISTENCE", enabled)
        act = stratmod.MarketActivityState(); act.last_side = "Up"
        rng = random.Random(42)
        return sum(stratmod.decide_side(asset, act, rng, held_side_price=held_price) == "Up" for _ in range(n)) / n

    def test_bitcoin_mid_persists_more_when_enabled(self, monkeypatch):
        on = self._rate("Bitcoin", 0.5, monkeypatch, True); off = self._rate("Bitcoin", 0.5, monkeypatch, False)
        assert abs(on - 0.913) < 0.02 and abs(off - bc.SIDE_PERSISTENCE["Bitcoin"]["MID"]) < 0.02 and on > off + 0.05

    def test_solana_untouched(self, monkeypatch):
        assert self._rate("Solana", 0.5, monkeypatch, True) == self._rate("Solana", 0.5, monkeypatch, False)

    def test_lookup_falls_back_to_default(self):
        assert bc.recent_side_persistence("Solana", "MID", 0.7239) == 0.7239
        assert bc.recent_side_persistence("Bitcoin", "HIGH", 0.5) == 0.951


class TestCheapDeepTouchSizeBoost:
    """decide_size's wiring of bc.cheap_deep_touch_edge_multiplier's EARLY
    (size-boost) half -- see TestCheapDeepTouchEdgeMultiplier in
    test_behavior_config.py for the function itself. Bitcoin has no
    floor-lot probability in CHEAP (left unmodeled), so a fixed seed's
    only remaining randomness is the sizing jitter, identical on both
    sides of the comparison -- the ratio isolates the multiplier exactly."""

    def test_boosts_size_in_the_early_zone(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", True)
        on, _ = decide_size("Bitcoin", "CHEAP", "first", 0.05, random.Random(1), seconds_remaining=300.0 - 90.0)
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", False)
        off, _ = decide_size("Bitcoin", "CHEAP", "first", 0.05, random.Random(1), seconds_remaining=300.0 - 90.0)
        assert abs(on / off - bc.CHEAP_EARLY_TOUCH_SIZE_MULTIPLIER) < 1e-6

    def test_no_boost_in_the_late_zone_size_stays_flat(self, monkeypatch):
        # the late zone is a SKIP dampener (build_order_intent), not a size change -- decide_size itself is a no-op there
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", True)
        on, _ = decide_size("Bitcoin", "CHEAP", "first", 0.05, random.Random(2), seconds_remaining=300.0 - 200.0)
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", False)
        off, _ = decide_size("Bitcoin", "CHEAP", "first", 0.05, random.Random(2), seconds_remaining=300.0 - 200.0)
        assert on == off

    def test_no_boost_above_price_threshold(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", True)
        on, _ = decide_size("Bitcoin", "CHEAP", "first", 0.20, random.Random(3), seconds_remaining=300.0 - 90.0)
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", False)
        off, _ = decide_size("Bitcoin", "CHEAP", "first", 0.20, random.Random(3), seconds_remaining=300.0 - 90.0)
        assert on == off

    def test_not_applied_outside_first_tier(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", True)
        on, _ = decide_size("Bitcoin", "CHEAP", "2nd_3rd", 0.05, random.Random(4), seconds_remaining=300.0 - 90.0)
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", False)
        off, _ = decide_size("Bitcoin", "CHEAP", "2nd_3rd", 0.05, random.Random(4), seconds_remaining=300.0 - 90.0)
        assert on == off

    def test_not_applied_outside_cheap_regime(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", True)
        on, _ = decide_size("Bitcoin", "MID", "first", 0.05, random.Random(5), seconds_remaining=300.0 - 90.0)
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", False)
        off, _ = decide_size("Bitcoin", "MID", "first", 0.05, random.Random(5), seconds_remaining=300.0 - 90.0)
        assert on == off


class TestCheapDeepTouchSkipDampenerWiring:
    """build_order_intent's wiring of bc.cheap_deep_touch_edge_multiplier's
    LATE (skip-dampener) half. window_start defaults to 0 (make_market's
    default slug), so seconds_into_window == now directly."""

    def test_skips_more_often_in_the_late_zone_at_a_deep_price(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", True)
        market = make_market(end_time=300.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.05, token_id="up")
        down_book = make_liquid_book(price=0.95, token_id="down")

        def n_placed(now):
            n = 0
            for seed in range(400):
                activity = MarketActivityState()
                intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=now)
                if intent is not None:
                    n += 1
            return n

        n_early = n_placed(now=90.0)   # seconds_into_window=90 -- early zone, no skip dampener
        n_late = n_placed(now=200.0)   # seconds_into_window=200 -- late zone, skip dampener fires
        assert n_late < n_early, "the late-zone skip dampener should reduce the entry rate at a deep price"

    def test_does_not_apply_above_the_price_threshold(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", True)
        market = make_market(end_time=300.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.80, token_id="down")

        def n_placed(now):
            n = 0
            for seed in range(400):
                activity = MarketActivityState()
                intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=now)
                if intent is not None:
                    n += 1
            return n

        assert n_placed(now=90.0) == n_placed(now=200.0)

    def test_respects_its_own_feature_flag(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_CHEAP_DEEP_TOUCH_EDGE", False)
        market = make_market(end_time=300.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.05, token_id="up")
        down_book = make_liquid_book(price=0.95, token_id="down")

        def n_placed(now):
            n = 0
            for seed in range(400):
                activity = MarketActivityState()
                intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=now)
                if intent is not None:
                    n += 1
            return n

        assert n_placed(now=90.0) == n_placed(now=200.0), "with the flag off, timing must not move the skip rate"


class TestParticipationAssetScaleWiring:
    """build_order_intent's wiring of bc.participation_asset_scale on top
    of the regime-engagement dampener -- see TestRegimeEngagementDampener
    above for that mechanism itself."""

    def test_raises_the_effective_engagement_rate_toward_one(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 0.3)
        monkeypatch.setattr(bc, "participation_asset_scale", lambda asset: 2.0)

        def n_placed(scale_on):
            monkeypatch.setattr(config, "ENABLE_PARTICIPATION_ASSET_SCALE", scale_on)
            n = 0
            for seed in range(400):
                activity = MarketActivityState()
                intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=100.0)
                if intent is not None:
                    n += 1
            return n

        assert n_placed(True) > n_placed(False)

    def test_never_pushes_the_effective_rate_above_one(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 0.9)
        monkeypatch.setattr(bc, "participation_asset_scale", lambda asset: 5.0)  # would overshoot 1.0 if not clamped
        monkeypatch.setattr(config, "ENABLE_PARTICIPATION_ASSET_SCALE", True)

        n = 0
        for seed in range(400):
            activity = MarketActivityState()
            intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=100.0)
            if intent is not None:
                n += 1
        assert n == 400  # never skips once the scaled multiplier clamps to 1.0

    def test_respects_its_own_feature_flag(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 0.3)
        monkeypatch.setattr(bc, "participation_asset_scale", lambda asset: 2.0)
        monkeypatch.setattr(config, "ENABLE_PARTICIPATION_ASSET_SCALE", False)

        def n_placed(seed_range):
            n = 0
            for seed in seed_range:
                activity = MarketActivityState()
                intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=100.0)
                if intent is not None:
                    n += 1
            return n

        with_flag_off = n_placed(range(400))
        monkeypatch.setattr(config, "ENABLE_PARTICIPATION_ASSET_SCALE", True)
        with_flag_on = n_placed(range(400))
        assert with_flag_on > with_flag_off, "sanity: the scale DOES do something when the flag is on"


class TestMidBoundarySizeBoost:
    """decide_size's wiring of bc.mid_boundary_edge_multiplier's low-MID
    (size-boost) half. Not scoped to position_tier (unlike the CHEAP
    deep-touch boost), matching what was actually measured."""

    def test_boosts_size_at_the_low_mid_boundary(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", True)
        on, _ = decide_size("Bitcoin", "MID", "first", 0.35, random.Random(1))
        monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", False)
        off, _ = decide_size("Bitcoin", "MID", "first", 0.35, random.Random(1))
        assert abs(on / off - bc.MID_LOW_BOUNDARY_SIZE_MULTIPLIER) < 1e-6

    def test_boost_applies_to_later_tiers_too(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", True)
        on, _ = decide_size("Bitcoin", "MID", "4th_plus", 0.35, random.Random(2))
        monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", False)
        off, _ = decide_size("Bitcoin", "MID", "4th_plus", 0.35, random.Random(2))
        assert abs(on / off - bc.MID_LOW_BOUNDARY_SIZE_MULTIPLIER) < 1e-6

    def test_no_boost_at_the_high_boundary_size_stays_flat(self, monkeypatch):
        # the high-boundary case is a SKIP dampener (build_order_intent), not a size change
        monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", True)
        on, _ = decide_size("Bitcoin", "MID", "first", 0.65, random.Random(3))
        monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", False)
        off, _ = decide_size("Bitcoin", "MID", "first", 0.65, random.Random(3))
        assert on == off

    def test_no_boost_outside_mid(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", True)
        on, _ = decide_size("Bitcoin", "CHEAP", "first", 0.20, random.Random(4))
        monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", False)
        off, _ = decide_size("Bitcoin", "CHEAP", "first", 0.20, random.Random(4))
        assert on == off


class TestMidBoundarySkipDampenerWiring:
    """build_order_intent's wiring of bc.mid_boundary_edge_multiplier's
    high-MID (skip-dampener) half."""

    def test_skips_more_often_at_the_high_mid_boundary(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", True)
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.65, token_id="up")   # high-MID boundary
        down_book = make_liquid_book(price=0.35, token_id="down")  # low-MID boundary

        def n_placed(price_up):
            book = make_liquid_book(price=price_up, token_id="up")
            n = 0
            for seed in range(400):
                activity = MarketActivityState()
                intent = build_order_intent(market, book, down_book, activity, random.Random(seed), now=100.0)
                if intent is not None:
                    n += 1
            return n

        assert n_placed(0.65) < n_placed(0.50)

    def test_does_not_apply_in_the_coin_flip_center(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", True)
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.50, token_id="up")
        down_book = make_liquid_book(price=0.50, token_id="down")

        def n_placed(flag):
            monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", flag)
            n = 0
            for seed in range(400):
                activity = MarketActivityState()
                intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=100.0)
                if intent is not None:
                    n += 1
            return n

        assert n_placed(True) == n_placed(False)

    def test_respects_its_own_feature_flag(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.65, token_id="up")
        down_book = make_liquid_book(price=0.35, token_id="down")

        def n_placed(flag):
            monkeypatch.setattr(config, "ENABLE_MID_BOUNDARY_EDGE", flag)
            n = 0
            for seed in range(400):
                activity = MarketActivityState()
                intent = build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=100.0)
                if intent is not None:
                    n += 1
            return n

        assert n_placed(False) > n_placed(True), "the dampener should reduce placements only when the flag is on"


class TestTickOffsetPlacement:
    """build_order_intent wiring for config.ENABLE_TICK_OFFSET_PLACEMENT,
    added 2026-09-24. Default OFF, and scoped to real first entries only
    (not hedges, not add-ons) -- see bc.TICK_OFFSET_DISTRIBUTION's docstring
    for the underlying MarketLens measurement this replicates."""

    def _wide_book(self, price=0.50, token_id="up"):
        # 4-tick-wide synthetic spread (0.01 default from make_liquid_book
        # is too narrow to test +2/+3 tick improvement without immediately
        # hitting the ask-clamp). Kept strictly under config.MAX_SPREAD=0.05
        # -- a full 0.05 spread trips the gate on floating-point rounding
        # (0.55-0.50 == 0.050000000000000044 > 0.05).
        book = BookState(token_id=token_id)
        book.apply_snapshot(bids=[(price, 100.0)], asks=[(round(price + 0.04, 6), 100.0)])
        return book

    def test_flag_off_leaves_price_at_bare_touch(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book, down_book = self._wide_book(0.50, "up"), self._wide_book(0.50, "down")
        monkeypatch.setattr(config, "ENABLE_TICK_OFFSET_PLACEMENT", False)
        monkeypatch.setattr(bc, "sample_tick_offset", lambda rng: 2)  # would improve if it ran

        intent = build_order_intent(market, up_book, down_book, MarketActivityState(), random.Random(0), now=100.0)
        assert intent is not None
        assert intent.price == pytest.approx(0.50)
        assert intent.offset == 0.0

    def test_flag_on_improves_price_by_the_sampled_tick_count(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book, down_book = self._wide_book(0.50, "up"), self._wide_book(0.50, "down")
        monkeypatch.setattr(config, "ENABLE_TICK_OFFSET_PLACEMENT", True)
        monkeypatch.setattr(bc, "sample_tick_offset", lambda rng: 2)

        intent = build_order_intent(market, up_book, down_book, MarketActivityState(), random.Random(0), now=100.0)
        assert intent is not None
        assert intent.price == pytest.approx(0.52)
        assert intent.offset == pytest.approx(-0.02)  # negative = improving, per the field's convention

    def test_flag_on_trails_behind_for_a_negative_sampled_tick_count(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book, down_book = self._wide_book(0.50, "up"), self._wide_book(0.50, "down")
        monkeypatch.setattr(config, "ENABLE_TICK_OFFSET_PLACEMENT", True)
        monkeypatch.setattr(bc, "sample_tick_offset", lambda rng: -3)

        intent = build_order_intent(market, up_book, down_book, MarketActivityState(), random.Random(0), now=100.0)
        assert intent is not None
        assert intent.price == pytest.approx(0.47)
        assert intent.offset == pytest.approx(0.03)  # positive = trails below, matching companion's convention

    def test_flag_on_zero_offset_is_a_true_noop(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book, down_book = self._wide_book(0.50, "up"), self._wide_book(0.50, "down")
        monkeypatch.setattr(config, "ENABLE_TICK_OFFSET_PLACEMENT", True)
        monkeypatch.setattr(bc, "sample_tick_offset", lambda rng: 0)

        intent = build_order_intent(market, up_book, down_book, MarketActivityState(), random.Random(0), now=100.0)
        assert intent is not None
        assert intent.price == pytest.approx(0.50)
        assert intent.offset == 0.0

    def test_never_crosses_the_spread_even_with_a_large_improving_sample(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book, down_book = self._wide_book(0.50, "up"), self._wide_book(0.50, "down")  # best_ask=0.54
        monkeypatch.setattr(config, "ENABLE_TICK_OFFSET_PLACEMENT", True)
        monkeypatch.setattr(bc, "sample_tick_offset", lambda rng: 20)  # would be 0.70, well past the ask

        intent = build_order_intent(market, up_book, down_book, MarketActivityState(), random.Random(0), now=100.0)
        assert intent is not None
        assert intent.price < 0.54  # never at-or-through the ask
        assert intent.price == pytest.approx(0.53)  # best_ask - one tick

    def test_never_applies_to_hedges(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book, down_book = self._wide_book(0.50, "up"), self._wide_book(0.50, "down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=5.0, regime="MID")  # establishes a held side
        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: "Down")  # force a hedge
        monkeypatch.setattr(config, "ENABLE_TICK_OFFSET_PLACEMENT", True)
        monkeypatch.setattr(bc, "sample_tick_offset", lambda rng: 3)  # would improve if it ran

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0)
        assert intent is not None
        assert intent.is_hedge is True
        assert intent.price == pytest.approx(0.50)  # bare touch -- offset never applied to hedges
        assert intent.offset == 0.0

    def test_never_applies_to_add_on_entries_only_first_entries(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book, down_book = self._wide_book(0.50, "up"), self._wide_book(0.50, "down")
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=5.0, regime="MID")  # real_fill_count now > 0
        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng, liquidity=None,
                             is_weekend=None, dominant_current_price=None, prev_hedge_rate=None,
                             after_big_loss=None, seconds_remaining=None, seconds_since_first_entry=None: None)
        monkeypatch.setattr(stratmod, "decide_side", lambda asset, activity, rng, held_side_price=None,
                             previous_market_first_side=None, previous_market_won=None: "Up")
        monkeypatch.setattr(config, "ENABLE_TICK_OFFSET_PLACEMENT", True)
        monkeypatch.setattr(bc, "sample_tick_offset", lambda rng: 3)  # would improve if it ran

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=100.0)
        assert intent is not None
        assert intent.price == pytest.approx(0.50)  # bare touch -- offset never applied past the first entry
        assert intent.offset == 0.0
