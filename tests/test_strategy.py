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

    def test_within_band_multiplier_is_a_noop_in_cheap_and_mid(self):
        # CHEAP/MID weren't calibrated for this -- price shouldn't move
        # the average size within those regimes via this mechanism.
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
                             lambda asset, activity, rng, held_side_price=None: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.side == "Down"
        assert intent.price == 0.04
        assert intent.regime == "CHEAP"  # not "HIGH", which up_book's 0.95 would wrongly give
        # sized off the CHEAP curve at Down's real price, not a HIGH notional / 0.04
        cheap_median = bc.median_entry_notional(market.asset, "CHEAP", "first")
        assert intent.notional_usd < cheap_median * 2  # generous jitter allowance

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

    def test_bitcoin_high_regime_cumulative_probability_matches_calibration(self):
        """CHANGED 2026-09-08: the calibrated 0.2967 for Bitcoin/HIGH is
        now a CUMULATIVE probability across the whole multi-attempt
        hazard window (hedge_attempt_hazard), not a single-shot rate at
        entry_count==1 -- simulate the real decide_hedge call pattern
        (one activity, entry_count advancing 1..10, stopping at the
        first trigger) across many independent markets and check the
        empirical hedge-at-all rate reproduces the calibrated target."""
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


class TestBuildOrderIntentHedge:
    def test_produces_a_hedge_intent_sized_off_dominant_cost(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="BNB")
        up_book = make_liquid_book(price=0.80, token_id="up")    # MID/CORE-ish primary
        down_book = make_liquid_book(price=0.15, token_id="down")  # cheap hedge side
        activity = MarketActivityState()
        activity.record_entry("Up", notional_usd=10.0, regime="CORE")

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng: "Down")

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

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None
        assert intent.is_hedge is True
        expected_ratio = bc.hedge_continuation_size_ratio(2)  # this is hedge #2
        assert expected_ratio != bc.hedge_size_ratio("BNB", "CORE")  # sanity: genuinely different curve
        dominant_cost = 10.0  # activity.cost_by_side["Up"], unaffected by the 1.0 hedge already placed
        expected_notional = dominant_cost * expected_ratio
        assert expected_notional * (1 - config.SIZING_JITTER_FRACTION) * 0.99 <= intent.notional_usd \
            <= expected_notional * (1 + config.SIZING_JITTER_FRACTION) * 1.01

    def test_no_hedge_falls_through_to_normal_flow(self, monkeypatch):
        market = make_market(end_time=1000.0, asset="Bitcoin")
        up_book = make_liquid_book(price=0.20, token_id="up")
        down_book = make_liquid_book(price=0.79, token_id="down")
        activity = MarketActivityState()

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng: None)

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

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng: "Down")

        intent = build_order_intent(market, up_book, down_book, activity, random.Random(0), now=0.0)

        assert intent is not None, "must not abandon the tick -- fall through to an ordinary entry"
        assert intent.is_hedge is False
        assert intent.side == "Down"
        assert intent.reason == "entry_curve"
        # The ordinary CORE/2nd_3rd curve, not the tiny hedge-ratio sizing.
        core_median = bc.median_entry_notional("Bitcoin", "CORE", "2nd_3rd")
        assert intent.notional_usd > core_median * 0.5
        assert intent.reason in ("entry_curve", "floor_lot")


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

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng: None)
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

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng: None)
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

        monkeypatch.setattr(stratmod, "decide_hedge", lambda asset, activity, rng: None)
        monkeypatch.setattr(bc, "scout_probability", lambda asset: 1.0)  # would always fire if reachable

        intent = build_order_intent(market, up_book, down_book, activity, rng, now=0.0)
        assert intent is not None
        assert intent.is_floor_lot is True
        assert intent.is_scout is False

    def test_scout_probability_and_ratio_default_to_no_op_for_uncalibrated_assets(self):
        assert bc.scout_probability("XRP") == 0.0
        assert bc.scout_size_ratio("XRP") == bc._DEFAULT_SCOUT_SIZE_RATIO
