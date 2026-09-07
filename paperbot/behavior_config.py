"""
Confirmed per-asset behavioral parameters, measured from a full
778,116-trade historical dataset spanning all six assets (never blended
across assets -- every table here is keyed by asset first).

Nothing in this file should be "smoothed" to look more uniform across
assets. Some rows increase where you might expect a decrease (Dogecoin
HIGH, BNB CORE) -- those are confirmed, real, and kept as-is.
"""
from dataclasses import dataclass
from typing import Union

# ---------------------------------------------------------------------------
# Regime price bands -- shared boundaries, not asset-specific.
# Convention: lower bound inclusive, upper bound exclusive, except HIGH's
# upper bound (1.0) which is inclusive.
# ---------------------------------------------------------------------------
REGIME_BANDS = (
    ("CHEAP", 0.00, 0.30),
    ("MID", 0.30, 0.70),
    ("CORE", 0.70, 0.90),
    ("HIGH", 0.90, 1.0000001),  # inclusive of price == 1.0
)
REGIME_NAMES = tuple(name for name, _, _ in REGIME_BANDS)


def classify_regime(price: float) -> str:
    """Classify a 0-1 price into CHEAP/MID/CORE/HIGH. Boundaries: 0.30,
    0.70, and 0.90 belong to the band that STARTS at that value (e.g.
    price == 0.30 is MID, not CHEAP)."""
    if price < 0 or price > 1:
        raise ValueError(f"price must be in [0, 1], got {price}")
    for name, lo, hi in REGIME_BANDS:
        if lo <= price < hi:
            return name
    return "HIGH"  # price == 1.0 edge case, belt-and-suspenders


ASSET_NAMES = (
    "Bitcoin",
    "Ethereum",
    "Solana",
    "Dogecoin",
    "Hyperliquid",
    "BNB",
)

# Ticker <-> canonical asset name, used by market discovery (slugs use
# tickers, behavior tables use full names).
ASSET_TICKERS = {
    "Bitcoin": "BTC",
    "Ethereum": "ETH",
    "Solana": "SOL",
    "Dogecoin": "DOGE",
    "Hyperliquid": "HYPE",
    "BNB": "BNB",
}

# ---------------------------------------------------------------------------
# Regime distribution targets (% of trades landing in each band, over a
# large sample). Not used to force a synthetic distribution -- the bot only
# acts when the REAL live price is genuinely in a band. Kept here as a
# calibration/validation reference and for tests that guard against the
# tables being accidentally collapsed into one shared shape.
# ---------------------------------------------------------------------------
ASSET_REGIME_DISTRIBUTION_PCT = {
    "Bitcoin":     {"CHEAP": 33.4, "MID": 39.9, "CORE": 16.4, "HIGH": 10.2},
    # Solana RECALIBRATED 2026-09-08 from post-data-gap live data (n=1,929
    # trades, well-supported across all four cells) -- the original
    # 58.2/24.6/9.2/8.1 was measured on the full 778k-trade historical
    # dataset, but Solana's OWN recent behavior has drifted meaningfully
    # away from it (confirmed real, not noise: CHEAP -12.5pp is ~11
    # standard errors from zero at this sample size), while Bitcoin and
    # Ethereum's own regime distributions stayed close to their historical
    # calibration over the same period -- this is a Solana-specific
    # change, not evidence the whole calibration approach is broken. See
    # trader_intel/README.md's status log for the full investigation
    # (including why NOT to blame this on the BNB/Dogecoin/Hyperliquid
    # exclusion -- that's a separate, only partially-overlapping effect).
    "Solana":      {"CHEAP": 45.7, "MID": 26.2, "CORE": 13.7, "HIGH": 14.4},
    "Dogecoin":    {"CHEAP": 85.9, "MID": 8.6,  "CORE": 1.8,  "HIGH": 3.7},
    "Hyperliquid": {"CHEAP": 75.6, "MID": 14.6, "CORE": 5.5,  "HIGH": 4.2},
    "Ethereum":    {"CHEAP": 59.9, "MID": 21.6, "CORE": 8.9,  "HIGH": 9.6},
    "BNB":         {"CHEAP": 44.7, "MID": 35.6, "CORE": 13.6, "HIGH": 6.1},
}

# ---------------------------------------------------------------------------
# Entry-count sizing curves: median USD notional for the 1st, 2nd-3rd, and
# 4th+ entry placed within the same market. Confirmed exceptions (Dogecoin
# HIGH, BNB CORE both INCREASE instead of decrease) are kept as measured.
# ---------------------------------------------------------------------------
POSITION_TIERS = ("first", "2nd_3rd", "4th_plus")

ENTRY_SIZING_USD = {
    "Bitcoin": {
        "CHEAP": {"first": 1.793, "2nd_3rd": 1.528, "4th_plus": 1.165},
        "MID":   {"first": 4.209, "2nd_3rd": 3.774, "4th_plus": 3.021},
        "CORE":  {"first": 12.403, "2nd_3rd": 10.989, "4th_plus": 7.900},
        "HIGH":  {"first": 39.009, "2nd_3rd": 39.960, "4th_plus": 26.010},
    },
    "Ethereum": {
        "CHEAP": {"first": 0.994, "2nd_3rd": 0.750, "4th_plus": 0.550},
        "MID":   {"first": 2.968, "2nd_3rd": 2.600, "4th_plus": 2.450},
        "CORE":  {"first": 9.148, "2nd_3rd": 7.677, "4th_plus": 6.381},
        "HIGH":  {"first": 28.292, "2nd_3rd": 26.758, "4th_plus": 19.380},
    },
    # Solana CHECKED for recalibration 2026-09-08, left at historical
    # values -- every (regime, position) cell's recent sample is below
    # the n>=500 trust threshold (ranged n=25 to n=411), so recent medians
    # here (some higher, some lower than historical, no clean directional
    # story) are more plausibly noise than a real shift. Revisit once more
    # post-gap data has accumulated. See ASSET_REGIME_DISTRIBUTION_PCT's
    # Solana comment for what WAS recalibrated in this same pass.
    "Solana": {
        "CHEAP": {"first": 1.097, "2nd_3rd": 0.805, "4th_plus": 0.490},
        "MID":   {"first": 2.850, "2nd_3rd": 2.597, "4th_plus": 2.387},
        "CORE":  {"first": 7.373, "2nd_3rd": 6.880, "4th_plus": 6.235},
        "HIGH":  {"first": 21.620, "2nd_3rd": 23.126, "4th_plus": 19.740},
    },
    "Dogecoin": {
        "CHEAP": {"first": 0.671, "2nd_3rd": 0.495, "4th_plus": 0.276},
        "MID":   {"first": 1.860, "2nd_3rd": 1.738, "4th_plus": 1.891},
        "CORE":  {"first": 5.196, "2nd_3rd": 5.175, "4th_plus": 5.032},
        # CONFIRMED INCREASE first -> 4th+. Real, not smoothed away.
        "HIGH":  {"first": 12.960, "2nd_3rd": 14.469, "4th_plus": 14.250},
    },
    "Hyperliquid": {
        "CHEAP": {"first": 0.703, "2nd_3rd": 0.450, "4th_plus": 0.300},
        "MID":   {"first": 1.891, "2nd_3rd": 1.860, "4th_plus": 1.860},
        "CORE":  {"first": 5.011, "2nd_3rd": 5.396, "4th_plus": 5.017},
        "HIGH":  {"first": 18.600, "2nd_3rd": 18.228, "4th_plus": 14.351},
    },
    "BNB": {
        "CHEAP": {"first": 0.985, "2nd_3rd": 0.840, "4th_plus": 0.614},
        "MID":   {"first": 2.548, "2nd_3rd": 2.652, "4th_plus": 2.671},
        # CONFIRMED INCREASE first -> 4th+. Real, not smoothed away.
        "CORE":  {"first": 5.436, "2nd_3rd": 6.480, "4th_plus": 6.480},
        "HIGH":  {"first": 18.500, "2nd_3rd": 19.000, "4th_plus": 15.667},
    },
}

# ---------------------------------------------------------------------------
# Side persistence: P(next entry in an already-active market keeps the same
# Up/Down side as the previous entry in that market). Modeled as a strong
# bias (a weighted coin flip), never a hard rule.
# ---------------------------------------------------------------------------
SIDE_PERSISTENCE = {
    "Bitcoin": 0.9117,
    "Ethereum": 0.9057,
    # RECALIBRATED 2026-09-08 from post-gap live data (n=1,607, trusted
    # sample) -- 86.34% -> 81.64%. See ASSET_REGIME_DISTRIBUTION_PCT's
    # Solana comment for the full context.
    "Solana": 0.8164,
    "Dogecoin": 0.9152,
    "Hyperliquid": 0.8512,
    "BNB": 0.8085,
}

# ---------------------------------------------------------------------------
# Universal weakness/strength gradient -- same shape confirmed across all six
# assets (CHEAP entries tend to follow a recent price drop, HIGH entries tend
# to follow a recent rise). These are the exact measured values from the
# full 778,116-trade dataset (they fall inside the ranges given in spec:
# CHEAP ~51-58%, HIGH ~51-77%). Used ONLY as a soft tiebreak between
# multiple simultaneously-eligible opportunities in a single bot cycle --
# never as a hard requirement to trade.
# ---------------------------------------------------------------------------
GRADIENT_BIAS_PCT = {
    "Bitcoin":     {"CHEAP_follows_drop": 51.16, "HIGH_follows_rise": 55.40},
    "Ethereum":    {"CHEAP_follows_drop": 51.07, "HIGH_follows_rise": 51.94},
    # CHEAP_follows_drop RECALIBRATED 2026-09-08 (n=766, trusted) ->
    # 62.14%. HIGH_follows_rise left at the historical value -- recent
    # sample (n=251) is below the n>=500 trust threshold used for this
    # recalibration pass, and this is only ever a soft tiebreak anyway.
    "Solana":      {"CHEAP_follows_drop": 62.14, "HIGH_follows_rise": 62.71},
    "Dogecoin":    {"CHEAP_follows_drop": 51.42, "HIGH_follows_rise": 76.89},
    "Hyperliquid": {"CHEAP_follows_drop": 56.16, "HIGH_follows_rise": 65.01},
    "BNB":         {"CHEAP_follows_drop": 57.09, "HIGH_follows_rise": 53.10},
}

# A small price move smaller than this (in price units) counts as "flat",
# not rising/falling -- matches the threshold used in the historical
# analysis this bot is calibrated on.
GRADIENT_FLAT_EPSILON = 0.005

# ---------------------------------------------------------------------------
# Floor-lot "probe" tier (Part 4). A real, deliberate, secondary sizing
# behavior that coexists with the curve above -- not noise to filter out.
#
# Two shapes:
#   - Hyperliquid: CONFIRMED position-dependent probabilities (rises with
#     entry position within a market). Encoded exactly per position tier.
#   - BNB: aggregate float-lot share is confirmed substantial (~50-70% in
#     most regime/position combos) but the per-position split was NOT
#     separately measured -- only Hyperliquid's was. BNB's curve below is
#     modeled after Hyperliquid's confirmed rising-with-position shape and
#     tuned so its position-weighted blend lands in the ~50-70% range that
#     WAS measured in aggregate. This is a documented approximation, not a
#     second confirmed measurement.
#   - Ethereum / Solana / Dogecoin: roughly flat-by-position probabilities,
#     using the exact aggregate per-(asset, regime) fractions measured in
#     the historical analysis (9% ETH CHEAP/MID/CORE and negligible HIGH;
#     15-22% SOL/DOGE CHEAP/MID/CORE, ~9-10% HIGH).
#   - Bitcoin: negligible -- not modeled at all (probability 0 everywhere).
# ---------------------------------------------------------------------------
FloorLotSpec = Union[float, dict]

FLOOR_LOT_PROBABILITY = {
    "Hyperliquid": {
        # CONFIRMED exact position-dependent values.
        "CHEAP": {"first": 0.245, "2nd_3rd": 0.277, "4th_plus": 0.586},
        "MID":   {"first": 0.434, "2nd_3rd": 0.618, "4th_plus": 0.883},
        "CORE":  {"first": 0.175, "2nd_3rd": 0.376, "4th_plus": 0.828},
        "HIGH":  {"first": 0.049, "2nd_3rd": 0.076, "4th_plus": 0.525},
    },
    "BNB": {
        # APPROXIMATION -- see docstring above. Modeled after Hyperliquid's
        # rising shape; blended level tuned to the measured ~50-70% aggregate.
        "CHEAP": {"first": 0.45, "2nd_3rd": 0.60, "4th_plus": 0.75},
        "MID":   {"first": 0.50, "2nd_3rd": 0.65, "4th_plus": 0.80},
        "CORE":  {"first": 0.45, "2nd_3rd": 0.62, "4th_plus": 0.78},
        "HIGH":  {"first": 0.35, "2nd_3rd": 0.50, "4th_plus": 0.68},
    },
    "Ethereum": {
        # Flat-by-position: same float for every position tier.
        "CHEAP": 0.0854, "MID": 0.0987, "CORE": 0.1030, "HIGH": 0.0428,
    },
    "Solana": {
        # CHEAP/MID RECALIBRATED 2026-09-08 (n=881/505, both trusted) --
        # 0.1704->0.2452, 0.2213->0.2376. CORE/HIGH left at historical
        # values -- recent samples (n=265/278) are below the n>=500 trust
        # threshold used for this pass.
        "CHEAP": 0.2452, "MID": 0.2376, "CORE": 0.2055, "HIGH": 0.0964,
    },
    "Dogecoin": {
        "CHEAP": 0.1520, "MID": 0.1986, "CORE": 0.2209, "HIGH": 0.0923,
    },
    "Bitcoin": {
        # Negligible -- not modeled.
        "CHEAP": 0.0, "MID": 0.0, "CORE": 0.0, "HIGH": 0.0,
    },
}

# Assets whose floor-lot probability is position-dependent (a dict per
# regime keyed by position tier) vs. flat (a single float per regime).
FLOOR_LOT_POSITION_DEPENDENT_ASSETS = {"Hyperliquid", "BNB"}


def floor_lot_probability(asset: str, regime: str, position_tier: str) -> float:
    """
    Return P(this entry is a floor-lot probe trade) for (asset, regime,
    position_tier). position_tier must be one of POSITION_TIERS. For assets
    with a flat-by-position table, position_tier is accepted but ignored.
    """
    if position_tier not in POSITION_TIERS:
        raise ValueError(f"unknown position tier {position_tier!r}")
    table = FLOOR_LOT_PROBABILITY.get(asset)
    if table is None:
        return 0.0
    spec = table.get(regime, 0.0)
    if isinstance(spec, dict):
        return spec.get(position_tier, 0.0)
    return float(spec)


@dataclass(frozen=True)
class BehaviorLookupError(Exception):
    asset: str
    regime: str

    def __str__(self) -> str:
        return f"no behavior data for asset={self.asset!r} regime={self.regime!r}"


def position_tier_for_index(entry_index: int) -> str:
    """entry_index is 0-based: 0 -> first, 1-2 -> 2nd_3rd, 3+ -> 4th_plus."""
    if entry_index < 0:
        raise ValueError("entry_index must be >= 0")
    if entry_index == 0:
        return "first"
    if entry_index <= 2:
        return "2nd_3rd"
    return "4th_plus"


def median_entry_notional(asset: str, regime: str, position_tier: str) -> float:
    try:
        return ENTRY_SIZING_USD[asset][regime][position_tier]
    except KeyError as exc:
        raise BehaviorLookupError(asset, regime) from exc


# ---------------------------------------------------------------------------
# Dual-sided "hedge" behavior. Confirmed from the full historical dataset
# (see deep_analysis.py / deep_analysis_output.json and hedge_calibration.py
# in the repo): when the trader's early second entry into a market lands on
# the OPPOSITE outcome from the first, it functions as deliberate insurance,
# not a persistence-roll failure --
#
#   - Single-sided markets: worst-case outcome is always -100% of stake.
#   - Dual-sided markets: worst-case outcome averages -20.5% of stake
#     (median -27.6%), and 24.9% of them are outright arbitrage --
#     guaranteed profit regardless of which side wins.
#
# Modeled as ONE roll, at the market's SECOND entry only (0-based
# entry_count == 1), keyed by (asset, regime of the FIRST entry) --
# because that's when it actually happens in the data: median first-hedge
# entry index is 2-5, and in the timing analysis 55.8% of hedges land
# within 5 seconds of the primary entry. If the roll doesn't trigger, the
# market is NOT re-rolled at every later entry -- it just proceeds as an
# ordinary single-side (or persistence/switch-governed) market, matching
# the real per-market hedge frequency rather than compounding a smaller
# probability across many entries into an inflated one.
#
# Hedge size = HEDGE_SIZE_RATIO[asset][primary_regime] * the dominant
# side's cumulative cost so far (not the position-count sizing curve --
# hedge sizing scales with what it's protecting, not with entry index).
# ---------------------------------------------------------------------------
# Solana's hedge tables were NOT recalibrated in the 2026-09-08 pass (see
# ASSET_REGIME_DISTRIBUTION_PCT's Solana comment for what WAS) -- these
# need a MARKET-level breakdown by first-entry regime, and the post-gap
# sample is only 322 Solana markets total, split across 4 regimes -- far
# too thin for CORE/HIGH first-entries specifically to trust at all.
# Left at historical values; revisit once more data has accumulated.
HEDGE_TRIGGER_PROBABILITY = {
    "Bitcoin":     {"CHEAP": 0.6242, "MID": 0.6697, "CORE": 0.4754, "HIGH": 0.2967},
    "Ethereum":    {"CHEAP": 0.4006, "MID": 0.6562, "CORE": 0.5064, "HIGH": 0.2971},
    "Solana":      {"CHEAP": 0.5950, "MID": 0.7286, "CORE": 0.5523, "HIGH": 0.3039},
    "Dogecoin":    {"CHEAP": 0.3515, "MID": 0.4531, "CORE": 0.6209, "HIGH": 0.3041},
    "Hyperliquid": {"CHEAP": 0.3506, "MID": 0.5819, "CORE": 0.5628, "HIGH": 0.3079},
    "BNB":         {"CHEAP": 0.8389, "MID": 0.9004, "CORE": 0.7806, "HIGH": 0.4884},
}

HEDGE_SIZE_RATIO = {
    "Bitcoin":     {"CHEAP": 0.1316, "MID": 0.2756, "CORE": 0.1062, "HIGH": 0.0306},
    "Ethereum":    {"CHEAP": 0.1243, "MID": 0.2511, "CORE": 0.0822, "HIGH": 0.0204},
    "Solana":      {"CHEAP": 0.1574, "MID": 0.2760, "CORE": 0.0625, "HIGH": 0.0159},
    "Dogecoin":    {"CHEAP": 0.1992, "MID": 0.2937, "CORE": 0.0942, "HIGH": 0.0480},
    "Hyperliquid": {"CHEAP": 0.1453, "MID": 0.1742, "CORE": 0.0833, "HIGH": 0.0358},
    "BNB":         {"CHEAP": 0.1063, "MID": 0.1686, "CORE": 0.0503, "HIGH": 0.0285},
}

# Fallback ratio if an (asset, regime) combo is ever missing -- shouldn't
# happen given all six assets and four regimes are populated above, but a
# defined fallback beats a KeyError taking down a market's evaluation.
_DEFAULT_HEDGE_SIZE_RATIO = 0.15


def hedge_trigger_probability(asset: str, primary_regime: str) -> float:
    return HEDGE_TRIGGER_PROBABILITY.get(asset, {}).get(primary_regime, 0.0)


def hedge_size_ratio(asset: str, primary_regime: str) -> float:
    return HEDGE_SIZE_RATIO.get(asset, {}).get(primary_regime, _DEFAULT_HEDGE_SIZE_RATIO)
