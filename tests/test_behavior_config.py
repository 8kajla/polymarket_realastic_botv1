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
        values = {
            asset: bc.ENTRY_SIZING_USD[asset]["HIGH"]["4th_plus"]
            for asset in bc.ASSET_NAMES
        }
        assert len(set(values.values())) == len(values)

    def test_bitcoin_and_dogecoin_curves_are_meaningfully_different(self):
        btc = bc.ENTRY_SIZING_USD["Bitcoin"]["CORE"]["first"]
        doge = bc.ENTRY_SIZING_USD["Dogecoin"]["CORE"]["first"]
        assert abs(btc - doge) > 1.0

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

    def test_side_persistence_for_matches_the_table(self):
        for asset, by_regime in bc.SIDE_PERSISTENCE.items():
            for regime, p in by_regime.items():
                assert bc.side_persistence_for(asset, regime) == p

    def test_side_persistence_for_raises_on_unknown_regime(self):
        with pytest.raises(bc.BehaviorLookupError):
            bc.side_persistence_for("Bitcoin", "NOT_A_REGIME")


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
        for asset in ("Ethereum", "Solana", "Dogecoin"):
            for regime in bc.REGIME_NAMES:
                first = bc.floor_lot_probability(asset, regime, "first")
                mid = bc.floor_lot_probability(asset, regime, "2nd_3rd")
                last = bc.floor_lot_probability(asset, regime, "4th_plus")
                assert first == mid == last

    def test_unknown_position_tier_rejected(self):
        with pytest.raises(ValueError):
            bc.floor_lot_probability("Bitcoin", "CHEAP", "5th")


class TestWithinBandSizeMultiplier:
    """Continuous price-vs-size scaling within CORE/HIGH, added 2026-09-08.
    Verified cell-by-cell (asset, regime, position_tier) before trusting it
    wasn't just position_tier correlating with price -- see
    behavior_config.WITHIN_BAND_SIZE_SLOPE's docstring."""

    def test_neutral_everywhere_not_specifically_calibrated(self):
        # CHEAP/MID, and the three dormant assets even in CORE/HIGH, must
        # be an exact no-op -- this feature was only verified for
        # Bitcoin/Ethereum/Solana's CORE and HIGH bands.
        for asset in bc.ASSET_NAMES:
            for regime in ("CHEAP", "MID"):
                assert bc.within_band_size_multiplier(asset, regime, 0.5) == 1.0
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            for regime in ("CORE", "HIGH"):
                assert bc.within_band_size_multiplier(asset, regime, 0.8) == 1.0

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
        # Ethereum HIGH has the steepest slope (21.09) -- at the extreme
        # edge of the band this must still be bounded, not blow up.
        mult = bc.within_band_size_multiplier("Ethereum", "HIGH", 1.0)
        assert mult <= 5.0


class TestTtcSizeMultiplier:
    """Time-to-close size scaling, added 2026-09-10 -- confirmed the
    trader actually SIZES differently by time remaining (not just present
    more late-window), unlike a separately-tested price-velocity signal
    that was checked the same way and rejected for not being something he
    actually acts on. See TTC_SIZE_MULTIPLIER's docstring for the real
    numbers this was calibrated from."""

    def test_neutral_for_assets_not_in_the_table(self):
        # Dogecoin/Hyperliquid/BNB aren't live-traded -- no data, must
        # stay a strict no-op regardless of regime/ttc.
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            for regime in ("CHEAP", "MID", "CORE", "HIGH"):
                assert bc.ttc_size_multiplier(asset, regime, 150.0) == 1.0

    def test_neutral_for_an_unrecognized_regime(self):
        assert bc.ttc_size_multiplier("Bitcoin", "NOT_A_REGIME", 150.0) == 1.0

    def test_exact_at_each_calibrated_midpoint(self):
        # At exactly a bucket midpoint, the interpolated value must equal
        # the calibrated value exactly (not an interpolation artifact).
        for asset, regimes in bc.TTC_SIZE_MULTIPLIER.items():
            for regime, curve in regimes.items():
                for midpoint, expected in curve.items():
                    got = bc.ttc_size_multiplier(asset, regime, float(midpoint))
                    assert abs(got - expected) < 1e-9, f"{asset}/{regime}@{midpoint}: {got} != {expected}"

    def test_interpolates_between_midpoints(self):
        # Bitcoin/HIGH: 210->0.6673, 150->0.9282 -- the midpoint of those
        # two (ttc=180) must fall exactly between them.
        low = bc.ttc_size_multiplier("Bitcoin", "HIGH", 210.0)
        high = bc.ttc_size_multiplier("Bitcoin", "HIGH", 150.0)
        mid = bc.ttc_size_multiplier("Bitcoin", "HIGH", 180.0)
        assert abs(mid - (low + high) / 2) < 1e-9

    def test_flat_beyond_the_measured_range_no_extrapolation(self):
        # Below 90s or above 270s must clamp to the nearest endpoint, not
        # extrapolate past what the data supports.
        at_90 = bc.ttc_size_multiplier("Bitcoin", "HIGH", 90.0)
        below_90 = bc.ttc_size_multiplier("Bitcoin", "HIGH", 10.0)
        assert at_90 == below_90

        at_270 = bc.ttc_size_multiplier("Bitcoin", "HIGH", 270.0)
        above_270 = bc.ttc_size_multiplier("Bitcoin", "HIGH", 295.0)
        assert at_270 == above_270

    def test_high_regime_grows_toward_close_for_every_live_asset(self):
        # The cleanest, most consistent real signal found: HIGH-band size
        # grows sharply as close approaches, for all three live assets.
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            early = bc.ttc_size_multiplier(asset, "HIGH", 270.0)
            late = bc.ttc_size_multiplier(asset, "HIGH", 90.0)
            assert late > early, f"{asset}: expected HIGH size to grow toward close"

    def test_cheap_regime_shrinks_toward_close_for_every_live_asset(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            early = bc.ttc_size_multiplier(asset, "CHEAP", 270.0)
            late = bc.ttc_size_multiplier(asset, "CHEAP", 90.0)
            assert late < early, f"{asset}: expected CHEAP size to shrink toward close"
        mult_low = bc.within_band_size_multiplier("Ethereum", "HIGH", 0.90)
        assert mult_low >= 1.0 / 5.0


class TestHedgeCalibration:
    """Confirmed from the full historical dataset: dual-sided markets have
    a dramatically better worst-case outcome than single-sided ones
    (-20.5% of stake on average vs -100%), and 24.9% are outright
    arbitrage. All six assets and four regimes must have real, distinct
    calibration data -- not a single shared fallback."""

    def test_all_six_assets_and_four_regimes_present_for_trigger_probability(self):
        assert set(bc.HEDGE_TRIGGER_PROBABILITY.keys()) == set(bc.ASSET_NAMES)
        for asset in bc.ASSET_NAMES:
            assert set(bc.HEDGE_TRIGGER_PROBABILITY[asset].keys()) == set(bc.REGIME_NAMES)

    def test_all_six_assets_and_four_regimes_present_for_size_ratio(self):
        assert set(bc.HEDGE_SIZE_RATIO.keys()) == set(bc.ASSET_NAMES)
        for asset in bc.ASSET_NAMES:
            assert set(bc.HEDGE_SIZE_RATIO[asset].keys()) == set(bc.REGIME_NAMES)

    def test_probabilities_are_valid_and_asset_specific(self):
        cheap_probs = {a: bc.hedge_trigger_probability(a, "CHEAP") for a in bc.ASSET_NAMES}
        for p in cheap_probs.values():
            assert 0.0 <= p <= 1.0
        assert len(set(cheap_probs.values())) == len(cheap_probs)

    def test_high_regime_has_the_lowest_trigger_probability_everywhere(self):
        """Confirmed pattern: a HIGH-band primary entry is already so
        confident that a hedge is added far less often than from any
        other regime, consistently across every asset."""
        for asset in bc.ASSET_NAMES:
            high_p = bc.hedge_trigger_probability(asset, "HIGH")
            for regime in ("CHEAP", "MID", "CORE"):
                assert high_p <= bc.hedge_trigger_probability(asset, regime)

    def test_unknown_asset_or_regime_falls_back_safely(self):
        assert bc.hedge_trigger_probability("NotAnAsset", "CHEAP") == 0.0
        assert bc.hedge_size_ratio("NotAnAsset", "CHEAP") == bc._DEFAULT_HEDGE_SIZE_RATIO
