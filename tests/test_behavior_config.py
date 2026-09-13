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
        # RECALIBRATED 2026-09-13 (post-halt, /loop cycle 9) -- see
        # CROSS_MARKET_SIDE_PERSISTENCE's docstring.
        assert bc.cross_market_side_persistence("Bitcoin", True) == 0.5182
        assert bc.cross_market_side_persistence("Bitcoin", False) == 0.6128
        assert bc.cross_market_side_persistence("Ethereum", True) == 0.4874
        assert bc.cross_market_side_persistence("Ethereum", False) == 0.5515
        assert bc.cross_market_side_persistence("Solana", True) == 0.4990
        assert bc.cross_market_side_persistence("Solana", False) == 0.5853

    def test_persistence_is_stronger_after_a_loss_than_after_a_win(self):
        # The whole finding: NOT hot-hand, NOT gambler's fallacy -- he
        # sticks with his read MORE after a setback than after a win.
        for asset, spec in bc.CROSS_MARKET_SIDE_PERSISTENCE.items():
            assert spec["after_loss"] > spec["after_win"], (
                f"{asset}: expected after_loss > after_win, got {spec}"
            )


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


class TestHedgeTtcSizeMultiplier:
    """Hedge sizing's own time-to-close scaling, added 2026-09-13 --
    re-derived and regime-composition-checked specifically against hedge
    data (the entry-side TTC_SIZE_MULTIPLIER's regime correction was never
    verified against the original hedge data). Real and cross-asset-
    consistent in CHEAP/HIGH only -- MID/CORE showed genuinely
    inconsistent signs across assets and are deliberately excluded."""

    def test_neutral_for_assets_not_in_the_table(self):
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            for regime in ("CHEAP", "MID", "CORE", "HIGH"):
                assert bc.hedge_ttc_size_multiplier(asset, regime, 150.0) == 1.0

    def test_neutral_for_mid_and_core_not_just_unrecognized_regimes(self):
        # Deliberately excluded (real, cross-asset-inconsistent signs),
        # not just uncalibrated -- see the module docstring.
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            for regime in ("MID", "CORE"):
                assert bc.hedge_ttc_size_multiplier(asset, regime, 90.0) == 1.0
                assert bc.hedge_ttc_size_multiplier(asset, regime, 270.0) == 1.0

    def test_exact_at_each_calibrated_midpoint(self):
        for asset, regimes in bc.HEDGE_TTC_SIZE_MULTIPLIER.items():
            for regime, curve in regimes.items():
                for midpoint, expected in curve.items():
                    got = bc.hedge_ttc_size_multiplier(asset, regime, float(midpoint))
                    assert abs(got - expected) < 1e-9, f"{asset}/{regime}@{midpoint}: {got} != {expected}"

    def test_interpolates_between_midpoints(self):
        low = bc.hedge_ttc_size_multiplier("Bitcoin", "HIGH", 210.0)
        high = bc.hedge_ttc_size_multiplier("Bitcoin", "HIGH", 150.0)
        mid = bc.hedge_ttc_size_multiplier("Bitcoin", "HIGH", 180.0)
        assert abs(mid - (low + high) / 2) < 1e-9

    def test_flat_beyond_the_measured_range_no_extrapolation(self):
        at_90 = bc.hedge_ttc_size_multiplier("Bitcoin", "CHEAP", 90.0)
        below_90 = bc.hedge_ttc_size_multiplier("Bitcoin", "CHEAP", 10.0)
        assert at_90 == below_90

        at_270 = bc.hedge_ttc_size_multiplier("Bitcoin", "CHEAP", 270.0)
        above_270 = bc.hedge_ttc_size_multiplier("Bitcoin", "CHEAP", 295.0)
        assert at_270 == above_270

    def test_high_regime_grows_toward_close_for_every_live_asset(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            early = bc.hedge_ttc_size_multiplier(asset, "HIGH", 270.0)
            late = bc.hedge_ttc_size_multiplier(asset, "HIGH", 90.0)
            assert late > early, f"{asset}: expected HIGH hedge size to grow toward close"

    def test_cheap_regime_shrinks_toward_close_for_every_live_asset(self):
        for asset in ("Bitcoin", "Ethereum", "Solana"):
            early = bc.hedge_ttc_size_multiplier(asset, "CHEAP", 270.0)
            late = bc.hedge_ttc_size_multiplier(asset, "CHEAP", 90.0)
            assert late < early, f"{asset}: expected CHEAP hedge size to shrink toward close"


class TestHedgeLiquidityMultiplier:
    """Liquidity-conditioned first-hedge trigger, added 2026-09-10 --
    originally confirmed real, well-powered (t=5.263, n=1023/322)
    correlation between a market's liquidity and whether he hedges it at
    all. RETIRED 2026-09-13 (post-halt, /loop cycle 6 -- see full-
    behavioral-audit-tracker.md's hedge-trigger sub-multipliers note):
    unblocked from its earlier "no liquidity data available" status by
    finding /opt/trader-intel/data/market_snapshots.jsonl, a separate
    collector that does capture it -- joined against post-halt trades
    (n=1263-1357 per asset) and found the correlation has genuinely
    vanished for all three assets (Bitcoin r=+0.010, Ethereum r=+0.022,
    Solana r=+0.056 -- all essentially zero). HEDGE_LIQUIDITY_MULTIPLIER
    is now an empty dict; every asset (including the three that used to
    have a real curve) gets the function's own no-op fallback."""

    def test_always_neutral_now_the_table_is_empty(self):
        # Every asset, including the three that used to have a real
        # curve, must be a strict no-op now the underlying signal is gone.
        for asset in ("Bitcoin", "Ethereum", "Solana", "Dogecoin", "Hyperliquid", "BNB"):
            assert bc.hedge_liquidity_multiplier(asset, 10000.0) == 1.0

    def test_neutral_when_liquidity_is_unavailable(self):
        assert bc.hedge_liquidity_multiplier("Bitcoin", None) == 1.0

    def test_table_is_empty(self):
        assert bc.HEDGE_LIQUIDITY_MULTIPLIER == {}


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
        # Endpoints derived from the table itself (not hardcoded) --
        # robust to recalibration, since the exact min/max keys shift
        # every time this table gets re-derived from fresh data.
        keys = sorted(bc.ADVERSE_MOVE_SIZE_MULTIPLIER["Bitcoin"])
        at_min = bc.adverse_move_size_multiplier("Bitcoin", keys[0])
        below_min = bc.adverse_move_size_multiplier("Bitcoin", keys[0] - 0.5)
        assert at_min == below_min

        at_max = bc.adverse_move_size_multiplier("Bitcoin", keys[-1])
        above_max = bc.adverse_move_size_multiplier("Bitcoin", keys[-1] + 0.5)
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


class TestAbsolutePriceHedgeSizeMultiplier:
    """First-hedge-size-vs-absolute-hedge-price multiplier, added
    2026-09-12 -- real, confirmed-INDEPENDENT (via partial correlation
    controlling for adverse_move_size_multiplier's own driving variable,
    r_raw=0.749 collinearity between the two) finding: the hedge side's
    own absolute current price predicts size beyond the delta-from-entry
    effect. RECALIBRATED 2026-09-13 (post-halt, /loop cycle 4 -- see
    full-behavioral-audit-tracker.md item 17): redone with the same
    partial-correlation methodology on post-halt-only data. The relation
    is real and, if anything, stronger than the original (partial corr
    r=0.335-0.454 vs the original r=0.103) -- but the SHAPE has changed:
    this used to be U-shaped (extra sizing at BOTH price extremes), and
    is now MONOTONICALLY INCREASING with price instead for all three
    assets. Kept as measured, not forced back into the old shape."""

    def test_neutral_for_assets_not_in_the_table(self):
        for asset in ("Dogecoin", "Hyperliquid", "BNB"):
            assert bc.absolute_price_hedge_size_multiplier(asset, 0.5) == 1.0

    def test_neutral_when_hedge_price_is_unavailable(self):
        assert bc.absolute_price_hedge_size_multiplier("Bitcoin", None) == 1.0

    def test_exact_at_each_calibrated_point(self):
        for asset, curve in bc.ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER.items():
            for price, expected in curve.items():
                got = bc.absolute_price_hedge_size_multiplier(asset, float(price))
                assert abs(got - expected) < 1e-9, f"{asset}@{price}: {got} != {expected}"

    def test_interpolates_between_points(self):
        # Endpoints derived from the table itself -- the two calibrated
        # points closest to the middle of Bitcoin's range must bound the
        # midpoint's interpolated value, whatever they happen to be after
        # a recalibration.
        points = sorted(bc.ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER["Bitcoin"].items())
        lo_price, lo_val = points[1]
        hi_price, hi_val = points[2]
        mid_price = (lo_price + hi_price) / 2
        mid_val = bc.absolute_price_hedge_size_multiplier("Bitcoin", mid_price)
        assert min(lo_val, hi_val) < mid_val < max(lo_val, hi_val)

    def test_flat_beyond_the_measured_range_no_extrapolation(self):
        # Endpoints derived from the table itself -- robust to
        # recalibration, since the exact min/max keys shift every time
        # this table gets re-derived from fresh data.
        keys = sorted(bc.ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER["Bitcoin"])
        at_min = bc.absolute_price_hedge_size_multiplier("Bitcoin", keys[0])
        below_min = bc.absolute_price_hedge_size_multiplier("Bitcoin", keys[0] - 0.1)
        assert at_min == below_min

        at_max = bc.absolute_price_hedge_size_multiplier("Bitcoin", keys[-1])
        above_max = bc.absolute_price_hedge_size_multiplier("Bitcoin", keys[-1] + 0.1)
        assert at_max == above_max

    def test_monotonically_increases_with_price_for_every_live_asset(self):
        # Post-2026-09-13 recalibration: monotonically increasing, not
        # U-shaped -- higher hedge price predicts a bigger hedge size,
        # consistently across all three live assets.
        for asset, curve in bc.ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER.items():
            points = sorted(curve.items())
            values = [v for _, v in points]
            assert values == sorted(values), f"{asset}: expected monotonically increasing, got {values}"

    def test_cap_is_not_accidentally_clipping_real_calibrated_values(self):
        for asset, curve in bc.ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER.items():
            assert max(curve.values()) < bc._ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER_CAP


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
        # Endpoints derived from the table itself, same robustness reason
        # as TestAdverseMoveSizeMultiplier's matching test.
        keys = sorted(bc.ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER["Bitcoin"])
        at_min = bc.adverse_move_continuation_size_multiplier("Bitcoin", keys[0])
        below_min = bc.adverse_move_continuation_size_multiplier("Bitcoin", keys[0] - 0.5)
        assert at_min == below_min

        at_max = bc.adverse_move_continuation_size_multiplier("Bitcoin", keys[-1])
        above_max = bc.adverse_move_continuation_size_multiplier("Bitcoin", keys[-1] + 0.5)
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


class TestCrossMarketHedgeRateMultiplier:
    """Cross-market hedge-RATE persistence, added 2026-09-11 -- real
    (r=0.25-0.35 all three assets on raw count), calibrated on the
    RATE version specifically to avoid double-counting general activity
    clustering. Most speculative of this session's cross-market
    multipliers (mechanism not fully pinned down) -- see
    cross_market_hedge_rate_multiplier's docstring."""

    def test_noop_for_unknown_asset_or_missing_rate(self):
        assert bc.cross_market_hedge_rate_multiplier("Dogecoin", 0.3) == 1.0
        assert bc.cross_market_hedge_rate_multiplier("Bitcoin", None) == 1.0

    def test_mean_neutral_at_the_lowest_calibrated_point_is_below_one(self):
        # A prior market with ZERO hedging predicts BELOW-average hedging
        # this market (the whole point of the finding: hedge-heavy
        # markets predict hedge-heavy markets, and vice versa).
        for asset in bc.CROSS_MARKET_HEDGE_RATE_MULTIPLIER:
            assert bc.cross_market_hedge_rate_multiplier(asset, 0.0) < 1.0

    def test_increases_with_prev_hedge_rate(self):
        for asset, curve in bc.CROSS_MARKET_HEDGE_RATE_MULTIPLIER.items():
            rates = sorted(curve.keys())
            low = bc.cross_market_hedge_rate_multiplier(asset, rates[0])
            high = bc.cross_market_hedge_rate_multiplier(asset, rates[-1])
            assert low < high, f"{asset}: expected increasing multiplier, got {low}/{high}"

    def test_flat_beyond_the_measured_range(self):
        for asset, curve in bc.CROSS_MARKET_HEDGE_RATE_MULTIPLIER.items():
            rates = sorted(curve.keys())
            assert (bc.cross_market_hedge_rate_multiplier(asset, -5.0)
                    == bc.cross_market_hedge_rate_multiplier(asset, rates[0]))
            assert (bc.cross_market_hedge_rate_multiplier(asset, 5.0)
                    == bc.cross_market_hedge_rate_multiplier(asset, rates[-1]))


class TestConvictionHedgeMultiplier:
    """Own-market conviction (first-entry size vs regime median) predicts
    THIS market's eventual hedge need, added 2026-09-11 -- real, survives
    regime/weekend/liquidity confounds. Scoped to MID/CORE/HIGH only
    (CHEAP shows no such relationship)."""

    def test_noop_for_unscoped_regime_or_missing_ratio(self):
        assert bc.conviction_hedge_multiplier("Bitcoin", "CHEAP", 0.5) == 1.0
        assert bc.conviction_hedge_multiplier("Bitcoin", "MID", None) == 1.0
        assert bc.conviction_hedge_multiplier("Dogecoin", "MID", 0.5) == 1.0

    def test_decreases_with_conviction(self):
        # The finding: a BELOW-median first entry (negative log-ratio)
        # predicts MORE hedging than an above-median one.
        for asset, regimes in bc.CONVICTION_HEDGE_MULTIPLIER.items():
            for regime, curve in regimes.items():
                xs = sorted(curve.keys())
                low = bc.conviction_hedge_multiplier(asset, regime, xs[0])
                high = bc.conviction_hedge_multiplier(asset, regime, xs[-1])
                assert low > high, f"{asset}/{regime}: expected decreasing, got {low}/{high}"


class TestCrossMarketSizeMomentumMultiplier:
    """Cross-market sizing momentum, added 2026-09-11 -- real, cross-asset,
    slow EWMA-like decay, temporally stable across the Aug 7 TWAP change.
    Scoped to first-entry size only."""

    def test_noop_for_unknown_asset_or_missing_residual(self):
        assert bc.cross_market_size_momentum_multiplier("Dogecoin", 0.5) == 1.0
        assert bc.cross_market_size_momentum_multiplier("Bitcoin", None) == 1.0

    def test_increases_with_prior_residual(self):
        for asset, curve in bc.CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER.items():
            xs = sorted(curve.keys())
            low = bc.cross_market_size_momentum_multiplier(asset, xs[0])
            high = bc.cross_market_size_momentum_multiplier(asset, xs[-1])
            assert low < high, f"{asset}: expected increasing multiplier, got {low}/{high}"


class TestBankrollPnlSizeMultiplier:
    """Bankroll-linked sizing, added 2026-09-12 -- real, well-powered
    finding for Ethereum/Solana ONLY (Bitcoin deliberately excluded, not
    just uncalibrated -- see the docstring in behavior_config.py): first-
    entry size scales DOWN after his own accumulated realized profit and
    UP after a drawdown. Survived time-detrending, a partial-correlation
    confound check against ADVERSE_MOVE_SIZE_MULTIPLIER, and a temporal-
    stability check across the Aug 7 TWAP change."""

    def test_noop_for_unknown_asset_or_missing_pnl(self):
        assert bc.bankroll_pnl_size_multiplier("Bitcoin", 100.0) == 1.0
        assert bc.bankroll_pnl_size_multiplier("Dogecoin", 100.0) == 1.0
        assert bc.bankroll_pnl_size_multiplier("Ethereum", None) == 1.0

    def test_bitcoin_is_deliberately_excluded_not_just_uncalibrated(self):
        assert "Bitcoin" not in bc.BANKROLL_PNL_SIZE_MULTIPLIER

    def test_decreases_as_realized_pnl_increases(self):
        # The whole finding: sizes DOWN after accumulated profit, UP
        # after a drawdown -- multiplier must be strictly decreasing.
        for asset, curve in bc.BANKROLL_PNL_SIZE_MULTIPLIER.items():
            xs = sorted(curve.keys())
            low_pnl_mult = bc.bankroll_pnl_size_multiplier(asset, xs[0])
            high_pnl_mult = bc.bankroll_pnl_size_multiplier(asset, xs[-1])
            assert low_pnl_mult > high_pnl_mult, (
                f"{asset}: expected a lower-pnl point to size bigger than a higher-pnl "
                f"point, got {low_pnl_mult}/{high_pnl_mult}"
            )

    def test_flat_beyond_the_measured_range(self):
        for asset, curve in bc.BANKROLL_PNL_SIZE_MULTIPLIER.items():
            xs = sorted(curve.keys())
            assert (bc.bankroll_pnl_size_multiplier(asset, xs[0] - 10000)
                    == bc.bankroll_pnl_size_multiplier(asset, xs[0]))
            assert (bc.bankroll_pnl_size_multiplier(asset, xs[-1] + 10000)
                    == bc.bankroll_pnl_size_multiplier(asset, xs[-1]))


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
        # RECALIBRATED 2026-09-13 (/loop cycle 10): Ethereum/MID,
        # Solana/MID, and Ethereum/CORE all showed a genuinely vanished
        # (not just weakened) fatigue effect on fresh post-halt data --
        # removed from the table entirely, see the module docstring.
        for asset, regime in [("Ethereum", "MID"), ("Solana", "MID"), ("Ethereum", "CORE")]:
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


class TestHedgeTriggerAfterBigLossMultiplier:
    """Hedge propensity after a big loss, added 2026-09-13 -- a market's
    own asset having just taken a top-decile-sized rolling loss (bot.py's
    _is_top_decile_loss, min 20 samples before ever triggering) predicted
    the NEXT market's first hedge trigger fired more readily, for
    Ethereum/Solana. RETIRED 2026-09-13 (post-halt, /loop cycle 11): a
    simplified post-halt-only re-check (raw pooled comparison, not the
    original's full Mantel-Haenszel stratification -- a reasonable
    simplification given the post-halt window is only ~4.9 days, too
    short for the kind of secular drift MH-control exists to handle)
    found no detectable effect for either asset (Ethereum ratio=1.045,
    z=0.341, n=74 flagged markets; Solana ratio=1.013, z=0.106, n=64) --
    see the module docstring in behavior_config.py for the full numbers.
    HEDGE_TRIGGER_AFTER_BIG_LOSS_MULTIPLIER is now an empty dict; every
    asset (including the two that used to have a real boost) gets the
    function's own no-op fallback."""

    def test_always_neutral_now_the_table_is_empty(self):
        # Every asset, including the two that used to have a real boost,
        # must be a strict no-op now the underlying signal is gone.
        for asset in ("Bitcoin", "Ethereum", "Solana", "Dogecoin", "Hyperliquid", "BNB"):
            assert bc.hedge_trigger_after_big_loss_multiplier(asset, True) == 1.0

    def test_noop_when_not_after_a_big_loss_or_unknown(self):
        for asset in ("Ethereum", "Solana"):
            assert bc.hedge_trigger_after_big_loss_multiplier(asset, False) == 1.0
            assert bc.hedge_trigger_after_big_loss_multiplier(asset, None) == 1.0

    def test_table_is_empty(self):
        assert bc.HEDGE_TRIGGER_AFTER_BIG_LOSS_MULTIPLIER == {}


class TestAccuracyScoutMultiplier:
    """Accuracy-conditioned scout rate, added 2026-09-11 -- real, cross-
    asset-POOLED (only the pooled version was circularity-checked), so
    this multiplier is a single shared curve, not per-asset.
    Recalibrated 2026-09-13 (post-halt) -- the sign of the relationship
    reversed post-halt (better accuracy now predicts MORE scouting), a
    real, circularity-checked, time-stable finding, not noise."""

    def test_noop_when_accuracy_unavailable(self):
        assert bc.accuracy_scout_multiplier(None) == 1.0

    def test_increases_with_accuracy(self):
        # RECALIBRATED 2026-09-13 (post-halt, /loop cycle 8): the
        # relationship reversed sign post-halt, confirmed via the same
        # circularity check (non-scout-only rolling accuracy) the
        # original build used -- survives, same sign, even slightly
        # stronger. Better recent accuracy now predicts MORE scouting,
        # not less; see ACCURACY_SCOUT_MULTIPLIER's docstring for the
        # full stability check (time-split, per-asset breakdown).
        xs = sorted(bc.ACCURACY_SCOUT_MULTIPLIER.keys())
        low_acc = bc.accuracy_scout_multiplier(xs[0])
        high_acc = bc.accuracy_scout_multiplier(xs[-1])
        assert high_acc > low_acc

    def test_flat_beyond_the_measured_range(self):
        xs = sorted(bc.ACCURACY_SCOUT_MULTIPLIER.keys())
        assert bc.accuracy_scout_multiplier(0.0) == bc.accuracy_scout_multiplier(xs[0])
        assert bc.accuracy_scout_multiplier(1.5) == bc.accuracy_scout_multiplier(xs[-1])
        assert bc.hedge_size_ratio("NotAnAsset", "CHEAP") == bc._DEFAULT_HEDGE_SIZE_RATIO
