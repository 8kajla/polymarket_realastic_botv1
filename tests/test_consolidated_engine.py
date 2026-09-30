"""Tests for the consolidated decision engine (paperbot/consolidated_engine.py),
2026-09-24 -- the "one function per decision type" bot built after the
full non-hedge validation pass. Focused on: (1) each of the four
decision functions behaves sanely on its own, (2) the orchestrator
produces valid OrderIntents end-to-end across both the first-entry and
hedge paths, (3) the three confirmed-dead multipliers (cross-market size
momentum, bankroll-linked sizing, accuracy-conditioned scouting) are
genuinely not called here."""
import random

import pytest

from paperbot import behavior_config as bc
from paperbot import consolidated_engine as ce
from paperbot.book import BookState
from paperbot.market_discovery import Market
from paperbot.strategy import MarketActivityState


def make_market(end_time, asset="Bitcoin", condition_id="c1", order_min_size=None, slug=None):
    return Market(
        condition_id=condition_id, slug=slug if slug is not None else "btc-updown-5m-0",
        question="q", asset=asset, end_time=end_time, token_id_up="up", token_id_down="down",
        order_min_size=order_min_size, order_min_tick_size=0.01,
    )


def make_liquid_book(price=0.20, token_id="up"):
    book = BookState(token_id=token_id)
    book.apply_snapshot(bids=[(price, 100.0)], asks=[(round(price + 0.01, 6), 100.0)])
    return book


def record_filled_entry(activity, side, notional_usd=0.0, regime=None, is_hedge=False, price=None):
    activity.record_entry(side, notional_usd=notional_usd, regime=regime, is_hedge=is_hedge, price=price)
    activity.record_real_fill(is_hedge=is_hedge)


class TestDecideEntrySide:
    def test_first_entry_falls_back_to_a_coinflip_with_no_priors(self):
        rng = random.Random(1)
        activity = MarketActivityState()
        up_book = make_liquid_book(0.50, "up")
        down_book = make_liquid_book(0.50, "down")
        side, tag = ce.decide_entry_side("Bitcoin", activity, up_book, down_book, rng)
        assert side in ("Up", "Down")
        assert tag == "persistence"

    def test_cross_market_persistence_fires_when_a_prior_side_is_known(self):
        rng = random.Random(1)
        activity = MarketActivityState()
        up_book = make_liquid_book(0.50, "up")
        down_book = make_liquid_book(0.50, "down")
        side, tag = ce.decide_entry_side(
            "Bitcoin", activity, up_book, down_book, rng,
            previous_market_first_side="Up", previous_market_won=True)
        assert side in ("Up", "Down")
        assert tag == "persistence"

    def test_established_side_uses_held_side_persistence(self):
        rng = random.Random(7)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=10.0, regime="CHEAP", price=0.20)
        up_book = make_liquid_book(0.20, "up")
        down_book = make_liquid_book(0.80, "down")
        side, tag = ce.decide_entry_side("Bitcoin", activity, up_book, down_book, rng)
        assert side in ("Up", "Down")
        assert tag == "persistence"

    def test_streak_fade_fires_and_picks_the_opposite_side(self, monkeypatch):
        rng = type("AlwaysFire", (), {"random": staticmethod(lambda: 0.0)})()
        activity = MarketActivityState()
        up_book = make_liquid_book(0.50, "up")
        down_book = make_liquid_book(0.50, "down")
        monkeypatch.setattr(bc, "streak_fade_probability", lambda length, elapsed=None: 1.0)
        side, tag = ce.decide_entry_side(
            "Bitcoin", activity, up_book, down_book, rng,
            streak_side="Up", streak_length=3, seconds_since_window_open=60.0)
        assert side == "Down"
        assert tag == "streak_fade"

    def test_no_streak_falls_through_to_persistence(self):
        rng = random.Random(1)
        activity = MarketActivityState()
        up_book = make_liquid_book(0.50, "up")
        down_book = make_liquid_book(0.50, "down")
        # streak_side/streak_length default to None/0 -- must behave
        # identically to a caller that doesn't know about streaks at all.
        side, tag = ce.decide_entry_side("Bitcoin", activity, up_book, down_book, rng)
        assert side in ("Up", "Down")
        assert tag == "persistence"

    def test_contrarian_keeps_priority_over_streak_fade(self, monkeypatch):
        rng = type("AlwaysFire", (), {"random": staticmethod(lambda: 0.0)})()
        activity = MarketActivityState()
        activity.up_first_price = 0.60
        activity.down_first_price = 0.40
        up_book = make_liquid_book(0.40, "up")   # MID, the "dip"
        down_book = make_liquid_book(0.60, "down")
        monkeypatch.setattr(bc, "mid_contrarian_bias_probability", lambda asset, magnitude: 1.0)
        monkeypatch.setattr(bc, "streak_fade_probability", lambda length, elapsed=None: 1.0)
        side, tag = ce.decide_entry_side(
            "Bitcoin", activity, up_book, down_book, rng,
            streak_side="Up", streak_length=3, seconds_since_window_open=60.0)
        assert tag == "contrarian"
        assert side == "Up"  # contrarian's pick, not streak-fade's "Down"


class TestEntryEligible:
    def test_hedge_leg_skips_every_ordinary_dampener(self):
        rng = random.Random(1)
        market = make_market(end_time=2000.0)
        activity = MarketActivityState()
        book = make_liquid_book(0.03, "down")  # deepest CHEAP tail, would normally risk a dampener
        eligible, reason = ce.entry_eligible(
            market, activity, "Down", book, 0.03, "CHEAP", now=1000.0, seconds_remaining=250.0,
            is_hedge=True, is_first_entry=True, regime_engagement_state=None, rng=rng)
        assert eligible is True
        assert reason == ""

    def test_fails_closed_when_the_book_is_illiquid(self):
        rng = random.Random(1)
        market = make_market(end_time=2000.0)
        activity = MarketActivityState()
        book = BookState(token_id="up")  # no bids/asks at all
        eligible, reason = ce.entry_eligible(
            market, activity, "Up", book, 0.50, "MID", now=1000.0, seconds_remaining=250.0,
            is_hedge=False, is_first_entry=True, regime_engagement_state=None, rng=rng)
        assert eligible is False
        assert reason != ""

    def test_fails_closed_outside_the_market_timing_window(self):
        rng = random.Random(1)
        market = make_market(end_time=1000.0)  # already closed relative to now below
        activity = MarketActivityState()
        book = make_liquid_book(0.50, "up")
        eligible, reason = ce.entry_eligible(
            market, activity, "Up", book, 0.50, "MID", now=999.5, seconds_remaining=0.5,
            is_hedge=False, is_first_entry=True, regime_engagement_state=None, rng=rng)
        assert eligible is False
        assert reason == "timing"

    def test_close_urgency_boost_can_only_recover_toward_full_eligibility(self, monkeypatch):
        """CLOSE-URGENCY boost, added 2026-09-25 -- entry_urgency_multiplier
        can only push the regime-engagement roll TOWARD certain eligibility
        (never past it, since it's a skip-probability gate, not a size
        multiplier -- see bc.entry_urgency_multiplier's own docstring).
        Force a suppressed base multiplier and an always-fail RNG roll:
        far from close it must skip, at the true close-urgency floor
        (75s) the boost-only table caps out at ENTRY_URGENCY_MULTIPLIER[75]
        (recalibrated 2026-09-26, see its docstring) -- not enough to
        fully recover a base of 0.3 -- so use a base that DOES fully
        recover at 75s to prove the boost is real."""
        market = make_market(end_time=2000.0)
        book = make_liquid_book(0.50, "up")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 0.7)
        monkeypatch.setattr(bc, "participation_asset_scale", lambda asset: 1.0)  # isolate urgency only
        rng_roll = 0.9  # only fails the gate when engagement_mult < this

        class FixedRng:
            def random(self):
                return rng_roll

        # Separate activity instances -- the regime-engagement roll latches
        # per (side, regime) episode (rolled once, not every tick), so
        # reusing one activity across both calls would let the far-case's
        # rejection block the near-case check regardless of urgency.
        far_eligible, _ = ce.entry_eligible(
            market, MarketActivityState(), "Up", book, 0.50, "MID", now=1000.0, seconds_remaining=200.0,
            is_hedge=False, is_first_entry=True, regime_engagement_state=None, rng=FixedRng())
        near_eligible, _ = ce.entry_eligible(
            market, MarketActivityState(), "Up", book, 0.50, "MID", now=1925.0, seconds_remaining=75.0,
            is_hedge=False, is_first_entry=True, regime_engagement_state=None, rng=FixedRng())
        assert far_eligible is False  # 0.7 < 0.9 roll -> skipped, no urgency boost this far out
        assert near_eligible is True  # 0.7 * ENTRY_URGENCY_MULTIPLIER[75] > 1.0 -> clamped -> never skipped

    def test_cheap_deep_tail_boost_can_recover_a_suppressed_deep_entry(self, monkeypatch):
        """CHEAP deep-tail participation boost, added 2026-09-28 -- see
        bc.cheap_deep_tail_participation_multiplier's docstring. No flag
        in this module, consistent with its own design."""
        market = make_market(end_time=2000.0)
        deep_book = make_liquid_book(0.03, "up")
        shallow_book = make_liquid_book(0.20, "up")
        monkeypatch.setattr(bc, "regime_engagement_multiplier", lambda asset, regime: 0.5)
        monkeypatch.setattr(bc, "cheap_deep_tail_participation_multiplier",
                             lambda asset, price: 3.0 if price < 0.08 else 1.0)
        rng_roll = 0.9  # fails a bare 0.5 roll, passes once boosted toward 1.0

        class FixedRng:
            def random(self):
                return rng_roll

        deep_eligible, _ = ce.entry_eligible(
            market, MarketActivityState(), "Up", deep_book, 0.03, "CHEAP", now=1000.0, seconds_remaining=200.0,
            is_hedge=False, is_first_entry=True, regime_engagement_state=None, rng=FixedRng())
        shallow_eligible, _ = ce.entry_eligible(
            market, MarketActivityState(), "Up", shallow_book, 0.20, "CHEAP", now=1000.0, seconds_remaining=200.0,
            is_hedge=False, is_first_entry=True, regime_engagement_state=None, rng=FixedRng())
        assert deep_eligible is True  # 0.5 * 3.0 -> clamped to 1.0 -> never skipped
        assert shallow_eligible is False  # 0.5 * 1.0 = 0.5 < 0.9 roll -> skipped, untouched

    def test_addon_continuation_dampener_can_skip_an_ordinary_addon(self, monkeypatch):
        rng = random.Random(1)
        market = make_market(end_time=2000.0)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=5.0, regime="CHEAP", price=0.20)
        book = make_liquid_book(0.20, "up")
        monkeypatch.setattr(bc, "addon_continuation_probability", lambda asset, regime: 0.0)

        eligible, reason = ce.entry_eligible(
            market, activity, "Up", book, 0.20, "CHEAP", now=1000.0, seconds_remaining=250.0,
            is_hedge=False, is_first_entry=False, regime_engagement_state=None, rng=rng)
        assert eligible is False
        assert reason == "addon_continuation"
        assert activity.is_episode_rejected("addon", "Up", 0) is True

    def test_addon_continuation_dampener_does_not_apply_to_the_first_entry(self, monkeypatch):
        rng = random.Random(1)
        market = make_market(end_time=2000.0)
        activity = MarketActivityState()
        book = make_liquid_book(0.20, "up")
        monkeypatch.setattr(bc, "addon_continuation_probability", lambda asset, regime: 0.0)

        eligible, reason = ce.entry_eligible(
            market, activity, "Up", book, 0.20, "CHEAP", now=1000.0, seconds_remaining=250.0,
            is_hedge=False, is_first_entry=True, regime_engagement_state=None, rng=rng)
        assert eligible is True

    def test_addon_continuation_dampener_does_not_apply_to_a_hedge(self, monkeypatch):
        rng = random.Random(1)
        market = make_market(end_time=2000.0)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=5.0, regime="CHEAP", price=0.20)
        book = make_liquid_book(0.80, "down")
        monkeypatch.setattr(bc, "addon_continuation_probability", lambda asset, regime: 0.0)

        eligible, reason = ce.entry_eligible(
            market, activity, "Down", book, 0.80, "HIGH", now=1000.0, seconds_remaining=250.0,
            is_hedge=True, is_first_entry=False, regime_engagement_state=None, rng=rng)
        assert eligible is True

    def test_addon_continuation_latch_blocks_a_second_identical_call(self, monkeypatch):
        market = make_market(end_time=2000.0)
        activity = MarketActivityState()
        record_filled_entry(activity, "Up", notional_usd=5.0, regime="CHEAP", price=0.20)
        book = make_liquid_book(0.20, "up")
        monkeypatch.setattr(bc, "addon_continuation_probability", lambda asset, regime: 0.3)

        class AlwaysFailRng:
            def random(self):
                return 0.9

        class AlwaysPassRng:
            def random(self):
                return 0.01

        eligible1, _ = ce.entry_eligible(
            market, activity, "Up", book, 0.20, "CHEAP", now=1000.0, seconds_remaining=250.0,
            is_hedge=False, is_first_entry=False, regime_engagement_state=None, rng=AlwaysFailRng())
        assert eligible1 is False

        # A second call with an RNG that WOULD pass the roll must still be
        # blocked -- the episode is latched, not re-rolled.
        eligible2, reason2 = ce.entry_eligible(
            market, activity, "Up", book, 0.20, "CHEAP", now=1000.0, seconds_remaining=250.0,
            is_hedge=False, is_first_entry=False, regime_engagement_state=None, rng=AlwaysPassRng())
        assert eligible2 is False
        assert reason2 == "addon_continuation_episode_already_rejected"


class TestDecideSize:
    def test_returns_a_positive_notional_for_an_ordinary_first_entry(self):
        rng = random.Random(3)
        notional, is_floor_lot = ce.decide_size(
            "Bitcoin", "MID", "first", 0.55, rng, seconds_remaining=200.0)
        assert notional > 0

    def test_hour_boost_increases_first_entry_notional(self):
        rng_a = random.Random(3)
        rng_b = random.Random(3)
        notional_noboost, _ = ce.decide_size(
            "Bitcoin", "CORE", "first", 0.75, rng_a, hour_of_day=None)
        notional_boosted, _ = ce.decide_size(
            "Bitcoin", "CORE", "first", 0.75, rng_b, hour_of_day=6)
        assert notional_boosted == pytest.approx(notional_noboost * bc.HOUR_SIZE_BOOST_MULTIPLIER)

    def test_hour_boost_scoped_to_first_entries_only(self):
        rng_a = random.Random(3)
        rng_b = random.Random(3)
        notional_noboost, _ = ce.decide_size(
            "Bitcoin", "CORE", "2nd_3rd", 0.75, rng_a, hour_of_day=None)
        notional_hour6, _ = ce.decide_size(
            "Bitcoin", "CORE", "2nd_3rd", 0.75, rng_b, hour_of_day=6)
        assert notional_hour6 == notional_noboost

    def test_decide_size_and_build_order_intent_smoke_test(self):
        # ACCURACY_SCOUT_MULTIPLIER / BANKROLL_PNL_SIZE_MULTIPLIER /
        # CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER were confirmed-dead empty
        # tables as of the 2026-09-24 validation pass, and the
        # consolidated engine never called the functions that read them
        # (unlike the old decide_size chain, which called them behind
        # now-permanently-true feature flags for zero effect) -- moot as
        # of 2026-09-26, when all three functions were removed from the
        # codebase entirely. Kept as a general smoke test across the
        # scout/non-scout first-entry branches.
        rng = random.Random(3)
        notional, _ = ce.decide_size("Bitcoin", "MID", "first", 0.55, rng, seconds_remaining=200.0)
        assert notional > 0

        market = make_market(end_time=2000.0)
        activity = MarketActivityState()
        up_book = make_liquid_book(0.55, "up")
        down_book = make_liquid_book(0.45, "down")
        # Exercise the full orchestrator too, across enough seeds to hit
        # both the scout and non-scout first-entry branches.
        for seed in range(50):
            ce.build_order_intent(market, up_book, down_book, activity, random.Random(seed), now=1000.0)




class TestBuildOrderIntentEndToEnd:
    def test_produces_a_valid_intent_on_some_seed(self):
        market = make_market(end_time=1250.0, order_min_size=5.0)
        up_book = make_liquid_book(0.62, "up")
        down_book = make_liquid_book(0.37, "down")
        found = False
        for seed in range(200):
            activity = MarketActivityState()
            intent = ce.build_order_intent(market, up_book, down_book, activity,
                                            random.Random(seed), now=1000.0)
            if intent is not None:
                found = True
                assert intent.side in ("Up", "Down")
                assert intent.size_shares > 0
                assert intent.notional_usd > 0
                assert intent.regime in ("CHEAP", "MID", "CORE", "HIGH")
                break
        assert found, "expected at least one non-skipped first entry across 200 seeds"

    def test_a_second_entry_can_become_a_hedge(self):
        market = make_market(end_time=1250.0, order_min_size=5.0)
        up_book = make_liquid_book(0.62, "up")
        down_book = make_liquid_book(0.37, "down")
        found_hedge = False
        for seed in range(500):
            rng = random.Random(seed)
            activity = MarketActivityState()
            first = ce.build_order_intent(market, up_book, down_book, activity, rng, now=1000.0)
            if first is None:
                continue
            record_filled_entry(activity, first.side, notional_usd=first.notional_usd,
                                 regime=first.regime, is_hedge=first.is_hedge, price=first.price)
            for i in range(40):
                nxt = ce.build_order_intent(market, up_book, down_book, activity, rng,
                                             now=1000.0 + 5 * (i + 1))
                if nxt is not None and nxt.is_hedge:
                    found_hedge = True
                    assert nxt.side != first.side
                    assert nxt.size_shares > 0
                    break
            if found_hedge:
                break
        assert found_hedge, "expected at least one hedge leg across 500 seeds x 40 ticks"

    def test_returns_none_outside_the_timing_window(self):
        market = make_market(end_time=1000.0)
        activity = MarketActivityState()
        up_book = make_liquid_book(0.50, "up")
        down_book = make_liquid_book(0.50, "down")
        assert ce.build_order_intent(market, up_book, down_book, activity,
                                      random.Random(1), now=999.9) is None

    def test_absorbs_unused_keyword_arguments_from_the_shared_call_site(self):
        # bot.py's _evaluate_one_market passes several kwargs only the
        # OTHER (non-consolidated) bots' decision path uses -- this
        # engine must accept and ignore them, not raise.
        market = make_market(end_time=1250.0, order_min_size=5.0)
        activity = MarketActivityState()
        up_book = make_liquid_book(0.62, "up")
        down_book = make_liquid_book(0.37, "down")
        ce.build_order_intent(
            market, up_book, down_book, activity, random.Random(1), now=1000.0,
            prev_hedge_rate=0.3, size_momentum_residual=1.0, bankroll_pnl_residual=5.0,
            after_big_loss=False, forced_first_entry_side="Up", rolling_accuracy=0.6)
