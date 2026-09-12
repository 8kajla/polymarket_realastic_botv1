"""
Confirmed per-asset behavioral parameters, measured from a full
778,116-trade historical dataset spanning all six assets (never blended
across assets -- every table here is keyed by asset first).

Nothing in this file should be "smoothed" to look more uniform across
assets. Some rows increase where you might expect a decrease (Dogecoin
HIGH, BNB CORE) -- those are confirmed, real, and kept as-is.
"""
import math
from dataclasses import dataclass
from typing import Optional, Union

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
    # RECALIBRATED 2026-09-08 from post-data-gap live data (n=9,248,
    # every cell well-supported). Was the blind spot flagged when Solana
    # got recalibrated first -- checking Bitcoin/Ethereum too (not just
    # trusting they were fine) found a real, smaller-than-Solana's but
    # still genuine shift. See trader_intel/README.md's status log.
    "Bitcoin":     {"CHEAP": 27.6, "MID": 41.3, "CORE": 19.7, "HIGH": 11.4},
    # Solana RECALIBRATED 2026-09-08 from post-data-gap live data (n=1,929
    # trades, well-supported across all four cells) -- the original
    # 58.2/24.6/9.2/8.1 was measured on the full 778k-trade historical
    # dataset, but Solana's OWN recent behavior has drifted meaningfully
    # away from it (confirmed real, not noise: CHEAP -12.5pp is ~11
    # standard errors from zero at this sample size). Checking Bitcoin and
    # Ethereum the same way (not just assuming they were fine) found a
    # real but smaller shift there too, applied above/below -- so this
    # isn't purely Solana-specific after all, just Solana-largest. See
    # trader_intel/README.md's status log for the full investigation
    # (including why NOT to blame this on the BNB/Dogecoin/Hyperliquid
    # exclusion -- that's a separate, only partially-overlapping effect).
    # RECALIBRATED AGAIN 2026-09-08 (later pass, n=4,093, every cell well
    # above trust threshold -- and see ENTRY_SIZING_USD's CORRECTION
    # note: the "last 14 days" query window is really ~2.3 days of real
    # trading since a previously unknown 13.6-day total silence ended
    # 2026-09-06 14:21 UTC): moved further still, beyond the post-data-gap
    # value directly above -- MID 26.2->34.5 (+8.3pp, the single biggest
    # shift of any cell checked in this pass), CHEAP 45.7->41.3 (-4.4pp),
    # HIGH 14.4->11.7 (-2.7pp), CORE roughly steady (13.7->12.5, not
    # significant at this n). Bitcoin and Ethereum were checked the same
    # way and left unchanged -- their gaps were all under 2pp,
    # statistically "significant" only because of the huge sample size,
    # not practically meaningful.
    "Solana":      {"CHEAP": 41.3, "MID": 34.5, "CORE": 12.5, "HIGH": 11.7},
    "Dogecoin":    {"CHEAP": 85.9, "MID": 8.6,  "CORE": 1.8,  "HIGH": 3.7},
    "Hyperliquid": {"CHEAP": 75.6, "MID": 14.6, "CORE": 5.5,  "HIGH": 4.2},
    # RECALIBRATED 2026-09-08 (n=2,998, all cells well-supported) --
    # see Bitcoin's comment above for context.
    "Ethereum":    {"CHEAP": 55.7, "MID": 25.1, "CORE": 8.3,  "HIGH": 10.9},
    "BNB":         {"CHEAP": 44.7, "MID": 35.6, "CORE": 13.6, "HIGH": 6.1},
}

# ---------------------------------------------------------------------------
# Entry-count sizing curves: median USD notional for the 1st, 2nd-3rd, and
# 4th+ entry placed within the same market. Confirmed exceptions (Dogecoin
# HIGH, BNB CORE both INCREASE instead of decrease) are kept as measured.
# ---------------------------------------------------------------------------
POSITION_TIERS = ("first", "2nd_3rd", "4th_plus")

ENTRY_SIZING_USD = {
    # RECALIBRATED AGAIN 2026-09-08 (later pass, independent of the
    # same-day recalibration noted below): a completely different check
    # this time -- not "is the full-history median still right" but "has
    # his CURRENT sizing drifted away from the full-history calibration
    # this table was originally built from". It has, a lot: a controlled
    # test (Welch's t, queried as "last 14 days" vs the full ~101-day
    # mirror) found his mean entry size down 38.8% (Bitcoin), 41.6%
    # (Ethereum), 20.9% (Solana) recently, all wildly significant
    # (t=34.6/17.9/6.4).
    #
    # CORRECTION, found later the same night: "last 14 days" as a QUERY
    # WINDOW is accurate, but as a description of how much real trading
    # it contains is not -- a previously unknown 13.6-day TOTAL silence
    # (2026-08-23 22:53 UTC to 2026-09-06 14:21 UTC, every asset, found
    # via a direct gap scan of the full mirror) falls almost entirely
    # inside that window. The 14-day query's cutoff (08-25 22:xx) lands
    # DURING the silence, before it ends -- so every cell below is
    # actually measuring only the ~2.3 days of real trading SINCE the
    # 09-06 resumption, not a natural 14-day sample. The arithmetic
    # itself is still correct (an empty stretch contributes nothing to a
    # sum either way, so the computed means/medians are exactly what the
    # post-resumption data alone produces) -- but treat every n below as
    # "over ~2.3 days," and treat the recalibration itself as capturing
    # his IMMEDIATE post-silence posture, not necessarily a settled new
    # normal: a single trader coming back from 13+ days away trading
    # smaller and hedging more for their first ~2 days back is at least
    # as consistent with cautious re-entry as with a permanent shift.
    # Re-check once more calendar time has passed since the resumption.
    #
    # Only cells whose sample itself clears the n>=500 trust threshold
    # were updated here (same bar as every other recalibration in this
    # file) -- 4th_plus is the tier with enough volume in ~2.3 days to
    # trust; first/2nd_3rd stay at their (older, full-history) values,
    # and CORE/HIGH's thinner cells here are Bitcoin-only.
    #
    # CORRECTED AGAIN 2026-09-08 (same night, a further pass): the values
    # right below this comment were measuring a CONTAMINATED population.
    # Prompted by the user's own hypothesis about tiny CHEAP-band bets
    # being deliberate tail insurance against bigger positions (confirmed:
    # 45.9% of CHEAP hedge-shaped trades specifically pair against a
    # HIGH-regime dominant position -- the single most common hedge
    # pairing found, and HEDGE_SIZE_RATIO/HEDGE_TRIGGER_PROBABILITY below
    # already model exactly this). Roughly half of CHEAP/MID trades in
    # this window are hedge-shaped (the smaller, non-dominant side in
    # their market) -- sized via a completely different mechanism
    # (proportional to the dominant position's cost) than the ordinary
    # entry-count curve this table represents. Blending them in dragged
    # the "ordinary" median down artificially. Re-measured STANDALONE-ONLY
    # (dominant-side trades only) for the same cells:
    #   Bitcoin MID/4th_plus:    2.226 -> 2.385 (n=5,268 standalone, +7%)
    #   Ethereum CHEAP/4th_plus: 0.652 -> 0.674 (n=1,577 standalone, +5%)
    #   Ethereum MID/4th_plus:   1.920 -> 1.947 (n=962 standalone, +1%)
    #   Solana CHEAP/4th_plus:   0.259 -> 0.354 (n=606 standalone, +36%)
    #   Solana MID/4th_plus:     0.852 -> 1.083 (n=730 standalone, +48%)
    # Bitcoin MID/2nd_3rd's standalone sample (n=474) stays just below the
    # trust bar -- left at its already-recalibrated 2.226 rather than
    # updating from a thin split. Bitcoin's other cells were untouched by
    # this correction (CHEAP/4th_plus, CORE/4th_plus, HIGH/4th_plus all
    # showed <1% standalone-vs-blended difference -- not worth a separate
    # note per cell). Solana was the one asset where this contamination
    # mattered a lot; Bitcoin and Ethereum were only mildly affected.
    "Bitcoin": {
        "CHEAP": {"first": 1.793, "2nd_3rd": 1.528, "4th_plus": 1.356},
        "MID":   {"first": 4.209, "2nd_3rd": 2.226, "4th_plus": 2.385},
        "CORE":  {"first": 12.403, "2nd_3rd": 10.989, "4th_plus": 5.396},
        "HIGH":  {"first": 39.009, "2nd_3rd": 39.960, "4th_plus": 16.284},
    },
    "Ethereum": {
        "CHEAP": {"first": 0.994, "2nd_3rd": 0.750, "4th_plus": 0.674},
        "MID":   {"first": 2.968, "2nd_3rd": 2.600, "4th_plus": 1.947},
        "CORE":  {"first": 9.148, "2nd_3rd": 7.677, "4th_plus": 6.381},
        "HIGH":  {"first": 28.292, "2nd_3rd": 26.758, "4th_plus": 19.380},
    },
    "Solana": {
        "CHEAP": {"first": 1.097, "2nd_3rd": 0.805, "4th_plus": 0.354},
        "MID":   {"first": 2.850, "2nd_3rd": 2.597, "4th_plus": 1.083},
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
    # MADE REGIME-DEPENDENT 2026-09-08 for the three active assets (Bitcoin/
    # Ethereum/Solana) -- TRADER_PROFILE.md section 8 confirmed a real,
    # non-confounded win-rate difference by (regime, persist-vs-switch) back
    # when SIDE_PERSISTENCE was still asset-level only, and flagged "would
    # need a second full calibration pass" to turn that into real per-regime
    # persistence rates. This is that pass: each cell below is P(this entry
    # keeps the same side | the side CURRENTLY held was last trading in this
    # regime) -- i.e. keyed by the PREVIOUS entry's own regime, not this
    # entry's resulting one (the only framing usable at decision time; see
    # strategy.decide_side and its docstring for why). Computed from the
    # full live mirror (n=7,937-74,500 per cell, all far past the n>=500
    # trust bar). Genuinely NOT uniform across regimes or assets -- e.g.
    # Bitcoin's CHEAP persistence (88.3%) is its lowest regime, while
    # Solana's CORE (79.2%) and HIGH (81.2%) are its lowest -- no single
    # "switches more at extremes" or "switches more in the middle" rule
    # holds across all three, which is exactly why this needed real
    # per-(asset,regime) data rather than a hand-picked rule.
    # RECALIBRATED AGAIN 2026-09-08 (later pass, same drift check already
    # applied to ENTRY_SIZING_USD and ASSET_REGIME_DISTRIBUTION_PCT --
    # and see ENTRY_SIZING_USD's CORRECTION note: "last 14 days" as
    # queried is really ~2.3 days of real trading since a previously
    # unknown 13.6-day total silence ended 2026-09-06 14:21 UTC, so read
    # every n/gap below as "since resumption," possibly cautious re-entry
    # rather than a settled new normal): Bitcoin drifted UP a modest but
    # real amount across three of four regimes -- CHEAP 0.8832->0.9076
    # (n=4,255, +2.4pp), CORE 0.9309->0.9511 (n=3,027, +2.0pp), HIGH
    # 0.9167->0.9448 (n=1,577, +2.8pp). MID's +0.7pp gap wasn't
    # significant, left as-is.
    "Bitcoin": {"CHEAP": 0.9076, "MID": 0.9295, "CORE": 0.9511, "HIGH": 0.9448},
    "Ethereum": {"CHEAP": 0.9179, "MID": 0.8948, "CORE": 0.8710, "HIGH": 0.8842},
    # RECALIBRATED AGAIN 2026-09-08, same later pass (and same "actually
    # since the 09-06 resumption" correction applies): CHEAP dropped
    # substantially, 0.8797->0.8065 (n=1,504, -7.3pp, the largest
    # persistence shift found in this asset/regime sweep). CORE and HIGH
    # both show bigger apparent swings (+5.5pp / +3.0pp) but their
    # post-resumption samples (n=436 / n=297) stay below the n>=500 trust
    # bar this file uses everywhere else -- left at historical values
    # rather than chasing a thin-sample number. MID's +0.9pp gap wasn't
    # significant either.
    # RECALIBRATED AGAIN 2026-09-09 (autonomous loop, /loop cycle):
    # checked all nine live cells (BTC/ETH/SOL x 4 regimes) against a
    # fresh 7-day window for drift since the pass above -- Bitcoin and
    # Ethereum held up well (every cell under 1.5pp). Solana/CORE had
    # moved a real amount: 79.17%->85.32% (n=695, +6.2pp, clears the
    # trust bar easily). HIGH showed a similar-looking +3.9pp gap but
    # n=424 stays below the n>=500 bar -- left alone, not chased.
    # CHEAP/MID both under 1pp, unchanged. This drift within ~1 day of
    # the pass above is itself evidence for the broader finding this
    # loop made tonight: his behavior (regime mix, hedge rate) has been
    # continuously shifting since the Sept 6 resumption, not settling
    # into a fixed new normal -- these tables are chasing a moving
    # target and may need periodic re-checks, not a one-time fix.
    "Solana": {"CHEAP": 0.8065, "MID": 0.8616, "CORE": 0.8532, "HIGH": 0.8120},
    # Dogecoin/Hyperliquid/BNB are dormant (see ASSET_REGIME_DISTRIBUTION_PCT's
    # comment) -- not worth the same rigor while untraded. Kept at their old
    # single blended value, just reshaped to the same per-regime dict shape
    # so side_persistence_for() has one uniform lookup path; this is a pure
    # type-consistency change, NOT a recalibration -- same number in all 4
    # regime cells, so behavior for these three is byte-for-byte unchanged
    # from before.
    "Dogecoin": {"CHEAP": 0.9152, "MID": 0.9152, "CORE": 0.9152, "HIGH": 0.9152},
    "Hyperliquid": {"CHEAP": 0.8512, "MID": 0.8512, "CORE": 0.8512, "HIGH": 0.8512},
    "BNB": {"CHEAP": 0.8085, "MID": 0.8085, "CORE": 0.8085, "HIGH": 0.8085},
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
    # Bitcoin RECALIBRATED 2026-09-08: CHEAP_follows_drop barely moved
    # (51.16->51.97, n=2,484 trusted -- essentially unchanged, updated
    # for precision). HIGH_follows_rise moved more (55.40->50.05,
    # n=1,049 trusted) -- a real ~5.4pp drop.
    "Bitcoin":     {"CHEAP_follows_drop": 51.97, "HIGH_follows_rise": 50.05},
    # Ethereum RECALIBRATED 2026-09-08: CHEAP_follows_drop barely moved
    # (51.07->51.91, n=1,520 trusted). HIGH_follows_rise left at the
    # historical value -- recent sample (n=300) is below the trust
    # threshold.
    "Ethereum":    {"CHEAP_follows_drop": 51.91, "HIGH_follows_rise": 51.94},
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
        # CHEAP/MID RECALIBRATED 2026-09-08 (n=1,669/752, both trusted)
        # -- 0.0854->0.1258 (real increase), 0.0987->0.0559 (real
        # decrease -- not a uniform direction across regimes).
        # CORE/HIGH left at historical values, recent samples (n=250/327)
        # below the trust threshold.
        "CHEAP": 0.1258, "MID": 0.0559, "CORE": 0.1030, "HIGH": 0.0428,
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
        # CHECKED 2026-09-08: recent floor-lot rate is 0.47-1.02%, still
        # within the same "negligible" range as historically (0.2-0.7%)
        # -- confirmed still negligible, not a change, left unmodeled.
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
# Continuous within-band size scaling. Added 2026-09-08: ENTRY_SIZING_USD
# only conditions on the discrete (regime, position_tier) bucket -- but a
# fresh investigation found a real, positive relationship between price and
# log(size) WITHIN every single (asset, CORE/HIGH regime, position_tier)
# cell individually (not an artifact of position_tier correlating with
# price -- checked explicitly cell-by-cell before trusting this, since
# position_tier already drives size and double-counting was the real risk).
# Correlations: 0.09-0.36 (weak-to-moderate but consistent in sign and
# rough slope magnitude across position tiers within an asset/regime).
# Restricted to CORE/HIGH for the three active assets only -- CHEAP/MID
# and the three dormant assets were not checked with this rigor and get a
# neutral 1.0x multiplier (unchanged behavior).
#
# slope = d(log usdc)/d(price), band_mean_price = the mean price this slope
# was measured around. Applied as a MEAN-NEUTRAL multiplier
# exp(slope * (price - band_mean_price)) on top of the existing
# position-tier median, so the calibrated average sizing per cell is
# unchanged -- this only adds the continuous within-band variation that
# ENTRY_SIZING_USD's discrete buckets can't express. Capped (see
# within_band_size_multiplier) against extrapolating past where the data
# actually supports it.
WITHIN_BAND_SIZE_SLOPE = {
    "Bitcoin": {"CORE": {"slope": 4.19, "band_mean_price": 0.795},
                "HIGH": {"slope": 13.47, "band_mean_price": 0.947},
                "CHEAP": {"slope": 4.7433, "band_mean_price": 0.1529},
                "MID": {"slope": 2.9664, "band_mean_price": 0.4900}},
    "Ethereum": {"CORE": {"slope": 3.21, "band_mean_price": 0.799},
                 "HIGH": {"slope": 21.09, "band_mean_price": 0.955},
                 "CHEAP": {"slope": 4.8979, "band_mean_price": 0.1212},
                 "MID": {"slope": 3.4679, "band_mean_price": 0.4726}},
    "Solana": {"CORE": {"slope": 4.50, "band_mean_price": 0.801},
               "HIGH": {"slope": 28.16, "band_mean_price": 0.949},
               "CHEAP": {"slope": 3.9698, "band_mean_price": 0.1350},
               "MID": {"slope": 3.1983, "band_mean_price": 0.4563}},
}
_WITHIN_BAND_MULTIPLIER_CAP = 5.0  # symmetric: multiplier clamped to [1/cap, cap]
# CHEAP/MID cells ADDED 2026-09-11: the original 2026-09-08 pass only
# checked CORE/HIGH ("CHEAP/MID... were not checked with this rigor and
# get a neutral 1.0x multiplier" -- see the comment above this table).
# A follow-up research pass found CHEAP/MID show an EQUALLY strong
# relationship, not weaker: CHEAP r=0.23-0.33, MID r=0.33-0.41 across all
# three assets (t=49-125, all comfortably clearing this file's own
# |t|>=2.58 bar) -- confirmed cross-asset AND temporally stable across
# the Aug 7 TWAP mechanism change (pre/post-TWAP both real and
# consistent in both regimes). This is the most thoroughly validated
# addition to this file: real, cross-asset, and stable across the
# platform's biggest structural change. Slopes fit the same way as
# CORE/HIGH (OLS of log(usdcSize) on price, band_mean_price = the mean
# price the slope was measured around).
#
# Gated separately from CORE/HIGH's own (flag-less, pre-existing)
# behavior -- CORE/HIGH predate this session's mini-control-flagging
# convention and stay unconditional for every instance; only the
# CHEAP/MID addition needs its own flag so paperbot-mini can opt out of
# it specifically. Gated at the CALL SITE in strategy.decide_size (via
# config.ENABLE_CHEAP_MID_WITHIN_BAND_SCALING), not inside
# within_band_size_multiplier() itself -- this file never imports
# config, same discipline as every other table here.


def within_band_size_multiplier(asset: str, regime: str, price: float) -> float:
    spec = WITHIN_BAND_SIZE_SLOPE.get(asset, {}).get(regime)
    if spec is None:
        return 1.0
    raw = math.exp(spec["slope"] * (price - spec["band_mean_price"]))
    return max(1.0 / _WITHIN_BAND_MULTIPLIER_CAP, min(_WITHIN_BAND_MULTIPLIER_CAP, raw))


# ---------------------------------------------------------------------------
# TIME-TO-CLOSE size scaling. Added 2026-09-10: the whole session's own
# reverse-engineering work found a real, structural signal (a market
# entering its 60s TWAP window makes the outcome increasingly computable
# as close approaches) AND, critically, confirmed the trader actually
# ACTS on it via SIZING, not just presence -- unlike a separately-tested
# "price velocity" signal that turned out to be a passive market fact he
# does NOT size against (checked and rejected the same night, same
# methodology, before touching any code here).
#
# Measured directly: mean $ per trade in 4 buckets by seconds-remaining
# (270/210/150/90s midpoints), per (asset, regime), 14-day window,
# BTC/ETH/SOL only (the three live-traded assets). Ratio to the n-weighted
# overall mean for that (asset, regime) -- mean-neutral by construction,
# same principle as WITHIN_BAND_SIZE_SLOPE above: this only redistributes
# size across time-within-window, it doesn't change the already-
# calibrated ENTRY_SIZING_USD average.
#
# Real, asset-consistent shapes found (all three assets agree in
# direction, magnitude varies):
#   CHEAP: shrinks toward close (a still-unlikely outcome gets LESS
#     appealing as time runs out) -- ~15-33% below the window-open level.
#   MID:   grows toward close (an undecided market nearing its real
#     decision point) -- Bitcoin mildly (+18%), Ethereum/Solana
#     dramatically (+63%/+98%).
#   CORE:  roughly flat -- price level alone already captures most of
#     what matters here, matching the earlier price-convergence finding
#     that CORE is a weaker/messier signal cell generally.
#   HIGH:  grows sharply toward close -- Bitcoin roughly doubles
#     (0.67x -> 1.17x), Ethereum/Solana similar. The clearest, cleanest
#     signal of the four, matching "size scales with TWAP-locked-in
#     confidence."
#
# n>=100 trust bar per bucket (comparable order of magnitude to
# WITHIN_BAND_SIZE_SLOPE's own trusted correlations). The one bucket per
# asset that fell short (HIGH's 240-300s/270 midpoint -- thin because
# HIGH-band trades this early in a window are rare for all three assets,
# n=71/20/29) is CLAMPED to the next trusted bucket's ratio (210) rather
# than used raw -- a real thin-sample ratio would either be noise or,
# worse, get silently trusted at face value; clamping is the same
# "don't extrapolate past what the data supports" caution
# _WITHIN_BAND_MULTIPLIER_CAP applies below, just at the input-trust
# stage instead of the output-clamp stage.
#
# Linear interpolation between bucket midpoints for continuous ttc
# values; flat (equal to the nearest endpoint) outside [90, 270] --
# no extrapolation past the measured window. Dogecoin/Hyperliquid/BNB
# omitted (not live-traded, no data) -- ttc_size_multiplier returns 1.0
# (no-op) for any asset/regime not in this table, same fallback pattern
# as within_band_size_multiplier.
TTC_SIZE_MULTIPLIER = {
    "Bitcoin": {
        "CHEAP": {270: 1.1502, 210: 1.063, 150: 0.9984, 90: 0.8641},
        "MID": {270: 0.9221, 210: 0.9878, 150: 1.045, 90: 1.0869},
        "CORE": {270: 0.9122, 210: 1.0321, 150: 1.0093, 90: 1.0001},
        "HIGH": {270: 0.6673, 210: 0.6673, 150: 0.9282, 90: 1.1731},
    },
    "Ethereum": {
        "CHEAP": {270: 1.1222, 210: 1.0766, 150: 0.9583, 90: 0.8513},
        "MID": {270: 0.773, 210: 0.9392, 150: 1.0528, 90: 1.2595},
        "CORE": {270: 1.0162, 210: 1.041, 150: 0.9756, 90: 0.9792},
        "HIGH": {270: 0.7042, 210: 0.7042, 150: 0.9046, 90: 1.1689},
    },
    "Solana": {
        "CHEAP": {270: 1.1344, 210: 0.9901, 150: 1.0373, 90: 0.8356},
        "MID": {270: 0.7752, 210: 0.9129, 150: 1.1741, 90: 1.5376},
        "CORE": {270: 0.9691, 210: 0.9821, 150: 1.0242, 90: 1.007},
        "HIGH": {270: 0.7084, 210: 0.7084, 150: 0.9776, 90: 1.1434},
    },
}
_TTC_MULTIPLIER_MIDPOINTS = (270, 210, 150, 90)


def ttc_size_multiplier(asset: str, regime: str, seconds_remaining: float) -> float:
    """Continuous, mean-neutral size multiplier as a function of time
    remaining in the market's 5-minute window. 1.0 (no-op) for any
    (asset, regime) not in TTC_SIZE_MULTIPLIER (Dogecoin/Hyperliquid/BNB,
    or an unrecognized regime). Flat beyond the measured [90, 270]
    range -- clamped to the nearest endpoint's value, never extrapolated."""
    curve = TTC_SIZE_MULTIPLIER.get(asset, {}).get(regime)
    if curve is None:
        return 1.0
    ttc = max(90.0, min(270.0, seconds_remaining))
    for i in range(len(_TTC_MULTIPLIER_MIDPOINTS) - 1):
        hi_mid, lo_mid = _TTC_MULTIPLIER_MIDPOINTS[i], _TTC_MULTIPLIER_MIDPOINTS[i + 1]
        if lo_mid <= ttc <= hi_mid:
            hi_val, lo_val = curve[hi_mid], curve[lo_mid]
            frac = (ttc - lo_mid) / (hi_mid - lo_mid)
            return lo_val + frac * (hi_val - lo_val)
    # ttc exactly at an endpoint (270 or 90) falls through the loop above
    return curve[_TTC_MULTIPLIER_MIDPOINTS[0] if ttc >= 270 else _TTC_MULTIPLIER_MIDPOINTS[-1]]


# ---------------------------------------------------------------------------
# RESUMPTION-CAUTION ramp. Added 2026-09-10: a real, confirmed pattern
# from the largest known silence this session found (327.46h/13.6-day gap,
# resumption at 2026-09-06 14:21 UTC) -- fine-grained hourly reconstruction
# of entry size after resumption found a genuine 3-phase shape, NOT a
# monotonic ramp:
#   hours 0-6:  SUPPRESSED entry size, 15-35% below the $3.71 steady-state
#               baseline
#   hours 7-9:  OVERSHOOT, 68-138% ABOVE baseline -- a real catch-up
#               burst, not just normalization
#   hour 10+:   settles into noisy oscillation around baseline -- back to
#               a genuine 1.0x, not held at any edge value
#
# CONFIRMED SCOPE, not extrapolated past it: the same session's own
# earlier work on smaller gaps (1.6-4.3h) found inconsistent, noise-level
# before/after differences -- only sufficiently large gaps (roughly above
# 4h) showed this genuine behavioral reset. This curve is therefore gated
# to apply ONLY after a gap of at least RESUMPTION_GAP_THRESHOLD_HOURS --
# see bot.py's own tracking of when a qualifying gap just ended. Applying
# it to every ordinary few-second pause between ticks (which is NOT what
# was measured or found) would be a real, unvalidated overreach.
#
# Single pooled curve (not per-asset, unlike TTC/liquidity above) -- the
# original investigation was necessarily built from ONE large real gap
# event (there is only one that large in the whole mirror), so there's
# no basis to split this by asset the way tables built from thousands of
# markets can be. Hour-bucket midpoints -> ratio to the $3.71 steady-
# state baseline measured in that same investigation.
RESUMPTION_GAP_THRESHOLD_HOURS = 4.0
RESUMPTION_SIZE_MULTIPLIER = {
    3: 0.72,    # hours 0-6 midpoint: suppressed (~15-35% below baseline, using the
                # middle of that measured range)
    8: 1.90,    # hours 7-9 midpoint: overshoot (~68-138% above baseline, middle of range)
    10: 1.0,    # hour 10+: back to genuine baseline, held flat beyond this point
}
_RESUMPTION_MULTIPLIER_MAX_HOURS = 10.0  # beyond this, a strict 1.0 no-op -- not
                                          # clamped to the last point's value the
                                          # way TTC/liquidity do, since "settles
                                          # into noisy oscillation around baseline"
                                          # means genuinely back to neutral, not
                                          # held at hour-10's specific ratio.


def resumption_size_multiplier(hours_since_resumption: Optional[float]) -> float:
    """Continuous multiplier on entry size as a function of hours since a
    QUALIFYING resumption (a gap >= RESUMPTION_GAP_THRESHOLD_HOURS just
    ended). 1.0 (no-op) if hours_since_resumption is None (no qualifying
    resumption is currently tracked) or >= _RESUMPTION_MULTIPLIER_MAX_HOURS
    (genuinely back to baseline, not held at an edge value)."""
    if hours_since_resumption is None or hours_since_resumption < 0:
        return 1.0
    if hours_since_resumption >= _RESUMPTION_MULTIPLIER_MAX_HOURS:
        return 1.0
    points = sorted(RESUMPTION_SIZE_MULTIPLIER.items())
    lo_h, lo_val = points[0]
    if hours_since_resumption <= lo_h:
        return lo_val  # flat before the first measured point -- no extrapolation
    for i in range(len(points) - 1):
        p_h, p_val = points[i]
        q_h, q_val = points[i + 1]
        if p_h <= hours_since_resumption <= q_h:
            frac = (hours_since_resumption - p_h) / (q_h - p_h) if q_h > p_h else 0.0
            return p_val + frac * (q_val - p_val)
    return points[-1][1]  # defensive fallback; unreachable given the >= max_hours check above


def side_persistence_for(asset: str, held_side_regime: str) -> float:
    """P(keep the currently-held side) given the regime that side is
    CURRENTLY trading in (i.e. the regime it would land in if persisted --
    see strategy.decide_side for why this, not the regime a switch would
    resolve to, is the only framing knowable before the decision is made).
    """
    try:
        return SIDE_PERSISTENCE[asset][held_side_regime]
    except KeyError as exc:
        raise BehaviorLookupError(asset, held_side_regime) from exc


# ---------------------------------------------------------------------------
# CROSS-MARKET first-entry side persistence. Added 2026-09-11: real,
# cross-asset-generalizing finding that a market's FIRST entry side isn't
# actually independent of the PREVIOUS market's first entry side, closing
# a documented gap in strategy.decide_side (previously: "there's no
# historical parameter for the *initial* side... so we pick uniformly at
# random -- a documented simplification").
#
# Found via a Wald-Wolfowitz runs test on the real chronological sequence
# of first-entry sides (one per market): observed far FEWER runs
# (alternations) than random chance predicts -- z=-12.75 (BTC, n=14,349
# markets) on the pooled sequence. Confirmed per-asset and per-asset
# CAUSALLY CLEAN: measured directly as P(this market's first side ==
# previous market's first side), restricted to genuinely-consecutive
# market pairs (gap <=1h, so a long silence never bridges two unrelated
# eras):
#   Bitcoin:  55.38% (n=14,300 consecutive pairs, z=12.88 vs 50%)
#   Ethereum: 52.65% (n=13,383, z=6.13)
#   Solana:   54.29% (n=13,212, z=9.87)
# All three comfortably clear this file's own |z|>=2.58 bar.
#
# CONFOUND CHECKED, not assumed: real BTC spot price direction across the
# same consecutive 5-min windows shows the OPPOSITE pattern (z=+3.01,
# MORE runs than random -- i.e. real spot is mildly anti-persistent/
# mean-reverting across windows, not trending). His side-choice
# persistence is NOT explained by, and actually runs against, what real
# market momentum would predict -- ruling out "he's just correctly
# tracking a real trend" as the mechanism. This is a genuine behavioral
# signature of the decision process itself.
# UPGRADED 2026-09-11: was a single flat probability per asset (below,
# now kept only as the WIN/LOSS-AVERAGED fallback). Real, cross-asset
# refinement found the same night: the flat rate is really an average of
# two quite different regimes -- persistence is much stronger after the
# previous market's first-entry side LOST than after it WON (BTC
# 51.48%/60.13% z=-5.80; ETH 46.75%/57.36% z=-3.28; SOL 48.12%/60.68%
# z=-3.92, all clearing this file's |z|>=2.58 bar on the win/loss
# difference itself, not just the aggregate rate). Interpretation: NOT
# hot-hand (would predict MORE persistence after a win) and NOT classic
# gambler's fallacy (would predict avoiding the same side after a loss)
# -- closer to the opposite of gambler's fallacy: a single 5-minute
# market not confirming his directional read doesn't disprove it, so he
# sticks with it MORE after a setback, not less.
#
# Checked and bounded before shipping: does NOT extend to hedge
# propensity (after-win 66.51% vs after-loss 65.83% dual-sided rate,
# z=-0.48 -- side-specific only); does NOT compound with consecutive-loss
# streak length (1/2/3+ losses all statistically indistinguishable,
# z well under 1 -- a binary trigger, not gradual evidence accumulation,
# so only ONE prior outcome is needed, not a streak counter); regime-
# conditioned shape does NOT generalize (BTC/SOL show MID/CORE strongest
# and CHEAP null, ETH shows the OPPOSITE -- CHEAP significant, MID/CORE
# null) -- deliberately NOT modeled with regime granularity here, using
# the flat asset-level after-win/after-loss split instead, per that
# cross-asset contradiction. Confirmed the CURRENT (post-TWAP) era shows
# a notably STRONGER effect than pre-TWAP (gap roughly doubled, 4.18pp ->
# 10.14pp) -- but the numbers below are still the safer POOLED whole-
# history values (not post-TWAP-only), since a full per-asset post-TWAP
# split wasn't computed for all three assets before this was implemented.
CROSS_MARKET_SIDE_PERSISTENCE = {
    "Bitcoin": {"after_win": 0.5148, "after_loss": 0.6013},
    "Ethereum": {"after_win": 0.4675, "after_loss": 0.5736},
    "Solana": {"after_win": 0.4812, "after_loss": 0.6068},
}


def cross_market_side_persistence(asset: str, previous_market_won: Optional[bool] = None) -> float:
    """P(this market's first entry matches the previous market's first
    entry side), conditioned on whether that previous market's
    first-entry side actually won. `previous_market_won` is Optional
    (default None) for two reasons: (1) every existing call site that
    predates this upgrade keeps working, returning the win/loss-averaged
    rate as a neutral fallback instead of crashing; (2) the caller
    genuinely doesn't know the previous market's outcome yet on a cold
    start (no prior resolution observed this run). 0.5 (no-op, matches
    the original uniform-random behavior) for any asset not in
    CROSS_MARKET_SIDE_PERSISTENCE."""
    spec = CROSS_MARKET_SIDE_PERSISTENCE.get(asset)
    if spec is None:
        return 0.5
    if previous_market_won is None:
        return (spec["after_win"] + spec["after_loss"]) / 2.0
    return spec["after_win"] if previous_market_won else spec["after_loss"]


# ---------------------------------------------------------------------------
# CROSS-MARKET sizing momentum. Added 2026-09-11: real, cross-asset
# finding (BTC r=0.146, ETH r=0.066, SOL r=0.097) that the PREVIOUS
# market's first-entry size, relative to that (asset, regime)'s own
# typical size, predicts the CURRENT market's first-entry size the same
# way -- a slow, EWMA-like decay (barely weakens lag1->lag10: 0.1445->
# 0.1163), not a one-shot echo. Temporally stable across the Aug 7 TWAP
# change (pre r=0.107/post r=0.140). Does NOT extend to hedge sizing
# (r=0.036, too weak to trust) -- scoped to first-entry size only.
#
# Because the real decay is slow rather than lag-1-only, the CALLER
# (bot.py) is expected to feed this a per-asset EWMA of past residuals,
# not a raw last-market lookup -- this function itself is agnostic to
# how its input was computed, same separation of concerns as every other
# multiplier here (behavior_config never owns bot-level state).
#
# Per-asset, quartile points of log(previous residual state) -> mean-
# neutral multiplier on the CURRENT market's first-entry notional.
CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER = {
    "Bitcoin": {-1.32: 0.7978, -0.26: 1.0128, 0.29: 1.0191, 1.10: 1.1702},
    "Ethereum": {-2.11: 0.9366, -0.39: 0.8591, 0.38: 1.0435, 1.22: 1.1608},
    "Solana": {-2.79: 0.9290, -0.37: 0.9170, 0.32: 0.9898, 1.17: 1.1642},
}
_CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER_CAP = 3.0


def cross_market_size_momentum_multiplier(asset: str, prev_size_residual: Optional[float]) -> float:
    """Continuous, mean-neutral multiplier on decide_size's ordinary
    output for a market's first entry, as a function of a per-asset
    rolling residual-size state (log scale, caller-maintained EWMA). 1.0
    (no-op) for any asset not in CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER,
    or when prev_size_residual is unavailable (e.g. no prior market
    tracked yet this run). Flat beyond the measured range."""
    curve = CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER.get(asset)
    if curve is None or prev_size_residual is None:
        return 1.0
    points = sorted(curve.items())
    lo_x, lo_val = points[0]
    hi_x, hi_val = points[-1]
    x = max(lo_x, min(hi_x, prev_size_residual))
    result = hi_val
    for i in range(len(points) - 1):
        p_x, p_val = points[i]
        q_x, q_val = points[i + 1]
        if p_x <= x <= q_x:
            frac = (x - p_x) / (q_x - p_x) if q_x > p_x else 0.0
            result = p_val + frac * (q_val - p_val)
            break
    cap = _CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER_CAP
    return max(1.0 / cap, min(cap, result))


# ---------------------------------------------------------------------------
# BANKROLL-linked sizing. Added 2026-09-12: real, well-powered finding for
# Ethereum/Solana specifically (NOT Bitcoin -- see below) that his first-
# entry size correlates NEGATIVELY with his own running realized P&L for
# that asset -- he sizes DOWN after accumulating profit, UP after a
# drawdown. The opposite of reinvestment/compounding (that hypothesis was
# tested first and rejected -- wrong direction); this reads as "protect
# accumulated gains, take bigger swings to recover from a drawdown."
#
# Validated to the same bar as every implemented finding this session:
#   - real and well-powered (n=1094-1104, |t|=6.56-10.58)
#   - survives removing each variable's own linear time-trend (ruling out
#     "both just happened to drift over the window" -- SOL -0.305->-0.263,
#     ETH -0.194->-0.154)
#   - survives as a PARTIAL correlation controlling for a rolling average
#     of the already-shipped ADVERSE_MOVE_SIZE_MULTIPLIER's own adverse-
#     move signal (~84-93% of the raw effect remains: SOL -0.305->-0.284,
#     ETH -0.194->-0.163) -- confirmed this is NOT a downstream echo of
#     that within-market mechanism, it's a genuinely separate, additional,
#     cross-market/account-level signal
#   - temporally stable across the Aug 7 TWAP change: same negative
#     direction, same rough magnitude, in both eras (pre-TWAP sample is
#     sparser/noisier -- SOL -0.184, ETH -0.095 -- but agrees in sign and
#     order of magnitude with the cleaner post-TWAP numbers above)
#
# Bitcoin explicitly excluded, not just uncalibrated: his BTC sizing is
# 2-2.4x tighter/more consistent than ETH/SOL (log-residual stdev 1.05 vs
# 2.19/2.54), leaving far less room for ANY external factor -- including
# this one -- to register as a detectable relationship, even under BTC's
# own asset-specific P&L (t moves from -1.44 to -1.92, same direction,
# still short of the bar). Consistent with BTC being his flagship, most
# rule-following asset elsewhere this session too (the cleanest match to
# theoretical Kelly math in HIGH, the flattest hedge-fraction-of-arbitrage
# across regimes). Not a data gap to fill in later -- a real asset
# asymmetry, same treatment as CHEAP/MID once was before being separately
# validated for WITHIN_BAND_SIZE_SLOPE.
#
# Quartile points of (cumulative realized P&L for this asset, mean-
# neutral multiplier), built the same way as every other table here:
# quartile-bucket means, normalized so the population average is 1.0.
BANKROLL_PNL_SIZE_MULTIPLIER = {
    "Ethereum": {-238.98: 1.1724, -17.38: 0.9851, 191.66: 0.9824, 416.47: 0.8601},
    "Solana": {-356.36: 1.2420, -262.25: 1.1362, -87.82: 0.8390, 54.75: 0.7830},
}
_BANKROLL_PNL_SIZE_MULTIPLIER_CAP = 3.0


def bankroll_pnl_size_multiplier(asset: str, cum_realized_pnl: Optional[float]) -> float:
    """Continuous, mean-neutral multiplier on decide_size's ordinary
    output for a market's first entry, as a function of this asset's own
    running realized P&L (BANKROLL_PNL_SIZE_MULTIPLIER's calibration is
    against `Ledger.realized_pnl_by_asset()`'s own directional-only
    total, NOT the with-rebates figure -- match that when calling this).
    1.0 (no-op) for any asset not in BANKROLL_PNL_SIZE_MULTIPLIER
    (Bitcoin and the three dormant assets, by design -- see the docstring
    above for why Bitcoin is deliberately excluded, not just
    uncalibrated), or when cum_realized_pnl is unavailable. Flat beyond
    the measured range, same discipline as every other multiplier here."""
    curve = BANKROLL_PNL_SIZE_MULTIPLIER.get(asset)
    if curve is None or cum_realized_pnl is None:
        return 1.0
    points = sorted(curve.items())
    lo_x, lo_val = points[0]
    hi_x, hi_val = points[-1]
    x = max(lo_x, min(hi_x, cum_realized_pnl))
    result = hi_val
    for i in range(len(points) - 1):
        p_x, p_val = points[i]
        q_x, q_val = points[i + 1]
        if p_x <= x <= q_x:
            frac = (x - p_x) / (q_x - p_x) if q_x > p_x else 0.0
            result = p_val + frac * (q_val - p_val)
            break
    cap = _BANKROLL_PNL_SIZE_MULTIPLIER_CAP
    return max(1.0 / cap, min(cap, result))


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
# RECALIBRATED AGAIN 2026-09-08 (later pass, same drift check already
# applied to ENTRY_SIZING_USD/ASSET_REGIME_DISTRIBUTION_PCT/
# SIDE_PERSISTENCE this session -- and see ENTRY_SIZING_USD's CORRECTION
# note: "last 14 days" as queried is really ~2.3 days of real trading
# since a previously unknown 13.6-day total silence ended 2026-09-06
# 14:21 UTC): a MARKET-level breakdown (not trade-level -- hedge
# frequency is "did this market ever go dual-sided", one data point per
# market, so this table's own trust bar is n>=200 MARKETS, much smaller
# in raw count than the trade-level tables' n>=500 but comparably
# meaningful given what it's counting). Most cells stayed too thin even
# over the post-resumption span (as low as 8 markets for Bitcoin/HIGH --
# not touched). Two cells
# cleared both the market-count bar and a genuine, large, significant gap:
#   Bitcoin/MID:  0.6697 -> 0.8018 (n=328 markets, +13.2pp)
#   Ethereum/CHEAP: 0.4006 -> 0.5336 (n=223 markets, +13.3pp)
# Both moved UP -- hedging MORE often recently -- consistent with the
# smaller, more risk-averse recent sizing found in the same pass
# (ENTRY_SIZING_USD's recalibration above). Solana and every other cell
# checked either weren't significant or stayed below the trust bar.
# RECALIBRATED AGAIN 2026-09-09 (autonomous /loop cycle, part of the same
# behavior-over-time drift sweep as SIDE_PERSISTENCE's Solana/CORE fix
# above): checked all 12 live cells against a fresh 7-day window. Only
# Solana/CHEAP cleared both the n>=200 trust bar and a real gap:
# 0.5950 -> 0.7225 (n=227 markets, +12.7pp). Every MID cell (the largest,
# most reliable samples given CHEAP's declining overall share -- see the
# broader regime-mix drift documented in this loop's other changes)
# stayed under 2.5pp, unchanged. CORE/HIGH cells for all three assets
# looked more dramatic on their face but stayed below the trust bar
# (n=8-136) -- not chased, same discipline as every thin cell in this file.
HEDGE_TRIGGER_PROBABILITY = {
    "Bitcoin":     {"CHEAP": 0.6242, "MID": 0.8018, "CORE": 0.4754, "HIGH": 0.2967},
    "Ethereum":    {"CHEAP": 0.5336, "MID": 0.6562, "CORE": 0.5064, "HIGH": 0.2971},
    "Solana":      {"CHEAP": 0.7225, "MID": 0.7286, "CORE": 0.5523, "HIGH": 0.3039},
    "Dogecoin":    {"CHEAP": 0.3515, "MID": 0.4531, "CORE": 0.6209, "HIGH": 0.3041},
    "Hyperliquid": {"CHEAP": 0.3506, "MID": 0.5819, "CORE": 0.5628, "HIGH": 0.3079},
    "BNB":         {"CHEAP": 0.8389, "MID": 0.9004, "CORE": 0.7806, "HIGH": 0.4884},
}

# RECALIBRATED 2026-09-09 (Bitcoin/MID and Solana/MID only): the same
# raw-trade-vs-decision fragmentation issue found for HEDGE_CONTINUATION_*
# below turned out to affect hedge#1 (this table) too -- these values were
# originally measured on raw trade rows, before that distinction existed.
# Re-measured on decision-collapsed data (5s same-side merge), per
# (asset, first_entry_regime), 14-day window, n>=200 markets trust bar
# (matching HEDGE_TRIGGER_PROBABILITY's own bar for this kind of
# once-per-market measurement):
#   Bitcoin/MID: 0.2756 -> 0.4764 (n=345, +72.9%)
#   Solana/MID:  0.2760 -> 0.6437 (n=233, +133.2%)
# Every OTHER cell stayed below n=200 and was left unchanged. CHEAP-band
# cells specifically were left alone at that time even where n looked
# sufficient, because their decision-based ratios came back nonsensically
# large (1332% for Bitcoin/CHEAP, up to 2396% for Solana/CHEAP) -- flagged
# as SCOUT's territory bleeding in (a genuinely tiny CHEAP first entry
# getting overtaken by a much bigger real conviction bet, not insurance
# sizing) and explicitly left unguessed-at pending a real fix.
#
# CHEAP-band cells FIXED 2026-09-09 (same night, later pass): the missing
# piece was a floor on the denominator, not the 5s-merge step above (which
# was already being applied and still gave the nonsensical numbers this
# comment used to cite). Reconstructed every (BTC/ETH/SOL) market
# chronologically, replicating strategy.py's own real-time logic exactly
# (running cost_by_side, first opposite-side decision after collapsing ==
# the hedge_count==0 case) -- but excluding cases where the "dominant"
# position at hedge time is itself still scout/fragment-sized (<$1
# notional, roughly the real orderMinSize=5-shares floor at CHEAP prices).
# Without that floor, a normal-sized second decision divided by a $0.02
# scout denominator is exactly what produced 1332%/2396% -- with it, real
# medians land far lower and stable across floor choices ($0.50-$3 all
# agree within ~20%): Bitcoin 2.6360, Ethereum 1.9937, Solana 0.9230
# (n=337/402/433, 21-day window). Ratios >1.0 are real, not a bug: a CHEAP
# first entry is a low-conviction feeler by construction, and this is
# specifically the situation where a much bigger real conviction bet often
# follows on the other side -- that's a bigger number than it's "hedging"
# by design, and strategy.py's is_hedge classification (opposite side from
# current running dominant) captures exactly that structural event
# regardless of whether "insurance" is the right economic label for it.
# Dogecoin/Hyperliquid/BNB CHEAP cells are untouched -- not currently
# live-traded (see the asset-basket timeline finding) and not part of
# either live bot's --assets scope, so recalibrating them isn't useful
# right now and there's no fresh data to do it with anyway.
HEDGE_SIZE_RATIO = {
    "Bitcoin":     {"CHEAP": 2.6360, "MID": 0.4764, "CORE": 0.1062, "HIGH": 0.0306},
    "Ethereum":    {"CHEAP": 1.9937, "MID": 0.2511, "CORE": 0.0822, "HIGH": 0.0204},
    "Solana":      {"CHEAP": 0.9230, "MID": 0.6437, "CORE": 0.0625, "HIGH": 0.0159},
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


# ---------------------------------------------------------------------------
# LIQUIDITY-conditioned hedge trigger. Added 2026-09-10: confirmed, real,
# well-powered correlation between a market's liquidity and whether he
# ever hedges it at all -- checked twice this session (original t=4.793,
# n=869/285 flagged low-power; rechecked with more data, t=5.263,
# n=1023/322, held up and strengthened) -- dual-sided (hedged) real
# markets averaged $9354.72 liquidity vs $7787.05 for single-sided ones.
# This IS an action-level measurement (whether he hedged), not an
# outcome/win-rate correlation -- unlike a separately-tested price-
# velocity signal that correlated with OUTCOME but not his own SIZE, and
# was correctly declined for that reason. Liquidity here is Gamma's own
# liquidityNum field (see Market.liquidity's docstring in
# market_discovery.py for why this is NOT the same quantity as
# BookState.total_depth_usd()) -- the calibration below is measured
# against, and this must stay paired with, that specific field.
#
# Per (asset), 4 quartile points from market liquidity-at-first-snapshot
# vs. real dual-sided rate, ratio to that asset's own overall hedge rate
# (mean-neutral by construction -- same principle as TTC_SIZE_MULTIPLIER
# and WITHIN_BAND_SIZE_SLOPE). BTC/ETH/SOL only (the three live-traded
# assets; n=624/601/599 markets respectively, all comfortably above the
# n>=200-market trust bar this file uses for once-per-market measurements).
# Bitcoin's shape is clean and monotonic; Ethereum/Solana are noisier in
# the middle quartiles but agree in the same overall direction (highest
# quartile meaningfully above 1.0 in all three: 1.07/1.14/1.11) --
# interpolated as-measured rather than smoothed into a false monotonic
# shape that isn't actually in the data.
HEDGE_LIQUIDITY_MULTIPLIER = {
    "Bitcoin": {8590: 0.9336, 13072: 0.9577, 15927: 1.0382, 19828: 1.0704},
    "Ethereum": {4191: 1.0128, 7378: 0.9015, 9076: 0.9460, 10100: 1.1388},
    "Solana": {2504: 1.0214, 3688: 0.8715, 5007: 0.9933, 5792: 1.1115},
}
_HEDGE_LIQUIDITY_MULTIPLIER_CAP = 3.0  # symmetric clamp on the FINAL probability multiplier


def hedge_liquidity_multiplier(asset: str, liquidity: Optional[float]) -> float:
    """Continuous, mean-neutral multiplier on the first-hedge trigger
    probability as a function of a market's liquidity. 1.0 (no-op) for
    any asset not in HEDGE_LIQUIDITY_MULTIPLIER, or when liquidity is
    unavailable (e.g. Gamma didn't return the field). Flat beyond the
    measured range -- clamped to the nearest endpoint, never extrapolated,
    same discipline as TTC_SIZE_MULTIPLIER."""
    curve = HEDGE_LIQUIDITY_MULTIPLIER.get(asset)
    if curve is None or liquidity is None:
        return 1.0
    points = sorted(curve.items())
    lo_liq, lo_val = points[0]
    hi_liq, hi_val = points[-1]
    liq = max(lo_liq, min(hi_liq, liquidity))
    for i in range(len(points) - 1):
        p_liq, p_val = points[i]
        q_liq, q_val = points[i + 1]
        if p_liq <= liq <= q_liq:
            frac = (liq - p_liq) / (q_liq - p_liq) if q_liq > p_liq else 0.0
            return p_val + frac * (q_val - p_val)
    return hi_val  # defensive fallback; the clamp above makes this unreachable


def hedge_size_ratio(asset: str, primary_regime: str) -> float:
    return HEDGE_SIZE_RATIO.get(asset, {}).get(primary_regime, _DEFAULT_HEDGE_SIZE_RATIO)


# ---------------------------------------------------------------------------
# ADVERSE-MOVE-conditioned hedge size. Added 2026-09-10: real, well-powered,
# cross-asset-generalizing finding that real FIRST-hedge SIZE scales with
# how far price has moved AGAINST the primary position since entry -- a
# dimension HEDGE_SIZE_RATIO has never had (it's a single fixed number per
# asset x first_entry_regime, set once and never adjusted for how much
# worse things get before the hedge actually lands).
#
# adverse_move = primary_entry_price - primary_current_price_at_hedge_time
# (positive = moved against the primary side). Measured on decision-
# collapsed real trades (5s same-side merge -- same discipline as
# MAX_HEDGE_COUNT_PER_MARKET's own corrected re-check, avoiding the raw-
# fill-fragmentation bug that has bitten this file more than once),
# restricted to ONLY each market's first hedge (matching exactly what this
# multiplier is applied to below -- HEDGE_CONTINUATION_SIZE_RATIO governs
# hedge_count>=1 separately and isn't part of this measurement).
#
# Correlation of log(hedge_ratio) vs adverse_move, CONTROLLING FOR
# first_entry_regime (fit separately within each of the 4 regimes to rule
# out this just being regime relabeling in disguise): r=0.44-0.60 (BTC),
# r=0.43-0.50 (ETH), r=0.18-0.37 (SOL) pooling all hedges; restricted to
# first-hedge-only (this table's actual scope) the correlation is smaller
# but still robust: BTC r=0.33/t=32.5 (n=8,670), ETH r=0.29/t=24.1
# (n=6,387), SOL r=0.22/t=20.6 (n=8,237) -- every cell clears this file's
# own |t|>=2.58 bar by a wide margin. TTC confound checked and ruled out:
# r(adverse_move, seconds_to_close) is essentially zero (-0.065 to 0.022
# across assets) -- this is NOT a proxy for how late in the window the
# hedge happens (already modeled via TTC_SIZE_MULTIPLIER).
#
# Per-asset (regime-pooled -- per-regime slopes were consistent, 2.3-4.7
# with no systematic pattern by regime, so a 16-cell table would overfit
# noise this data doesn't support), 4 quartile points of adverse_move vs.
# MEDIAN-based, median-neutral multiplier on HEDGE_SIZE_RATIO's base value
# -- median chosen over mean (unlike HEDGE_LIQUIDITY_MULTIPLIER's rate-
# based mean) because hedge_ratio is extremely right-skewed and
# HEDGE_SIZE_RATIO's own base cells were themselves calibrated on medians
# for exactly that reason (see its CHEAP-band fix note above: "real
# medians land far lower and stable across floor choices" where raw means
# were not trustworthy). Interpolated as-measured, not forced monotonic --
# all three assets show q2 (near-zero move) higher than q3 (largest
# adverse move), a consistent shape kept as real per this file's own
# "never smoothed to look more uniform" rule (see module docstring).
ADVERSE_MOVE_SIZE_MULTIPLIER = {
    "Bitcoin": {-0.35: 0.3366, -0.10: 0.5621, 0.05: 2.8117, 0.27: 2.0852},
    "Ethereum": {-0.48: 0.3405, -0.13: 0.4466, 0.02: 5.0748, 0.21: 2.3048},
    "Solana": {-0.43: 0.4320, -0.14: 0.4885, 0.02: 2.6819, 0.21: 2.2770},
}
_ADVERSE_MOVE_SIZE_MULTIPLIER_CAP = 6.0  # symmetric clamp on the final size
# multiplier -- wider than HEDGE_LIQUIDITY_MULTIPLIER's 3.0 cap since the
# measured range itself reaches ~5.07 (ETH q2); mirrors that table's
# ~2.6x-over-max-measured headroom convention.


def adverse_move_size_multiplier(asset: str, adverse_move: Optional[float]) -> float:
    """Continuous, median-neutral multiplier on HEDGE_SIZE_RATIO's base
    value as a function of how far price has moved against the primary
    position since entry (positive = against). 1.0 (no-op) for any asset
    not in ADVERSE_MOVE_SIZE_MULTIPLIER, or when adverse_move is
    unavailable (e.g. first_entry_price wasn't recorded). Flat beyond the
    measured range -- clamped to the nearest endpoint, never extrapolated,
    same discipline as HEDGE_LIQUIDITY_MULTIPLIER/TTC_SIZE_MULTIPLIER.
    Final result clamped to _ADVERSE_MOVE_SIZE_MULTIPLIER_CAP."""
    curve = ADVERSE_MOVE_SIZE_MULTIPLIER.get(asset)
    if curve is None or adverse_move is None:
        return 1.0
    points = sorted(curve.items())
    lo_move, lo_val = points[0]
    hi_move, hi_val = points[-1]
    move = max(lo_move, min(hi_move, adverse_move))
    result = hi_val
    for i in range(len(points) - 1):
        p_move, p_val = points[i]
        q_move, q_val = points[i + 1]
        if p_move <= move <= q_move:
            frac = (move - p_move) / (q_move - p_move) if q_move > p_move else 0.0
            result = p_val + frac * (q_val - p_val)
            break
    cap = _ADVERSE_MOVE_SIZE_MULTIPLIER_CAP
    return max(1.0 / cap, min(cap, result))


# CONFIRMED LIVE (2026-09-12, external-bot-strategies-loop-progress
# iterations 9-10): the absolute current hedge-side price predicts hedge
# SIZE beyond what ADVERSE_MOVE_SIZE_MULTIPLIER's delta-from-entry already
# captures -- not a redundant re-derivation of that mechanism. Verified
# with a PARTIAL correlation, not a raw one (raw price and adverse_move
# are collinear, r=0.749): after fitting log(shares) ~ a + b*adverse_move
# by OLS and taking the RESIDUAL, that residual still correlates with the
# raw hedge price (partial corr r=+0.103, t=+5.16, n=2,499, first hedges
# only). The shape is a real, consistent U across all three assets --
# extra sizing at BOTH price extremes (near-certain win AND near-certain
# loss for the dominant side), not a one-sided effect -- reads as "near
# true certainty in either direction deserves more conviction than a
# purely linear function of how far price has traveled." Calibrated the
# same way as every other multiplier in this file: fit the OLS baseline,
# take exp(residual) as a per-trade multiplicative correction, bucket by
# hedge-price quartile, take the MEDIAN correction per quartile (median,
# not mean -- same reasoning as ADVERSE_MOVE_SIZE_MULTIPLIER's own choice:
# size is heavily right-skewed), then rescale so the 4 quartile points are
# median-neutral (average to 1.0, doesn't change the population's overall
# calibration, only redistributes it by price). Applied MULTIPLICATIVELY
# on top of HEDGE_SIZE_RATIO * ADVERSE_MOVE_SIZE_MULTIPLIER, first hedge
# only (hedge_count==0), same call site as both of those.
ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER = {
    "Bitcoin": {0.11: 1.3183, 0.39: 0.6725, 0.70: 0.6817, 0.94: 1.5730},
    "Ethereum": {0.09: 1.2663, 0.41: 0.6629, 0.81: 0.7337, 0.97: 2.0510},
    "Solana": {0.13: 1.1448, 0.44: 0.4986, 0.79: 0.8552, 0.95: 2.0437},
}
_ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER_CAP = 3.0  # measured range tops out
# at ~2.05 (ETH/SOL q4); same cap convention as HEDGE_LIQUIDITY_MULTIPLIER.


def absolute_price_hedge_size_multiplier(asset: str, hedge_price: Optional[float]) -> float:
    """Continuous, median-neutral multiplier on HEDGE_SIZE_RATIO's base
    value (applied alongside adverse_move_size_multiplier, not instead of
    it) as a function of the hedge side's own ABSOLUTE current price --
    a real, confirmed-independent residual effect beyond the delta-from-
    entry mechanism ADVERSE_MOVE_SIZE_MULTIPLIER already models. 1.0
    (no-op) for any asset not in ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER, or
    when hedge_price is unavailable. Flat beyond the measured range --
    clamped to the nearest endpoint, never extrapolated, same discipline
    as every other multiplier in this file. Final result clamped to
    _ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER_CAP."""
    curve = ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER.get(asset)
    if curve is None or hedge_price is None:
        return 1.0
    points = sorted(curve.items())
    lo_price, lo_val = points[0]
    hi_price, hi_val = points[-1]
    price = max(lo_price, min(hi_price, hedge_price))
    result = hi_val
    for i in range(len(points) - 1):
        p_price, p_val = points[i]
        q_price, q_val = points[i + 1]
        if p_price <= price <= q_price:
            frac = (price - p_price) / (q_price - p_price) if q_price > p_price else 0.0
            result = p_val + frac * (q_val - p_val)
            break
    cap = _ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER_CAP
    return max(1.0 / cap, min(cap, result))


# ---------------------------------------------------------------------------
# WEEKEND-conditioned hedge trigger. Added 2026-09-10: real, well-powered
# finding that his dual-sided (hedged) rate is meaningfully higher on
# weekends than weekdays -- weekday=58.87% (n=10,183 BTC markets),
# weekend=68.2% (n=3,902), z=9.9. This is a per-market binary measurement
# ("did this market ever go dual-sided"), not a hedge-COUNT, so it isn't
# exposed to the raw-fill-vs-decision fragmentation bug that affects
# count-based measurements in this file (see MAX_HEDGE_COUNT_PER_MARKET's
# own docstring in config.py, and the corrected 1.57%-not-16% re-check
# that ruled out raising that cap the same night this was added).
#
# Full causal chain confirmed, not just a correlation: real BTC spot
# volatility is ~47% lower on weekends -> markets stay in MID/undecided
# territory far more often (48.04% of weekend trades land in MID vs
# 36.93% weekday) -> MID is where hedging concentrates (already
# established via HEDGE_TRIGGER_PROBABILITY's own MID cells being the
# highest of the four regimes) -> higher weekend hedge rate. Checked and
# ruled out two plausible confounds before landing on this: weekend book-
# depth/liquidity via a tick-density proxy (no effect, but that proxy
# measures update frequency not $ depth, so a depth-based mechanism isn't
# fully ruled out) and cross-asset generalization (ETH/SOL do NOT show
# this shift -- BTC-specific, same recurring pattern as several other
# BTC-vs-ETH/SOL divergences found the same session).
#
# HEDGE_TRIGGER_PROBABILITY itself was calibrated from all-days blended
# data, which skews weekday-heavy (5/7 of days, and weekday volume is
# also higher per the day-of-week finding) -- so weekday stays a 1.0
# no-op baseline and only weekend gets scaled up, same pattern as every
# other mean-adjustment table in this file (TTC_SIZE_MULTIPLIER,
# HEDGE_LIQUIDITY_MULTIPLIER). BTC-only for now, matching the scope of
# what was actually verified -- 1.0 (no-op) for every other asset.
WEEKEND_HEDGE_MULTIPLIER = {
    "Bitcoin": 68.2 / 58.87,  # ~1.1585
}


def weekend_hedge_multiplier(asset: str, is_weekend: bool) -> float:
    """1.0 (no-op) on weekdays, for any asset not in
    WEEKEND_HEDGE_MULTIPLIER, or when is_weekend is None-like/False."""
    if not is_weekend:
        return 1.0
    return WEEKEND_HEDGE_MULTIPLIER.get(asset, 1.0)


# ---------------------------------------------------------------------------
# Hedge TIMING. Added 2026-09-08, correcting a confirmed live mismatch:
# decide_hedge previously checked ONLY at entry_count==1 (the market's
# literal 2nd entry) using HEDGE_TRIGGER_PROBABILITY directly as a
# single-shot probability there. Fresh re-derivation from the full mirror
# (803,047 trades / 77,753 markets, 39,228 of them dual-sided) found the
# real first-opposite-side-entry index distribution is nowhere near that
# concentrated: only 23.5% of real hedges land at index 1; median index
# is 4, mean 5.59, and coverage only reaches 85% by index 10. The
# single-check design was structurally missing the majority of when
# hedges actually happen -- not a sizing or trigger-rate problem, a
# TIMING one.
#
# _HEDGE_HAZARD_SHAPE is the empirical hazard curve -- P(first opposite-
# side entry happens exactly at this entry_count | market reached this
# entry_count without having hedged yet) -- for entry_count = 1..10
# (covers 85.4% of real hedges; the long tail past 10 contributes little
# and this bot's 90-second timing cutoff usually ends a market's entries
# well before an index that deep anyway).
#
# DISCLOSED DEVIATION from this file's own "never blended across assets"
# rule (see module docstring): this shape is pooled across all six
# assets, not measured per-asset. Per-(asset, regime) hazard curves would
# need to split ~39k dual-sided markets across 24 cells -- far too thin
# to trust the SHAPE at that granularity (unlike HEDGE_TRIGGER_PROBABILITY
# and HEDGE_SIZE_RATIO above, which only need one number per cell and
# have enough support for that). The compromise, standard in survival
# analysis (a shared baseline hazard, scaled per group -- the same idea
# as a Cox proportional-hazards model): use this one pooled TIMING shape
# for every cell, but rescale it per (asset, regime) so the cumulative
# probability across the whole attempt sequence exactly reproduces that
# cell's own already-calibrated HEDGE_TRIGGER_PROBABILITY. Nothing about
# the OVERALL per-cell hedge frequency changes -- only the shape of WHEN,
# within a market, that single hedge gets attempted.
_HEDGE_HAZARD_SHAPE = [
    0.1281, 0.0998, 0.0894, 0.0861, 0.0805,
    0.0799, 0.0811, 0.0793, 0.0774, 0.0759,
]


def _solve_hazard_scale(shape, target_cumulative, lo=0.0, hi=50.0, iters=60):
    """Binary search for the odds-scale k such that applying it to every
    entry of `shape` via the proportional-odds transform (odds = h/(1-h),
    scaled_odds = k*odds, scaled_h = scaled_odds/(1+scaled_odds) -- always
    stays in [0, 1), unlike naive linear scaling of h itself) yields a
    survival-curve cumulative probability equal to target_cumulative.
    cumulative_for(k) is monotonically increasing in k, so bisection is
    exact to float precision within `iters` steps."""
    def cumulative_for(k):
        survival = 1.0
        for h in shape:
            odds = h / (1 - h)
            scaled_h = (k * odds) / (1 + k * odds)
            survival *= (1 - scaled_h)
        return 1 - survival

    if target_cumulative <= 0:
        return 0.0
    for _ in range(iters):
        mid = (lo + hi) / 2
        if cumulative_for(mid) < target_cumulative:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _build_hedge_attempt_hazards():
    """Precomputed once at import time (24 cells x ~60 bisection steps x
    10 hazard evaluations -- trivial cost): (asset, regime) -> list of
    per-attempt hazards for entry_count 1..len(_HEDGE_HAZARD_SHAPE)."""
    out = {}
    for asset, regimes in HEDGE_TRIGGER_PROBABILITY.items():
        out[asset] = {}
        for regime, target in regimes.items():
            k = _solve_hazard_scale(_HEDGE_HAZARD_SHAPE, target)
            hazards = []
            for h in _HEDGE_HAZARD_SHAPE:
                odds = h / (1 - h)
                scaled_h = (k * odds) / (1 + k * odds)
                hazards.append(scaled_h)
            out[asset][regime] = hazards
    return out


HEDGE_ATTEMPT_HAZARDS = _build_hedge_attempt_hazards()


def hedge_attempt_hazard(asset: str, primary_regime: str, attempt_index: int) -> float:
    """attempt_index is entry_count (1 = the market's 2nd entry, matching
    decide_hedge's old single check point; up to len(_HEDGE_HAZARD_SHAPE)).
    Returns 0.0 past the modeled window or for an unknown cell -- the
    caller (decide_hedge) treats that as "never triggers from here",
    exactly like today's behavior once a market ages past where a hedge
    was ever going to happen."""
    hazards = HEDGE_ATTEMPT_HAZARDS.get(asset, {}).get(primary_regime)
    if not hazards or attempt_index < 1 or attempt_index > len(hazards):
        return 0.0
    return hazards[attempt_index - 1]


# ---------------------------------------------------------------------------
# ADVERSE-MOVE-conditioned hedge TRIGGER (as opposed to SIZE -- see
# ADVERSE_MOVE_SIZE_MULTIPLIER/ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER
# above, which cover how BIG a hedge is once one happens). Added
# 2026-09-10, same night: real, well-powered finding that WHETHER he
# hedges at all also scales with adverse_move magnitude, on top of (not
# explained by) primary_regime and attempt_index, both already modeled
# by hedge_attempt_hazard above.
#
# adverse_move = primary_entry_price - primary_current_price (positive =
# against the primary side), same since-origin definition as the size
# multipliers. Deliberately scoped to adverse_move >= 0 ONLY -- the
# favorable-move side (price moved a lot IN his favor) shows a real but
# DIFFERENT, non-monotonic (U-shaped) pattern that looks like the same
# SCOUT-bleeding-into-CHEAP-classification effect already documented in
# HEDGE_SIZE_RATIO's own CHEAP-band fix note above (an opportunistic
# "harvest the now-cheap confirmed loser" bet gets counted as a "hedge"
# by the is_hedge==opposite-side-of-dominant classification, even though
# it isn't economically insurance). Left unmodeled here on purpose, same
# discipline as this file's other multipliers leaving what isn't cleanly
# measured at a no-op -- adverse_move_hedge_trigger_multiplier returns
# 1.0 for any adverse_move < 0.
#
# Survived every confound check this file's other multipliers were held
# to, run BEFORE building, not after:
#   - primary_regime-controlled: monotonic in 11/12 (asset, regime)
#     cells (adverse-only slice). One exception (Solana/HIGH) was noisy
#     at n=199 -- not independently confirmed there, same disclosure
#     pattern as the continuation-hedge multiplier's Solana/HIGH caveat.
#   - attempt_index-controlled: fixing attempt_index==1 EXACTLY (zero
#     confound by construction) still shows a clean gradient: BTC
#     12.1%->33.4% (z~14.7), ETH 8.7%->14.7% (z~5.5), SOL 8.9%->17.6%
#     (z~7.5) across adverse-move quartiles. Holds independently at
#     attempt_index 2/3/4+ too.
#   - TTC confound: unlike the SIZE multipliers (where TTC was ~0
#     correlation and cleanly ruled out), here adverse_move IS correlated
#     with TTC (r=-0.29 BTC/-0.12 ETH/-0.23 SOL -- bigger moves tend to
#     happen later, which makes mechanical sense). Controlled directly by
#     TTC-stratifying (early ttc>150s vs late ttc<=150s, attempt_index==1
#     only): the adverse-move gradient survives independently in BOTH
#     strata for all three assets (e.g. BTC early 8.0%->22.5%, late
#     26.6%->44.8%) -- lateness raises the baseline (already modeled via
#     the hazard curve), adverse_move adds a real, separate gradient on
#     top of it.
#
# Per-asset (pooled across attempt indices 1-4+, adverse-only), 4
# quartile points, RATE-based mean-neutral multiplier on
# hedge_attempt_hazard's output -- same construction as
# HEDGE_LIQUIDITY_MULTIPLIER (a rate, not a skewed size ratio, so mean
# rather than median is the right center here). First breakpoint pinned
# at move=0.0 (the empirical q0 median move was 0.01-0.02, close enough
# to round to the no-op boundary) using its own measured multiplier --
# NOT forced to 1.0, so there IS a real, disclosed discontinuity right at
# the adverse/favorable boundary (multiplier drops from 1.0 just below
# zero to ~0.61-0.78 just above it, before climbing back above 1.0 at
# larger adverse moves). This reflects real data, not measurement noise:
# right around zero is the calm trough between the favorable side's
# SCOUT-bleed spike (excluded above) and the adverse side's genuine
# escalation -- kept as-measured rather than smoothed into a fake
# continuous curve, same "never smoothed to look more uniform" rule this
# whole file follows (see module docstring).
ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER = {
    "Bitcoin": {0.0: 0.6123, 0.07: 0.7856, 0.15: 1.0786, 0.30: 1.5235},
    "Ethereum": {0.0: 0.7620, 0.07: 0.8913, 0.14: 0.8900, 0.26: 1.4567},
    "Solana": {0.0: 0.7804, 0.08: 0.8631, 0.15: 0.9977, 0.27: 1.3587},
}
_ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER_CAP = 3.0  # same cap as
# HEDGE_LIQUIDITY_MULTIPLIER -- measured range tops out at ~1.52 (Bitcoin
# q3), comfortably inside it.


def adverse_move_hedge_trigger_multiplier(asset: str, adverse_move: Optional[float]) -> float:
    """Continuous, mean-neutral multiplier on hedge_attempt_hazard's
    output as a function of how far price has moved against the primary
    position since entry. 1.0 (no-op) for any asset not in
    ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER, when adverse_move is
    unavailable, OR when adverse_move < 0 (favorable move -- deliberately
    unmodeled, see this section's docstring). Flat beyond the measured
    range, same discipline as every other multiplier in this file. Final
    result clamped to _ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER_CAP -- the
    caller (decide_hedge) is still responsible for clamping the final
    PROBABILITY to [0, 1], same as it already does for
    hedge_liquidity_multiplier/weekend_hedge_multiplier."""
    if adverse_move is None or adverse_move < 0:
        return 1.0
    curve = ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER.get(asset)
    if curve is None:
        return 1.0
    points = sorted(curve.items())
    lo_move, lo_val = points[0]
    hi_move, hi_val = points[-1]
    move = max(lo_move, min(hi_move, adverse_move))
    result = hi_val
    for i in range(len(points) - 1):
        p_move, p_val = points[i]
        q_move, q_val = points[i + 1]
        if p_move <= move <= q_move:
            frac = (move - p_move) / (q_move - p_move) if q_move > p_move else 0.0
            result = p_val + frac * (q_val - p_val)
            break
    cap = _ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER_CAP
    return max(1.0 / cap, min(cap, result))


# ---------------------------------------------------------------------------
# CROSS-MARKET hedge-RATE persistence. Added 2026-09-11: real, cross-asset
# finding (r=0.25-0.35, among the strongest correlations found all
# night) that how hedge-heavy the PREVIOUS market was predicts how
# hedge-heavy the CURRENT one will be -- survives being confound-checked
# against current-market regime (fixed to MID, still r=0.2525) and
# against weekend (survives independently in both subsets). Calibrated
# on RATE (hedges / total decisions in a market), not raw hedge count --
# a follow-up check found raw count partly conflates genuine hedge
# propensity with "markets with more total activity have more hedges
# too"; rate isolates the propensity-specific signal (smaller effect,
# r=0.10, but not double-counting general activity clustering).
#
# HONEST CAVEAT, disclosed rather than smoothed over: the decay shape
# barely weakens even out to 20 markets back (~100+ minutes), which is
# more consistent with some real market-condition persistence (which a
# bot already inherits for free from live prices) than a purely
# HIS-OWN-specific behavioral echo -- tested the most obvious version of
# that alternative (real prior-30min realized volatility -> hedge count)
# directly and it does NOT explain the pattern (wrong sign), which
# restores some confidence, but the mechanism isn't fully pinned down.
# Treat this as the most speculative of tonight's cross-market
# multipliers.
#
# Per-asset, quartile points of PREVIOUS market's hedge rate -> mean-
# neutral multiplier on hedge_attempt_hazard's output for the CURRENT
# market. Ethereum has only 3 points (its first two empirical quartiles
# both landed at prev_rate=0.0 -- merged into one point using each
# quartile's own n as weight) instead of 4.
CROSS_MARKET_HEDGE_RATE_MULTIPLIER = {
    "Bitcoin": {0.0: 0.7761, 0.0843: 1.0300, 0.3638: 1.1056, 0.6138: 1.0883},
    "Ethereum": {0.0: 0.8023, 0.2353: 1.1693, 0.5663: 1.2258},
    "Solana": {0.0: 0.7585, 0.0882: 1.0570, 0.3419: 1.0927, 0.5972: 1.0917},
}
_CROSS_MARKET_HEDGE_RATE_MULTIPLIER_CAP = 3.0  # same cap tier as HEDGE_LIQUIDITY_MULTIPLIER


def cross_market_hedge_rate_multiplier(asset: str, prev_hedge_rate: Optional[float]) -> float:
    """Continuous, mean-neutral multiplier on hedge_attempt_hazard's
    output as a function of the PREVIOUS market's hedge rate
    (hedges/total decisions). 1.0 (no-op) for any asset not in
    CROSS_MARKET_HEDGE_RATE_MULTIPLIER, or when prev_hedge_rate is
    unavailable (e.g. no previous market yet this run). Flat beyond the
    measured range, same discipline as every other multiplier in this
    file."""
    curve = CROSS_MARKET_HEDGE_RATE_MULTIPLIER.get(asset)
    if curve is None or prev_hedge_rate is None:
        return 1.0
    points = sorted(curve.items())
    lo_rate, lo_val = points[0]
    hi_rate, hi_val = points[-1]
    rate = max(lo_rate, min(hi_rate, prev_hedge_rate))
    result = hi_val
    for i in range(len(points) - 1):
        p_rate, p_val = points[i]
        q_rate, q_val = points[i + 1]
        if p_rate <= rate <= q_rate:
            frac = (rate - p_rate) / (q_rate - p_rate) if q_rate > p_rate else 0.0
            result = p_val + frac * (q_val - p_val)
            break
    cap = _CROSS_MARKET_HEDGE_RATE_MULTIPLIER_CAP
    return max(1.0 / cap, min(cap, result))


# ---------------------------------------------------------------------------
# CONVICTION-conditioned hedge trigger. Added 2026-09-11: real, regime-
# confound-checked finding that the market's own FIRST-entry size,
# relative to that (asset, regime)'s own typical first-entry size,
# predicts whether THIS SAME market ends up hedged -- a smaller-than-
# typical first entry means a higher chance of hedging later, holding
# regime fixed. Survives weekend and liquidity confounds too. Scoped to
# MID/CORE/HIGH only -- CHEAP shows no such relationship (checked and
# confirmed flat: 4.05%/9.76%/16.75%/17.68%-style near-constant win-rate
# and hedge patterns across conviction levels there, consistent with
# CHEAP being scout/floor-lot-dominated with little real size variance
# to carry signal). Confirmed temporally stable across the Aug 7 TWAP
# change (both eras show the same real decline, MID regime).
#
# HONEST CAVEAT: the WHY is unresolved. Tested and ruled out the most
# obvious explanation (smaller first entry = lower conviction = more
# often WRONG, needing correction) directly -- trade-count win rate does
# NOT differ between below/above-median first entries in any regime.
# Whatever the real mechanism is, it isn't a simple accuracy pathway.
# Real, usable, currently-unexploited signal; not a solved mystery.
#
# Per-asset per-regime, quartile points of log(first_entry_size /
# regime_median_first_entry_size) -> mean-neutral multiplier on
# hedge_attempt_hazard's output for THIS SAME market (not cross-market --
# uses activity.first_entry_price/notional already recorded for the
# CURRENT market, available the moment a hedge decision is evaluated).
CONVICTION_HEDGE_MULTIPLIER = {
    "Bitcoin": {
        "MID": {-1.2847: 1.1663, -0.2979: 1.0246, 0.3362: 0.9836, 1.1762: 0.8256},
        "CORE": {-1.2467: 1.2145, -0.2764: 1.0425, 0.2869: 0.9565, 1.0314: 0.7868},
        "HIGH": {-1.4306: 1.1379, -0.2219: 1.0862, 0.2480: 0.9310, 0.9495: 0.8448},
    },
    "Ethereum": {
        "MID": {-1.7387: 1.0774, -0.3069: 1.0851, 0.3249: 0.9659, 1.2327: 0.8715},
        "CORE": {-1.7015: 1.1179, -0.3338: 1.0792, 0.2698: 0.9817, 1.0705: 0.8216},
        "HIGH": {-2.0675: 1.1027, -0.4785: 0.9398, 0.3429: 1.2335, 1.3050: 0.7245},
    },
    "Solana": {
        "MID": {-2.9161: 1.0375, -0.2787: 1.0097, 0.2772: 0.9953, 1.1344: 0.9575},
        "CORE": {-2.2315: 1.1244, -0.2211: 1.0358, 0.2642: 0.9608, 1.0403: 0.8790},
        "HIGH": {-1.7171: 1.2411, -0.2722: 1.1757, 0.3308: 1.0669, 1.1614: 0.5193},
    },
}
_CONVICTION_HEDGE_MULTIPLIER_CAP = 3.0


def conviction_hedge_multiplier(asset: str, regime: str, conviction_log_ratio: Optional[float]) -> float:
    """Continuous, mean-neutral multiplier on hedge_attempt_hazard's
    output as a function of log(this market's first-entry size /
    (asset, regime)'s typical first-entry size). 1.0 (no-op) for any
    (asset, regime) not in CONVICTION_HEDGE_MULTIPLIER (includes CHEAP
    and the three dormant assets by design), or when
    conviction_log_ratio is unavailable. Flat beyond the measured range."""
    curve = CONVICTION_HEDGE_MULTIPLIER.get(asset, {}).get(regime)
    if curve is None or conviction_log_ratio is None:
        return 1.0
    points = sorted(curve.items())
    lo_x, lo_val = points[0]
    hi_x, hi_val = points[-1]
    x = max(lo_x, min(hi_x, conviction_log_ratio))
    result = hi_val
    for i in range(len(points) - 1):
        p_x, p_val = points[i]
        q_x, q_val = points[i + 1]
        if p_x <= x <= q_x:
            frac = (x - p_x) / (q_x - p_x) if q_x > p_x else 0.0
            result = p_val + frac * (q_val - p_val)
            break
    cap = _CONVICTION_HEDGE_MULTIPLIER_CAP
    return max(1.0 / cap, min(cap, result))


# ---------------------------------------------------------------------------
# HEDGE CONTINUATION. Added 2026-09-09, closing the multi-hedge gap found
# this session: decide_hedge above only ever fired ONCE per market
# (hedge_placed permanently locked it after the first success). Real data
# says otherwise -- and this took two passes to get right. The first pass
# (raw trade rows) found a wild-looking tail: median 5 hedge-side trades
# per market, 25% of markets with 7+. That turned out to be substantially
# inflated: 63.5% of raw trade rows are CLOB FRAGMENTS of one order
# (median 2s gap between them), not separate decisions -- collapsing
# same-side trades within 5s of each other into one decision (median gap
# between genuine decisions: 20s) revised the picture down a lot, but a
# real multi-hedge pattern still survives it: only 39.8% of dual-sided
# markets stop at 1 hedge decision; 60.2% get a 2nd.
#
# The conditional continuation rate (P(one more hedge | got this many
# already), measured on decisions) is remarkably stable once past the
# first hedge -- close enough to a flat curve that one pooled lookup
# captures the shape without needing the full per-attempt hazard/
# bisection machinery above (that machinery solves WITHIN-cycle TIMING
# for a single event; this is a simpler between-event continuation rate):
#   P(2nd hedge | 1st happened) = 60.2%   P(3rd | 2nd) = 59.8%
#   P(4th | 3rd)  = 53.3%                 P(5th+ | 4th+) ~= 50.5% (blended)
#
# Sizing is the same decision-corrected pass, keyed by hedge INDEX (the
# 1-based position of the hedge about to be placed) rather than by
# entry_count, as a ratio to the dominant side's RUNNING cost at that
# moment (not the eventual final total -- unknowable at decision time,
# same reasoning as SCOUT_SIZE_RATIO below):
#   hedge#1 = 70.0% (unchanged -- still HEDGE_SIZE_RATIO/hedge_size_ratio's
#             job; this table only starts at index 2)
#   hedge#2 = 18.6%   hedge#3 = 13.3%   hedge#4+ = 10.6%
#
# CALIBRATED 2026-09-09, 14-day window, BTC/ETH/SOL, decision-collapsed
# (5s same-side merge) trade mirror -- pooled across assets/regimes, the
# same thin-sample tradeoff _HEDGE_HAZARD_SHAPE above already documents:
# splitting this by (asset, regime) too would leave too few genuine
# decisions per cell to trust the shape. DISCLOSED GAP: HEDGE_SIZE_RATIO's
# own hedge#1 calibration was measured on RAW trades, before this
# fragment-vs-decision distinction was discovered -- it may itself be
# under-measuring hedge#1's true size the same way this section's first
# pass was. Not revised here (out of scope for this change, and it's the
# already-tested, deployed mechanism); worth a dedicated recalibration
# pass later.
HEDGE_CONTINUATION_PROBABILITY = {
    1: 0.602,
    2: 0.598,
    3: 0.533,
}
_DEFAULT_HEDGE_CONTINUATION_PROBABILITY = 0.505  # hedge_count >= 4

HEDGE_CONTINUATION_SIZE_RATIO = {
    2: 0.186,
    3: 0.133,
}
_DEFAULT_HEDGE_CONTINUATION_SIZE_RATIO = 0.106  # hedge index >= 4


def hedge_continuation_probability(hedge_count: int) -> float:
    """hedge_count is how many hedge-shaped entries this market already
    has (only called with hedge_count >= 1 -- hedge_count == 0 goes
    through hedge_attempt_hazard above instead, unchanged)."""
    return HEDGE_CONTINUATION_PROBABILITY.get(hedge_count, _DEFAULT_HEDGE_CONTINUATION_PROBABILITY)


def hedge_continuation_size_ratio(hedge_index: int) -> float:
    """hedge_index is the 1-based index of the hedge ABOUT TO BE placed
    (2 = the market's 2nd hedge-shaped entry, etc. -- index 1 is not
    handled here, see HEDGE_SIZE_RATIO/hedge_size_ratio above)."""
    return HEDGE_CONTINUATION_SIZE_RATIO.get(hedge_index, _DEFAULT_HEDGE_CONTINUATION_SIZE_RATIO)


# ---------------------------------------------------------------------------
# ADVERSE-MOVE-conditioned CONTINUATION hedge size. Added 2026-09-10, same
# night as ADVERSE_MOVE_SIZE_MULTIPLIER above (which covers hedge_count==0
# only) -- direct extension of the same confirmed mechanism to hedge_count
# >= 1 (2nd/3rd/4th+ hedges), using the same since-ORIGIN adverse_move
# definition (primary_entry_price - primary_current_price_at_this_hedge).
#
# NOT just assumed to generalize -- deep-research pass run before building:
#   - Regime-controlled (fit separately per first_entry_regime): holds in
#     11 of 12 (asset, regime) cells, r=0.23-0.64, t=4.36-58.17. ONE cell
#     fails this file's own |t|>=2.58 bar: Solana/HIGH, t=2.46 (n=108,
#     thin). Every other cell, including small BTC/HIGH (t=4.36, n=66),
#     clears comfortably. Not chased further per this file's own thin-cell
#     discipline -- the pooled-per-asset curve below is not regime-split,
#     so this doesn't block it, but Solana/HIGH specifically shouldn't be
#     treated as independently confirmed.
#   - TTC confound ruled out again: r(adverse_move, seconds_to_close) is
#     ~0 (-0.076 to 0.028 across assets) -- not a lateness proxy here
#     either.
#   - REAL open question tested, not assumed: does continuation-hedge size
#     track cumulative move SINCE HIS ORIGINAL ENTRY, or incremental move
#     SINCE HIS LAST HEDGE? Tested both as competing hypotheses --
#     since-origin wins clearly (r=0.44-0.57 pooled) over since-last-hedge
#     (r=0.13-0.23, still significant but meaningfully weaker). Confirms
#     reusing MarketActivityState.first_entry_price (not re-anchoring at
#     each hedge) is the empirically better model, not just a convenient
#     reuse of existing plumbing.
#
# Per-asset (all continuation-hedge indices 2/3/4+ pooled -- same
# parsimony rationale as ADVERSE_MOVE_SIZE_MULTIPLIER: per-index slopes
# were consistent, r=0.24-0.50 BTC / 0.18-0.36 ETH / 0.13-0.27 SOL across
# individual indices, checked before pooling), 4 quartile points, median-
# based, median-neutral multiplier on HEDGE_CONTINUATION_SIZE_RATIO's base
# value -- same construction as ADVERSE_MOVE_SIZE_MULTIPLIER (median over
# mean for the same right-skew reason). Interpolated as-measured: BTC dips
# slightly at q3 (matching the first-hedge multiplier's own shape); ETH
# and SOL are fully monotonic here, unlike the first-hedge case -- kept as
# real, not forced to match the other table's shape.
ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER = {
    "Bitcoin": {-0.41: 0.2700, -0.09: 0.6799, 0.12: 2.5175, 0.34: 2.1524},
    "Ethereum": {-0.58: 0.3775, -0.28: 0.3701, 0.02: 2.7088, 0.27: 3.0832},
    "Solana": {-0.56: 0.3890, -0.30: 0.5251, -0.02: 1.6234, 0.22: 3.8519},
}
_ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER_CAP = 6.0  # same cap as
# ADVERSE_MOVE_SIZE_MULTIPLIER -- measured range tops out at ~3.85 (Solana
# q3), comfortably inside it.


def adverse_move_continuation_size_multiplier(asset: str, adverse_move: Optional[float]) -> float:
    """Continuous, median-neutral multiplier on HEDGE_CONTINUATION_SIZE_RATIO's
    base value, as a function of how far price has moved against the
    primary position since the market's ORIGINAL entry (not since the
    most recent hedge -- see this section's docstring for why that
    reference point won the direct comparison). 1.0 (no-op) for any asset
    not in ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER, or when adverse_move
    is unavailable. Flat beyond the measured range, same discipline as
    every other multiplier in this file. Final result clamped to
    _ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER_CAP."""
    curve = ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER.get(asset)
    if curve is None or adverse_move is None:
        return 1.0
    points = sorted(curve.items())
    lo_move, lo_val = points[0]
    hi_move, hi_val = points[-1]
    move = max(lo_move, min(hi_move, adverse_move))
    result = hi_val
    for i in range(len(points) - 1):
        p_move, p_val = points[i]
        q_move, q_val = points[i + 1]
        if p_move <= move <= q_move:
            frac = (move - p_move) / (q_move - p_move) if q_move > p_move else 0.0
            result = p_val + frac * (q_val - p_val)
            break
    cap = _ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER_CAP
    return max(1.0 / cap, min(cap, result))


# ---------------------------------------------------------------------------
# SCOUT sizing for the market's very FIRST entry. Added 2026-09-09, closing
# a confirmed structural gap in the hedge model above: decide_hedge() can
# only ever fire once entry_count >= 1 -- there's nothing to hedge against
# yet at entry_count==0, dominant_side() is still None -- so the bot's
# first entry in every market was always sized at the full ordinary
# "first" tier, never as a deliberately small, tentative stake. (Not
# reusing the word "probe" -- that's already the floor-lot tier's name,
# see FLOOR_LOT_SIZE_SHARES; this is a different mechanism, applies at a
# different point, and the two can both roll on the same first entry.)
#
# Real data says the first entry frequently ISN'T the trader's real bet:
# in dual-sided markets (2026-09-09 hedge-order-loss-impact + probe-
# calibration passes), the first entry is the eventually-non-dominant
# (outpaced) side 28.5-39.0% of the time depending on asset, and when it
# is, it's sized at roughly a fifth to two-thirds of what the ordinary
# "first" tier curve alone would have produced -- not just noise around
# the same median. Separately, the ordering itself matters for real PNL:
# a 7-day, 1,318-pair, 321-loss-event check found the dominant side loses
# LESS OFTEN (18.0% vs 31.2%) and the hedge covers MORE of the damage when
# it does lose (61.3% vs 44.5% loss-cut) specifically in the hedge-first
# cases our bot could never produce before this.
#
# SCOUT_PROBABILITY[asset]: rolled ONCE, at entry_count==0, before side is
# even picked -- matches how the real decision has to work, since he can't
# know in advance which side will end up dominant either. Fraction of ALL
# markets (single- and multi-trade) where the real first entry turned out
# to be the eventually-non-dominant side.
#
# SCOUT_SIZE_RATIO[asset]: median(first_entry_usdc / ENTRY_SIZING_USD's
# "first"-tier median for that entry's own regime), measured only over the
# scout cases above. Applied as a straight multiplier on the ordinary
# decide_size() output for position_tier=="first" -- NOT a fraction of the
# (unknowable at decision time) eventual dominant total, which is the
# quantity the raw "first entry is ~3% of eventual dominant cost" finding
# used but can't be computed forward from inside build_order_intent.
#
# CALIBRATED 2026-09-09, 14-day window, BTC/ETH/SOL only (the three assets
# with any real recent activity -- see ASSET_REGIME_DISTRIBUTION_PCT's
# dormancy notes for Dogecoin/Hyperliquid/BNB, all fully dormant this
# entire window). n=263/187/239 markets respectively -- below this
# project's usual n>=500 trade-level trust bar (though comparable to
# HEDGE_TRIGGER_PROBABILITY's n>=200-market bar for the same kind of
# once-per-market binary measurement). Treat as a first-pass estimate, not
# a fully-trusted recalibration -- revisit once more post-resumption data
# accumulates. Dogecoin/Hyperliquid/BNB default to 0.0 probability
# (no-op, unchanged first-entry behavior) since there's no live data to
# calibrate them from at all right now.
# ACCURACY-conditioned scout rate. Added 2026-09-11: real, cross-asset-
# POOLED finding (BTC-only was marginal, z=2.18; pooling all three assets
# strengthens it to z=6.94) that his ROLLING recent accuracy (correctness
# of his last ~10 markets' dominant-side calls, per asset) predicts how
# often his NEXT first entry is scout-sized (tentative/small) rather than
# a full-conviction entry -- worse recent accuracy, more scouting.
# Pooled rather than per-asset because only the pooled version was
# thoroughly circularity-checked (below); a per-asset table would imply a
# precision this finding wasn't actually validated at.
#
# CIRCULARITY CHECKED, not assumed: scout bets are separately known to be
# less accurate than committed bets, so "recent accuracy" computed from
# ALL recent trades is partly mechanically caused by recent scouting
# itself -- a spurious self-referential loop, not a real predictive
# signal. Re-tested using accuracy computed from ONLY non-scout trades in
# the rolling window: survives, actually strengthens (z=4.80). Does NOT
# extend to overall sizing beyond the scout/commit decision itself (r=
# 0.0156, t=1.24 for general entry sizing vs rolling accuracy) -- scoped
# strictly to SCOUT_PROBABILITY.
#
# BIGGEST ARCHITECTURAL LIFT of this session's implementation pass: the
# bot has never before needed real-time resolution feedback during
# trading (resolution_tick previously only fed settle_order() for P&L,
# never fed back into strategy state) -- bot.py now maintains a rolling
# per-asset accuracy tracker (deque of the last 10 markets' correctness,
# dominant_side vs actual winning_side) populated inside resolution_tick.
#
# Rolling-accuracy bucket -> mean-neutral multiplier on SCOUT_PROBABILITY.
# Pooled across BTC/ETH/SOL (overall pooled scout rate = 0.07475).
ACCURACY_SCOUT_MULTIPLIER = {
    0.3: 1.8753,
    0.6: 1.3751,
    0.8: 0.7228,
    1.0: 0.4133,
}
_ACCURACY_SCOUT_MULTIPLIER_CAP = 3.0


def accuracy_scout_multiplier(rolling_accuracy: Optional[float]) -> float:
    """Continuous, mean-neutral multiplier on scout_probability's output
    as a function of a per-asset rolling recent-accuracy fraction
    (caller-maintained, last ~10 resolved markets' dominant-side
    correctness). 1.0 (no-op) when rolling_accuracy is unavailable (e.g.
    fewer than a few resolved markets observed yet this run). Flat
    beyond the measured range, same discipline as every other multiplier
    in this file."""
    if rolling_accuracy is None:
        return 1.0
    points = sorted(ACCURACY_SCOUT_MULTIPLIER.items())
    lo_x, lo_val = points[0]
    hi_x, hi_val = points[-1]
    x = max(lo_x, min(hi_x, rolling_accuracy))
    result = hi_val
    for i in range(len(points) - 1):
        p_x, p_val = points[i]
        q_x, q_val = points[i + 1]
        if p_x <= x <= q_x:
            frac = (x - p_x) / (q_x - p_x) if q_x > p_x else 0.0
            result = p_val + frac * (q_val - p_val)
            break
    cap = _ACCURACY_SCOUT_MULTIPLIER_CAP
    return max(1.0 / cap, min(cap, result))


SCOUT_PROBABILITY = {
    "Bitcoin":  0.390,
    "Ethereum": 0.285,
    "Solana":   0.376,
}

SCOUT_SIZE_RATIO = {
    "Bitcoin":  0.567,
    "Ethereum": 0.672,
    "Solana":   0.185,
}

# Only reachable if SCOUT_PROBABILITY somehow got a nonzero entry for an
# asset without a matching ratio -- shouldn't happen since both dicts are
# maintained together above, but a defined fallback beats a KeyError.
_DEFAULT_SCOUT_SIZE_RATIO = 0.5


def scout_probability(asset: str) -> float:
    return SCOUT_PROBABILITY.get(asset, 0.0)


def scout_size_ratio(asset: str) -> float:
    return SCOUT_SIZE_RATIO.get(asset, _DEFAULT_SCOUT_SIZE_RATIO)
