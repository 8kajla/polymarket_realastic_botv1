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

    def test_exact_values_for_the_three_calibrated_assets(self):
        assert bc.cross_market_side_persistence("Bitcoin") == 0.5538
        assert bc.cross_market_side_persistence("Ethereum") == 0.5265
        assert bc.cross_market_side_persistence("Solana") == 0.5429

    def test_all_calibrated_values_are_above_fifty_fifty(self):
        # The whole finding: persistence, not indifference.
        for asset, p in bc.CROSS_MARKET_SIDE_PERSISTENCE.items():
            assert 0.5 < p < 1.0, f"{asset} value {p} should be a real persistence above 50%"


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


class TestHedgeLiquidityMultiplier:
    """Liquidity-conditioned first-hedge trigger, added 2026-09-10 --
    confirmed real, well-powered (t=5.263, n=1023/322) correlation
    between a market's liquidity and whether he hedges it at all. Unlike
    TTC (a size signal) this is an ACTION-level measurement already (did
    he hedge or not), so it clears the same bar the price-velocity signal
    failed without needing a separate sizing-reaction check."""

    def test_neutral_for_assets_not_in_the_table(self):
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            assert bc.hedge_liquidity_multiplier(asset, 10000.0) == 1.0

    def test_neutral_when_liquidity_is_unavailable(self):
        assert bc.hedge_liquidity_multiplier("Bitcoin", None) == 1.0

    def test_exact_at_each_calibrated_point(self):
        for asset, curve in bc.HEDGE_LIQUIDITY_MULTIPLIER.items():
            for liq, expected in curve.items():
                got = bc.hedge_liquidity_multiplier(asset, float(liq))
                assert abs(got - expected) < 1e-9, f"{asset}@{liq}: {got} != {expected}"

    def test_interpolates_between_points(self):
        # Bitcoin: 13072->0.9577, 15927->1.0382 -- the midpoint liquidity
        # value must interpolate to (roughly) the midpoint ratio.
        mid_liq = (13072 + 15927) / 2
        mid_val = bc.hedge_liquidity_multiplier("Bitcoin", mid_liq)
        assert 0.9577 < mid_val < 1.0382

    def test_flat_beyond_the_measured_range_no_extrapolation(self):
        at_min = bc.hedge_liquidity_multiplier("Bitcoin", 8590.0)
        below_min = bc.hedge_liquidity_multiplier("Bitcoin", 100.0)
        assert at_min == below_min

        at_max = bc.hedge_liquidity_multiplier("Bitcoin", 19828.0)
        above_max = bc.hedge_liquidity_multiplier("Bitcoin", 1_000_000.0)
        assert at_max == above_max

    def test_higher_liquidity_means_higher_multiplier_for_every_live_asset(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            curve = bc.HEDGE_LIQUIDITY_MULTIPLIER[asset]
            lowest_liq = min(curve)
            highest_liq = max(curve)
            low_val = bc.hedge_liquidity_multiplier(asset, lowest_liq)
            high_val = bc.hedge_liquidity_multiplier(asset, highest_liq)
            assert high_val > low_val, f"{asset}: expected highest-liquidity point to score above lowest"


class TestAdverseMoveSizeMultiplier:
    """First-hedge-size-vs-adverse-move multiplier, added 2026-09-10 --
    real, cross-asset-generalizing finding that first-hedge SIZE scales
    with how far price has moved against the dominant side since entry.
    Controlled for first_entry_regime (fit separately per regime, still
    holds), and for the TTC confound (r(adverse_move, seconds_to_close)
    ~0 -- ruled out being a lateness proxy). Restricted to first-hedge-
    only data (HEDGE_CONTINUATION_SIZE_RATIO covers 2nd+ separately):
    BTC t=32.5 (n=8,670), ETH t=24.1 (n=6,387), SOL t=20.6 (n=8,237)."""

    def test_neutral_for_assets_not_in_the_table(self):
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            assert bc.adverse_move_size_multiplier(asset, 0.3) == 1.0

    def test_neutral_when_adverse_move_is_unavailable(self):
        assert bc.adverse_move_size_multiplier("Bitcoin", None) == 1.0

    def test_exact_at_each_calibrated_point(self):
        for asset, curve in bc.ADVERSE_MOVE_SIZE_MULTIPLIER.items():
            for move, expected in curve.items():
                got = bc.adverse_move_size_multiplier(asset, float(move))
                assert abs(got - expected) < 1e-9, f"{asset}@{move}: {got} != {expected}"

    def test_interpolates_between_points(self):
        # Bitcoin: -0.10->0.5621, 0.05->2.8117 -- the midpoint move value
        # must interpolate to (roughly) the midpoint multiplier.
        mid_move = (-0.10 + 0.05) / 2
        mid_val = bc.adverse_move_size_multiplier("Bitcoin", mid_move)
        assert 0.5621 < mid_val < 2.8117

    def test_flat_beyond_the_measured_range_no_extrapolation(self):
        at_min = bc.adverse_move_size_multiplier("Bitcoin", -0.35)
        below_min = bc.adverse_move_size_multiplier("Bitcoin", -0.99)
        assert at_min == below_min

        at_max = bc.adverse_move_size_multiplier("Bitcoin", 0.27)
        above_max = bc.adverse_move_size_multiplier("Bitcoin", 0.99)
        assert at_max == above_max

    def test_largest_adverse_move_scores_higher_than_most_favorable_move(self):
        # Not strictly monotonic (q2 > q3 for all three assets, kept
        # as-measured rather than smoothed) -- but the two extremes
        # (most-favorable vs most-adverse) are ordered correctly for
        # every live asset.
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            curve = bc.ADVERSE_MOVE_SIZE_MULTIPLIER[asset]
            most_favorable = min(curve)
            most_adverse = max(curve)
            low_val = bc.adverse_move_size_multiplier(asset, most_favorable)
            high_val = bc.adverse_move_size_multiplier(asset, most_adverse)
            assert high_val > low_val, f"{asset}: expected the most-adverse move to score above the most-favorable"

    def test_cap_is_not_accidentally_clipping_real_calibrated_values(self):
        for asset, curve in bc.ADVERSE_MOVE_SIZE_MULTIPLIER.items():
            assert max(curve.values()) < bc._ADVERSE_MOVE_SIZE_MULTIPLIER_CAP


class TestAdverseMoveContinuationSizeMultiplier:
    """Continuation-hedge (hedge_count>=1) extension of
    TestAdverseMoveSizeMultiplier, added 2026-09-10 same night. Deep-
    research pass before building: regime-controlled holds in 11/12 cells
    (Solana/HIGH is the one exception, t=2.46, thin n=108); TTC confound
    ruled out (r~0 across assets); since-ORIGIN reference beats a tested
    since-last-hedge alternative (r=0.44-0.57 vs r=0.13-0.23 pooled).
    Pooled across hedge indices 2/3/4+: BTC t=31.3/25.2/34.3, ETH
    t=20.0/17.6/24.1, SOL t=17.4/15.3/16.6 -- every cell clears the bar."""

    def test_neutral_for_assets_not_in_the_table(self):
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            assert bc.adverse_move_continuation_size_multiplier(asset, 0.3) == 1.0

    def test_neutral_when_adverse_move_is_unavailable(self):
        assert bc.adverse_move_continuation_size_multiplier("Bitcoin", None) == 1.0

    def test_exact_at_each_calibrated_point(self):
        for asset, curve in bc.ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER.items():
            for move, expected in curve.items():
                got = bc.adverse_move_continuation_size_multiplier(asset, float(move))
                assert abs(got - expected) < 1e-9, f"{asset}@{move}: {got} != {expected}"

    def test_interpolates_between_points(self):
        # Bitcoin: -0.09->0.6799, 0.12->2.5175 -- the midpoint move value
        # must interpolate to (roughly) the midpoint multiplier.
        mid_move = (-0.09 + 0.12) / 2
        mid_val = bc.adverse_move_continuation_size_multiplier("Bitcoin", mid_move)
        assert 0.6799 < mid_val < 2.5175

    def test_flat_beyond_the_measured_range_no_extrapolation(self):
        at_min = bc.adverse_move_continuation_size_multiplier("Bitcoin", -0.41)
        below_min = bc.adverse_move_continuation_size_multiplier("Bitcoin", -0.99)
        assert at_min == below_min

        at_max = bc.adverse_move_continuation_size_multiplier("Bitcoin", 0.34)
        above_max = bc.adverse_move_continuation_size_multiplier("Bitcoin", 0.99)
        assert at_max == above_max

    def test_largest_adverse_move_scores_higher_than_most_favorable_move(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            curve = bc.ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER[asset]
            most_favorable = min(curve)
            most_adverse = max(curve)
            low_val = bc.adverse_move_continuation_size_multiplier(asset, most_favorable)
            high_val = bc.adverse_move_continuation_size_multiplier(asset, most_adverse)
            assert high_val > low_val, f"{asset}: expected the most-adverse move to score above the most-favorable"

    def test_cap_is_not_accidentally_clipping_real_calibrated_values(self):
        for asset, curve in bc.ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER.items():
            assert max(curve.values()) < bc._ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER_CAP


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


class TestWeekendHedgeMultiplier:
    """Weekend-conditioned first-hedge trigger, added 2026-09-10 --
    real, well-powered (z=9.9, n=10,183/3,902) finding that his
    dual-sided (hedged) rate is higher on weekends, with a fully
    confirmed causal chain (lower weekend volatility -> more MID/
    undecided markets -> more hedging). Per-market binary measurement,
    not a hedge-count, so immune to the raw-fill-vs-decision
    fragmentation bug that ruled out raising MAX_HEDGE_COUNT_PER_MARKET
    the same night. BTC-only -- ETH/SOL were checked and don't show this
    shift."""

    def test_neutral_on_weekdays(self):
        assert bc.weekend_hedge_multiplier("Bitcoin", False) == 1.0

    def test_neutral_when_is_weekend_is_none(self):
        assert bc.weekend_hedge_multiplier("Bitcoin", None) == 1.0

    def test_neutral_for_assets_not_in_the_table(self):
        for asset in ("Ethereum", "Solana", "Dogecoin", "Hyperliquid", "BNB"):
            assert bc.weekend_hedge_multiplier(asset, True) == 1.0

    def test_bitcoin_weekend_multiplier_matches_the_calibrated_ratio(self):
        got = bc.weekend_hedge_multiplier("Bitcoin", True)
        expected = 68.2 / 58.87
        assert abs(got - expected) < 1e-9

    def test_weekend_multiplier_is_greater_than_one(self):
        # Real finding: hedging is MORE common on weekends, not less.
        assert bc.weekend_hedge_multiplier("Bitcoin", True) > 1.0


class TestResumptionSizeMultiplier:
    """Resumption-caution ramp, added 2026-09-10 -- a real, precise
    3-phase shape (suppressed 0-6h, overshoot 7-9h, back to baseline
    10h+) found from the largest known real silence this session. See
    its docstring for the confirmed scope caveat: only gaps >=
    RESUMPTION_GAP_THRESHOLD_HOURS qualify -- smaller gaps were
    separately checked and found to show no such reset."""

    def test_neutral_when_no_resumption_is_tracked(self):
        assert bc.resumption_size_multiplier(None) == 1.0

    def test_neutral_for_a_negative_value(self):
        # Defensive: should never happen in practice, but must not crash
        # or misbehave if it somehow does.
        assert bc.resumption_size_multiplier(-1.0) == 1.0

    def test_suppressed_in_the_early_hours(self):
        # Hours 0-6: confirmed 15-35% below baseline -- must be < 1.0.
        assert bc.resumption_size_multiplier(0.0) < 1.0
        assert bc.resumption_size_multiplier(3.0) < 1.0

    def test_overshoots_in_the_middle_window(self):
        # Hours 7-9: confirmed 68-138% ABOVE baseline -- must be > 1.0,
        # and clearly higher than the early suppressed phase.
        early = bc.resumption_size_multiplier(3.0)
        overshoot = bc.resumption_size_multiplier(8.0)
        assert overshoot > 1.0
        assert overshoot > early

    def test_genuinely_neutral_beyond_the_measured_window(self):
        # Hour 10+: settles into noisy oscillation around baseline --
        # a STRICT 1.0 no-op, not clamped to hour-10's specific ratio
        # the way TTC/liquidity clamp to their edge values.
        assert bc.resumption_size_multiplier(10.0) == 1.0
        assert bc.resumption_size_multiplier(50.0) == 1.0

    def test_interpolates_smoothly_between_calibrated_points(self):
        # Between hour 3 (0.72) and hour 8 (1.90), the value at hour 5.5
        # (the midpoint) must land between them.
        low = bc.resumption_size_multiplier(3.0)
        mid = bc.resumption_size_multiplier(5.5)
        high = bc.resumption_size_multiplier(8.0)
        assert low < mid < high


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
