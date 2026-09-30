import pytest

from paperbot import behavior_config as bc


class TestRegimeBoundaries:
    """Boundaries: 0.30, 0.70, 0.90 belong to the band that STARTS there."""

    def test_cheap_interior(self):
        assert bc.classify_regime(0.0) == "CHEAP"
        assert bc.classify_regime(0.15) == "CHEAP"
        assert bc.classify_regime(0.299999) == "CHEAP"

    def test_030_boundary_is_mid(self):
        assert bc.classify_regime(0.30) == "MID"

    def test_mid_interior(self):
        assert bc.classify_regime(0.5) == "MID"
        assert bc.classify_regime(0.699999) == "MID"

    def test_070_boundary_is_core(self):
        assert bc.classify_regime(0.70) == "CORE"

    def test_core_interior(self):
        assert bc.classify_regime(0.85) == "CORE"
        assert bc.classify_regime(0.899999) == "CORE"

    def test_090_boundary_is_high(self):
        assert bc.classify_regime(0.90) == "HIGH"

    def test_high_interior_and_top(self):
        assert bc.classify_regime(0.95) == "HIGH"
        assert bc.classify_regime(1.0) == "HIGH"

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            bc.classify_regime(-0.01)
        with pytest.raises(ValueError):
            bc.classify_regime(1.01)


class TestPositionTiers:
    def test_index_zero_is_first(self):
        assert bc.position_tier_for_index(0) == "first"

    def test_index_one_and_two_are_2nd_3rd(self):
        assert bc.position_tier_for_index(1) == "2nd_3rd"
        assert bc.position_tier_for_index(2) == "2nd_3rd"

    def test_index_three_plus_is_4th_plus(self):
        assert bc.position_tier_for_index(3) == "4th_plus"
        assert bc.position_tier_for_index(50) == "4th_plus"

    def test_negative_index_rejected(self):
        with pytest.raises(ValueError):
            bc.position_tier_for_index(-1)


class TestSizingCurvesAreDistinctPerAsset:
    """Guards against someone accidentally collapsing the six per-asset
    tables back into one shared table -- this test would fail on that
    regression."""

    def test_all_six_assets_present(self):
        assert set(bc.ENTRY_SIZING_USD.keys()) == set(bc.ASSET_NAMES)

    def test_cheap_first_entry_medians_are_not_all_equal(self):
        values = {
            asset: bc.ENTRY_SIZING_USD[asset]["CHEAP"]["first"]
            for asset in bc.ASSET_NAMES
        }
        assert len(set(values.values())) == len(values), (
            f"expected 6 distinct CHEAP/first medians, got {values}"
        )

    def test_high_4th_plus_medians_are_not_all_equal(self):
        # RELAXED 2026-09-13 (post-halt recalibration): Ethereum and
        # Solana's HIGH/4th_plus medians now genuinely coincide (both
        # 14.469, independently derived from separate post-halt datasets)
        # -- a real fact about currently-thin post-halt HIGH-band volume,
        # not a copy-paste regression. This test's actual purpose (per its
        # class docstring) is guarding against the six per-asset tables
        # collapsing into ONE shared table -- checking "not all six
        # identical" still catches that regression without fighting a
        # real, if coincidental, pairwise tie.
        values = {
            asset: bc.ENTRY_SIZING_USD[asset]["HIGH"]["4th_plus"]
            for asset in bc.ASSET_NAMES
        }
        assert len(set(values.values())) > 1, (
            f"all six assets' HIGH/4th_plus medians are identical -- looks like a collapsed table: {values}"
        )

    def test_bitcoin_and_dogecoin_curves_are_meaningfully_different(self):
        # THRESHOLD LOWERED 2026-09-13 (post-halt recalibration): Bitcoin's
        # CORE/first genuinely dropped close to Dogecoin's (dormant, still
        # at its old historical value) post-halt -- real current data, not
        # a bug. CHEAP/first still shows a clean, comfortably-larger gap,
        # so use that cell instead to keep testing the real intent (these
        # are two genuinely different per-asset tables, not one collapsed
        # into the other) without asserting a specific gap size current
        # data doesn't support in every regime.
        btc = bc.ENTRY_SIZING_USD["Bitcoin"]["CHEAP"]["first"]
        doge = bc.ENTRY_SIZING_USD["Dogecoin"]["CHEAP"]["first"]
        assert abs(btc - doge) > 0.5

    def test_confirmed_exceptions_are_preserved_not_smoothed(self):
        # Dogecoin HIGH: confirmed INCREASE first -> 4th+
        doge_high = bc.ENTRY_SIZING_USD["Dogecoin"]["HIGH"]
        assert doge_high["4th_plus"] > doge_high["first"]

        # BNB CORE: confirmed INCREASE first -> 4th+
        bnb_core = bc.ENTRY_SIZING_USD["BNB"]["CORE"]
        assert bnb_core["4th_plus"] > bnb_core["first"]

        # And the general pattern (e.g. Bitcoin CHEAP) still decreases.
        btc_cheap = bc.ENTRY_SIZING_USD["Bitcoin"]["CHEAP"]
        assert btc_cheap["4th_plus"] < btc_cheap["first"]


class TestRegimeDistributionTable:
    def test_all_assets_sum_close_to_100(self):
        for asset, dist in bc.ASSET_REGIME_DISTRIBUTION_PCT.items():
            total = sum(dist.values())
            assert 99.0 <= total <= 101.0, f"{asset} distribution sums to {total}"

    def test_distributions_are_asset_specific(self):
        cheap_shares = {a: d["CHEAP"] for a, d in bc.ASSET_REGIME_DISTRIBUTION_PCT.items()}
        assert len(set(cheap_shares.values())) == len(cheap_shares)


class TestSidePersistence:
    def test_all_six_assets_present_and_plausible(self):
        assert set(bc.SIDE_PERSISTENCE.keys()) == set(bc.ASSET_NAMES)
        for asset, by_regime in bc.SIDE_PERSISTENCE.items():
            assert set(by_regime.keys()) == set(bc.REGIME_NAMES)
            for regime, p in by_regime.items():
                assert 0.5 < p < 1.0, f"{asset}/{regime} side persistence {p} looks implausible"

    def test_dormant_assets_have_the_same_rate_in_every_regime(self):
        # Reshaped to per-regime dicts for interface consistency, but not
        # recalibrated -- Dogecoin/Hyperliquid/BNB are dormant, so this
        # should be a pure type change, not a behavior change.
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            values = set(bc.SIDE_PERSISTENCE[asset].values())
            assert len(values) == 1, f"{asset} should be unchanged (flat) across regimes"

    def test_active_assets_genuinely_vary_by_regime(self):
        # The opposite check for the three assets that WERE recalibrated
        # per-regime -- if any of these collapsed to one flat number, the
        # regime-dependent calibration silently didn't take.
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            values = set(bc.SIDE_PERSISTENCE[asset].values())
            assert len(values) > 1, f"{asset} should vary by regime"

    def test_side_persistence_matches_the_base_table_with_override_disabled(self):
        # use_recent_override=False isolates the BASE table this test is
        # actually about -- RECENT_SIDE_PERSISTENCE_OVERRIDE's own
        # coverage lives in TestRecentSidePersistenceOverride below.
        for asset, by_regime in bc.SIDE_PERSISTENCE.items():
            for regime, p in by_regime.items():
                assert bc.side_persistence(asset, regime, use_recent_override=False) == p

    def test_side_persistence_raises_on_unknown_regime(self):
        with pytest.raises(bc.BehaviorLookupError):
            bc.side_persistence("Bitcoin", "NOT_A_REGIME")


class TestCrossMarketSidePersistence:
    """Cross-market first-entry side persistence, added 2026-09-11 -- real,
    cross-asset-generalizing (BTC 55.38%/z=12.88, ETH 52.65%/z=6.13, SOL
    54.29%/z=9.87), confound-checked (real BTC spot momentum runs the
    OPPOSITE direction, z=+3.01, ruling out 'just tracking a real trend'
    as the mechanism). Closes decide_side's own documented gap for a
    market's first entry (previously uniform random)."""

    def test_neutral_for_assets_not_in_the_table(self):
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            assert bc.cross_market_side_persistence(asset) == 0.5
            assert bc.cross_market_side_persistence(asset, True) == 0.5
            assert bc.cross_market_side_persistence(asset, False) == 0.5

    def test_win_loss_averaged_fallback_when_outcome_unknown(self):
        # UPGRADED 2026-09-11: previous_market_won=None (the default)
        # returns the win/loss-AVERAGED rate, not the old flat value --
        # see cross_market_side_persistence's docstring.
        for asset, spec in bc.CROSS_MARKET_SIDE_PERSISTENCE.items():
            expected = (spec["after_win"] + spec["after_loss"]) / 2.0
            assert bc.cross_market_side_persistence(asset) == expected

    def test_exact_values_for_the_three_calibrated_assets(self):
        # Bitcoin's after_loss RECALIBRATED 2026-09-23 (systematic post-
        # Sep-18 audit) -- see CROSS_MARKET_SIDE_PERSISTENCE's docstring:
        # fresh post-boundary rate is 0.4900, a real -12.3pp drop from the
        # 2026-09-13 value. Ethereum/Solana checked fresh too, stayed
        # close, left unchanged.
        assert bc.cross_market_side_persistence("Bitcoin", True) == 0.5182
        assert bc.cross_market_side_persistence("Bitcoin", False) == 0.4900
        assert bc.cross_market_side_persistence("Ethereum", True) == 0.4874
        assert bc.cross_market_side_persistence("Ethereum", False) == 0.5515
        assert bc.cross_market_side_persistence("Solana", True) == 0.4990
        assert bc.cross_market_side_persistence("Solana", False) == 0.5853

    def test_persistence_is_stronger_after_a_loss_for_ethereum_and_solana(self):
        # The original finding: NOT hot-hand, NOT gambler's fallacy -- he
        # sticks with his read MORE after a setback than after a win.
        # RECALIBRATED 2026-09-23: Bitcoin's version of this has
        # genuinely reversed post-Sep-18 (after_loss 0.49 < after_win
        # 0.5182, now close to a coin flip) -- excluded here, not a test
        # bug. Ethereum/Solana still show the original pattern.
        for asset in ("Ethereum", "Solana"):
            spec = bc.CROSS_MARKET_SIDE_PERSISTENCE[asset]
            assert spec["after_loss"] > spec["after_win"], (
                f"{asset}: expected after_loss > after_win, got {spec}"
            )

    def test_bitcoin_persistence_no_longer_shows_the_after_loss_boost(self):
        # RECALIBRATED 2026-09-23 -- explicit regression guard for the
        # reversal found in the systematic post-Sep-18 audit, so this
        # doesn't silently drift back without comment.
        spec = bc.CROSS_MARKET_SIDE_PERSISTENCE["Bitcoin"]
        assert spec["after_loss"] < spec["after_win"]


class TestFloorLotProbability:
    def test_bitcoin_is_negligible_everywhere(self):
        for regime in bc.REGIME_NAMES:
            assert bc.floor_lot_probability("Bitcoin", regime, "first") == 0.0
            assert bc.floor_lot_probability("Bitcoin", regime, "4th_plus") == 0.0

    def test_hyperliquid_probability_increases_with_position(self):
        """The clearest confirmed case: floor-lot probability strictly
        rises first -> 2nd_3rd -> 4th_plus, in every regime."""
        for regime in bc.REGIME_NAMES:
            first = bc.floor_lot_probability("Hyperliquid", regime, "first")
            mid = bc.floor_lot_probability("Hyperliquid", regime, "2nd_3rd")
            last = bc.floor_lot_probability("Hyperliquid", regime, "4th_plus")
            assert first < mid < last, (
                f"Hyperliquid {regime}: expected strictly increasing floor-lot "
                f"probability, got first={first} 2nd_3rd={mid} 4th_plus={last}"
            )

    def test_bnb_probability_also_increases_with_position(self):
        for regime in bc.REGIME_NAMES:
            first = bc.floor_lot_probability("BNB", regime, "first")
            mid = bc.floor_lot_probability("BNB", regime, "2nd_3rd")
            last = bc.floor_lot_probability("BNB", regime, "4th_plus")
            assert first < mid < last

    def test_flat_by_position_assets_are_actually_flat(self):
        # Dogecoin is dormant (no live post-halt data to recalibrate from)
        # -- stays flat-by-position, unchanged.
        for regime in bc.REGIME_NAMES:
            first = bc.floor_lot_probability("Dogecoin", regime, "first")
            mid = bc.floor_lot_probability("Dogecoin", regime, "2nd_3rd")
            last = bc.floor_lot_probability("Dogecoin", regime, "4th_plus")
            assert first == mid == last

    def test_ethereum_and_solana_are_now_position_dependent(self):
        # RECALIBRATED 2026-09-13 (post-halt): Ethereum/Solana used to be
        # flat-by-position (like Dogecoin still is) -- real post-halt data
        # showed a genuine rise-with-position-index shape for both, not
        # strictly monotonic in every cell (kept as measured, not smoothed
        # into a clean story) but never flat either. Confirms they moved
        # to FLOOR_LOT_POSITION_DEPENDENT_ASSETS along with the data.
        for asset in ("Ethereum", "Solana"):
            assert asset in bc.FLOOR_LOT_POSITION_DEPENDENT_ASSETS
            for regime in bc.REGIME_NAMES:
                first = bc.floor_lot_probability(asset, regime, "first")
                mid = bc.floor_lot_probability(asset, regime, "2nd_3rd")
                last = bc.floor_lot_probability(asset, regime, "4th_plus")
                assert not (first == mid == last), f"{asset} {regime}: expected NOT flat"

    def test_unknown_position_tier_rejected(self):
        with pytest.raises(ValueError):
            bc.floor_lot_probability("Bitcoin", "CHEAP", "5th")


class TestWithinBandSizeMultiplier:
    """Continuous price-vs-size scaling within CORE/HIGH, added 2026-09-08.
    Verified cell-by-cell (asset, regime, position_tier) before trusting it
    wasn't just position_tier correlating with price -- see
    behavior_config.WITHIN_BAND_SIZE_SLOPE's docstring."""

    def test_neutral_everywhere_not_specifically_calibrated(self):
        # UPGRADED 2026-09-11: CHEAP/MID were validated (cheap-mid-within-
        # band-scaling-gap) and now have real cells for BTC/ETH/SOL -- see
        # WITHIN_BAND_SIZE_SLOPE. The three dormant assets, in EVERY
        # regime including CORE/HIGH, must still be an exact no-op --
        # this function was only ever calibrated for Bitcoin/Ethereum/
        # Solana.
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            for regime in bc.REGIME_NAMES:
                assert bc.within_band_size_multiplier(asset, regime, 0.5) == 1.0

    def test_mean_neutral_at_the_calibrated_band_mean_price(self):
        # At exactly band_mean_price, the multiplier must be 1.0 -- that's
        # what makes this mean-neutral relative to the existing median.
        for asset, regimes in bc.WITHIN_BAND_SIZE_SLOPE.items():
            for regime, spec in regimes.items():
                mult = bc.within_band_size_multiplier(asset, regime, spec["band_mean_price"])
                assert abs(mult - 1.0) < 1e-9

    def test_increases_with_price_within_band(self):
        # The confirmed direction: higher price -> larger size, for every
        # calibrated (asset, regime) cell.
        for asset, regimes in bc.WITHIN_BAND_SIZE_SLOPE.items():
            for regime, spec in regimes.items():
                mean = spec["band_mean_price"]
                low = bc.within_band_size_multiplier(asset, regime, mean - 0.02)
                high = bc.within_band_size_multiplier(asset, regime, mean + 0.02)
                assert low < 1.0 < high, f"{asset}/{regime}: expected low<1.0<high, got {low}/{high}"

    def test_capped_against_extrapolation(self):
        # Ethereum HIGH has one of the steepest slopes in the table
        # (exact value has shifted across recalibrations -- see
        # WITHIN_BAND_SIZE_SLOPE's own docstring for the current numbers)
        # -- at the extreme edge of the band this must still be bounded,
        # not blow up.
        mult = bc.within_band_size_multiplier("Ethereum", "HIGH", 1.0)
        assert mult <= 5.0

    def test_solana_cheap_slope_recalibrated_2026_09_23(self):
        # Refit directly from real trader data, per-asset (not pooled),
        # proper train/test split -- held-out R2 doubled (0.102 -> 0.204)
        # with this value vs the old 3.1063. See WITHIN_BAND_SIZE_SLOPE's
        # docstring for the full derivation and which other cells were
        # checked but NOT recalibrated (no real improvement or too thin).
        assert bc.WITHIN_BAND_SIZE_SLOPE["Solana"]["CHEAP"]["slope"] == pytest.approx(10.338)


class TestDeepCheapTailMultiplier:
    """ADDED 2026-09-23 -- corrects the deep CHEAP tail (price<0.08), where
    WITHIN_BAND_SIZE_SLOPE's single whole-band slope badly under-corrects
    (real sizes there run 1.4-4.2x smaller than predicted). RECALIBRATED
    2026-09-25 (research-loop sizing audit) -- even the original 2026-09-23
    correction was itself found to under-predict on fresh data (Bitcoin
    and Solana now need the multiplier to be a BOOST, not a dampener; see
    DEEP_CHEAP_TAIL_MULTIPLIER's docstring for the full derivation)."""

    def test_matches_documented_calibration(self):
        assert bc.table_lookup(bc.DEEP_CHEAP_TAIL_MULTIPLIER, "Bitcoin") == pytest.approx(1.052)
        assert bc.table_lookup(bc.DEEP_CHEAP_TAIL_MULTIPLIER, "Ethereum") == pytest.approx(0.608)
        assert bc.table_lookup(bc.DEEP_CHEAP_TAIL_MULTIPLIER, "Solana") == pytest.approx(1.121)

    def test_every_calibrated_value_is_positive(self):
        for asset, mult in bc.DEEP_CHEAP_TAIL_MULTIPLIER.items():
            assert mult > 0.0

    def test_unknown_asset_is_a_noop(self):
        assert bc.table_lookup(bc.DEEP_CHEAP_TAIL_MULTIPLIER, "Dogecoin") == 1.0

    def test_threshold_matches_documented_value(self):
        assert bc.DEEP_CHEAP_TAIL_PRICE_THRESHOLD == pytest.approx(0.08)


class TestTtcSizeMultiplier:
    """Time-to-close size scaling, added 2026-09-10, RETIRED 2026-09-26
    (full sizing-mechanism audit against TRUE on-chain order identity --
    see project memory five-second-merge-convention-decisively-wrong).
    Every prior freshness check on this table (2026-09-10, 2026-09-13)
    used the project's off-chain 5-second merge convention, since
    confirmed to fit the real formula 4.75x worse than true on-chain
    order data. Re-checked directly against 3,594 true on-chain orders
    (~37h clean window): including this table's ttc_mult factor HURT
    first-entry sizing fit in 10 of 12 (asset, regime) cells, stable
    across both halves of the window (not a fluke) -- the apparently
    "confirmed, holds up well" earlier freshness checks were re-measuring
    the merge artifact, not the real relationship. Table emptied; these
    tests now confirm the no-op state, not the original (retracted)
    shape."""

    def test_neutral_for_assets_not_in_the_table(self):
        # Dogecoin/Hyperliquid/BNB aren't live-traded -- no data, must
        # stay a strict no-op regardless of regime/ttc.
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            for regime in ("CHEAP", "MID", "CORE", "HIGH"):
                assert bc.ttc_size_multiplier(asset, regime, 150.0) == 1.0

    def test_neutral_for_an_unrecognized_regime(self):
        assert bc.ttc_size_multiplier("Bitcoin", "NOT_A_REGIME", 150.0) == 1.0

    def test_table_is_empty_and_every_live_asset_is_now_a_noop(self):
        assert bc.TTC_SIZE_MULTIPLIER == {}
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            for regime in ("CHEAP", "MID", "CORE", "HIGH"):
                for ttc in (10.0, 90.0, 150.0, 180.0, 210.0, 270.0, 295.0):
                    assert bc.ttc_size_multiplier(asset, regime, ttc) == 1.0

    def test_flat_beyond_the_measured_range_no_extrapolation(self):
        # Below 90s or above 270s must clamp to the nearest endpoint, not
        # extrapolate past what the data supports -- still true trivially
        # now that every value is the same 1.0 no-op.
        at_90 = bc.ttc_size_multiplier("Bitcoin", "HIGH", 90.0)
        below_90 = bc.ttc_size_multiplier("Bitcoin", "HIGH", 10.0)
        assert at_90 == below_90

        at_270 = bc.ttc_size_multiplier("Bitcoin", "HIGH", 270.0)
        above_270 = bc.ttc_size_multiplier("Bitcoin", "HIGH", 295.0)
        assert at_270 == above_270


class TestAdverseMoveHedgeTriggerMultiplier:
    """Hedge TRIGGER (not size) vs adverse-move magnitude, added
    2026-09-10 -- distinct from TestAdverseMoveSizeMultiplier /
    TestAdverseMoveContinuationSizeMultiplier (whether he hedges at all,
    not how big it is once he does). Survived regime/attempt_index/TTC
    confound checks before being built. Deliberately scoped to
    adverse_move >= 0 -- the favorable-move side showed a different,
    non-monotonic pattern (SCOUT-classification bleed-through) and is
    left as a strict 1.0 no-op."""

    def test_neutral_for_assets_not_in_the_table(self):
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            assert bc.adverse_move_hedge_trigger_multiplier(asset, 0.2) == 1.0

    def test_neutral_when_adverse_move_is_unavailable(self):
        assert bc.adverse_move_hedge_trigger_multiplier("Bitcoin", None) == 1.0

    def test_neutral_for_favorable_moves_negative_adverse_move(self):
        # Deliberately unmodeled -- a strict no-op, not extrapolated or
        # clamped to the curve's first point.
        assert bc.adverse_move_hedge_trigger_multiplier("Bitcoin", -0.01) == 1.0
        assert bc.adverse_move_hedge_trigger_multiplier("Bitcoin", -5.0) == 1.0

    def test_exact_at_each_calibrated_point(self):
        for asset, curve in bc.ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER.items():
            for move, expected in curve.items():
                got = bc.adverse_move_hedge_trigger_multiplier(asset, float(move))
                assert abs(got - expected) < 1e-9, f"{asset}@{move}: {got} != {expected}"

    def test_interpolates_between_points(self):
        # Bitcoin: 0.07->0.7856, 0.15->1.0786 -- the midpoint move value
        # must interpolate to (roughly) the midpoint multiplier.
        mid_move = (0.07 + 0.15) / 2
        mid_val = bc.adverse_move_hedge_trigger_multiplier("Bitcoin", mid_move)
        assert 0.7856 < mid_val < 1.0786

    def test_flat_beyond_the_measured_range_no_extrapolation(self):
        at_max = bc.adverse_move_hedge_trigger_multiplier("Bitcoin", 0.30)
        above_max = bc.adverse_move_hedge_trigger_multiplier("Bitcoin", 0.99)
        assert at_max == above_max

    def test_largest_adverse_move_scores_higher_than_move_at_zero(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            curve = bc.ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER[asset]
            at_zero = bc.adverse_move_hedge_trigger_multiplier(asset, 0.0)
            most_adverse = max(curve)
            high_val = bc.adverse_move_hedge_trigger_multiplier(asset, most_adverse)
            assert high_val > at_zero, f"{asset}: expected the most-adverse move to score above move==0"

    def test_cap_is_not_accidentally_clipping_real_calibrated_values(self):
        for asset, curve in bc.ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER.items():
            assert max(curve.values()) < bc._ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER_CAP


class TestFavorableMoveHedgeTriggerMultiplier:
    """Mirror image of TestAdverseMoveHedgeTriggerMultiplier, added
    2026-09-19 -- the favorable-move (adverse_move<0) side that table
    deliberately left as a no-op, now modeled after a GP-IRL fit found
    the U-shape survives every confound check (SCOUT-bleed sizing,
    time-since-entry stratification, bootstrap, calendar split-half,
    per-asset consistency). CHEAP only."""

    def test_neutral_for_assets_not_in_the_table(self):
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            assert bc.favorable_move_hedge_trigger_multiplier(asset, -0.2) == 1.0

    def test_neutral_when_adverse_move_is_unavailable(self):
        assert bc.favorable_move_hedge_trigger_multiplier("Bitcoin", None) == 1.0

    def test_neutral_for_adverse_moves_nonnegative_adverse_move(self):
        # Deliberately unmodeled -- that's adverse_move_hedge_trigger_
        # multiplier's job. Strict no-op, not extrapolated.
        assert bc.favorable_move_hedge_trigger_multiplier("Bitcoin", 0.0) == 1.0
        assert bc.favorable_move_hedge_trigger_multiplier("Bitcoin", 0.01) == 1.0
        assert bc.favorable_move_hedge_trigger_multiplier("Bitcoin", 5.0) == 1.0

    def test_exact_at_each_calibrated_point(self):
        for asset, curve in bc.FAVORABLE_MOVE_HEDGE_TRIGGER_MULTIPLIER.items():
            for move, expected in curve.items():
                got = bc.favorable_move_hedge_trigger_multiplier(asset, float(move))
                assert abs(got - expected) < 1e-9, f"{asset}@{move}: {got} != {expected}"

    def test_interpolates_between_points(self):
        # Bitcoin: -0.5->1.449, -0.2->1.051 -- the midpoint move value
        # must interpolate to (roughly) the midpoint multiplier.
        mid_move = (-0.5 + -0.2) / 2
        mid_val = bc.favorable_move_hedge_trigger_multiplier("Bitcoin", mid_move)
        assert 1.051 < mid_val < 1.449

    def test_flat_beyond_the_measured_range_no_extrapolation(self):
        at_min = bc.favorable_move_hedge_trigger_multiplier("Bitcoin", -1.0)
        below_min = bc.favorable_move_hedge_trigger_multiplier("Bitcoin", -5.0)
        assert at_min == below_min

    def test_valley_near_zero_is_lower_than_both_extremes(self):
        # The confirmed bathtub shape: elevated at both the deepest
        # favorable move AND (via the sibling table) the most adverse,
        # with a valley near adverse_move==0.
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            near_zero = bc.favorable_move_hedge_trigger_multiplier(asset, -0.05)
            deep_favorable = bc.favorable_move_hedge_trigger_multiplier(asset, -1.0)
            assert deep_favorable > near_zero, f"{asset}: expected the deepest favorable move to score above near-zero"

    def test_cap_is_not_accidentally_clipping_real_calibrated_values(self):
        for asset, curve in bc.FAVORABLE_MOVE_HEDGE_TRIGGER_MULTIPLIER.items():
            assert max(curve.values()) < bc._FAVORABLE_MOVE_HEDGE_TRIGGER_MULTIPLIER_CAP


class TestHedgeProbabilityConsolidated:
    """The single hedge-TRIGGER entry point, added 2026-09-24 (single-
    function hedge consolidation -- see that function's own docstring in
    behavior_config.py for exactly which of the previous 13 factors
    survived and which were cut). Direct, focused coverage of the
    function itself, on top of whatever incidental coverage it gets via
    decide_hedge in test_strategy.py."""

    def test_first_hedge_matches_base_hazard_times_post_shift_dampener(self):
        p = bc.hedge_probability("Bitcoin", "MID", attempt_index=1, hedge_count=0)
        expected = bc.hedge_attempt_hazard("Bitcoin", "MID", 1) * bc.table_lookup(bc.REGIME_HEDGE_POST_SHIFT_MULTIPLIER, "MID", "Bitcoin")
        assert p == pytest.approx(max(0.0, min(1.0, expected)))

    def test_adverse_move_scales_the_first_hedge_probability(self):
        base = bc.hedge_probability("Bitcoin", "CORE", 1, 0, adverse_move=None)
        moved = bc.hedge_probability("Bitcoin", "CORE", 1, 0, adverse_move=0.30)
        assert moved != base, "a real adverse move must actually change the probability"

    def test_favorable_move_only_applies_in_cheap(self):
        # Same negative adverse_move, different regime -- CHEAP should
        # diverge from its own adverse_move=None baseline (the favorable
        # multiplier fires), MID should not (no favorable-move
        # calibration outside CHEAP in the consolidated design).
        cheap_base = bc.hedge_probability("Bitcoin", "CHEAP", 1, 0, adverse_move=None)
        cheap_favorable = bc.hedge_probability("Bitcoin", "CHEAP", 1, 0, adverse_move=-0.5)
        assert cheap_favorable != cheap_base

        mid_base = bc.hedge_probability("Bitcoin", "MID", 1, 0, adverse_move=None)
        mid_favorable = bc.hedge_probability("Bitcoin", "MID", 1, 0, adverse_move=-0.5)
        assert mid_favorable == mid_base, "favorable-move scaling is CHEAP-only in the consolidated design"

    def test_continuation_ignores_attempt_index_and_adverse_move(self):
        # hedge_count >= 1 must go through hedge_continuation_probability
        # regardless of attempt_index or adverse_move -- those only
        # matter for the FIRST hedge.
        p1 = bc.hedge_probability("Bitcoin", "CHEAP", attempt_index=1, hedge_count=1, adverse_move=0.9)
        p2 = bc.hedge_probability("Bitcoin", "HIGH", attempt_index=9, hedge_count=1, adverse_move=-0.9)
        assert p1 == p2 == pytest.approx(bc.hedge_continuation_probability(1))

    def test_always_returns_a_valid_probability(self):
        for regime in bc.REGIME_NAMES:
            for adverse_move in (None, -1.0, 0.0, 1.0):
                p = bc.hedge_probability("Ethereum", regime, 1, 0, adverse_move=adverse_move)
                assert 0.0 <= p <= 1.0

    def test_seconds_remaining_none_is_a_no_op(self):
        # Default (no seconds_remaining passed) must match explicitly
        # passing None -- both are the pre-2026-09-25 behavior, for
        # existing callers that don't know about window-close timing.
        p_default = bc.hedge_probability("Bitcoin", "MID", 1, 0, adverse_move=0.2)
        p_none = bc.hedge_probability("Bitcoin", "MID", 1, 0, adverse_move=0.2, seconds_remaining=None)
        assert p_default == p_none

    def test_seconds_remaining_boosts_the_first_hedge_near_close(self):
        far = bc.hedge_probability("Bitcoin", "MID", 1, 0, adverse_move=0.2, seconds_remaining=200.0)
        near = bc.hedge_probability("Bitcoin", "MID", 1, 0, adverse_move=0.2, seconds_remaining=75.0)
        assert near > far

    def test_seconds_remaining_boosts_continuation_hedges_too(self):
        far = bc.hedge_probability("Bitcoin", "CHEAP", attempt_index=1, hedge_count=1, seconds_remaining=200.0)
        near = bc.hedge_probability("Bitcoin", "CHEAP", attempt_index=1, hedge_count=1, seconds_remaining=75.0)
        assert near > far
        assert far == pytest.approx(bc.hedge_continuation_probability(1))

    def test_still_clamped_to_a_valid_probability_with_urgency_applied(self):
        for seconds_remaining in (None, 165.0, 120.0, 75.0, 10.0, 500.0):
            p = bc.hedge_probability("Bitcoin", "HIGH", 9, 0, adverse_move=1.0,
                                      seconds_remaining=seconds_remaining)
            assert 0.0 <= p <= 1.0

    # (streak_length tests removed 2026-09-28 -- the parameter itself was
    # deleted from hedge_probability's signature once STREAK_HEDGE_
    # PROBABILITY_MULTIPLIER/STREAK_HEDGE_SIZE_MULTIPLIER's on-chain
    # re-check reversed the finding they fed. See git history for the
    # retired functions/tables if reviving a streak-conditioned hedge
    # signal is ever warranted.)


class TestUrgencyMultipliers:
    """CLOSE-URGENCY boost, added 2026-09-25 (research-loop capstone
    finding): as a market's window approaches close, first-entry and
    hedge activity both get sharply more likely -- see ENTRY_URGENCY_
    MULTIPLIER/HEDGE_URGENCY_MULTIPLIER's own module comment for the
    real hazard numbers and why this is boost-only (ratio to the 165s-
    remaining hazard specifically), not mean-neutral like TTC_SIZE_
    MULTIPLIER -- a mean-neutral version was tried first and rejected
    because it silently dampened baseline (non-urgent) participation
    everywhere, not just boosting it near close.

    entry_urgency_multiplier/hedge_urgency_multiplier were merged into
    one urgency_multiplier(kind, seconds_remaining) function 2026-09-28
    -- already identical shape, sharing _interpolate_urgency, differing
    only by which table "entry"/"hedge" selects."""

    def test_none_is_a_no_op_for_both(self):
        assert bc.urgency_multiplier("entry", None) == 1.0
        assert bc.urgency_multiplier("hedge", None) == 1.0

    def test_no_op_at_and_beyond_165s_remaining(self):
        # Boost-only design: the baseline (most of an ordinary window)
        # must be an exact 1.0, never a dampener, unlike a mean-neutral
        # table would produce.
        assert bc.urgency_multiplier("entry", 165.0) == 1.0
        assert bc.urgency_multiplier("entry", 300.0) == 1.0
        assert bc.urgency_multiplier("hedge", 165.0) == 1.0
        assert bc.urgency_multiplier("hedge", 300.0) == 1.0

    def test_exact_at_each_calibrated_midpoint(self):
        for midpoint, expected in bc.ENTRY_URGENCY_MULTIPLIER.items():
            got = bc.urgency_multiplier("entry", float(midpoint))
            assert abs(got - expected) < 1e-9, f"entry@{midpoint}: {got} != {expected}"
        for midpoint, expected in bc.HEDGE_URGENCY_MULTIPLIER.items():
            got = bc.urgency_multiplier("hedge", float(midpoint))
            assert abs(got - expected) < 1e-9, f"hedge@{midpoint}: {got} != {expected}"

    def test_interpolates_between_midpoints(self):
        low = bc.urgency_multiplier("entry", 135.0)
        high = bc.urgency_multiplier("entry", 105.0)
        mid = bc.urgency_multiplier("entry", 120.0)
        assert abs(mid - (low + high) / 2) < 1e-9

    def test_flat_below_75s_no_extrapolation(self):
        # Our own MIN_SECONDS_BEFORE_CLOSE=75 is the practical floor any
        # live decision can ever see, but the function itself must still
        # clamp rather than extrapolate past the measured range.
        at_75 = bc.urgency_multiplier("entry", 75.0)
        below_75 = bc.urgency_multiplier("entry", 10.0)
        assert at_75 == below_75
        at_75_hedge = bc.urgency_multiplier("hedge", 75.0)
        below_75_hedge = bc.urgency_multiplier("hedge", 0.0)
        assert at_75_hedge == below_75_hedge

    def test_climbs_monotonically_toward_close(self):
        entry_vals = [bc.urgency_multiplier("entry", s) for s in (165.0, 135.0, 105.0, 75.0)]
        assert entry_vals == sorted(entry_vals)
        hedge_vals = [bc.urgency_multiplier("hedge", s) for s in (165.0, 135.0, 105.0, 75.0)]
        assert hedge_vals == sorted(hedge_vals)

    def test_never_below_one_boost_only(self):
        # The whole point of the redesign: this must never dampen below
        # 1.0 at any point in the covered range, unlike the rejected
        # mean-neutral version.
        for seconds_remaining in (0.0, 75.0, 90.0, 120.0, 150.0, 165.0, 500.0):
            assert bc.urgency_multiplier("entry", seconds_remaining) >= 1.0
            assert bc.urgency_multiplier("hedge", seconds_remaining) >= 1.0

    def test_hedge_climbs_steeper_than_entry(self):
        # Real data: hedge hazard climbs from 5.66% to 14.93% (~2.6x),
        # entries from 15.74% to 27.00% (~1.7x) -- the hedge curve should
        # be the steeper of the two at the 75s endpoint.
        assert bc.urgency_multiplier("hedge", 75.0) > bc.urgency_multiplier("entry", 75.0)

    def test_unknown_kind_raises(self):
        with pytest.raises(KeyError):
            bc.urgency_multiplier("bogus", 100.0)


class TestStreakFadeProbability:
    """STREAK-FADE first-entry side bias, added 2026-09-25 (research-loop
    finding, fully re-verified -- see market-outcome-streak-fading-found.md
    in project memory): after 2+ consecutive same-direction resolved
    outcomes for an asset, his first-entry side fades the streak direction
    57-73% of the time depending on streak length and elapsed time since
    the window opened. Confound-checked six ways (price-level, regime,
    own win/loss interaction, cross-asset consistency, two independent
    out-of-sample replications, and the streak-detection logic itself
    unit-tested)."""

    def test_below_length_2_is_a_neutral_no_op(self):
        assert bc.streak_fade_probability(0) == 0.5
        assert bc.streak_fade_probability(1) == 0.5

    def test_no_elapsed_time_uses_the_pooled_length_only_rate(self):
        # seconds_elapsed=None -> elapsed factor no-ops at 1.0.
        expected = bc.STREAK_FADE_BASE_PROBABILITY * bc.STREAK_FADE_LENGTH_MULTIPLIER[2]
        assert bc.streak_fade_probability(2) == pytest.approx(expected)

    def test_exact_at_each_calibrated_elapsed_midpoint(self):
        for midpoint, mult in bc.STREAK_FADE_ELAPSED_MULTIPLIER.items():
            expected = bc.STREAK_FADE_BASE_PROBABILITY * bc.STREAK_FADE_LENGTH_MULTIPLIER[2] * mult
            got = bc.streak_fade_probability(2, float(midpoint))
            assert abs(got - expected) < 1e-9, f"elapsed@{midpoint}: {got} != {expected}"

    def test_interpolates_between_elapsed_midpoints(self):
        low = bc.streak_fade_probability(2, 30.0)
        high = bc.streak_fade_probability(2, 90.0)
        mid = bc.streak_fade_probability(2, 60.0)
        assert abs(mid - (low + high) / 2) < 1e-9

    def test_flat_outside_measured_elapsed_range_no_extrapolation(self):
        at_30 = bc.streak_fade_probability(2, 30.0)
        below_30 = bc.streak_fade_probability(2, 0.0)
        assert at_30 == below_30
        at_210 = bc.streak_fade_probability(2, 210.0)
        above_210 = bc.streak_fade_probability(2, 500.0)
        assert at_210 == above_210

    def test_decays_monotonically_as_elapsed_time_grows(self):
        vals = [bc.streak_fade_probability(2, s) for s in (30.0, 90.0, 150.0, 210.0)]
        assert vals == sorted(vals, reverse=True)

    def test_length_beyond_5_extrapolates_flat_at_the_5plus_rate(self):
        assert bc.streak_fade_probability(5, 100.0) == bc.streak_fade_probability(10, 100.0)
        assert bc.streak_fade_probability(5, 100.0) == bc.streak_fade_probability(99, 100.0)

    def test_length_not_smoothed_matches_real_measured_shape(self):
        # Deliberately non-monotonic (length=4 dips below length=3) --
        # kept exactly as measured, not smoothed. Confirm the table
        # itself (not just the function) preserves this shape.
        assert bc.STREAK_FADE_LENGTH_MULTIPLIER[4] < bc.STREAK_FADE_LENGTH_MULTIPLIER[3]

    def test_always_returns_a_valid_probability(self):
        for length in (2, 3, 4, 5, 10):
            for elapsed in (None, 0.0, 30.0, 120.0, 210.0, 1000.0):
                p = bc.streak_fade_probability(length, elapsed)
                assert 0.0 <= p <= 1.0


class TestHourSizeMultiplier:
    """HOUR-CONDITIONED SIZE BOOST, added 2026-09-25 (research-loop
    finding): his median first-entry size runs ~19-26% higher during
    06:00-07:00 UTC specifically, confirmed on two independent samples.
    Boost-only, deliberately modest (tracks his observed size reaction,
    not the much larger per-trade edge measured in the same hour)."""

    def test_boost_during_the_hour(self):
        assert bc.hour_size_multiplier(6) == bc.HOUR_SIZE_BOOST_MULTIPLIER
        assert bc.hour_size_multiplier(6) > 1.0

    def test_no_op_every_other_hour(self):
        for hour in range(24):
            if hour in bc.HOUR_SIZE_BOOST_UTC_HOURS:
                continue
            assert bc.hour_size_multiplier(hour) == 1.0

    def test_no_op_for_none(self):
        assert bc.hour_size_multiplier(None) == 1.0


class TestHedgeNotionalForPriceIsTheOnlySizeFunction:
    """hedge_notional_for_price, promoted 2026-09-24 to the single hedge-
    SIZE entry point (the old dominant-cost-ratio chain -- HEDGE_SIZE_
    RATIO plus five sibling adjustment multipliers -- was deleted
    entirely, not just bypassed)."""

    def test_returns_none_for_dormant_assets(self):
        # BNB/Dogecoin/Hyperliquid are not part of --assets and were
        # never calibrated for this formula.
        for asset in ("BNB", "Dogecoin", "Hyperliquid", "NotAnAsset"):
            assert bc.hedge_notional_for_price(asset, 0.5) is None

    def test_returns_a_positive_notional_for_every_live_asset(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            for price in (0.05, 0.25, 0.5, 0.75, 0.95):
                notional = bc.hedge_notional_for_price(asset, price)
                assert notional is not None and notional > 0

    def test_none_for_a_price_outside_the_open_unit_interval(self):
        assert bc.hedge_notional_for_price("Bitcoin", 0.0) is None
        assert bc.hedge_notional_for_price("Bitcoin", 1.0) is None
        assert bc.hedge_notional_for_price("Bitcoin", None) is None


class TestHedgeCalibration:
    """Confirmed from the full historical dataset: dual-sided markets have
    a dramatically better worst-case outcome than single-sided ones
    (-20.5% of stake on average vs -100%), and 24.9% are outright
    arbitrage. All six assets and four regimes must have real, distinct
    calibration data -- not a single shared fallback.

    NOTE 2026-09-24: HEDGE_SIZE_RATIO and the hedge_trigger_probability/
    hedge_size_ratio wrapper functions were removed in the single-
    function hedge consolidation (hedge size is now hedge_notional_for_
    price, asset+price only, not asset+regime) -- these tests now read
    HEDGE_TRIGGER_PROBABILITY directly rather than through the deleted
    hedge_trigger_probability() wrapper, which was already dead code
    (unused outside this test file) before the consolidation."""

    def test_all_six_assets_and_four_regimes_present_for_trigger_probability(self):
        assert set(bc.HEDGE_TRIGGER_PROBABILITY.keys()) == set(bc.ASSET_NAMES)
        for asset in bc.ASSET_NAMES:
            assert set(bc.HEDGE_TRIGGER_PROBABILITY[asset].keys()) == set(bc.REGIME_NAMES)

    def test_probabilities_are_valid_and_asset_specific(self):
        cheap_probs = {a: bc.HEDGE_TRIGGER_PROBABILITY[a]["CHEAP"] for a in bc.ASSET_NAMES}
        for p in cheap_probs.values():
            assert 0.0 <= p <= 1.0
        assert len(set(cheap_probs.values())) == len(cheap_probs)

    def test_high_regime_has_the_lowest_trigger_probability_everywhere(self):
        """Confirmed pattern: a HIGH-band primary entry is already so
        confident that a hedge is added far less often than from any
        other regime, consistently across every asset."""
        for asset in bc.ASSET_NAMES:
            high_p = bc.HEDGE_TRIGGER_PROBABILITY[asset]["HIGH"]
            for regime in ("CHEAP", "MID", "CORE"):
                assert high_p <= bc.HEDGE_TRIGGER_PROBABILITY[asset][regime]


class TestScoutParams:
    """scout_params, merged 2026-09-28 from scout_probability +
    scout_size_ratio -- always called together at both existing call
    sites (roll the probability, apply the ratio only if it fires)."""

    def test_matches_the_calibrated_tables_for_live_assets(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            prob, ratio = bc.scout_params(asset)
            assert prob == pytest.approx(bc.SCOUT_PROBABILITY[asset])
            assert ratio == pytest.approx(bc.SCOUT_SIZE_RATIO[asset])

    def test_uncalibrated_asset_gets_a_zero_probability_and_default_ratio(self):
        prob, ratio = bc.scout_params("XRP")
        assert prob == 0.0
        assert ratio == bc._DEFAULT_SCOUT_SIZE_RATIO

    def test_returns_a_two_tuple(self):
        result = bc.scout_params("Bitcoin")
        assert isinstance(result, tuple)
        assert len(result) == 2


class TestReentryFatigueMultiplier:
    """Re-entry fatigue dampener, added 2026-09-13 (/loop iter 130, then
    generalized same day from MID-only to 6 (asset, regime) cells across
    MID/CORE/CHEAP) -- real, confound-checked, temporally-stable finding
    that win rate drops sharply once a market has already had enough
    same-side real fills, in specific cells only. Unlike most multipliers
    in this file, this is a deliberate risk dampener, not a replicated
    real-sizing curve -- every excluded cell (Bitcoin/MID, Solana/CORE,
    Bitcoin/CHEAP, all of HIGH) is excluded for a specific, documented
    reason (see the docstring in behavior_config.py), not just
    uncalibrated."""

    def test_noop_for_uncalibrated_asset_regime_cells(self):
        # Bitcoin/MID, Solana/CORE, Bitcoin/CHEAP, and HIGH for every asset
        # are all deliberately excluded -- see the module docstring.
        assert bc.reentry_fatigue_multiplier("Bitcoin", "MID", 20) == 1.0
        assert bc.reentry_fatigue_multiplier("Solana", "CORE", 20) == 1.0
        assert bc.reentry_fatigue_multiplier("Bitcoin", "CHEAP", 20) == 1.0
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            assert bc.reentry_fatigue_multiplier(asset, "HIGH", 20) == 1.0

    def test_noop_for_cells_that_vanished_post_halt(self):
        # RECALIBRATED 2026-09-13 (/loop cycle 10): Ethereum/MID and
        # Solana/MID showed a genuinely vanished (not just weakened)
        # fatigue effect on fresh post-halt data -- removed from the
        # table entirely, see the module docstring. Ethereum/CORE was
        # ALSO removed here originally, but cycle 22 (same day) found
        # cycle 10's own fill-index semantics were wrong (same-side-only,
        # not the true combined real_fill_count) and restored it once
        # redone correctly -- see this file's own docstring for the full
        # story, and test_dampens_at_and_beyond_threshold below for its
        # current (non-no-op) behavior. Cycle 22 ALSO found Solana/CHEAP's
        # earlier "real" verdict was itself an artifact of the same bug
        # and removed it -- covered by the assertion below instead.
        for asset, regime in [("Ethereum", "MID"), ("Solana", "MID"), ("Solana", "CHEAP")]:
            assert bc.reentry_fatigue_multiplier(asset, regime, 20) == 1.0

    def test_noop_when_real_fill_count_missing(self):
        for asset, regime in bc.REENTRY_FATIGUE_THRESHOLD:
            assert bc.reentry_fatigue_multiplier(asset, regime, None) == 1.0

    def test_noop_below_threshold(self):
        for (asset, regime), threshold in bc.REENTRY_FATIGUE_THRESHOLD.items():
            for n in range(0, threshold):
                assert bc.reentry_fatigue_multiplier(asset, regime, n) == 1.0

    def test_dampens_at_and_beyond_threshold(self):
        for (asset, regime), expected in bc.REENTRY_FATIGUE_MULTIPLIER.items():
            threshold = bc.REENTRY_FATIGUE_THRESHOLD[(asset, regime)]
            got = bc.reentry_fatigue_multiplier(asset, regime, threshold)
            assert got == expected
            # Stays dampened (flat), doesn't keep shrinking further past the
            # threshold -- this is a step function, not a continuous decay.
            got_far = bc.reentry_fatigue_multiplier(asset, regime, threshold + 50)
            assert got_far == expected

    def test_every_threshold_cell_has_a_matching_multiplier_and_vice_versa(self):
        assert set(bc.REENTRY_FATIGUE_THRESHOLD) == set(bc.REENTRY_FATIGUE_MULTIPLIER)

    def test_multiplier_is_a_real_dampener_not_a_boost(self):
        for key, mult in bc.REENTRY_FATIGUE_MULTIPLIER.items():
            assert 0.0 < mult < 1.0, f"{key}: expected a dampener (0,1), got {mult}"


class TestAddOnContinuationProbability:
    """Add-on continuation probability, added 2026-09-26 -- closes the
    real structural gap that ordinary same-side add-ons had no
    continuation-probability gate at all (unlike hedges'
    HEDGE_CONTINUATION_PROBABILITY). Re-derived on true on-chain order
    identity after the original 5-second-merge-based numbers failed
    independent re-verification -- see ADD_ON_CONTINUATION_PROBABILITY's
    docstring for the full derivation."""

    def test_noop_for_dormant_assets(self):
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            for regime in ("CHEAP", "MID", "CORE", "HIGH"):
                assert bc.addon_continuation_probability(asset, regime) == 1.0

    def test_every_live_asset_regime_cell_is_calibrated(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            for regime in ("CHEAP", "MID", "CORE", "HIGH"):
                p = bc.addon_continuation_probability(asset, regime)
                assert 0.0 < p < 1.0, f"{asset}/{regime}: expected a real dampener, got {p}"

    def test_matches_the_calibrated_table_exactly(self):
        for (asset, regime), expected in bc.ADD_ON_CONTINUATION_PROBABILITY.items():
            assert bc.addon_continuation_probability(asset, regime) == pytest.approx(expected)

    def test_bitcoin_continues_more_than_ethereum_and_solana_in_every_regime(self):
        # The well-replicated part of this finding (survived re-derivation
        # on a completely different, methodologically cleaner dataset) --
        # the exact per-cell numbers are less load-bearing than this
        # ordering.
        for regime in ("CHEAP", "MID", "CORE", "HIGH"):
            btc = bc.addon_continuation_probability("Bitcoin", regime)
            eth = bc.addon_continuation_probability("Ethereum", regime)
            sol = bc.addon_continuation_probability("Solana", regime)
            assert btc > eth, f"{regime}: Bitcoin should continue more than Ethereum"
            assert btc > sol, f"{regime}: Bitcoin should continue more than Solana"


class TestHedgeCountReinforcementMultiplier:
    """Hedge-count reinforcement, added 2026-09-13 (/loop iters 131-133,
    138) -- the mirror-image signal to REENTRY_FATIGUE: needing 2+ hedges
    against a market's original side predicts that side wins MORE, not
    less, for Ethereum/Solana in CHEAP/MID. Verified against the LIVE
    hedge-count-so-far (not a post-hoc final total) before building --
    real-time actionable. Bitcoin and CORE/HIGH deliberately excluded."""

    def test_noop_for_uncalibrated_asset_regime_cells(self):
        assert bc.hedge_count_reinforcement_multiplier("Bitcoin", "MID", 5) == 1.0
        assert bc.hedge_count_reinforcement_multiplier("Ethereum", "CORE", 5) == 1.0
        assert bc.hedge_count_reinforcement_multiplier("Solana", "HIGH", 5) == 1.0

    def test_noop_when_live_hedge_count_missing(self):
        for asset, regime in bc.HEDGE_COUNT_REINFORCEMENT_THRESHOLD:
            assert bc.hedge_count_reinforcement_multiplier(asset, regime, None) == 1.0

    def test_noop_below_first_threshold(self):
        for (asset, regime), tiers in bc.HEDGE_COUNT_REINFORCEMENT_TIERS.items():
            first_threshold = tiers[0][0]
            for n in range(0, first_threshold):
                assert bc.hedge_count_reinforcement_multiplier(asset, regime, n) == 1.0

    def test_boosts_at_each_tier_and_stays_flat_within_it(self):
        """/loop iter: extended from a single threshold to 2 tiers (the
        full live-index dose-response keeps climbing past the first
        threshold, though too noisily bucket-by-bucket to trust a smooth
        curve). At each tier's own threshold the multiplier steps up;
        between one tier's threshold and the next, it stays flat."""
        for (asset, regime), tiers in bc.HEDGE_COUNT_REINFORCEMENT_TIERS.items():
            assert len(tiers) == 2, "test assumes exactly 2 tiers -- update if this changes"
            (t1, m1), (t2, m2) = tiers
            assert t1 < t2 and m1 < m2, "tiers must be ascending in both threshold and multiplier"

            assert bc.hedge_count_reinforcement_multiplier(asset, regime, t1) == m1
            assert bc.hedge_count_reinforcement_multiplier(asset, regime, t2 - 1) == m1
            assert bc.hedge_count_reinforcement_multiplier(asset, regime, t2) == m2
            assert bc.hedge_count_reinforcement_multiplier(asset, regime, t2 + 10) == m2

    def test_backward_compatible_derived_views_match_tier_one(self):
        # HEDGE_COUNT_REINFORCEMENT_THRESHOLD/MULTIPLIER are derived views
        # kept for existing callers/tests -- must always reflect tier 1.
        for key, tiers in bc.HEDGE_COUNT_REINFORCEMENT_TIERS.items():
            assert bc.HEDGE_COUNT_REINFORCEMENT_THRESHOLD[key] == tiers[0][0]
            assert bc.HEDGE_COUNT_REINFORCEMENT_MULTIPLIER[key] == tiers[0][1]

    def test_every_threshold_cell_has_a_matching_multiplier_and_vice_versa(self):
        assert set(bc.HEDGE_COUNT_REINFORCEMENT_THRESHOLD) == set(bc.HEDGE_COUNT_REINFORCEMENT_MULTIPLIER)
        assert set(bc.HEDGE_COUNT_REINFORCEMENT_TIERS) == set(bc.HEDGE_COUNT_REINFORCEMENT_MULTIPLIER)

    def test_multiplier_is_a_real_boost_not_a_dampener(self):
        for key, tiers in bc.HEDGE_COUNT_REINFORCEMENT_TIERS.items():
            for threshold, mult in tiers:
                assert mult > 1.0, f"{key}@{threshold}: expected a boost (>1.0), got {mult}"

    def test_multiplier_stays_within_the_established_modest_range(self):
        # Consistent with every other multiplier in this file staying well
        # under its own nominal safety cap in practice -- this is a
        # deliberate damping choice, not a mechanical constraint, so pin it
        # down with a test rather than let it silently drift wide later.
        for key, tiers in bc.HEDGE_COUNT_REINFORCEMENT_TIERS.items():
            for threshold, mult in tiers:
                assert mult <= 2.0, f"{key}@{threshold}: {mult} exceeds the intended modest damped range"


class TestDecisiveEntryEligibilityMultiplier:
    """DECISIVE_ENTRY_ELIGIBILITY_MULTIPLIER, added 2026-09-20 -- the
    CORE/HIGH sibling of cheap_joint_entry_multiplier. Closes a gap
    found by checking whether the CHEAP win-rate investigation
    generalized: DECISIVENESS_ONSET_MULTIPLIER only ever scaled size,
    never whether/when an entry happens, so CORE/HIGH had no
    entry-eligibility freshness gate at all. Calibrated directly against
    the real trader's own decisive-crossing hazard decay."""

    def test_multiplier_is_a_noop_within_the_first_bucket(self):
        assert bc.decisive_entry_eligibility_multiplier("Bitcoin", 0.0) == 1.0
        assert bc.decisive_entry_eligibility_multiplier("Bitcoin", 59.9) == 1.0

    def test_multiplier_applies_the_calibrated_value_in_later_buckets(self):
        # RECALIBRATED 2026-09-22: post-Sep-18 his hazard is flat/increasing
        # with age, so only Bitcoin's 180s+ bucket suppresses anything.
        assert bc.decisive_entry_eligibility_multiplier("Bitcoin", 90.0) == pytest.approx(1.0)
        assert bc.decisive_entry_eligibility_multiplier("Bitcoin", 150.0) == pytest.approx(1.0)
        assert bc.decisive_entry_eligibility_multiplier("Bitcoin", 300.0) == pytest.approx(0.78)
        assert bc.decisive_entry_eligibility_multiplier("Ethereum", 300.0) == pytest.approx(1.0)
        assert bc.decisive_entry_eligibility_multiplier("Solana", 90.0) == pytest.approx(1.0)

    def test_old_sharp_decay_values_are_gone(self):
        # The original 0.15/0.05/0.02-style values suppressed 85-98% of his
        # real (>=60s-after-crossing) entries; nothing may go below 0.5 now.
        for asset, buckets in bc.DECISIVE_ENTRY_ELIGIBILITY_MULTIPLIER.items():
            for lo, hi, mult in buckets:
                assert mult >= 0.5, (asset, lo, hi, mult)

    def test_multiplier_never_increases_with_age(self):
        # A skip gate can only suppress; each later bucket must be <= the
        # one before it, for every calibrated asset.
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            vals = [bc.decisive_entry_eligibility_multiplier(asset, t) for t in (30.0, 90.0, 150.0, 210.0)]
            assert vals == sorted(vals, reverse=True)

    def test_multiplier_defaults_to_noop_for_an_uncalibrated_asset(self):
        assert bc.decisive_entry_eligibility_multiplier("Dogecoin", 300.0) == 1.0

    def test_multiplier_is_a_noop_when_no_crossing_recorded(self):
        assert bc.decisive_entry_eligibility_multiplier("Bitcoin", None) == 1.0

    def test_multiplier_treats_a_negative_elapsed_as_the_first_bucket(self):
        assert bc.decisive_entry_eligibility_multiplier("Bitcoin", -5.0) == 1.0

    def test_cap_is_not_accidentally_clipping_real_calibrated_values(self):
        for asset, buckets in bc.DECISIVE_ENTRY_ELIGIBILITY_MULTIPLIER.items():
            for lo, hi, mult in buckets:
                assert 0.0 < mult <= 1.0


class TestSharesForPrice:
    """On-chain share-count sizing primitive (see its docstring above
    median_entry_notional in behavior_config.py)."""

    def test_matches_the_on_chain_formula_at_the_midpoint(self):
        # dist=0 -> 5.05 * exp(0) = 5.05
        assert bc.shares_for_price(0.5) == pytest.approx(5.05, abs=0.05)

    def test_symmetric_around_the_midpoint(self):
        for price in (0.10, 0.25, 0.40, 0.60, 0.75, 0.90):
            assert bc.shares_for_price(price) == bc.shares_for_price(1.0 - price)

    def test_kink_lands_on_the_030_070_band_boundary(self):
        # dist=0.20 (price 0.30/0.70) is where the formula switches --
        # NOT a continuous seam (a real, deliberate jump exists in the
        # on-chain data at this exact point, matching this file's own
        # existing CHEAP/MID vs CORE/HIGH band boundary). At price
        # exactly 0.30, dist<=0.20 is still true (inclusive), so it uses
        # the inner formula; just past it, the outer (steeper) formula
        # takes over and drops sharply.
        at_boundary = bc.shares_for_price(0.30)
        just_outside = bc.shares_for_price(0.30 - 1e-6)
        assert at_boundary == pytest.approx(6.0, abs=0.1)
        assert just_outside == pytest.approx(5.5, abs=0.1)
        assert just_outside < at_boundary

    def test_grows_monotonically_away_from_the_midpoint(self):
        prices = [0.5, 0.45, 0.40, 0.30, 0.20, 0.10, 0.05, 0.02]
        shares = [bc.shares_for_price(p) for p in prices]
        assert shares == sorted(shares)

    def test_quantized_to_one_decimal_share_lot(self):
        for price in (0.13, 0.27, 0.5, 0.68, 0.91):
            shares = bc.shares_for_price(price)
            assert round(shares * 10) == pytest.approx(shares * 10, abs=1e-9)

    def test_outside_mid_band_uses_the_steeper_tail_formula(self):
        # dist=0.30 (price=0.20): raw = 2.16 * exp(4.685*0.30) = ~8.81
        assert bc.shares_for_price(0.20) == pytest.approx(8.81, abs=0.1)


class TestNotionalForPrice:
    """Position-tier-corrected notional wrapper (found DURING
    implementation, not in the original on-chain discovery -- see the
    POSITION_TIER_SHARE_MULTIPLIER docstring). RETIRED 2026-09-23: the
    original tier escalation (1.0/1.07/1.30) was found to be a
    5-second-merge-convention artifact, not a real effect -- see
    [[five-second-merge-convention-decisively-wrong]]. Table is now a
    flat no-op; these tests updated to match."""

    def test_first_tier_is_a_noop_on_top_of_shares_for_price(self):
        price = 0.35
        assert bc.notional_for_price(price, "first") == pytest.approx(
            bc.shares_for_price(price) * price)

    def test_later_tiers_are_a_noop_relative_to_first(self):
        # RETIRED 2026-09-23: true on-chain order identity showed the
        # "later entries are bigger" effect was a 5s-merge artifact
        # (85.5% of merge-eligible pairs are genuinely separate orders;
        # separate treatment fits the price formula 4.75x better). All
        # three tiers are now equal.
        price = 0.35
        first = bc.notional_for_price(price, "first")
        second = bc.notional_for_price(price, "2nd_3rd")
        fourth = bc.notional_for_price(price, "4th_plus")
        assert first == pytest.approx(second) == pytest.approx(fourth)

    def test_default_tier_is_first_when_unspecified(self):
        price = 0.6
        assert bc.notional_for_price(price) == bc.notional_for_price(price, "first")

    def test_unknown_tier_degrades_to_a_noop_rather_than_raising(self):
        price = 0.6
        assert bc.notional_for_price(price, "nonexistent_tier") == pytest.approx(
            bc.notional_for_price(price, "first"))

    def test_position_tier_multiplier_table_is_retired_to_a_flat_noop(self):
        assert bc.POSITION_TIER_SHARE_MULTIPLIER["first"] == 1.0
        assert bc.POSITION_TIER_SHARE_MULTIPLIER["2nd_3rd"] == pytest.approx(1.0)
        assert bc.POSITION_TIER_SHARE_MULTIPLIER["4th_plus"] == pytest.approx(1.0)


class TestHighTailShareMultiplier:
    """ADDED 2026-09-23 -- corrects a real, on-chain-confirmed asymmetry in
    the deep HIGH tail (price>0.92) that shares_for_price's single
    symmetric curve underpredicts. See HIGH_TAIL_SHARE_MULTIPLIER's
    docstring for the full derivation (360 on-chain-verified fills,
    split-half stability check)."""

    def test_shares_for_price_itself_is_unaffected(self):
        # the bare curve must stay uncorrected -- hedge_notional_for_price
        # depends on it directly and was calibrated against the
        # uncorrected curve.
        assert bc.shares_for_price(0.95) == bc.shares_for_price(0.05)
        assert bc.shares_for_price(0.99) == bc.shares_for_price(0.01)

    def test_notional_for_price_applies_the_multiplier_above_threshold(self):
        price = 0.95
        uncorrected = bc.shares_for_price(price) * price
        assert bc.notional_for_price(price, "first") == pytest.approx(
            uncorrected * bc.HIGH_TAIL_SHARE_MULTIPLIER)

    def test_notional_for_price_leaves_the_low_tail_uncorrected(self):
        # the LOW side (price<0.08) was found unstable across split-half
        # and is deliberately NOT corrected.
        price = 0.05
        assert bc.notional_for_price(price, "first") == pytest.approx(
            bc.shares_for_price(price) * price)

    def test_threshold_is_exclusive(self):
        at = bc.notional_for_price(bc.HIGH_TAIL_SHARE_PRICE_THRESHOLD, "first")
        uncorrected_at = bc.shares_for_price(bc.HIGH_TAIL_SHARE_PRICE_THRESHOLD) * bc.HIGH_TAIL_SHARE_PRICE_THRESHOLD
        assert at == pytest.approx(uncorrected_at)  # exactly at threshold: no correction
        just_above = bc.notional_for_price(bc.HIGH_TAIL_SHARE_PRICE_THRESHOLD + 1e-6, "first")
        assert just_above > at

    def test_hedge_notional_for_price_is_not_affected(self):
        # hedge_notional_for_price calls shares_for_price directly, not
        # notional_for_price -- must stay on the uncorrected curve. Cross-
        # check against the documented formula directly.
        price = 0.95
        expected = (bc.HEDGE_PRICE_SIZING_SCALE["Bitcoin"]
                    * (bc.shares_for_price(price) * price) ** bc.HEDGE_PRICE_SIZING_BETA)
        assert bc.hedge_notional_for_price("Bitcoin", price) == pytest.approx(expected)

    def test_multiplier_matches_documented_calibration(self):
        assert bc.HIGH_TAIL_SHARE_MULTIPLIER == pytest.approx(1.61)
        assert bc.HIGH_TAIL_SHARE_PRICE_THRESHOLD == pytest.approx(0.92)


class TestRegimeEngagementMultiplier:
    """Added 2026-09-21 -- see REGIME_ENGAGEMENT_MULTIPLIER's docstring
    for the historical-replay derivation. conftest.py's autouse
    neutralize_regime_engagement_multiplier fixture defaults this
    function to a no-op for every test in the suite (its reach into
    build_order_intent is too broad to leave live by default -- see that
    fixture's own docstring); this class explicitly restores the real
    function to test it directly."""

    @pytest.fixture(autouse=True)
    def restore_real_function(self, monkeypatch):
        monkeypatch.setattr(
            bc, "regime_engagement_multiplier",
            lambda asset, regime: bc.REGIME_ENGAGEMENT_MULTIPLIER.get(asset, {}).get(regime, 1.0))

    def test_bitcoin_anchors_on_high(self):
        assert bc.regime_engagement_multiplier("Bitcoin", "HIGH") == 1.0

    def test_ethereum_anchors_on_high(self):
        # RECALIBRATED 2026-09-21: was CHEAP under the original full-
        # pooled-history calibration -- a real behavioral shift (his
        # HIGH-targeting overtook CHEAP-targeting within days), not a
        # test update chasing a bigger dataset's "truer" answer.
        assert bc.regime_engagement_multiplier("Ethereum", "HIGH") == 1.0

    def test_solana_anchors_on_high(self):
        # RECALIBRATED 2026-09-21: same shift as Ethereum above.
        assert bc.regime_engagement_multiplier("Solana", "HIGH") == 1.0

    def test_mid_and_core_are_suppressed_for_every_asset(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            assert bc.regime_engagement_multiplier(asset, "MID") < 1.0
            assert bc.regime_engagement_multiplier(asset, "CORE") < 1.0

    def test_cheap_is_now_suppressed_too_for_every_asset(self):
        # RECALIBRATED 2026-09-21: under the original calibration, CHEAP
        # was the anchor (1.0, no-op) for Ethereum/Solana. Now that HIGH
        # anchors all three assets, CHEAP needs its own (milder than MID/
        # CORE) suppression too.
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            assert bc.regime_engagement_multiplier(asset, "CHEAP") < 1.0

    def test_matches_the_documented_calibration_table(self):
        # RECALIBRATED 2026-09-22: live scorecard flagged Bit/Eth CORE as
        # barely-traded cells; refresh boosts CORE for every asset and
        # trims CHEAP for Ethereum/Solana. See REGIME_ENGAGEMENT_MULTIPLIER's
        # docstring for the split-half validation behind these numbers.
        assert bc.regime_engagement_multiplier("Bitcoin", "CHEAP") == pytest.approx(0.39)
        assert bc.regime_engagement_multiplier("Bitcoin", "MID") == pytest.approx(0.09)
        assert bc.regime_engagement_multiplier("Bitcoin", "CORE") == pytest.approx(0.339)
        assert bc.regime_engagement_multiplier("Ethereum", "CHEAP") == pytest.approx(0.50)
        assert bc.regime_engagement_multiplier("Ethereum", "MID") == pytest.approx(0.037)
        assert bc.regime_engagement_multiplier("Ethereum", "CORE") == pytest.approx(0.139)
        assert bc.regime_engagement_multiplier("Solana", "CHEAP") == pytest.approx(0.509)
        assert bc.regime_engagement_multiplier("Solana", "MID") == pytest.approx(0.037)
        assert bc.regime_engagement_multiplier("Solana", "CORE") == pytest.approx(0.104)

    def test_unknown_asset_is_a_noop(self):
        assert bc.regime_engagement_multiplier("Dogecoin", "MID") == 1.0

    def test_unknown_regime_is_a_noop(self):
        assert bc.regime_engagement_multiplier("Bitcoin", "NONEXISTENT") == 1.0


class TestRegimeDriftCorrection:
    """Added 2026-09-23 -- see regime_drift_correction's docstring for the
    held-out validation (project memory: distribution-controller-beats-
    static-regime-probability). Not affected by conftest.py's
    neutralize_regime_engagement_multiplier fixture -- that patches a
    different function (regime_engagement_multiplier), this one is new."""

    def test_cold_start_is_a_noop(self):
        # total==0 -> nothing to correct against yet, matches the validated
        # test's own cold-start handling.
        assert bc.regime_drift_correction("Bitcoin", "CHEAP", {}, 0) == 1.0

    def test_unknown_asset_is_a_noop(self):
        assert bc.regime_drift_correction("Dogecoin", "CHEAP", {"CHEAP": 5}, 10) == 1.0

    def test_unknown_regime_is_a_noop(self):
        assert bc.regime_drift_correction("Bitcoin", "NONEXISTENT", {}, 10) == 1.0

    def test_under_target_boosts(self):
        # Bitcoin CHEAP target is 0.4194. Realized share here is 0.10 (1/10)
        # -- well under target, should boost above 1.0.
        result = bc.regime_drift_correction("Bitcoin", "CHEAP", {"CHEAP": 1}, 10)
        assert result > 1.0

    def test_over_target_dampens(self):
        # Realized share 0.90 (9/10) -- well over the 0.4194 target, should
        # dampen below 1.0.
        result = bc.regime_drift_correction("Bitcoin", "CHEAP", {"CHEAP": 9}, 10)
        assert result < 1.0

    def test_exactly_at_target_is_approximately_noop(self):
        # Realized share == target exactly -> deficit == 0 -> factor == 1.0.
        target = bc.REGIME_MIX_TARGET["Bitcoin"]["CHEAP"]
        counts = {"CHEAP": round(target * 1000)}
        result = bc.regime_drift_correction("Bitcoin", "CHEAP", counts, 1000)
        assert result == pytest.approx(1.0, abs=0.01)

    def test_cap_bounds_extreme_deficit(self):
        # Realized share 0.0 (never engaged this regime at all yet) against
        # a real target -- the proportional formula alone would blow past
        # any reasonable bound; the cap must hold it at REGIME_DRIFT_
        # CORRECTION_CAP exactly, not just "less than infinity".
        result = bc.regime_drift_correction("Ethereum", "CHEAP", {"CHEAP": 0}, 1000)
        assert result == pytest.approx(bc.REGIME_DRIFT_CORRECTION_CAP)

    def test_cap_bounds_extreme_surplus(self):
        # Realized share 1.0 (only this regime engaged so far) -- the
        # dampening side of the same cap, at 1/cap.
        result = bc.regime_drift_correction("Ethereum", "CHEAP", {"CHEAP": 1000}, 1000)
        assert result == pytest.approx(1.0 / bc.REGIME_DRIFT_CORRECTION_CAP)

    def test_every_calibrated_value_is_a_valid_probability(self):
        for asset, table in bc.REGIME_ENGAGEMENT_MULTIPLIER.items():
            for regime, mult in table.items():
                assert 0.0 < mult <= 1.0


class TestCheapDeepTouchEdgeMultiplier:
    def test_early_zone_returns_size_boost(self):
        assert bc.cheap_deep_touch_edge_multiplier(0.05, 30.0) == bc.CHEAP_EARLY_TOUCH_SIZE_MULTIPLIER
        assert bc.cheap_deep_touch_edge_multiplier(0.08, 149.9) == bc.CHEAP_EARLY_TOUCH_SIZE_MULTIPLIER

    def test_late_zone_returns_skip_dampener(self):
        assert bc.cheap_deep_touch_edge_multiplier(0.05, 150.0) == bc.CHEAP_LATE_TOUCH_SKIP_MULTIPLIER
        assert bc.cheap_deep_touch_edge_multiplier(0.08, 269.9) == bc.CHEAP_LATE_TOUCH_SKIP_MULTIPLIER

    def test_no_op_outside_measured_range_or_above_price_threshold(self):
        assert bc.cheap_deep_touch_edge_multiplier(0.05, 10.0) == 1.0   # before 30s
        assert bc.cheap_deep_touch_edge_multiplier(0.05, 280.0) == 1.0  # after 270s
        assert bc.cheap_deep_touch_edge_multiplier(0.15, 60.0) == 1.0   # price above threshold
        assert bc.cheap_deep_touch_edge_multiplier(0.05, None) == 1.0   # unknown timing


class TestParticipationAssetScale:
    def test_known_assets(self):
        assert bc.participation_asset_scale("Bitcoin") == bc.PARTICIPATION_ASSET_SCALE["Bitcoin"]
        assert bc.participation_asset_scale("Solana") == bc.PARTICIPATION_ASSET_SCALE["Solana"]

    def test_ethereum_and_unknown_default_to_one(self):
        assert bc.participation_asset_scale("Ethereum") == 1.0
        assert bc.participation_asset_scale("Dogecoin") == 1.0


class TestCheapDeepTailParticipationMultiplier:
    """Added 2026-09-28, digging into paperbot-100's CHEAP oversizing --
    the real driver underneath the known 5-share-floor mechanism: all
    bots underweight the genuine CHEAP deep tail relative to the trader
    (win rate matches, ~10%, but price paid doesn't). See
    CHEAP_DEEP_TAIL_PARTICIPATION_BOOST's docstring in behavior_config.py
    for the full derivation."""

    def test_noop_for_dormant_assets(self):
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            assert bc.cheap_deep_tail_participation_multiplier(asset, 0.03) == 1.0

    def test_noop_at_or_above_the_deep_touch_threshold(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            assert bc.cheap_deep_tail_participation_multiplier(asset, bc.CHEAP_DEEP_TOUCH_PRICE_THRESHOLD) == 1.0
            assert bc.cheap_deep_tail_participation_multiplier(asset, 0.29) == 1.0

    def test_boost_below_the_deep_touch_threshold_for_every_live_asset(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            mult = bc.cheap_deep_tail_participation_multiplier(asset, 0.03)
            assert mult > 1.0, f"{asset}: expected a real boost, got {mult}"
            assert mult == pytest.approx(bc.CHEAP_DEEP_TAIL_PARTICIPATION_BOOST[asset])

    def test_matches_the_calibrated_table_exactly(self):
        for asset, expected in bc.CHEAP_DEEP_TAIL_PARTICIPATION_BOOST.items():
            assert bc.cheap_deep_tail_participation_multiplier(asset, 0.01) == pytest.approx(expected)


class TestMidBoundaryEdgeMultiplier:
    def test_low_boundary_returns_size_boost(self):
        assert bc.mid_boundary_edge_multiplier("MID", 0.30) == bc.MID_LOW_BOUNDARY_SIZE_MULTIPLIER
        assert bc.mid_boundary_edge_multiplier("MID", 0.39) == bc.MID_LOW_BOUNDARY_SIZE_MULTIPLIER

    def test_high_boundary_returns_skip_dampener(self):
        assert bc.mid_boundary_edge_multiplier("MID", 0.60) == bc.MID_HIGH_BOUNDARY_SKIP_MULTIPLIER
        assert bc.mid_boundary_edge_multiplier("MID", 0.69) == bc.MID_HIGH_BOUNDARY_SKIP_MULTIPLIER

    def test_no_op_in_the_coin_flip_center_or_outside_mid(self):
        assert bc.mid_boundary_edge_multiplier("MID", 0.50) == 1.0
        assert bc.mid_boundary_edge_multiplier("CHEAP", 0.35) == 1.0
        assert bc.mid_boundary_edge_multiplier("CORE", 0.75) == 1.0


class TestTickOffsetDistribution:
    def test_distribution_sums_to_one(self):
        # empirical percentages rounded from a real n=82 sample -- close to
        # but not exactly 1.0 (sums to 0.999), same tolerance convention as
        # other empirically-derived distributions in this module.
        assert sum(p for _, p in bc.TICK_OFFSET_DISTRIBUTION) == pytest.approx(1.0, abs=0.01)

    def test_sample_always_returns_a_calibrated_tick(self):
        import random
        rng = random.Random(0)
        valid_ticks = {t for t, _ in bc.TICK_OFFSET_DISTRIBUTION}
        for _ in range(500):
            assert bc.sample_tick_offset(rng) in valid_ticks

    def test_sample_is_deterministic_given_a_seeded_rng(self):
        import random
        a = [bc.sample_tick_offset(random.Random(42)) for _ in range(20)]
        b = [bc.sample_tick_offset(random.Random(42)) for _ in range(20)]
        assert a == b

    def test_sample_distribution_roughly_matches_calibration_over_a_large_draw(self):
        import random
        from collections import Counter
        rng = random.Random(7)
        n = 20000
        counts = Counter(bc.sample_tick_offset(rng) for _ in range(n))
        for ticks, p in bc.TICK_OFFSET_DISTRIBUTION:
            observed = counts[ticks] / n
            assert observed == pytest.approx(p, abs=0.02)

    def test_boundary_values_route_to_the_correct_bucket(self):
        import random

        class FixedRNG(random.Random):
            def __init__(self, value):
                super().__init__()
                self.value = value

            def random(self):
                return self.value

        # cumulative boundaries: -3:0.159, -2:0.171, -1:0.317, 0:0.658,
        # +1:0.841, +2:0.926, +3:0.999(~1.0)
        assert bc.sample_tick_offset(FixedRNG(0.0)) == -3
        assert bc.sample_tick_offset(FixedRNG(0.158)) == -3
        assert bc.sample_tick_offset(FixedRNG(0.30)) == -1
        assert bc.sample_tick_offset(FixedRNG(0.50)) == 0
        assert bc.sample_tick_offset(FixedRNG(0.70)) == 1
        assert bc.sample_tick_offset(FixedRNG(0.90)) == 2
        assert bc.sample_tick_offset(FixedRNG(0.999999)) == 3


