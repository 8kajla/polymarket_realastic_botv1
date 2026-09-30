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


def table_lookup(table: dict, *keys, default: float = 1.0) -> float:
    """Generic nested-dict lookup for the many single- and two-key
    calibration tables below (e.g. ENTRY_SIZE_LEVEL_CORRECTION[asset][regime],
    DEEP_CHEAP_TAIL_MULTIPLIER[asset]). Walks `keys` in the exact order the
    caller passes them -- some tables here are asset-first, others are
    regime-first, so callers must match each table's own key order; this
    function does not normalize that. Missing keys at any level fall back to
    `default` (1.0, i.e. a no-op multiplier, unless the table's own function
    needs a different one)."""
    node = table
    for key in keys[:-1]:
        node = node.get(key, {})
    return node.get(keys[-1], default)


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
    # RECALIBRATED 2026-09-13 (full-audit root-cause pass): every cell
    # above this comment for Bitcoin/Ethereum/Solana was measuring a
    # PRE-HALT regime that no longer exists. The full audit (see
    # full-behavioral-audit-tracker.md in project memory) found SIX
    # independent sizing signals had all "gone stale" in the same
    # direction; tracing the actual weekly timeline (not just before/
    # after) showed this isn't a continuous drift at all -- it's a
    # discrete STEP CHANGE that lines up exactly with the 13.6-day halt
    # (2026-08-23 22:53 -> 2026-09-06 14:21 UTC): pre-halt weeks (Aug 8-22)
    # and post-halt weeks (Sep 6-19, now a full ~8 days and STABLE across
    # both weeks measured, not still moving) are two genuinely different,
    # internally-consistent regimes. Every cell below is now measured
    # STANDALONE-ONLY (dominant-side, same discipline as the 2026-09-08
    # correction note above) from ONLY the post-halt window --
    # pre-halt data is deliberately excluded, not blended, since blending
    # two different regimes together would just recreate a confused
    # in-between number that matches neither. n>=200 cleared the trust
    # bar for every cell except the four marked THIN (CORE/HIGH's 4th_plus
    # tier for Ethereum/Solana specifically, where post-halt volume is
    # still catching up) -- those are a first-pass estimate, not a fully-
    # trusted recalibration; revisit once more post-halt volume
    # accumulates. Re-derive again if a comparably large gap ever recurs.
    #
    # "first" TIER RE-SPOT-CHECKED 2026-09-13 (/loop cycle 19), then
    # SUPERSEDED THE SAME DAY (/loop cycle 20) once a methodology flaw in
    # cycle 19's own check was found -- see cycle 20's note below, which
    # replaced every number cycle 19 touched. Kept here only as a record
    # of what happened, not as the source of the values below.
    #
    # FULL RE-DERIVATION 2026-09-13 (/loop cycle 20): re-reading this
    # table's own 2026-09-08 methodology note (above) more carefully
    # after cycle 19 shipped, found cycle 19's "first" tier check had
    # used the WRONG population -- it required the entire market to be
    # single-sided (no hedge EVER), which is a strict subset of what
    # this table was actually built on ("STANDALONE-ONLY (dominant-side
    # trades only)" -- i.e. every DECISION on the dominant side, at its
    # own index, regardless of whether the market later got hedged).
    # That mismatch understated sample sizes badly (Bitcoin CHEAP
    # n=128 vs the correct n=253) and produced at least one value
    # (Bitcoin MID 2.279) that overshot where the correctly-defined
    # population actually sits (2.391). Redone properly for ALL THREE
    # tiers (first/2nd_3rd/4th_plus), correcting BOTH cycle 18's
    # boundary bug AND cycle 19's population-definition bug at once:
    # for each market, decision-collapse, find the dominant side by
    # total cost, take ONLY that side's own decisions in order (index
    # 0="first", 1-2="2nd_3rd", 3+="4th_plus"), regime from each
    # decision's own price. 11 of 12 cells now clear n>=200 (only
    # Solana CORE/4th_plus remains thin, n=158, kept unchanged) --
    # dramatically more complete coverage than either cycle 1's
    # original pass or cycle 19's flawed one. Every cell below reflects
    # this corrected re-derivation; most sit within a few percent of
    # what was already deployed (confirming cycle 18's broader "no
    # qualitative reversal" claim), with Bitcoin HIGH/first the one
    # cell showing a real, well-powered further decline (21.620->19.000,
    # n=305, -12.1%).
    #
    # INDEX SEMANTICS CORRECTED 2026-09-13 (/loop cycle 21, same day):
    # verified cycle 20's own indexing against bot.py directly (not just
    # against the docstring's own prose) and found a further, more
    # fundamental mismatch -- real_fill_count (what position_tier_for_
    # index actually receives live) increments for EVERY confirmed fill
    # in a market, hedge or ordinary alike (see bot.py's
    # record_real_fill), not just for decisions on the dominant side.
    # Cycle 20 assigned tier by a dominant-side-ONLY sub-index (ignoring
    # interspersed hedge fills advancing the shared counter), which
    # quietly shifts every dominant decision's effective tier earlier
    # than it truly is in any market that hedges before that decision.
    # Also directly compared against "first-entered side" (the side of
    # the market's very first fill) rather than "dominant by final
    # cost" as the SIDE filter, since decide_side can genuinely persist-
    # or-switch the bot's own held side mid-market (see decide_side's
    # own docstring) -- but this whole project's own established
    # convention throughout (is_hedge == opposite-of-dominant, used to
    # classify every real trade in every other table in this file) already
    # treats "dominant by cost" as the operational stand-in for
    # "ordinary" trades, so kept that as the side filter for consistency
    # with the rest of this file rather than introducing a second,
    # incompatible convention just for this table.
    # Redone a second time same day: for each market, walk ALL decisions
    # (both sides) in chronological order maintaining ONE shared index
    # (matching real_fill_count exactly), but only SAMPLE dominant-side
    # decisions at their true combined-index tier. Most cells shift a
    # further 0-10% from cycle 20's dominant-sub-index numbers; a few
    # HIGH-band cells shift more (12-20%) but those were already the
    # thinnest, least-certain cells in every prior pass. Cells whose
    # combined-index sample fell below n>=200 (several HIGH/first and
    # CORE/first cells specifically -- hedges are more common in HIGH,
    # thinning out how many EARLY dominant decisions remain unhedged
    # enough to still sit at a low combined index) were left at their
    # last-known (cycle 20 or original) value rather than shipped on a
    # newly-thin sample.
    "Bitcoin": {
        "CHEAP": {"first": 1.596, "2nd_3rd": 1.463, "4th_plus": 1.400},
        "MID":   {"first": 2.332, "2nd_3rd": 2.332, "4th_plus": 2.496},
        "CORE":  {"first": 5.396, "2nd_3rd": 5.928, "4th_plus": 5.928},
        "HIGH":  {"first": 19.000, "2nd_3rd": 16.284, "4th_plus": 19.000},  # first THIN (n=79)
    },
    "Ethereum": {
        "CHEAP": {"first": 1.150, "2nd_3rd": 1.024, "4th_plus": 0.700},
        "MID":   {"first": 1.995, "2nd_3rd": 1.995, "4th_plus": 2.136},
        "CORE":  {"first": 5.613, "2nd_3rd": 5.032, "4th_plus": 4.818},  # first THIN (n=176)
        "HIGH":  {"first": 16.160, "2nd_3rd": 14.400, "4th_plus": 15.980},  # first THIN (n=105)
    },
    "Solana": {
        "CHEAP": {"first": 1.348, "2nd_3rd": 0.999, "4th_plus": 0.600},
        "MID":   {"first": 1.788, "2nd_3rd": 1.891, "4th_plus": 1.891},
        "CORE":  {"first": 5.175, "2nd_3rd": 5.175, "4th_plus": 5.074},  # first THIN (n=170)
        "HIGH":  {"first": 14.469, "2nd_3rd": 14.249, "4th_plus": 15.326},  # first THIN (n=113)
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
    # CORRECTED 2026-09-13 (full-audit root-cause pass): the claim below
    # ("TRADER_PROFILE.md section 8 confirmed a real, non-confounded
    # win-rate difference by persist-vs-switch") is WRONG -- re-derived
    # fresh and found the original measurement was confounded by
    # CHEAP/HIGH price complementarity (switching AWAY from a held CHEAP
    # side mechanically lands you near the complementary HIGH side, which
    # wins more because it's priced as the favorite, nothing to do with
    # persist-vs-switch itself). Properly controlled (bucketed by the
    # CURRENT/resulting entry's own price band, not the held side's), 11
    # of 12 (asset, regime) cells show NO significant win-rate difference.
    # This table still accurately replicates a REAL, large behavioral
    # FREQUENCY (he does persist this often) -- that part was never in
    # question -- but it is STYLE, not an EDGE: don't read the values
    # below as "this predicts winning," only as "this matches how often
    # he sticks with a side." See full-behavioral-audit-tracker.md item 1
    # in project memory for the complete derivation.
    #
    # MADE REGIME-DEPENDENT 2026-09-08 for the three active assets (Bitcoin/
    # Ethereum/Solana). This is that pass: each cell below is P(this entry
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
    # RECALIBRATED 2026-09-13 (post-halt, same root cause as ENTRY_SIZING_
    # USD/HEDGE_TRIGGER_PROBABILITY's matching notes): every cell above
    # was still measuring the pre-halt regime. Post-halt-only data (since
    # 2026-09-06 14:21 UTC, ~1 week, all cells n>=200 OK) shows a real,
    # substantial drop almost everywhere -- Bitcoin CHEAP -13.5pp, MID
    # -7.5pp, CORE -8.5pp, HIGH -6.1pp; Ethereum -7.9pp to -9.3pp across
    # all four regimes; Solana CHEAP -14.3pp, MID -13.8pp, CORE -10.1pp
    # (Solana HIGH close, +3.8pp). His persistence HABIT shifted alongside
    # his sizing habit at the same halt boundary -- one more facet of the
    # same broader post-halt behavioral regime change.
    "Bitcoin": {"CHEAP": 0.7731, "MID": 0.8246, "CORE": 0.8663, "HIGH": 0.8841},
    "Ethereum": {"CHEAP": 0.8253, "MID": 0.8027, "CORE": 0.7814, "HIGH": 0.8055},
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
    "Solana": {"CHEAP": 0.6640, "MID": 0.7239, "CORE": 0.7525, "HIGH": 0.8500},
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

# REMOVED 2026-09-28 (function-count minimization pass): GRADIENT_BIAS_PCT/
# GRADIENT_FLAT_EPSILON + strategy.gradient_score() were a real, calibrated
# soft-tiebreak signal (measured from the full 778,116-trade dataset) meant
# to rank multiple simultaneously-eligible candidates within one bot cycle.
# Found orphaned: bot.py still computed the input (_recent_price_delta) and
# threaded it all the way through build_order_intent's parameter list, but
# nothing ever called gradient_score with it -- the current architecture
# evaluates each market independently in _evaluate_one_market's per-market
# loop, with no cross-candidate ranking step left for it to plug into.
# Confirmed zero call sites and zero test coverage before removal. If this
# gets revisited, the exact historical bias values are in git history at
# this commit.

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
    # RECALIBRATED 2026-09-13 (post-halt, /loop cycle 6 continued -- see
    # full-behavioral-audit-tracker.md item 9): re-derived using RAW
    # per-fill share sizes (not the collapsed-decision view every other
    # recalibration this session used -- floor lots ARE tiny fragments
    # by definition, collapsing would erase the exact population being
    # measured), threshold size<=0.05 shares (a real, clean bimodal split
    # in the data: p1-p15 of all raw fill sizes sit at EXACTLY 0.02, then
    # jump straight to 0.5+ at p20 -- not a fuzzy percentile guess).
    # Floor-lot rate is now MEANINGFULLY HIGHER than the flat historical
    # values AND shows a real rising-with-position-index shape for both
    # assets (previously modeled as flat-by-position, like Dogecoin) --
    # moved to the same per-tier dict structure as Hyperliquid/BNB below,
    # and added to FLOOR_LOT_POSITION_DEPENDENT_ASSETS.
    #
    # INDEX SEMANTICS CORRECTED 2026-09-13 (/loop cycle 26, same day as
    # ENTRY_SIZING_USD's and REENTRY_FATIGUE's own fixes): this table is
    # ALSO keyed by position_tier, which live comes from position_tier_
    # for_index(real_fill_count) -- the same combined hedge+ordinary
    # counter, not a same-side/dominant-only sub-index. The cycle-6
    # derivation above used a sub-index (didn't account for interspersed
    # hedge fills advancing the true shared counter), same bug class as
    # cycles 20-22. Redone with the correct combined index (raw,
    # uncollapsed fills -- floor lots ARE the tiny fragments collapsing
    # would erase, unchanged from cycle 6's own reasoning) plus the
    # cycle-18-corrected HALT_END boundary, never previously applied to
    # this table. Most cells shift a further 10-30%, mostly DOWN (fewer
    # floor-lot probes at a given position than the mis-indexed version
    # suggested) -- consistent direction and magnitude with the broader
    # post-halt softening pattern found across many other tables this
    # session. CORE/first and HIGH/first for both assets stayed below
    # n>=200 under this more selective definition and were left
    # unchanged (Ethereum HIGH/first looked like a +197% swing but at
    # n=106 that's thin-sample noise, not a real signal).
    "Ethereum": {
        "CHEAP": {"first": 0.0917, "2nd_3rd": 0.0860, "4th_plus": 0.1133},
        "MID":   {"first": 0.1183, "2nd_3rd": 0.1341, "4th_plus": 0.1035},
        "CORE":  {"first": 0.0857, "2nd_3rd": 0.1100, "4th_plus": 0.1577},  # first THIN (n=172)
        "HIGH":  {"first": 0.0127, "2nd_3rd": 0.0512, "4th_plus": 0.0618},  # first THIN (n=106)
    },
    "Solana": {
        "CHEAP": {"first": 0.0877, "2nd_3rd": 0.1101, "4th_plus": 0.1931},
        "MID":   {"first": 0.2296, "2nd_3rd": 0.2454, "4th_plus": 0.3314},
        "CORE":  {"first": 0.1736, "2nd_3rd": 0.2069, "4th_plus": 0.2547},  # first THIN (n=165)
        "HIGH":  {"first": 0.0854, "2nd_3rd": 0.0780, "4th_plus": 0.1359},  # first THIN (n=113)
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
FLOOR_LOT_POSITION_DEPENDENT_ASSETS = {"Hyperliquid", "BNB", "Ethereum", "Solana"}


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


# ENTRY_SIZE_LEVEL_CORRECTION -- ADDED 2026-09-22, REDESIGNED 2026-09-22
# (same day, second pass). ENTRY_SIZING_USD was last calibrated 2026-09-08
# on ~2.3 days of trading right after the 13.6-day silence (its own comment
# asked for a recheck once more time had passed).
#
# FIRST PASS (superseded, do not resurrect): keyed by position_tier ONLY,
# applied identically across every regime for an asset. Verified against
# the COMPLETE trader collector (see trader-master-pipeline-mac and
# analysis-loop-2026-09-22-findings project memory -- the collector this
# was originally measured against lost fill fragments and understated
# real sizes) the day it shipped: the gap does NOT move uniformly with
# tier, it moves with REGIME, and a flat per-tier factor was actively
# OVER-correcting Bitcoin CHEAP (ratio ~0.96, needs ~no correction, was
# getting the full 1.40x) while badly UNDER-correcting Bitcoin MID (ratio
# ~2.96, was only getting 1.40x). Re-measured cleanly this time: bot
# PLACED first-entry sizes (live logs, since the 94f42a5/companion-order
# deploys) vs the real trader's COMPLETE post-Sep-18 filled medians,
# matched by (asset, regime), n>=8 both sides:
#   Bitcoin  CHEAP 0.96 | MID 2.96 | CORE 1.22 | HIGH 1.43
#   Ethereum CHEAP 0.82 | MID 2.33 | CORE 2.75 | HIGH 1.84
#   Solana   CHEAP 0.79 | MID 1.83 | CORE 3.73 | HIGH 1.44
# (Solana CORE is thin -- n=11 bot / 24 trader -- treat with more caution
# than the rest.) Values below are shrunk to 80% of the log gap, same
# convention as the first pass. Keyed by (asset, regime) now, applied
# uniformly across tiers within a regime (a coarser approximation than a
# full (asset, regime, tier) grid, but a real fix for the regime-blindness
# bug above; full grid data is in trader_master/out/loop/spec_data/
# size_correction_ratios_asset_regime_p2.csv if this needs refining).
# Applied to the base, outside COMBINED_SIZE_MULTIPLIER_CAP (a level
# recalibration, not a cross-market signal). Re-measure after ~1 week --
# CHEAP already composes with cheap_deep_touch_edge_multiplier's own,
# separately-targeted size boost (a different mechanism: a narrow
# early-window/deep-price condition, not a general CHEAP level).
#
# CHEAP-Bitcoin/Ethereum REFRESHED 2026-09-22, same day as the above --
# in direct response to the live fidelity scorecard showing Bitcoin/
# Ethereum CHEAP sizing still at 0.54-0.65x the real trader's despite
# this same-day fix already being live. Re-measured directly against
# the last 3 days (the freshest reasonably-sized window): with the
# ABOVE correction already applied, real/predicted ratio is still
# 2.07 for Bitcoin, 1.25 for Ethereum -- i.e. even the just-shipped
# correction undershoots the CURRENT reality. Applied the SAME 80%-of-
# log-gap shrinkage convention to this fresh delta on top of the
# existing values (0.968 -> 1.735, 0.853 -> 1.020).
#
# HONEST CAVEAT, stronger than usual (same pattern as the Bitcoin HIGH
# fix in DECISIVENESS_TTC_OVERSHOOT_CORRECTION above): this ratio is
# NOT stable -- split-half over just the last 3 days gives 1.62 (first
# half) vs 2.13 (second half) for Bitcoin, 0.98 vs 1.48 for Ethereum --
# a real, ongoing upward drift, plausibly connected to CHEAP entries
# getting substantially deeper post-boundary (median price 0.17->0.06
# over the same period, per this session's separate research). Solana
# CHEAP checked too and left UNCHANGED -- its fresh ratio (1.17) is
# much smaller and more stable (1.18 vs 1.11 split-half), under the
# threshold this project treats as actually broken. RE-CHECK ALL THREE
# WITHIN DAYS, not the usual ~1 week -- if the drift continues, today's
# refreshed values will already be stale by the next review.
# MID/CORE/HIGH RECALIBRATED 2026-09-23 (systematic post-Sep-18 audit,
# see complete-code-audit-2026-09-23 in project memory) -- these 9 cells
# were the only ones the Sep-22 refresh above didn't touch (that pass
# was specifically triggered by a CHEAP-only scorecard complaint).
# Checked against the clean 1,610-market on-chain-verified dataset built
# for the Phase-4 regime-scoping redo (real_usd vs median_entry_notional
# * within_band_size_multiplier * this table's own then-current value):
# 7 of 9 cells were oversizing, three by more than 2x --
#   Bitcoin  MID  n=246 bias=-0.836 (~2.3x)   CORE n=247 bias=-0.006 (~1.0x, fine)   HIGH n=153 bias=-0.226 (~1.25x)
#   Ethereum MID  n=120 bias=-0.530 (~1.7x)   CORE n=107 bias=-0.798 (~2.2x)         HIGH n=155 bias=-0.322 (~1.4x)
#   Solana   MID  n=103 bias=-0.218 (~1.24x)  CORE n=101 bias=-0.860 (~2.4x)         HIGH n=157 bias=-0.121 (~1.13x)
# Random-subsample stability check (this dataset's build order isn't
# chronological, so this tests sampling noise rather than temporal
# drift): tight for most cells (e.g. Bitcoin MID -0.836/-0.836 identical,
# Solana MID -0.218/-0.218 identical, Ethereum/Solana CORE within 0.03-
# 0.04 of each other), a bit wider for the HIGH cells (0.07-0.15 apart)
# but always the same direction. Bitcoin CORE's near-zero bias is a
# genuine confirmation this cell was already correct, not left
# unchecked -- kept as-is. 80%-of-log-gap shrinkage applied (this
# table's own established convention, same as the CHEAP refresh above):
# new = old * exp(0.8 * bias).
#
# RECALIBRATED 2026-09-25 (research-loop sizing audit, verified pass).
# Re-measured real/predicted directly against 5-second-merged post-Sep-18
# on-chain fills (activity_v2.jsonl), using bc.classify_regime's REAL
# boundaries (CHEAP 0-0.30, MID 0.30-0.70, CORE 0.70-0.90, HIGH 0.90-1.0) --
# this project's own ad-hoc research scripts had been using a fake set of
# boundaries (CHEAP<0.42/MID<0.5/CORE<0.58/HIGH>=0.58) for most of that
# session; the numbers below are the corrected, verified re-derivation, not
# the mistaken first pass.
#
# This table is applied at decide_size's FINAL composition step for BOTH
# the old (median_entry_notional * within_band) base AND the share-based
# (notional_for_price) base -- see decide_size's own call site. Measured
# both paths' residual-on-top-of-this-table's-CURRENT-value separately and
# combined them with a geometric mean per (asset, regime) cell, since one
# shared correction value has to serve both bases at once:
#   Bitcoin  MID  old-path 3.349 / share-based 3.216 (n=269 both)
#            CORE old-path 2.635 / share-based 2.137 (n=270 both)
#            HIGH old-path 2.422 / share-based 1.946 (n=331/441)
#   Ethereum MID  old-path 1.951 / share-based 1.584 (n=77)
#            CORE old-path 2.047 / share-based 1.620 (n=78)
#            HIGH old-path 1.571 / share-based 1.107 (n=291/335)
#   Solana   MID  old-path 1.910 / share-based 1.443 (n=81)
#            CORE old-path 1.806 / share-based 1.334 (n=59)
#            HIGH old-path 1.086 / share-based 0.852 (n=280/328, share-based
#            actually already slightly OVER-predicts here)
# CHEAP re-measured on the upper sub-band only (0.08-0.30 -- the deep tail
# below 0.08 has its own separate, narrower DEEP_CHEAP_TAIL_MULTIPLIER):
# Bitcoin 1.126 (n=312), Ethereum 1.428 (n=253), Solana 1.639 (n=294).
# Same 80%-of-log-gap shrinkage convention as every other recalibration in
# this table: new = old * residual**0.8.
ENTRY_SIZE_LEVEL_CORRECTION = {
    "Bitcoin":  {"CHEAP": 1.908, "MID": 3.159, "CORE": 2.328, "HIGH": 2.066},
    "Ethereum": {"CHEAP": 1.356, "MID": 2.021, "CORE": 1.916, "HIGH": 1.571},
    "Solana":   {"CHEAP": 1.229, "MID": 2.043, "CORE": 2.048, "HIGH": 1.178},
}


# ---------------------------------------------------------------------------
# SHARE-COUNT-BASED ENTRY SIZING. Added 2026-09-20/21, replacing
# median_entry_notional() + within_band_size_multiplier() as the ENTRY
# base (hedges are untouched -- see ENABLE_SHARE_BASED_SIZING's docstring
# in config.py for scope). On-chain ground truth (CTFExchange OrderFilled
# events, not the off-chain data-api's fragmented per-print `size`) found
# he sizes in exact SHARE COUNT, quantized to 0.1-share lots, as one
# symmetric piecewise-exponential function of |price-0.5| -- asset-
# independent (confirmed identical BTC/ETH/SOL) and identical for
# dominant-side entries vs hedge-side fills in the on-chain sample, one
# shared curve instead of this file's previous 16 independently-fit
# per-(asset,regime) exponentials. See onchain-share-sizing-discovery-
# and-code-audit.md (project memory) for the full discovery/audit trail.
#
# dist<=0.20 (price 0.30-0.70): shares = 5.05 * exp(0.873*dist), r=0.956
# dist>0.20 (price outside that): shares = 2.16 * exp(4.685*dist), r=0.983
# The kink lands almost exactly on this file's own existing CHEAP/MID vs
# CORE/HIGH band boundary (0.30/0.70) -- not a coincidence being forced
# in, the two boundaries were derived completely independently and
# happen to agree.
#
# POSITION-TIER CORRECTION -- RETIRED 2026-09-23, was a methodology
# artifact, not a real effect. Original story (found during Phase 1
# implementation): the on-chain sample never checked whether entry ORDER
# (1st/2nd_3rd/4th_plus into a market) carries information beyond price
# alone. Checked against trades.jsonl using this project's standard
# 5-second CLOB-fragment collapse and found what looked like a real,
# monotonic, split-half-confirmed effect -- later entries sized ~7-30%
# bigger than price alone predicts (2nd_3rd=1.07, 4th_plus=1.30,
# n=9,798-15,449, calibrated 2026-09-20/21).
#
# THE 5-SECOND MERGE CONVENTION ITSELF WAS THEN FOUND TO BE DECISIVELY
# WRONG (2026-09-23, see [[five-second-merge-convention-decisively-wrong]]
# in project memory): an on-chain orderHash test showed 85.5% of his
# same-side trades within 5s of each other are genuinely SEPARATE signed
# orders, not one order filled in pieces -- and treating them separately
# fits the price formula 4.75x better (median|log error| 0.144 vs 0.684)
# than the project's merge convention does. Position tier is exactly the
# kind of measurement this bug would corrupt: a market where he fires
# several near-simultaneous orders gets its LATER entries' sizes wrongly
# inflated by merging in a sibling order's shares, and that inflation
# concentrates in the 2nd_3rd/4th_plus buckets by construction (they're
# defined as "not the first entry").
#
# Re-derived from scratch on TRUE on-chain order identity (orderHash),
# not the merge heuristic: sampled 80 markets (mix of 1-fill up to
# 43-fill markets for tier diversity), decoded every trader fill via
# eth_getTransactionReceipt, grouped fills by real orderHash into true
# orders, sorted by each order's first-fill time, assigned position_tier
# from THAT true sequence. Ran the OLD 5s-merge methodology on the
# IDENTICAL 80 markets as a direct control -- it reproduced the
# originally-shipped table almost exactly (2nd_3rd=1.092, 4th_plus=1.299
# vs the shipped 1.07/1.30), confirming the sample is representative and
# the comparison is apples-to-apples. The TRUE on-chain numbers on the
# same markets:
#   tier       TRUE on-chain    OLD 5s-merge (same 80 markets)
#   first          1.000             1.000
#   2nd_3rd        0.997             1.092
#   4th_plus       1.044             1.299
# The "later entries are bigger" effect is almost entirely a merge
# artifact -- it vanishes under true order identity. Split-half checked
# (40 markets each): 2nd_3rd normalized 1.007/0.977, 4th_plus
# 1.077/0.985 -- both straddle 1.0 in both halves, consistent with no
# real residual effect rather than a smaller-but-real one.
# Retired to a flat no-op rather than recalibrated to the small residual
# (which split-half shows is statistically indistinguishable from noise
# around 1.0).
POSITION_TIER_SHARE_MULTIPLIER = {
    "first": 1.0,
    "2nd_3rd": 1.0,
    "4th_plus": 1.0,
}


# REGIME SCOPE (2026-09-22, Phase 4 validation of Phase 1): the share-based
# base only earns its keep in MID/CORE. Against the real trader's decision-
# collapsed first-entry notionals (n=5102, Sep-12+; median |ln(real/pred)|,
# lower = better): share base everywhere 0.493 (post-Sep-18: 0.616) is WORSE
# than the old median x within_band path 0.417 (0.449); scoped to MID+CORE
# with the old path elsewhere 0.396 (0.443) is best or tied on both windows.
# MID/CORE gains are large for Ethereum/Solana (MID 0.12/0.04 vs 0.45/0.29,
# CORE 0.21/0.10 vs 0.25/0.41, prices 0.30-0.90 bias ~0 vs +0.32..+0.47).
# It LOSES in HIGH (0.56-0.80 vs 0.36-0.60 depending on asset) and at the
# price extremes (price<0.05 underpredicts ~3x, price>=0.95 ~1.7x) -- the
# same deep-tail region where 2026-09-21 on-chain data showed the formula
# does not extrapolate (see hedge-size-ratio-confirmed-misspec-and-onchain-
# deeptail-unblocked). CHEAP is mixed (Bitcoin worse, ETH/SOL slightly
# better) so it stays on the old path.
#
# HIGH ADDED 2026-09-23, via HIGH_TAIL_SHARE_MULTIPLIER below: a much
# bigger on-chain re-collection (360 verified fills, up from the original
# handful in the deep tail) found the HIGH-side underprediction is a real,
# well-powered, DIRECTIONALLY stable asymmetry -- not just noise -- so it's
# now corrected rather than treated as a reason to exclude HIGH outright.
# See that constant's own docstring for the full derivation. With the
# correction applied, HIGH's real-vs-predicted fit matches or beats the old
# path (MALE 0.490-0.596 corrected vs OLD 0.497/0.661, ALL/post-boundary)
# and the whole-dataset HYBRID fit improves too (0.437/0.598 vs the
# MID+CORE-only hybrid's 0.449/0.608). Regimes outside this tuple use the
# old median x within_band path even when ENABLE_SHARE_BASED_SIZING is on.
SHARE_BASED_SIZING_REGIMES = ("MID", "CORE", "HIGH")


# HIGH_TAIL_SHARE_MULTIPLIER -- ADDED 2026-09-23. shares_for_price's single
# symmetric curve (dist = |price-0.5|) was built on a thin original sample
# (4 points beyond dist=0.35) and its own docstring already flagged the
# deep tail as unverified. Re-collected on-chain via the tx-hash lookup
# method (no eth_getLogs retention limit): 280 new receipts fetched
# (154 Solana-targeted, 84 very-deep dist>0.45, 42 deep dist 0.42-0.45),
# yielding 309 new verified fills, combined with the prior 63 into 360
# matched, hedge/dominant-labeled points via trades.jsonl sequence join.
#
# Finding: the deep tail is NOT symmetric around 0.5 the way the shipped
# formula assumes. Split by price side, dist>0.42, DOMINANT (non-hedge)
# fills only (n=240, hedge composition checked and ruled out as the
# explanation -- the asymmetry survives within dominant-only and is even
# stronger within the small hedge-only subset):
#   LOW  (price<0.08):  median real/predicted = 0.630  (formula OVER-predicts)
#   HIGH (price>0.92):  median real/predicted = 1.816  (formula UNDER-predicts)
# A full continuous re-fit (log(shares) ~ dist, separately per side) was
# tried and abandoned -- r^2 = 0.03-0.16, i.e. distance from 0.5 explains
# almost none of the per-trade variance out here, consistent with this
# project's established "predictability ceiling" (per-decision replication
# is not achievable from public data; only distributional/average
# calibration is). Split-half checked: the LOW side is NOT stable (median
# ratio flips 0.52 -> 1.39 between halves) and is deliberately left
# UNCORRECTED -- not safe to ship a fix there. The HIGH side IS
# directionally stable (2.61 -> 1.82 across halves, always >1, i.e. always
# underpredicting) even though its exact magnitude still moves -- a
# multiplicative bias correction is justified, a precise per-trade curve
# is not.
#
# Value: 80%-of-log-gap shrinkage (this file's standard convention) on the
# full-sample HIGH-side median ratio: exp(0.8 * ln(1.816)) = 1.61. Verified
# against the real trader's decision-collapsed first entries (same method
# as SHARE_BASED_SIZING_REGIMES's own derivation): this lands within the
# best-or-near-best tested range (x1.6-x1.8 both close to optimal on both
# the ALL and post-boundary windows; x1.6 chosen as the principled shrunk
# value rather than hand-picking whichever backtested best, given the
# magnitude instability noted above). Applies ONLY to price>0.92 (the side
# confirmed stable) -- price<0.08 is deliberately left as the bare,
# uncorrected curve.
#
# DELIBERATELY applied in notional_for_price (the entry-only wrapper), NOT
# in shares_for_price itself -- hedge_notional_for_price also calls
# shares_for_price internally (hedge_usd = scale * (shares_for_price(p)*p)
# ** beta), and its own scale/beta constants (Phase 3, 2026-09-22) were
# fit against the UNCORRECTED curve. Baking this correction into
# shares_for_price would have silently shifted hedge sizing in the deep-
# HIGH tail too, on an entry-side finding never checked against hedges --
# exactly the kind of unintended cross-mechanism coupling this project has
# been bitten by before. If a future pass wants this for hedges too, it
# needs its own hedge-side validation, not inheritance by accident.
HIGH_TAIL_SHARE_MULTIPLIER = 1.61
HIGH_TAIL_SHARE_PRICE_THRESHOLD = 0.92


def shares_for_price(price: float) -> float:
    """The bare price->shares curve, no position-tier correction and no
    HIGH-tail correction (see notional_for_price and
    HIGH_TAIL_SHARE_MULTIPLIER for that) -- also used directly by
    hedge_notional_for_price, which must stay on the uncorrected curve.
    Exposed separately from notional_for_price for direct testing against
    the on-chain discovery's own numbers."""
    dist = abs(price - 0.5)
    if dist <= 0.20:
        raw = 5.05 * math.exp(0.873 * dist)
    else:
        raw = 2.16 * math.exp(4.685 * dist)
    return round(raw, 1)  # 0.1-share lot quantization, matches on-chain ground truth


def notional_for_price(price: float, position_tier: str = "first") -> float:
    """shares_for_price(price), corrected for position_tier (see this
    section's docstring above) and for the HIGH-tail asymmetry (see
    HIGH_TAIL_SHARE_MULTIPLIER, price>0.92 only), converted to a USD
    notional. position_tier defaults to "first" (multiplier 1.0, a no-op)
    so any call site that doesn't know its own tier degrades to the
    bare price-only curve rather than raising. Entry-only wrapper --
    hedge_notional_for_price does NOT call this, it uses shares_for_price
    directly and stays uncorrected (see HIGH_TAIL_SHARE_MULTIPLIER's
    docstring for why)."""
    shares = shares_for_price(price) * POSITION_TIER_SHARE_MULTIPLIER.get(position_tier, 1.0)
    if price > HIGH_TAIL_SHARE_PRICE_THRESHOLD:
        shares *= HIGH_TAIL_SHARE_MULTIPLIER
    return shares * price


# PRICE-BASED HEDGE SIZING (2026-09-22, share-based-sizing Phase 3). The
# production hedge chain (dominant_cost x HEDGE_SIZE_RATIO x adverse_move x
# absolute_price x hedge_ttc, or the continuation ratio for 2nd+ hedges) was
# tested out-of-sample against the real trader's 21,101 first hedges and
# 23,636 continuation hedges (fit on the first 70% of pre-Sep-18 data, tested
# on the last 30% and on post-Sep-18): it is WORSE than predicting a per-asset
# constant (median |ln(real/pred)| 0.94-1.25 by regime vs 0.56-0.64; R2 down to
# -3.4), because real hedge size is essentially independent of the dominant
# position's cost (see hedge-size-ratio-confirmed-misspec-...). A single
# price-only form fits far better and generalizes across periods:
#     hedge_usd = HEDGE_PRICE_SIZING_SCALE[asset] * (shares_for_price(p)*p) ** BETA
#
# RECALIBRATED 2026-09-23 -- the original fit above (BETA=0.926, scale
# BTC=2.992/ETH=1.919/SOL=1.525) was derived from trades.jsonl's 5-second
# merge convention, which was then found to be decisively wrong (see project
# memory: five-second-merge-convention-decisively-wrong -- 85.5% of same-
# side trades within 5s are genuinely separate orders, and treating them
# separately fits the share formula 4.75x better than merging). Hedges are
# exactly the kind of trade most exposed to this: re-derived on TRUE on-chain
# order identity (924 hedge markets sampled across all 4 price regimes,
# first-hedge order reconstructed via orderHash from eth_getTransactionReceipt,
# not the merge heuristic) and the original constants turned out to be
# substantially oversized:
#   asset       shipped scale   implied real/predicted ratio (TRUE data)
#   Bitcoin       2.992          0.36x  (formula ~2.76x too big)
#   Ethereum      1.919          0.56x  (formula ~1.77x too big)
#   Solana        1.525          0.70x  (formula ~1.42x too big)
# Re-fit log(hedge_usd) = a_asset + beta*log(shares_for_price(p)*p) on the
# same 924-market TRUE dataset (n=469/212/243 by asset), shared beta across
# assets matching the original model structure: beta=1.054 (vs shipped
# 0.926), scale Bitcoin=0.929, Ethereum=0.718, Solana=0.657 (all 2.3-3.2x
# smaller than shipped). R2=0.622. Stability-checked via a random 50/50
# split (not time-based, since this new sample doesn't span enough calendar
# range to split that way) rather than shrunk by the usual 80%-of-log-gap
# convention -- the split is unusually tight for this file (beta 1.035/
# 1.069; scale BTC 0.910/0.948, ETH 0.741/0.699, SOL 0.668/0.650, all within
# ~4% of the pooled fit), and the current shipped values are already known
# to be substantially wrong in the same direction the fix moves, so
# shrinking back toward them would be counterproductive here.
# One model still covers first AND continuation hedges (unchanged from the
# original design). Not applied to the floor of the exchange minimum
# (callers keep their existing min-size fall-through). Gated by
# ENABLE_PRICE_BASED_HEDGE_SIZING (paperbot-100 only) -- this recalibration
# is a live-risk fix, not a cosmetic one: production hedges on that bot were
# oversized by up to ~2.8x (Bitcoin) before this.
HEDGE_PRICE_SIZING_BETA = 1.054

# RECALIBRATED 2026-09-25 (research-loop sizing audit) -- SUPERSEDED,
# SEE BELOW. That pass re-measured real/predicted against 5-second-merged
# post-Sep-18 hedge fills and pushed Bitcoin/Ethereum/Solana back up to
# 2.806/1.457/1.281 -- almost exactly back to the ORIGINAL pre-fix values
# (2.992/1.919/1.525) this same table's docstring, two paragraphs above,
# had already identified as ~1.4-2.8x too big and corrected away from
# (0.929/0.718/0.657). In hindsight this was the 5-second-merge bug
# reasserting itself: hedges are exactly the trade type most prone to
# being wrongly fragment-merged (see five-second-merge-convention-
# decisively-wrong in project memory), so the "3.981x under-predicted"
# signal that pass measured was largely the merge inflating apparent
# hedge size, not a real gap -- and "fixing" it by cranking the scale
# back up undid the correct on-chain fix instead of confirming it.
#
# RE-DERIVED 2026-09-26 (full-codebase "verify everything against the
# trader" audit) directly on TRUE on-chain order identity again -- 529
# true hedge orders (OrderFilled events grouped by orderHash) over a
# fresh ~37h window, none of which overlaps the sample used for either
# prior on-chain fit. beta kept fixed at HEDGE_PRICE_SIZING_BETA (already
# on-chain-derived, untouched by the 5s-merge regression); per-asset
# scale re-measured as median(real_usdc / (shares_for_price(p)*p)**beta):
#   Bitcoin (n=435): 1.886 -- excellent split-half stability (first half
#     1.886, second half 1.880, <1% gap) and consistent across all 4
#     regime contexts the hedge itself lands in (1.67-2.33). Shipped
#     value (2.806) already independently confirmed too big in the same
#     direction this moves -- shipped directly, no shrinkage, same
#     reasoning the original on-chain fit used for its own tight split.
#   Ethereum (n=39) and Solana (n=55): implied scale 1.024/1.452 pooled,
#     but split-half is NOT stable (Ethereum 2.045 -> 0.641, over 3x;
#     Solana 1.736 -> 1.013, inconsistent direction relative to shipped)
#     -- too thin and unstable to trust a recalibration either direction.
#     LEFT UNCHANGED at 1.457/1.281 pending a longer, more stable sample;
#     re-check before ever moving these two again.
HEDGE_PRICE_SIZING_SCALE = {"Bitcoin": 1.886, "Ethereum": 1.457, "Solana": 1.281}


def hedge_notional_for_price(asset: str, price: float) -> Optional[float]:
    """The single entry point for hedge SIZE (2026-09-24 consolidation --
    this is now the only hedge-sizing function; the old dominant-cost-
    ratio chain it used to fall back to -- HEDGE_SIZE_RATIO and its five
    sibling adjustment multipliers -- was deleted, since this price+asset
    formula was validated to fit the real trader's hedge sizes better
    out-of-sample across every regime tested, not just as-good). Returns
    the unscaled hedge notional (USD) for a hedge buying at `price`, or
    None for an asset with no calibration (the three dormant assets --
    caller should skip placing a hedge entirely in that case, there is no
    fallback path left). The caller applies SIZE_SCALE_FACTOR and jitter,
    same as the ordinary entry path."""
    scale = HEDGE_PRICE_SIZING_SCALE.get(asset)
    if scale is None or price is None or not (0.0 < price < 1.0):
        return None
    return scale * (shares_for_price(price) * price) ** HEDGE_PRICE_SIZING_BETA


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
# RECALIBRATED 2026-09-13 (post-halt, same root cause as ENTRY_SIZING_
# USD/SIDE_PERSISTENCE's matching notes above -- see full-behavioral-
# audit-tracker.md item 10 in project memory): every cell below was
# still fit on pre-halt-dominated data. Re-derived via the same OLS-of-
# log(usdcSize)-on-price methodology, restricted to ONLY post-halt data
# (since 2026-09-06 14:21 UTC). CHEAP/MID cleared n>=200 for all three
# assets; CORE/HIGH stay THIN (n=69-139) -- a real, substantial slope
# still shows up in the same direction everywhere, but treat these as a
# first-pass estimate pending more post-halt CORE/HIGH volume. The
# slope changes are NOT uniform by direction, only by regime: CHEAP
# dropped sharply for all three assets (e.g. Bitcoin 4.74->1.92, Solana
# 3.97->0.65), MID mostly dropped too, but CORE actually rose for all
# three (Bitcoin 4.19->4.78, Ethereum 3.21->8.65, Solana 4.50->7.74)
# while HIGH dropped for all three (e.g. Bitcoin 13.47->8.46). Real,
# regime-specific shifts, not a single uniform "everything flattened"
# story -- kept as measured, not smoothed toward one narrative.
#
# CHEAP cells RECALIBRATED 2026-09-19 via GP-IRL: a plain (non-hazard)
# Gaussian Process regression of log(usdcSize) on price -- letting the
# curve's SHAPE be data-driven instead of assuming it's log-linear --
# found CHEAP diverges substantially from the shipped slopes for
# Ethereum and Solana. Checked for floor-lot contamination first (many
# of the lowest-price trades were literal $0.00-$0.05 fills, obvious
# minimum-size probes, not confidence-scaled): excluding trades <$0.20
# notional at two thresholds, the divergence shrank for Bitcoin (which
# then matched the shipped shape closely, left unchanged below) but
# persisted, large, for Ethereum and Solana. Re-fit with the same OLS-
# of-log(usdcSize)-on-price methodology as every other cell in this
# table, floor-lot-excluded, full observed window (~1 week,
# price_history_dense.jsonl's coverage):
#   Bitcoin:  1.9179 -> 1.6699 (modest, within this table's own
#             established recalibration noise -- see PRE/POST-boundary
#             split below)
#   Ethereum: 0.8809 -> 2.9746 (large)
#   Solana:   0.6450 -> 3.1063 (large)
# Bootstrap-confirmed (400 resamples, resampling each price bin's
# observations): Ethereum's rise 90% CI=[0.407,0.731], Solana's
# 90% CI=[0.736,1.027] -- both entirely above the shipped slopes'
# implied rise, no overlap. Calendar split-half (first half of the week
# vs second): Ethereum 0.495->0.702, Solana 0.697->0.919 -- both real
# and STABLE, if anything strengthening, not a temporary blip. Pre vs
# post the ~2026-09-18 boundary: Ethereum 3.07 (pre, n=810) vs 3.15
# (post, n=148) -- highly consistent, not boundary-driven. Solana 2.89
# (pre, n=800) vs 4.32 (post, n=98, thinner) -- directionally the same,
# post is thinner but if anything steeper still.
#
# HONEST RESIDUAL, not fully captured by this recalibration: Ethereum's
# CHEAP curve is NOT actually log-linear -- the GP found a genuine dip
# around price=0.05 (notably the same threshold CHEAP_JOINT_PRICE_
# THRESHOLD uses) before rising to a peak near 0.25. A single slope is
# the best LINEAR approximation of that shape, not a full fit -- kept
# as the simplest safe improvement over the old, far-worse-fitting
# slope rather than force a bigger architecture change (a piecewise
# curve) on ~1 week of data for one cell. Revisit if more data
# accumulates or this residual turns out to matter economically.
WITHIN_BAND_SIZE_SLOPE = {
    "Bitcoin": {"CORE": {"slope": 4.7755, "band_mean_price": 0.7865},
                "HIGH": {"slope": 8.4602, "band_mean_price": 0.9396},
                "CHEAP": {"slope": 1.6699, "band_mean_price": 0.1482},
                "MID": {"slope": 2.3301, "band_mean_price": 0.4639}},
    "Ethereum": {"CORE": {"slope": 8.6511, "band_mean_price": 0.8012},
                 "HIGH": {"slope": 14.8418, "band_mean_price": 0.9369},
                 "CHEAP": {"slope": 2.9746, "band_mean_price": 0.1297},
                 "MID": {"slope": 3.7741, "band_mean_price": 0.4550}},
    "Solana": {"CORE": {"slope": 7.7354, "band_mean_price": 0.8080},
               "HIGH": {"slope": 20.5223, "band_mean_price": 0.9340},
               "CHEAP": {"slope": 10.338, "band_mean_price": 0.1530},
               "MID": {"slope": 1.6615, "band_mean_price": 0.4407}},
}
# RECALIBRATED 2026-09-23: Solana CHEAP's slope was 3.1063 -- refit directly
# from real trader data (OLS of log(size) on price-within-band, per asset,
# not pooled across assets), proper time-split (train=first 70%, test=last
# 30%). Held-out R2 doubles (0.102 -> 0.204) with the new slope (10.338),
# the only cell across all 12 (asset, regime) combinations where a refit
# produced a clearly real, well-powered improvement -- Bitcoin CHEAP and
# Ethereum HIGH showed no real gain (refit R2 still negative/flat), Bitcoin
# HIGH and Ethereum CHEAP showed smaller real gains not yet acted on,
# Ethereum/Solana MID/CORE are too thin (n=12-24) to refit at all. See
# project memory: first-principles-mechanism-design-edge-decomposition
# (Test 4, which first found the aggregate convexity mismatch -- pooled
# across assets, likely partly a composition artifact given how much
# steeper Solana's true slope turns out to be) and the follow-up per-asset
# refit that produced this specific number. band_mean_price unchanged
# (held fixed as the fit anchor, not re-derived).
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


# DEEP_CHEAP_TAIL_MULTIPLIER -- ADDED 2026-09-23. WITHIN_BAND_SIZE_SLOPE's
# CHEAP cells were fit as ONE log-linear slope across the whole 0.00-0.30
# band (that table's own docstring already flags this as an approximation,
# noting Ethereum's true shape isn't log-linear). The deepest part of that
# band -- price<0.08 -- turns out to be badly underserved by that single
# slope: real (on-chain) sizes there are 1.4-4.2x smaller than what
# median_entry_notional * within_band_size_multiplier predicts, and this
# ISN'T a rare edge case -- it's ~28-30% of every CHEAP-regime entry by
# count (16-24% by dollar volume) across all three live bots.
#
# Found while investigating the same on-chain "LOW tail" region the share-
# based sizing project's deep-tail work covers for its OWN formula (see
# HIGH_TAIL_SHARE_MULTIPLIER) -- but that work doesn't touch this OLD path
# at all (CHEAP stays on median_entry_notional/within_band_size_multiplier
# even where share-based sizing is enabled, see SHARE_BASED_SIZING_REGIMES).
# An initial "it's just market liquidity" hypothesis was tested and
# REFUTED once asset was properly controlled for (pooled corr with
# liquidity was a between-asset confound, not a real effect -- Bitcoin/
# Ethereum/Solana individually showed no significant liquidity relationship,
# t=0.37/0.79/1.29). What DOES hold up is price itself, once each asset's
# own baseline is accounted for -- exactly what this per-asset multiplier
# adds on top of the existing (asset-blind-within-band) slope.
#
# Measured directly against real, decision-collapsed on-chain fills
# (n=315/184/101 BTC/ETH/SOL, price<0.08, verified via
# eth_getTransactionReceipt): median log(real / (median_entry_notional *
# within_band_size_multiplier)) = -0.344 / -1.047 / -1.430. Split-half
# checked (calendar, ~half each): Bitcoin -0.225/-0.653 (real spread, but
# always negative i.e. always oversizing), Ethereum -1.012/-1.136 (tight),
# Solana -1.462/-1.214 (reasonably tight). 80%-of-log-gap shrinkage (this
# file's standard convention, applied here given Bitcoin's real split
# spread): Bitcoin exp(0.8*-0.344)=0.759, Ethereum exp(0.8*-1.047)=0.433,
# Solana exp(0.8*-1.430)=0.319.
#
# Applied ONLY for regime=="CHEAP" and price<0.08, multiplying into the
# existing within_band value at the OLD-path call site in decide_size --
# composes with (doesn't replace) WITHIN_BAND_SIZE_SLOPE's own CHEAP
# dampening, same layering pattern HIGH_TAIL_SHARE_MULTIPLIER uses for the
# share-based path. Not applied when ENABLE_SHARE_BASED_SIZING routes
# CHEAP through the share-based base instead (it currently never does --
# CHEAP is not in SHARE_BASED_SIZING_REGIMES -- but scoped this way in
# case that changes).
DEEP_CHEAP_TAIL_PRICE_THRESHOLD = 0.08

# RECALIBRATED 2026-09-25 (research-loop sizing audit, verified pass).
# Re-measured real/predicted (median_entry_notional * within_band *
# THIS multiplier * entry_size_level_correction) against 5-second-merged
# post-Sep-18 fills, price<0.08 only: Bitcoin 1.504 (n=288), Ethereum
# 1.528 (n=345), Solana 4.809 (n=237) -- all real, well-powered, still
# under-predicting even with the existing correction applied. Solana's
# gap is far bigger than Bitcoin/Ethereum's here (the opposite ordering
# from the entry/add-on/hedge floor work elsewhere in this file, which
# found BITCOIN specifically maintains a real minimum stake in this same
# deep tail -- see bitcoin-deep-cheap-tail-hedge-undersized in project
# memory). Each asset has a genuinely different shape within this tail
# (Bitcoin decays from the extreme, Ethereum is U-shaped, Solana grows
# toward the 0.08 boundary) that a single flat multiplier can't capture --
# this recalibration only corrects the MEDIAN level, not the shape; a
# future pass fitting a price-graded curve per asset would do better.
# Same 80%-of-log-gap shrinkage convention as this table's original
# calibration: new = old * residual**0.8.
DEEP_CHEAP_TAIL_MULTIPLIER = {"Bitcoin": 1.052, "Ethereum": 0.608, "Solana": 1.121}


# ---------------------------------------------------------------------------
# CHEAP JOINT entry dampener (price-band x freshness-bucket). Formerly
# two SEPARATE mechanisms -- DEEP_LONGSHOT_ENTRY_MULTIPLIER (price only)
# and CHEAP_FRESHNESS_MULTIPLIER (time-since-crossing only), each fit
# independently and applied as two sequential multiplicative skip gates
# on the same first-entry decision -- MERGED 2026-09-19 after a joint
# logistic-regression model of the trader's CHEAP first-entry hazard
# (price, freshness, asset, boundary-era, momentum, time-to-close and
# cross-asset-recency all fit SIMULTANEOUSLY instead of one univariate
# table at a time, n=18,440 crossing-bucket rows) found a strong,
# highly significant is_deep x freshness-bucket interaction (coef=-0.79,
# SE=0.15, p<1e-7) -- being both deep-priced AND stale is NOT
# independent of being either alone, so multiplying two separately-fit
# 1D tables cannot represent it.
#
# Quantified how wrong that independence assumption was: comparing the
# OLD architecture's prediction (rest-band bucket-0 empirical rate x the
# old DEEP_LONGSHOT ratio x the old CHEAP_FRESHNESS bucket ratio) against
# the TRUE empirical joint rate in the same (deep, bucket) cells,
# pre-boundary, in the best-populated buckets (1-3, n=160-412/cell,
# bucket 0's deep row is too thin at n=29-30 to trust):
#   Bitcoin:  actual/predicted = 2.40x, 3.30x, 2.40x (buckets 1,2,3)
#   Ethereum: actual/predicted = 0.84x, 2.68x, 4.20x
#   Solana:   actual/predicted = 1.15x, 6.54x, 7.33x
# The old two-independently-multiplied-tables design systematically
# OVER-suppressed deep+stale entries relative to reality, by as much as
# 7x in the best-powered cells, consistently across all three assets.
#
# RECALIBRATED 2026-09-21: the "leave both rows identical" placeholder
# above was never actually revisited before it caused a real, confirmed
# bug. Root cause chain (see cheap-floor-entry-suppression-fixed.md,
# project memory): coinbase-bot's live CHEAP entries were landing at a
# much higher average price (0.1244) than the real trader's (0.0758) in
# the same window. Traced with a real episode reconstruction (price_
# history_dense.jsonl ticks joined to logged SKIP/PLACE events, not just
# aggregate stats): of CHEAP episodes that survived REGIME_ENGAGEMENT_
# MULTIPLIER's episode-latch and later drifted to price<0.10 (62% of
# them did), only 12% of actual PLACEs landed there -- because this
# table's identical-rows placeholder meant `seconds_since_cheap_crossing`
# (measured from the ORIGINAL ~0.24 crossing, not from reaching the deep
# price) kept accumulating as price fell, so by the time an episode
# reached the floor it was usually already "stale" by this clock and hit
# the harshest bucket (0.02-0.03x) despite the price itself being fresh.
#
# Two redesigns of the CLOCK itself (re-latching freshness on price-tier
# crossings, then on a deepest-point-reached ratchet) were tried and
# REJECTED after replay validation against real logged SKIP events: tick-
# level CHEAP price is noisy enough (genuine non-monotonic bouncing
# within a single episode, not smooth decay) that either clock redefinition
# neutralized the mechanism almost everywhere (near-100% flip rate in
# every price band), not just at the floor -- over-correcting rather than
# fixing the targeted bug. Reverted to the original, stable
# seconds_since_cheap_crossing clock; fixed the PRICE axis instead, which
# this table already had a slot for and was simply never calibrated.
#
# Recalibrated is_deep=True and is_deep=False independently via the same
# hazard-rate methodology as the original CHEAP_FRESHNESS derivation
# below, but using ONLY recent data (last 84h, all post-2026-09-18-
# boundary -- the pre-boundary "actual/predicted" ratios quantified above
# were NOT reused directly, consistent with this project's now-repeated
# finding that his behavior drifts on a timescale of days). Real,
# well-powered hazard rates (n=163-1100 at-risk per cell, all 24 cells):
# is_deep=True has a 2-4x higher hazard than is_deep=False at every
# bucket for every asset, and decays far more gently over time -- e.g.
# Bitcoin's 180s+ bucket: shallow hazard crashes to 0.61% (near-total
# abandonment) while deep stays at 9.06% (barely decayed from its own
# 15.28% baseline). Bucket-normalized (ratio to that row's own 0-60s
# rate, same convention as the original table) and validated by replaying
# against the same real SKIP events used to catch the two rejected clock
# designs: the 180s+ bucket -- the one doing the most damage under the
# old identical-rows table -- now gives deep prices meaningfully more
# room than shallow on all three assets (Bitcoin 5.9x, Ethereum 2.7x,
# Solana 2.0x more lenient). Two of the eight non-terminal (asset,
# bucket) cells show a mild, likely noise-level reversal (deep slightly
# BELOW shallow) rather than the expected direction -- not chased further
# given the dominant, best-powered signal (the 180s+ bucket, most
# consequential and most consistent) is strong and correctly directed on
# every asset. Side effect, disclosed rather than hidden: fitting fresh
# data revealed the FULL freshness decay had also drifted looser since
# the original 2026-09-19 derivation (both rows are less punishing than
# before, not just the is_deep split) -- this ships as a full
# recalibration of the mechanism, not a narrow single-axis patch.
#
# Original derivations, preserved for context:
#
# DEEP_LONGSHOT (price axis), occupancy-normalized: his CHEAP first-entry
# price distribution was found to be dramatically cheaper than the bot's
# (median 0.12 vs his 0.19) -- root-caused via entry RATE PER UNIT OF
# OBSERVED OPPORTUNITY (price_history_dense.jsonl book-price polls as an
# unbiased occupancy baseline, matched against trades.jsonl entries in
# the SAME calendar window), a sharp, specific avoidance of price<0.05
# only (not a smooth gradient across 0.05-0.30, that shape was checked
# and rejected): Bitcoin 0.30x, Ethereum 0.36x, Solana 0.26x relative to
# the rest of CHEAP. Scope: first entries only (real_fill_count==0), not
# hedges (a hedge's price is wherever the dominant side sits, not a
# fresh preference) and not excluding scout/floor-lot (his own raw trade
# log has no such distinction).
#
# CHEAP_FRESHNESS (time axis): real, equal-width (60s) hazard-rate
# buckets (of crossings still untaken going into a bucket, what fraction
# get taken during it), same price_history_dense.jsonl crossing-detection
# matched against trades.jsonl first-entry timestamps:
#   Bitcoin:  9.81% (0-60s) -> 1.44% (60-120s) -> 0.45% (120-180s) -> 0.16% (180s+)
#   Ethereum: 7.70% -> 2.07% -> 0.98% -> 0.24%
#   Solana:   10.95% -> 2.02% -> 0.85% -> 0.25%
# All three assets: a steep (~80-85%) drop from the first to second
# bucket, then a long, thin, near-zero tail. Deliberately SOFTER than a
# literal per-tick hazard inversion (bucket-relative multipliers, not
# raw hazard values) because decide_side's own persistence/switch gate
# already provides SOME independent reluctance to jump onto a newly-
# favored side, and stacking a second, precisely-inverted probability on
# top risks compounding into stronger suppression than either mechanism
# alone would produce -- no clean way to separate the two effects
# without live monitoring, so treat as a monitored change (same
# discipline as QUEUE_SAFETY_FACTOR_OVERRIDE_BY_REGIME/BY_ASSET).
#
# CHEAP_JOINT_CROSSING_THRESHOLD (0.30) marks the CHEAP-regime entry
# itself (when a side's price first drops below the whole CHEAP band,
# latched by MarketActivityState.update_cheap_crossings) --
# CHEAP_JOINT_PRICE_THRESHOLD (0.05) is the SEPARATE, narrower split
# within CHEAP between "deep longshot" and "the rest of CHEAP" that
# is_deep keys off of. Do not confuse the two.
#
# CHEAP_JOINT_PRICE_THRESHOLD's exact value (0.05), NOT YET CHANGED:
# GP-IRL research 2026-09-19 reconciled why the original occupancy-
# normalized derivation (a sharp step at 0.05) and this session's
# hazard-based GP fit (a smooth curve) looked like they disagreed --
# they don't, once given the same resolution. Re-running the ORIGINAL
# occupancy-normalized methodology across fine price bins instead of
# just the one deep(<0.05)-vs-rest(0.05-0.30) split shows entry rate is
# essentially ZERO below ~0.02 (literally 0 entries across 116-212
# occupancy ticks, every asset) and ramps up sharply through 0.02-0.05,
# already back to normal by ~0.04. The original 2-bin split averaged a
# true, near-total floor (below ~0.02) together with a region already
# recovered (0.03-0.05), producing the misleadingly uniform "0.30x for
# the whole band" summary this file's DEEP_LONGSHOT/CHEAP_JOINT history
# was built on. The REAL floor is closer to 0.02, not 0.05 -- left
# unchanged here only because CHEAP_JOINT_ENTRY_MULTIPLIER's own values
# are currently 1.0 (disabled, see its docstring below) regardless of
# where this threshold sits, so there is no live behavior to fix yet.
# Revisit this constant, not just the multiplier values, whenever this
# mechanism is next recalibrated.
CHEAP_JOINT_CROSSING_THRESHOLD = 0.30
CHEAP_JOINT_PRICE_THRESHOLD = 0.05
CHEAP_JOINT_ENTRY_MULTIPLIER = {
    "Bitcoin": {
        False: [(0.0, 60.0, 1.00), (60.0, 120.0, 0.7225), (120.0, 180.0, 0.7997), (180.0, math.inf, 0.1002)],
        True:  [(0.0, 60.0, 1.00), (60.0, 120.0, 0.75),   (120.0, 180.0, 0.7291), (180.0, math.inf, 0.5929)],
    },
    "Ethereum": {
        False: [(0.0, 60.0, 1.00), (60.0, 120.0, 0.8059), (120.0, 180.0, 0.7366), (180.0, math.inf, 0.2356)],
        True:  [(0.0, 60.0, 1.00), (60.0, 120.0, 0.7264), (120.0, 180.0, 0.9247), (180.0, math.inf, 0.6332)],
    },
    "Solana": {
        False: [(0.0, 60.0, 1.00), (60.0, 120.0, 0.5222), (120.0, 180.0, 0.5222), (180.0, math.inf, 0.5137)],
        True:  [(0.0, 60.0, 1.00), (60.0, 120.0, 0.6876), (120.0, 180.0, 0.5887), (180.0, math.inf, 1.00)],
    },
}


def cheap_joint_entry_multiplier(asset: str, price: float,
                                  seconds_since_cheap_crossing: Optional[float]) -> float:
    """1.0 (no-op) when there's no recorded crossing yet
    (seconds_since_cheap_crossing is None -- e.g. a market that opened
    already-cheap with no pre-crossing tick captured) or for any asset
    with no calibration -- otherwise the freshness bucket for this
    (asset, is_deep, elapsed) cell, applied as a skip-probability at the
    call site (see build_order_intent). A negative elapsed (defensive:
    crossing timestamp somehow after `now`) falls back to the first
    (no-op) bucket rather than raising. See CHEAP_JOINT_ENTRY_MULTIPLIER's
    docstring for the 2026-09-21 recalibration that differentiated the
    is_deep=True/False rows (previously identical placeholders)."""
    if seconds_since_cheap_crossing is None:
        return 1.0
    table = CHEAP_JOINT_ENTRY_MULTIPLIER.get(asset)
    if table is None:
        return 1.0
    is_deep = price < CHEAP_JOINT_PRICE_THRESHOLD
    elapsed = max(0.0, seconds_since_cheap_crossing)
    for lo, hi, mult in table[is_deep]:
        if lo <= elapsed < hi:
            return mult
    return 1.0


# ---------------------------------------------------------------------------
# CHEAP EARLY/LATE DEEP-TOUCH EDGE (2026-09-22). Independent of everything
# above: a STRUCTURAL MARKET FACT, not a behavioral-fidelity finding --
# confirmed on both eras (Sep 12-18 and post-Sep-18) and re-verified through
# 2026-09-22 with window-clustered bootstrap CIs that exclude zero in BOTH
# directions. Measures the UNCONDITIONAL win rate of buying EITHER side the
# moment its bid first falls to <= CHEAP_DEEP_TOUCH_PRICE_THRESHOLD (not
# specific to the real trader's own selection), split by how far into the
# 5-minute window that collapse happens:
#   touch at 30-150s into the window: wins MORE than its price implies
#     (+6.68pp [95% CI +2.89,+10.86], n=402 touch events / 295 windows,
#     window-clustered bootstrap) -- an early collapse to a deep price
#     tends to be an overshoot that partially reverts.
#   touch at 150-270s: wins LESS than its price implies (-2.57pp
#     [-4.03,-0.86], n=811/520) -- a late collapse is closer to genuine
#     information (the side really is fading) and is already priced
#     fairly-to-generously.
# This is NOT about matching the real trader's own behaviour (see
# CHEAP_JOINT_ENTRY_MULTIPLIER above for that -- a freshness-since-crossing
# axis, keyed to HIS crossing timestamp). This is a fact about the market
# itself, true for any buyer, which is exactly why it's actionable even
# though the trader's own late-touch selection edge (see project memory
# cheap-early-late-touch-edge.md) has no observable predictor we could
# copy. Full derivation: trader_master/out/loop/FINDINGS.md, 2026-09-22
# entries "STRUCTURAL CHEAP EDGE" and the same-day re-verification pass.
#
# Two DIFFERENT responses, not one multiplier applied two ways, because a
# skip-probability gate (this file's usual dampener pattern) can only ever
# REDUCE a probability, never increase it past whatever the rest of the
# stack already decided:
#   - EARLY (proven positive): a SIZE multiplier (decide_size) -- keep
#     participation as-is, just stake more on it.
#   - LATE (proven negative): a SKIP-probability multiplier (entry
#     eligibility, same convention as cheap_joint_entry_multiplier: < 1.0
#     means rng.random() > mult skips the entry) -- the correct response
#     to a proven-negative edge is fewer trades there, not smaller ones.
# Both values are shrunk to roughly 60-70% of what the raw point estimate
# would imply on a naive edge/price translation, deliberately conservative
# given the CIs are wide on a few hundred clustered windows and CHEAP's
# level has been shown to drift (see analysis-loop-2026-09-22-findings.md
# project memory) -- re-verify before trusting these numbers past a few
# weeks. Scoped to FIRST entries only (position_tier == "first"), matching
# exactly what was measured (a touch is a first-crossing event).
# ---------------------------------------------------------------------------
CHEAP_DEEP_TOUCH_PRICE_THRESHOLD = 0.08
CHEAP_EARLY_TOUCH_WINDOW_SECONDS = (30.0, 150.0)
CHEAP_LATE_TOUCH_WINDOW_SECONDS = (150.0, 270.0)
CHEAP_EARLY_TOUCH_SIZE_MULTIPLIER = 1.75
CHEAP_LATE_TOUCH_SKIP_MULTIPLIER = 0.35


def cheap_deep_touch_edge_multiplier(price: float, seconds_into_window: Optional[float]) -> float:
    """Returns CHEAP_EARLY_TOUCH_SIZE_MULTIPLIER (>1.0, apply as a size
    boost) if price is at/below the deep threshold and seconds_into_window
    falls in the proven-positive early zone; CHEAP_LATE_TOUCH_SKIP_
    MULTIPLIER (<1.0, apply as a skip-probability dampener) if it falls in
    the proven-negative late zone; 1.0 (no-op) otherwise -- including
    price above the threshold, seconds_into_window unknown (None), or
    before 30s / after 270s (outside what was actually measured).

    Approximates "the moment of the touch" with "current price and current
    time" at first-entry evaluation -- exact for a market being evaluated
    right as it enters CHEAP, a reasonable proxy otherwise. A dedicated
    crossing-timestamp tracker (mirroring MarketActivityState.up_cheap_
    crossed_at) would be more precise but wasn't built for this first cut.
    """
    if seconds_into_window is None or price > CHEAP_DEEP_TOUCH_PRICE_THRESHOLD:
        return 1.0
    lo1, hi1 = CHEAP_EARLY_TOUCH_WINDOW_SECONDS
    if lo1 <= seconds_into_window < hi1:
        return CHEAP_EARLY_TOUCH_SIZE_MULTIPLIER
    lo2, hi2 = CHEAP_LATE_TOUCH_WINDOW_SECONDS
    if lo2 <= seconds_into_window < hi2:
        return CHEAP_LATE_TOUCH_SKIP_MULTIPLIER
    return 1.0


# ---------------------------------------------------------------------------
# MID BOUNDARY-ADJACENCY EDGE (2026-09-22). A SECOND, independent structural
# market fact discovered by deliberately researching whether "increase MID
# engagement to match his mix" (the original P3 idea) would help or hurt
# before touching REGIME_ENGAGEMENT_MULTIPLIER's MID rate at all -- pooled
# MID edge is NOT established (ROI -4.6% [95% CI -30,+23], window-clustered
# bootstrap), so a blanket mix retarget was correctly NOT built. Splitting
# MID (price 0.30-0.70) by price sub-band instead found the pooled null
# hides a real, opposite-signed pair of pockets -- the same "overshoot near
# a regime boundary" shape CHEAP_DEEP_TOUCH_PRICE_THRESHOLD already
# captures, recurring at MID's own two boundaries:
#   0.30-0.40 (adjacent to CHEAP): excess +18.97pp [95% CI +4.5,+33.2],
#     EXCLUDES ZERO; split-half stable (+12.9pp -> +25.2pp, same
#     direction, strengthening). n=115 decisions/61 windows.
#   0.60-0.70 (adjacent to CORE): ROI -33.5% [95% CI -61.6,-2.8], EXCLUDES
#     ZERO (negative); split-half stable (-26.7% -> -41.3% ROI, negative
#     both halves). n=70/44 windows.
#   0.40-0.60 (the coin-flip center): no signal either direction --
#     economically sensible, this is where the market should be hardest
#     to beat, and left untouched.
# Checked whether this generalizes to CORE's own two boundaries (0.70-0.80
# adjacent to MID, 0.80-0.90 adjacent to HIGH) and it does NOT -- both CIs
# include zero. CORE sits between two already-decisive prices; MID
# straddles genuine 50/50 uncertainty, which is why the boundary effect
# concentrates there and not in every regime edge.
# Same two-response design as CHEAP_DEEP_TOUCH_PRICE_THRESHOLD, for the
# same reason (a skip gate can only reduce, never increase, a probability):
# low-MID -> SIZE boost (decide_size); high-MID -> SKIP dampener
# (build_order_intent entry eligibility). Deliberately does NOT touch
# REGIME_ENGAGEMENT_MULTIPLIER's overall MID rate -- this is a narrower,
# price-conditioned overlay, not a mix retarget. Full derivation:
# trader_master/out/loop/FINDINGS.md, 2026-09-22 "[P3 PROPER RESEARCH]".
# ---------------------------------------------------------------------------
MID_LOW_BOUNDARY_WINDOW = (0.30, 0.40)
MID_HIGH_BOUNDARY_WINDOW = (0.60, 0.70)
MID_LOW_BOUNDARY_SIZE_MULTIPLIER = 1.55
MID_HIGH_BOUNDARY_SKIP_MULTIPLIER = 0.40


def mid_boundary_edge_multiplier(regime: str, price: float) -> float:
    """Returns MID_LOW_BOUNDARY_SIZE_MULTIPLIER (>1.0, apply as a size
    boost) if regime is MID and price is in the proven-positive band
    adjacent to CHEAP; MID_HIGH_BOUNDARY_SKIP_MULTIPLIER (<1.0, apply as
    a skip-probability dampener) if price is in the proven-negative band
    adjacent to CORE; 1.0 (no-op) otherwise, including the coin-flip
    center band and every other regime."""
    if regime != "MID":
        return 1.0
    lo1, hi1 = MID_LOW_BOUNDARY_WINDOW
    if lo1 <= price < hi1:
        return MID_LOW_BOUNDARY_SIZE_MULTIPLIER
    lo2, hi2 = MID_HIGH_BOUNDARY_WINDOW
    if lo2 <= price < hi2:
        return MID_HIGH_BOUNDARY_SKIP_MULTIPLIER
    return 1.0


# ---------------------------------------------------------------------------
# DECISIVE-ENTRY ELIGIBILITY (CORE/HIGH). Added 2026-09-20, closing a gap
# found while checking whether the CHEAP win-rate investigation's
# findings generalized: they did, worse. DECISIVENESS_ONSET_MULTIPLIER
# above scales SIZE only -- it has zero influence on decide_side's own
# entry-hazard logic, so nothing previously made the bot less likely to
# enter a CORE/HIGH market the longer it's been decisive, mirroring
# cheap_joint_entry_multiplier's role for CHEAP. Measured directly (real
# per-tick hazard, equal-width 60s buckets, price_history_dense.jsonl
# decisive crossings joined against the real trader's trades.jsonl,
# price>=0.165 to isolate freshness from the already-known deep-price
# effects): the trader's own hazard rate decays sharply after a market
# snaps decisive and is essentially fully given up on by 120s+ --
# Bitcoin 9.81%->1.44%->0.45%->0.16%, Ethereum 7.70%->2.07%->0.98%->
# 0.24%, Solana 10.95%->2.02%->0.85%->0.25%. Normalized to the first
# bucket (this is a skip-probability multiplier, not an absolute rate)
# and applied the same way cheap_joint_entry_multiplier is -- a skip
# roll at the call site, first entries only (not hedges, which target
# the dominant side wherever it sits, not a fresh preference).
# RECALIBRATED 2026-09-22 -- the original values above were WRONG, found via
# a bot-vs-trader participation check (his HIGH first entries occur in 28% of
# windows with a HIGH-priced side vs ~12% filled for the bots; this gate
# blocked 102 of 358 such windows). Re-measured directly (bot's own crossing
# definition: side bid first >= DECISIVENESS_THRESHOLD 0.70; "taken" = his
# first decision of the market is on that side at price >= 0.70; equal-width
# 60s buckets, at-risk hazard, dense book history Sep 12-21). Bucket-
# normalized hazard ratios vs bucket 0 (table values were 0.15/0.05/0.02):
#   PRE-boundary  n~2.2k crossings/asset: Bitcoin 0.67/0.46/0.18,
#     Ethereum 0.94/0.75/0.44, Solana 1.48/1.15/0.57 (gentle decay at most)
#   POST-Sep-18   n~1.35k/asset: Bitcoin 1.11/1.21/0.78, Ethereum
#     1.18/1.35/1.02, Solana 2.22/2.58/1.58 (flat or INCREASING with age)
# 64-80% of his CORE/HIGH first entries happen >=60s AFTER the decisive
# crossing -- exactly what the old table suppressed by 85-98%. A skip-
# probability gate cannot boost (>1.0), so the new values are the post-
# boundary ratios capped at 1.0: only Bitcoin's 180s+ bucket (0.78, within
# noise of 1.0) suppresses anything. The SAME failure mode as the CHEAP
# freshness table (stale, pre-boundary, sharper than reality); both share the
# lesson that freshness tables must be re-measured on recent data.
DECISIVE_ENTRY_ELIGIBILITY_MULTIPLIER = {
    "Bitcoin":  [(0.0, 60.0, 1.00), (60.0, 120.0, 1.00), (120.0, 180.0, 1.00), (180.0, math.inf, 0.78)],
    "Ethereum": [(0.0, 60.0, 1.00), (60.0, 120.0, 1.00), (120.0, 180.0, 1.00), (180.0, math.inf, 1.00)],
    "Solana":   [(0.0, 60.0, 1.00), (60.0, 120.0, 1.00), (120.0, 180.0, 1.00), (180.0, math.inf, 1.00)],
}


def decisive_entry_eligibility_multiplier(asset: str, seconds_since_decisive_crossing: Optional[float]) -> float:
    """1.0 (no-op) when there's no recorded decisive crossing yet
    (seconds_since_decisive_crossing is None -- e.g. a market that
    opened already-decisive with no pre-crossing tick captured) or for
    any asset with no calibration -- otherwise the freshness bucket for
    this (asset, elapsed) cell, applied as a skip-probability at the
    call site (see build_order_intent), same discipline as
    cheap_joint_entry_multiplier. A negative elapsed (defensive:
    crossing timestamp somehow after `now`) falls back to the first
    (no-op) bucket rather than raising."""
    if seconds_since_decisive_crossing is None:
        return 1.0
    table = DECISIVE_ENTRY_ELIGIBILITY_MULTIPLIER.get(asset)
    if table is None:
        return 1.0
    elapsed = max(0.0, seconds_since_decisive_crossing)
    for lo, hi, mult in table:
        if lo <= elapsed < hi:
            return mult
    return 1.0


# ---------------------------------------------------------------------------
# REGIME ENGAGEMENT (2026-09-21): unlike every gate above/below (all keyed
# on FRESHNESS of a specific crossing, first entries only), this is a
# structurally different question -- does he engage in this REGIME at all,
# independent of how fresh or stale the current price level is. Found via
# a historical replay (see historical-replay-item-20-first-version.md /
# regime-targeting-gap-design.md, project memory): the real trader
# targets CHEAP/HIGH at 1.5-2.2x their NATURAL occurrence rate (computed
# from raw price_history_dense.jsonl ticks, i.e. what a perfectly
# time-uniform non-selective observer would see) and avoids MID/CORE at
# 0.3-0.75x -- validated per-asset AND calendar split-half, zero sign
# reversals anywhere. The replica's own regime mix, with every existing
# gate already active, tracked the NATURAL rate almost exactly (no active
# targeting at all) -- confirming this is a genuinely missing mechanism,
# not a miscalibration of an existing one.
#
# Calibrated as ratio_regime / ratio_anchor, where ratio is real-trader-
# share / natural-occurrence-rate and anchor is whichever regime has the
# HIGHEST ratio for that asset (kept at 1.0, a no-op -- can't "boost"
# above the existing baseline, only suppress relative to it). Bitcoin
# anchors on HIGH (1.85x vs CHEAP's 1.57x); Ethereum/Solana anchor on
# CHEAP (2.21x/2.13x vs HIGH's 1.55x/1.48x) -- a real, asset-specific
# asymmetry, not a simplification error.
#
# RECALIBRATED 2026-09-21, hours after the first version shipped: the
# disclosed drift risk above materialized faster and further than
# expected. A live comparison against the real trader's ACTUAL activity
# in the same wall-clock window (not his historical average) found he'd
# shifted so hard toward HIGH that the original table -- built on the
# full pooled historical window -- badly OVERSHOT relative to his
# CURRENT behavior: bot CHEAP hit 65-71% live (target at the time was
# 53.8%, but the trader's actual concurrent share was only 28.8%), MID/
# CORE were crushed to 2-6% (his concurrent share: ~10-20%/~8-10%), HIGH
# undershot at 24-25% (his concurrent share: 32-41%). Re-derived using
# ONLY recent data (last 72h, real trader n=541-1439/asset, natural rate
# from the same window) instead of the full pooled history -- stability-
# checked across last_24h/48h/72h windows first (same direction, some
# magnitude spread, matching this project's established secular-drift
# pattern rather than being a one-off blip). HIGH is now the anchor for
# ALL THREE assets (was CHEAP for Ethereum/Solana) -- a real behavioral
# shift, not a bigger dataset revealing the "true" original answer.
# Re-validated via the same historical-replay harness against this same
# recent window before shipping: CHEAP 52.1% vs his 49.4%, MID 6.7% vs
# 9.8%, CORE 7.6% vs 8.4%, HIGH 33.6% vs 32.3% -- all within a few
# points, a dramatic improvement over the table this replaces.
#
# STANDING RISK, now confirmed real rather than hypothetical: his
# regime-targeting behavior drifts meaningfully on a timescale of DAYS,
# not months. This table should be treated as needing periodic re-
# derivation against recent data, not a settled constant -- unlike most
# other tables in this file, which have stayed stable for weeks once
# validated. Revisit if a future live comparison shows a similar-sized
# gap reopening.
#
# REFRESHED 2026-09-22, exactly the drift this table's own risk note
# predicted: the live fidelity scorecard flagged CORE as a "barely
# traded" cell (n<8) for Bitcoin/Ethereum across all three active bots.
# Re-derived with the identical methodology (ratio_regime/ratio_anchor,
# last 72h natural rate from price_history_dense.jsonl pooled up/down
# ticks, real trader's first-entry share over the same window), split-
# half checked before shipping (same discipline as the 09-21 version):
#   Bitcoin  n=593:  CHEAP 0.40/0.37 half-split, MID 0.09/0.09, CORE
#                     0.34/0.33 -- all stable.
#   Ethereum n=410:  CHEAP 0.59/0.43 (more spread but same direction,
#                     both well below the old 0.83), MID 0.05/0.03,
#                     CORE 0.14/0.15 -- stable.
#   Solana   n=361:  CHEAP 0.55/0.46, MID 0.05/0.03, CORE 0.08/0.12 --
#                     CORE direction stable (both above old 0.08), some
#                     magnitude spread.
# Net effect vs the 09-21 table: CORE moves UP for every asset (0.23->
# 0.339 Bitcoin, 0.11->0.139 Ethereum, 0.08->0.104 Solana) -- directly
# targeting the scorecard's flagged gap. CHEAP moves DOWN meaningfully
# for Ethereum/Solana (0.83->0.50, 0.76->0.509) and mildly for Bitcoin
# (0.45->0.39). MID drops further for all three. HIGH stays the anchor
# (1.0) for every asset, unchanged.
REGIME_ENGAGEMENT_MULTIPLIER = {
    "Bitcoin":  {"CHEAP": 0.39, "MID": 0.09, "CORE": 0.339, "HIGH": 1.00},
    "Ethereum": {"CHEAP": 0.50, "MID": 0.037, "CORE": 0.139, "HIGH": 1.00},
    "Solana":   {"CHEAP": 0.509, "MID": 0.037, "CORE": 0.104, "HIGH": 1.00},
}


# PARTICIPATION_ASSET_SCALE -- ADDED 2026-09-22. A DIFFERENT axis from
# REGIME_ENGAGEMENT_MULTIPLIER just above: that table governs the MIX
# (which regime, once engaged) and, as a side effect, overall engagement
# rate too, but the two aren't separable within one table -- pushing
# overall participation up by raising every cell uniformly would also
# distort the regime mix that table was carefully calibrated for. This is
# a plain per-asset SCALE on top of it (min(1.0, base * scale) at the
# call site), leaving the regime-mix ratios between CHEAP/MID/CORE/HIGH
# exactly as calibrated -- only how often the asset gets engaged at all.
#
# Measured fresh 2026-09-22 (paperbot, since its current deploy, vs the
# real trader over the identical window, COMPLETE collector): Bitcoin bot
# 52.0% of windows vs his 84.0% (ratio 1.62); Ethereum bot 41.6% vs his
# 41.6% (ratio 1.00 -- already correct, left out of the table below,
# defaults to a no-op); Solana bot 26.4% vs his 44.8% (ratio 1.70).
# Shrunk to 80% of the log gap, same convention as every other level
# correction in this file.
PARTICIPATION_ASSET_SCALE = {
    "Bitcoin": 1.471,
    "Solana": 1.529,
}


def participation_asset_scale(asset: str) -> float:
    return PARTICIPATION_ASSET_SCALE.get(asset, 1.0)


def regime_engagement_multiplier(asset: str, regime: str) -> float:
    """1.0 (no-op) for any asset/regime with no calibration -- otherwise
    the per-asset targeting multiplier for this regime, applied as a
    skip-probability at the call site (see build_order_intent), same
    discipline as every other multiplier in this file. Unlike
    cheap_joint_entry_multiplier/decisive_entry_eligibility_multiplier,
    the CALLER is responsible for latching this per (side, regime)
    EPISODE, not re-rolling it every tick -- see build_order_intent's own
    comment at the call site for why (a flat per-tick roll against a
    long-lived regime "eventually" passes regardless of how low the
    probability is, confirmed live in the historical-replay validation
    before this was fixed)."""
    return REGIME_ENGAGEMENT_MULTIPLIER.get(asset, {}).get(regime, 1.0)


# ---------------------------------------------------------------------------
# CHEAP DEEP-TAIL PARTICIPATION BOOST. Added 2026-09-28, digging into
# paperbot-100's CHEAP oversizing (see project memory:
# cheap-deep-tail-underweight-shared-across-bots -- the real driver
# underneath the already-known 5-share exchange-minimum floor). Checked
# whether the bots' negative CHEAP ROI (paperbot-100 -38.6%/-21.5% on
# floor/non-floor trades, paperbot -9.1%) traced back to bad market
# selection -- it doesn't (win rate matches the trader almost exactly,
# ~10% either way) -- it traces back to PRICE PAID: the trader
# concentrates far more of his real CHEAP activity in the genuine deep
# tail (price < CHEAP_DEEP_TOUCH_PRICE_THRESHOLD, reusing that constant
# for consistency -- this is a DIFFERENT finding from
# cheap_deep_touch_edge_multiplier's structural-market-EV one, just the
# same natural depth cutoff) than any bot currently does:
#   asset      bot deep-fraction   trader deep-fraction   ratio needed
#   Bitcoin    9.0%  (n=398)       43.9%  (n=1,189)        7.91x
#   Ethereum   18.9% (n=164)       52.9%  (n=539)           4.82x
#   Solana     41.2% (n=85)        50.9%  (n=436)           1.48x
# (bot side measured on paperbot, the full-bankroll instance, so this
# isn't a floor-lot artifact -- confirmed the same shallow skew exists
# with no floor mechanism at play at all.)
#
# This is a PARTICIPATION gap (which price within CHEAP he actually
# trades), not a sizing one -- DEEP_CHEAP_TAIL_MULTIPLIER already scales
# SIZE once an entry happens; nothing before this scaled the LIKELIHOOD
# of entering at a deep price at all. Applied as a boost-only multiplier
# on top of REGIME_ENGAGEMENT_MULTIPLIER's own CHEAP roll (same
# architectural slot as PARTICIPATION_ASSET_SCALE/CLOSE_URGENCY --
# boost-only, "toward 1.0, never past" discipline), so the already-
# calibrated shallow-CHEAP rate is completely undisturbed and only deep
# entries get more likely.
#
# The "ratio needed" above (assuming the CURRENT bot deep-fraction
# approximates the natural tick-occurrence ratio, since a uniform
# dampener doesn't change relative proportions) would require pushing
# every asset's effective CHEAP-entry probability essentially to the 1.0
# ceiling for deep prices specifically -- a first-pass, single-window
# finding with no split-half validation yet (blocked the same way
# cheap_joint/decisiveness re-verification is: the dense book-history
# collector needed to independently confirm the "natural occurrence"
# assumption has been stale since Sep-22). Shipped at the standard
# 80%-of-the-way-to-the-ceiling shrinkage instead of the full correction:
# new_effective = baseline + 0.8*(1.0 - baseline), expressed here as the
# multiplier on top of baseline.
CHEAP_DEEP_TAIL_PARTICIPATION_BOOST = {
    "Bitcoin": 1.594,
    "Ethereum": 1.800,
    "Solana": 1.228,
}


def cheap_deep_tail_participation_multiplier(asset: str, price: float) -> float:
    """1.0 (no-op) for any asset with no calibration, or when price is at
    or above CHEAP_DEEP_TOUCH_PRICE_THRESHOLD (the shallow/broad part of
    CHEAP, deliberately left untouched -- see this table's own docstring).
    Boost-only (>=1.0) for the genuine deep tail -- the caller composes
    this multiplicatively with regime_engagement_multiplier's own result
    and clamps to 1.0, same pattern as entry_urgency_multiplier."""
    if price >= CHEAP_DEEP_TOUCH_PRICE_THRESHOLD:
        return 1.0
    return CHEAP_DEEP_TAIL_PARTICIPATION_BOOST.get(asset, 1.0)


# REGIME DRIFT CORRECTION -- ADDED 2026-09-23. A closed-loop overlay on TOP
# of REGIME_ENGAGEMENT_MULTIPLIER's static table above, same "plain scale on
# top of it" pattern PARTICIPATION_ASSET_SCALE already uses -- does not
# replace or re-derive the static calibration, just corrects for drift
# between recalibrations.
#
# Origin: idea sourced from a user-shared GitHub repo's DistributionController
# class (its own PnL claim was NOT trusted -- confirmed zero fill-friction
# modeling, "execution_mode": "PASSIVE_BID_PROXY", every signal treated as an
# instant guaranteed full fill at the best bid). The DISTRIBUTION-TRACKING
# idea itself was tested independently against our own real data (project
# memory: distribution-controller-beats-static-regime-probability) with
# proper train/test discipline: real chronological Bitcoin first-entry-regime
# sequence, static probability calibrated on the first half, both mechanisms
# evaluated held-out on the second half. Result: a closed-loop corrector beat
# the static-only mechanism by 64.6% on rolling-window mix error, robust
# 20/20 across random seeds -- because the static mechanism's HIGH share blew
# up to 52% on held-out data (target 27.6%) while the closed-loop version
# self-corrected. That's the exact real-world failure mode REGIME_ENGAGEMENT_
# MULTIPLIER has hit three times this month (684b655 -> 5c3b2ce -> 3f2c1f4).
#
# REGIME_MIX_TARGET: real trader's per-asset first-entry regime shares,
# post-Sep-18 boundary (n=713/540/458 Bitcoin/Ethereum/Solana, well-powered).
REGIME_MIX_TARGET = {
    "Bitcoin":  {"CHEAP": 0.4194, "MID": 0.1501, "CORE": 0.1543, "HIGH": 0.2763},
    "Ethereum": {"CHEAP": 0.5667, "MID": 0.0574, "CORE": 0.0519, "HIGH": 0.3241},
    "Solana":   {"CHEAP": 0.5175, "MID": 0.0721, "CORE": 0.0502, "HIGH": 0.3603},
}
# k=3.0 matches the exact value validated in the held-out test above -- not
# independently re-tuned, since re-tuning on the same data it was validated
# against would be circular. Cap bounds the correction factor so a market
# that has seen very few decisions so far (noisy realized share) can't send
# the multiplier to an extreme -- same discipline as HEDGE_RATIO_MULTIPLIER_CAP.
REGIME_DRIFT_CORRECTION_K = 3.0
REGIME_DRIFT_CORRECTION_CAP = 2.0


def regime_drift_correction(asset: str, regime: str, realized_counts: dict, realized_total: int) -> float:
    """Multiplicative correction factor applied ON TOP of
    regime_engagement_multiplier's static base (see call site in
    build_order_intent). 1.0 (no-op) when this asset has no target, or when
    realized_total==0 (cold start -- nothing to correct against yet, matches
    the validated test's cold-start handling). Otherwise boosts the
    engagement probability when this (asset, regime)'s cumulative realized
    share is BELOW its target, dampens when ABOVE -- self-correcting,
    doesn't require a manual recalibration pass to track drift."""
    target = REGIME_MIX_TARGET.get(asset, {}).get(regime)
    if target is None or realized_total <= 0:
        return 1.0
    realized_share = realized_counts.get(regime, 0) / realized_total
    deficit = target - realized_share
    factor = 1.0 + REGIME_DRIFT_CORRECTION_K * deficit
    cap = REGIME_DRIFT_CORRECTION_CAP
    return max(1.0 / cap, min(cap, factor))


# ---------------------------------------------------------------------------
# CHEAP ENTRY-SELECTIVITY vs TIME-TO-CLOSE. Added 2026-09-20, closing the
# other half of the same code-audit gap ADVERSE_MOVE_TTC_INTERACTION_COEF
# closes on the hedge side: this file has extensive TTC-aware SIZING but
# nothing that makes first-entry price SELECTIVITY depend on how much
# time remains -- cheap_joint_entry_multiplier above only ever sees
# seconds_since_cheap_crossing (time SINCE the crossing), never
# seconds_remaining (time UNTIL close).
#
# Derivation: Avellaneda-Stoikov's OTHER core formula (optimal spread
# widens with time remaining -- more time left means more risk, so a
# market maker demands a better price before committing) predicts he
# should be MORE selective (get a relatively cheaper fill) early in a
# window and LESS selective (accept whatever's available) as it nears
# close. A naive test of this (raw entry price vs time-to-close-at-entry)
# found a huge effect in the WRONG direction -- but that was confounded
# by the already-known mechanical fact that price drifts downward over a
# 5-minute window regardless of any deliberate choice (the same
# price/freshness collinearity found elsewhere in this file). Fixed by
# comparing his entry price's PERCENTILE within the distribution of
# prices actually available at that same time-to-close (occupancy-
# normalized against price_history_dense.jsonl, same trusted method as
# CHEAP_JOINT_ENTRY_MULTIPLIER's own original derivation) instead of the
# raw price level -- this isolates selectivity from the mechanical
# drift. Once corrected: Ethereum (t=-4.72) and Solana (t=-4.78) both
# show the predicted direction, bootstrap-confirmed (300 resamples,
# 100% negative i.e. more-selective-with-more-time, both 90% CIs
# entirely on the predicted side). Bitcoin shows nothing (t=-0.95) --
# excluded.
#
# Implementation, deliberately conservative: rather than reproduce the
# live percentile computation (needs the full occupancy distribution,
# not practical to carry at runtime), CHEAP_TTC_SELECTIVITY_REFERENCE_
# PRICE is a STATIC snapshot of the median available CHEAP price per
# (asset, time-to-close bucket) from the same derivation. At decision
# time, only a price ABOVE that bucket's reference (i.e. worse than
# typical for this point in the window) is dampened, scaled by how much
# time remains (more time left -> more willing to wait for better,
# matching the confirmed direction) and by how far above reference the
# price is. A price AT OR BELOW the reference is never dampened -- it's
# already as good or better than typical, nothing to be selective about.
# Uses the ABSOLUTE price difference from the reference, deliberately
# NOT a ratio -- the reference price itself scales roughly with ttc by
# construction (the same mechanical drift this mechanism exists to see
# past), so a ratio-based excess would largely self-cancel against the
# (ttc/300) scaling term (a shrinking reference inflates the ratio at
# low ttc, roughly offsetting the smaller ttc factor) -- caught in
# testing, before shipping, by a wiring test that initially showed no
# detectable effect. Explicitly a first-pass, monitored calibration
# (the coefficient below is chosen conservatively, not fit to a
# specific target magnitude) -- same discipline CHEAP_FRESHNESS_
# MULTIPLIER's own docstring already uses for a similarly soft,
# not-precisely-fit multiplier.
#
# Scope: CHEAP first entries only (real_fill_count==0 at the call site),
# not hedges, same gating as CHEAP_JOINT_ENTRY_MULTIPLIER.
CHEAP_TTC_SELECTIVITY_ASSETS = {"Ethereum", "Solana"}  # Bitcoin excluded, t=-0.95 n.s.
CHEAP_TTC_SELECTIVITY_REFERENCE_PRICE = {
    "Ethereum": [(0.0, 60.0, 0.0350), (60.0, 120.0, 0.0650), (120.0, 180.0, 0.1250),
                 (180.0, 240.0, 0.1750), (240.0, 300.0, 0.2250)],
    "Solana":   [(0.0, 60.0, 0.0500), (60.0, 120.0, 0.0650), (120.0, 180.0, 0.1150),
                 (180.0, 240.0, 0.1750), (240.0, 300.0, 0.2350)],
}
CHEAP_TTC_SELECTIVITY_COEF = 2.0
_CHEAP_TTC_SELECTIVITY_PRICE_DIFF_CAP = 0.15   # cap how much one extreme outlier price can suppress
_CHEAP_TTC_SELECTIVITY_FLOOR = 0.3             # never suppress harder than this from this mechanism alone


def cheap_ttc_selectivity_multiplier(asset: str, price: float, seconds_remaining: Optional[float]) -> float:
    """1.0 (no-op) for any asset outside CHEAP_TTC_SELECTIVITY_ASSETS,
    when seconds_remaining is unavailable, or when price is at or below
    the reference price for this (asset, time-to-close) bucket -- only a
    worse-than-typical price gets dampened, and only proportionally to
    how much time remains (more time left -> more selective), applied
    as a skip-probability at the call site alongside
    cheap_joint_entry_multiplier."""
    if asset not in CHEAP_TTC_SELECTIVITY_ASSETS or seconds_remaining is None:
        return 1.0
    buckets = CHEAP_TTC_SELECTIVITY_REFERENCE_PRICE.get(asset)
    if buckets is None:
        return 1.0
    ttc = max(0.0, min(300.0, seconds_remaining))
    reference = None
    for lo, hi, ref_price in buckets:
        if lo <= ttc < hi:
            reference = ref_price
            break
    if reference is None or reference <= 0 or price <= reference:
        return 1.0
    # ABSOLUTE price difference, deliberately NOT a ratio to the
    # reference: the reference price itself scales roughly with ttc by
    # construction (the same mechanical drift this whole mechanism
    # exists to see past), so a RATIO-based excess would largely
    # cancel against the (ttc/300) term below (shrinking reference ->
    # inflated ratio at low ttc, roughly offsetting the smaller ttc
    # factor) -- caught in testing before shipping. Absolute price
    # difference doesn't have this self-cancellation.
    price_diff = min(price - reference, _CHEAP_TTC_SELECTIVITY_PRICE_DIFF_CAP)
    result = 1.0 - CHEAP_TTC_SELECTIVITY_COEF * (ttc / 300.0) * price_diff
    return max(_CHEAP_TTC_SELECTIVITY_FLOOR, min(1.0, result))


# ---------------------------------------------------------------------------
# MID-BAND CONTRARIAN FIRST-ENTRY SIDE SELECTION. Added 2026-09-18: the
# real trader's first-entry side choice in MID genuinely favors whichever
# side has LOST value since the window opened -- not a coin flip, and not
# explained by anything already modeled (cross-market persistence,
# hedging, or the bot's own reprice mechanics). See
# external-bot-strategies-loop-progress.md's 2026-09-18 entries for the
# full derivation; summary of what was checked before trusting this:
#
# - Cross-asset: Bitcoin 62.2%, Ethereum 66.7%, Solana 70.5% (SWITCH
#   decisions, i.e. not explained by persistence) all independently
#   clear the 50% no-preference baseline.
# - Hedges REJECTED as the cause: hedge-shaped trades skew POSITIVE
#   (+0.02 mean velocity), the negative skew lives in ordinary entries.
# - Cross-market persistence REJECTED as the sole cause: PERSIST
#   decisions (69.6%) and SWITCH decisions (65.5%) show the same
#   pattern -- present regardless of whether he carried over his prior
#   side.
# - Side-choice base-rate REJECTED as a confound: he picks Up/Down
#   almost exactly 50/50, BOTH sides independently show the effect
#   (Up-choices 64.4%, Down-choices 71.4%), and the underlying market's
#   own Up-vs-Down price drift is essentially perfectly symmetric across
#   all 5,542 markets checked (50.1% vs 49.9%) -- ruling out "the market
#   trended one way during this window" as an alternative explanation.
# - Temporal-ordering integrity: 0/1,116 rows had the reference price
#   recorded AFTER his trade (no reversed-causality contamination).
# - Time-in-window: a real, SEPARATE effect (74.7% early -> 59.7% late,
#   strongest for fast/early moves) that does not explain away the
#   magnitude-graded pattern below.
# - Formal significance: every per-asset magnitude tercile clears
#   z>=1.94, all but one clear z>=2.57 (Bitcoin/Solana small-move
#   terciles and every medium/large tercile clear comfortably;
#   Ethereum's smallest-magnitude tercile is the one under-powered cell,
#   z=1.94, n=86 -- calibrated conservatively below rather than trusted
#   at face value).
#
# HONEST FRAMING, more important here than for any other table in this
# file: replicating this makes the bot's OWN paper win rate WORSE, not
# better (negative velocity correlates with lower win rate for both
# populations) -- this is a deliberate behavioral-fidelity build, not a
# performance one, per explicit instruction. Scoped to MID only (the
# ONLY regime where this is even measurable -- CHEAP/CORE/HIGH's own
# price boundaries mechanically force the sign of velocity for any
# market that reaches them, so there's no uninformed baseline to compare
# against there; this is not evidence the effect doesn't apply, just
# that those regimes can't test it).
#
# Applied as a PRIORITY override before decide_side's own persistence/
# random logic (same architectural pattern as forced_first_entry_side),
# not a blend -- the real mechanism (how this signal actually combines
# with cross-market persistence in his own decision process) isn't
# known; this is the simplest defensible first implementation, flagged
# for refinement if a future thread jointly models both signals.
#
# Magnitude tercile boundaries, per asset, in price units (|velocity|):
MID_CONTRARIAN_MAGNITUDE_TERCILES = {
    "Bitcoin":  (0.045, 0.125),
    "Ethereum": (0.075, 0.150),
    "Solana":   (0.080, 0.165),
}
# P(pick the side that has lost value since window-open), per asset per
# magnitude tercile (small, medium, large). Ethereum's small-move value
# is pulled toward Bitcoin/Solana's small-move average (60.6%) rather
# than its own raw 60.47% reading -- negligible difference in this case
# since the raw value already agrees closely, but documented as the
# deliberate, conservative treatment for that one under-powered cell.
MID_CONTRARIAN_BIAS_PROBABILITY = {
    "Bitcoin":  (0.5946, 0.6596, 0.7037),
    "Ethereum": (0.6060, 0.6744, 0.8276),
    "Solana":   (0.6082, 0.6809, 0.8200),
}
# Contrarian first entries are sized smaller -- a real, ~28% gap found
# the same day ($1.87 mean vs $2.61 mean for momentum-following first
# entries), consistent with treating a contrarian pick as a tentative,
# lower-conviction bet rather than a confident directional one. One
# flat ratio (not per-asset/tercile -- the size finding wasn't broken
# down that granularly), applied multiplicatively to the ordinary
# entry-curve notional.
MID_CONTRARIAN_SIZE_RATIO = 0.72


def mid_contrarian_bias_probability(asset: str, velocity_magnitude: float) -> float:
    """P(the contrarian first-entry override should fire) for this
    asset and this side's |velocity| since window-open. 0.0 (no-op --
    never fires) for an asset with no calibration, matching this file's
    usual pattern for uncalibrated/dormant assets."""
    terciles = MID_CONTRARIAN_MAGNITUDE_TERCILES.get(asset)
    probs = MID_CONTRARIAN_BIAS_PROBABILITY.get(asset)
    if terciles is None or probs is None:
        return 0.0
    lo, hi = terciles
    if velocity_magnitude < lo:
        return probs[0]
    if velocity_magnitude < hi:
        return probs[1]
    return probs[2]


# ---------------------------------------------------------------------------
# CROSS-ASSET DECISION CARRYOVER. Added 2026-09-19: the real trader's
# first-entry side choice on one asset (Bitcoin/Ethereum/Solana) is
# measurably correlated with his MOST RECENT first-entry side choice on
# a DIFFERENT asset, if that other decision happened within the last
# ~2-5 minutes -- decaying smoothly back to the 50% independence
# baseline beyond that. See external-bot-strategies-loop-progress.md's
# 2026-09-19 iterations 39-41 for the full derivation; summary of what
# was checked before trusting this:
#
# - NOT real cross-asset spot-price co-movement: BTC/ETH/SOL spot
#   returns genuinely co-move 80-82% of the time in the same 5-min
#   window, but his cross-asset side-agreement is statistically FLAT
#   regardless of whether real spot actually co-moved that window
#   (e.g. eth-sol: 64.1% when it did vs 64.4% when it didn't).
# - NOT a longer-horizon (30-min) trend either -- same flat-regardless-
#   of-real-co-movement result.
# - NOT a per-asset Up/Down base-rate artifact -- all three assets sit
#   at ~50/50 individually; the implied independent-baseline agreement
#   from those base rates alone is exactly 50.0% for every pair.
# - NOT the "harvest near close" mechanic -- excluding every HIGH-band
#   (>=0.90) entry from either side of the pair left the decay pattern
#   unchanged (if anything slightly stronger at the short end).
# - Corroborated on a SECOND, independent dimension: trade SIZE shows
#   the identical decay-with-gap pattern (log-size correlation +0.156
#   at <=15s decaying to +0.037 at 120-300s), and is consistently
#   stronger for same-side pairs than different-side pairs at every
#   gap bucket -- the signature of a real conviction/mood carryover,
#   not coincidence.
# - Passed temporal stability across pre-TWAP_SWITCH, post-TWAP/pre-
#   halt, and post-halt periods independently -- same decay shape in
#   all three, if anything slightly stronger post-halt.
# - Magnitude of the LEADING decision does NOT predict override
#   probability (flat ~60-65% across leader-size quartiles) -- the
#   mechanism below is deliberately probability-only, not scaled by
#   the other asset's trade size.
# - Directional leadership is real but asymmetric: Bitcoin is
#   consistently the stronger leader in both its pairs (see the table
#   below) -- consistent with Bitcoin's already-established attention-
#   priority status elsewhere in this project.
#
# SCOPED ENTIRELY TO CHEAP/MID (the follower's own book price at the
# suggested side, same convention as MID_CONTRARIAN below) -- CORE/HIGH
# show ZERO effect (48.1%/49.3%, exactly chance): those first entries
# are mechanically driven by that specific market's own near-certain
# price ("harvest the decided side"), leaving no room for cross-asset
# mood to intrude. This table was calibrated on CHEAP/MID follower
# entries specifically, not the broader all-regime aggregate.
#
# HONEST FRAMING: win-rate impact differs by regime, checked directly
# against real resolution data (2026-09-19). In CHEAP, following this
# signal is win-rate NEUTRAL (16.16% matched vs 16.10% unmatched --
# negligible). In MID, following it correlates with a real, measured
# ~5pp WORSE win rate (46.05% matched vs 51.10% unmatched) -- the same
# fidelity-over-performance tradeoff category as MID_CONTRARIAN_BIAS_
# PROBABILITY above, not a free win. Built anyway per this project's
# standing priority (replicate the trader's real behavior, not
# optimize the bot's own paper PnL).
#
# Probability of following the leader's side, per (leader, follower)
# ordered pair, by time-gap-since-the-leader's-decision bucket in
# seconds. Beyond the last bucket's upper bound (300s), treat as no
# signal (see cross_asset_carryover_probability's 0.0 default).
CROSS_ASSET_CARRYOVER_MAX_GAP_S = 300.0
CROSS_ASSET_CARRYOVER_PROBABILITY = {
    ("Bitcoin", "Ethereum"): ((0, 15, 0.697), (15, 30, 0.618), (30, 60, 0.544), (60, 120, 0.518), (120, 300, 0.515)),
    ("Ethereum", "Bitcoin"): ((0, 15, 0.736), (15, 30, 0.666), (30, 60, 0.657), (60, 120, 0.605), (120, 300, 0.601)),
    ("Bitcoin", "Solana"):   ((0, 15, 0.675), (15, 30, 0.575), (30, 60, 0.540), (60, 120, 0.538), (120, 300, 0.518)),
    ("Solana", "Bitcoin"):   ((0, 15, 0.698), (15, 30, 0.657), (30, 60, 0.645), (60, 120, 0.605), (120, 300, 0.525)),
    ("Ethereum", "Solana"):  ((0, 15, 0.781), (15, 30, 0.677), (30, 60, 0.660), (60, 120, 0.595), (120, 300, 0.554)),
    ("Solana", "Ethereum"):  ((0, 15, 0.778), (15, 30, 0.702), (30, 60, 0.680), (60, 120, 0.595), (120, 300, 0.538)),
}


def cross_asset_carryover_probability(leader_asset: str, follower_asset: str,
                                       gap_seconds: float) -> float:
    """P(the follower's first-entry side should be biased toward
    matching the leader's most recent first-entry side). 0.0 (no-op)
    for an uncalibrated pair or a gap outside the measured range --
    matching this file's usual pattern for "no signal available"."""
    if gap_seconds < 0 or gap_seconds >= CROSS_ASSET_CARRYOVER_MAX_GAP_S:
        return 0.0
    buckets = CROSS_ASSET_CARRYOVER_PROBABILITY.get((leader_asset, follower_asset))
    if buckets is None:
        return 0.0
    for lo, hi, prob in buckets:
        if lo <= gap_seconds < hi:
            return prob
    return 0.0


# ---------------------------------------------------------------------------
# STREAK-FADE first-entry side bias. Added 2026-09-25 (research-loop
# finding, fully re-verified -- see market-outcome-streak-fading-found.md
# in project memory for the complete derivation and every confound check
# below): after 2+ consecutive same-direction RESOLVED outcomes for an
# asset (independent of whether he traded any of the streak's own
# windows -- this is about the asset's real outcome history, not his own
# decisions), his first-entry side choice on the NEXT window fades --
# bets opposite the streak direction -- at 57-73% depending on streak
# length and how much time has elapsed since the current window opened,
# vs a 50% baseline.
#
# CONFOUND-CHECKED SIX WAYS:
# - Not a price-level artifact: fade bets are priced HIGHER, not lower,
#   than follow bets (median $0.575 vs $0.420) -- rules out "he's just
#   buying whichever side is cheap."
# - Strongest in HIGH regime specifically (63.5% fade, the highest of
#   any regime bucket) -- exactly where a cheap-pricing explanation
#   cannot apply at all.
# - Independent of the existing lag-1 cross_market_side_persistence
#   mechanism above: fade rate is flat (60.1% vs 58.1%) regardless of
#   whether his own previous bet on that same asset won or lost --
#   that mechanism's own docstring says persistence is "much stronger
#   after a loss than after a win," so if streak-fade were just a
#   restatement of it, fade rate would differ substantially between
#   those two subgroups. It doesn't.
# - Consistent across all three live assets (Bitcoin 56.2%, Ethereum
#   58.7%, Solana 70.1%), none reversing direction.
# - Replicated on a genuinely independent, later out-of-sample window
#   (73.3% fade vs the original 59.0%, same direction, if anything
#   stronger) -- both the base rate AND the elapsed-time decay shape
#   reproduced independently.
# - The streak-detection logic itself was unit-tested against 5
#   hand-built cases (a simple streak, a gap that must break the
#   streak, a missing-immediate-predecessor that must return no
#   signal, alternating outcomes that must show no streak, and a clean
#   5-long streak) -- all passed exactly as expected.
#
# IMPORTANT HONEST FRAMING (checked directly, not assumed): this is NOT
# primarily a rational response to a real market tendency. The REAL
# market's own base-rate reversal after a streak (checked across every
# resolved window, not just his trades) is only ~45-55%, roughly flat
# across streak lengths -- while his own fade rate is 57-73% and RISES
# with length even though the real predictive edge doesn't get any
# stronger there. He over-fades relative to what's actually predictive
# -- closer to gambler's-fallacy than skill. Built anyway per this
# project's standing priority to replicate his real observed behavior,
# not "correct" it toward the market's own true statistical structure
# (same principle already established by CROSS_ASSET_CARRYOVER_
# PROBABILITY's own honest-framing note above).
#
# Two independently-measured dimensions (streak length; seconds elapsed
# since the window opened), composed multiplicatively as ratios to the
# pooled overall fade rate -- same convention as every other two-
# dimensional table in this file (e.g. PARTICIPATION_ASSET_SCALE x
# REGIME_DRIFT_CORRECTION): a real joint (length x elapsed-time)
# cross-tab wasn't measured (samples too thin once split both ways), so
# this is the most faithful combination of what was actually measured,
# not an approximation of a joint table that doesn't exist.
STREAK_FADE_BASE_PROBABILITY = 0.590
# By streak length (2, 3, 4, 5+ capped): real fade rates 57.0%/65.2%/
# 56.5%/65.6%, expressed as ratios to the 0.590 base. Deliberately NOT
# monotonic -- kept exactly as measured (length=4 dips below length=3),
# not smoothed, matching this file's usual precedent (e.g. cross-market
# size momentum's own "kept exactly as measured, not smoothed" note).
STREAK_FADE_LENGTH_MULTIPLIER = {2: 0.966, 3: 1.105, 4: 0.958, 5: 1.112}
_STREAK_FADE_LENGTH_DEFAULT_MULTIPLIER = 1.112  # extrapolate flat past the measured 5+ bucket
# By seconds elapsed since the window opened (0-60/60-120/120-180/
# 180-240s midpoints): real fade rates 71.6%/64.7%/55.3%/52.3%,
# expressed as ratios to the 0.590 base. Strongest right after the
# window opens, decaying to near-baseline by 3+ minutes elapsed --
# mirrors TTC_SIZE_MULTIPLIER/entry_urgency_multiplier's own "no
# extrapolation past the measured window" discipline (clamped flat
# outside [30, 210]s elapsed since window open).
STREAK_FADE_ELAPSED_MULTIPLIER_MIDPOINTS = (30, 90, 150, 210)
STREAK_FADE_ELAPSED_MULTIPLIER = {30: 1.214, 90: 1.097, 150: 0.937, 210: 0.886}


def _interpolate_streak_elapsed(seconds_elapsed: float) -> float:
    # NOTE: unlike _interpolate_urgency elsewhere in this file (whose
    # midpoints run DESCENDING, 165->75), STREAK_FADE_ELAPSED_MULTIPLIER_
    # MIDPOINTS runs ASCENDING (30->210), since "elapsed since window
    # open" grows the opposite direction from "seconds remaining." The
    # loop below is written for ascending midpoints specifically -- do
    # not copy the descending-midpoint version from _interpolate_urgency.
    se = max(30.0, min(210.0, seconds_elapsed))
    mids = STREAK_FADE_ELAPSED_MULTIPLIER_MIDPOINTS
    for i in range(len(mids) - 1):
        lo_mid, hi_mid = mids[i], mids[i + 1]
        if lo_mid <= se <= hi_mid:
            lo_val, hi_val = STREAK_FADE_ELAPSED_MULTIPLIER[lo_mid], STREAK_FADE_ELAPSED_MULTIPLIER[hi_mid]
            frac = (se - lo_mid) / (hi_mid - lo_mid)
            return lo_val + frac * (hi_val - lo_val)
    return STREAK_FADE_ELAPSED_MULTIPLIER[mids[-1]]  # defensive; se is always in [30,210] by the clamp above


def streak_fade_probability(streak_length: int, seconds_elapsed: Optional[float] = None) -> float:
    """P(his first-entry side choice fades -- bets opposite -- the
    asset's recent resolved-outcome streak). `streak_length` must be >=2
    for a real signal (the caller should not invoke this for a shorter
    or nonexistent streak); returns a neutral 0.5 defensively if it
    somehow is. `seconds_elapsed` (seconds since the current market's
    own window opened) is Optional -- default None makes the elapsed-
    time factor no-op at 1.0 (the pooled length-only rate), so a caller
    without timing info still gets a sensible answer rather than
    crashing. Always returns a valid probability in [0, 1]."""
    if streak_length < 2:
        return 0.5
    length_mult = STREAK_FADE_LENGTH_MULTIPLIER.get(
        min(streak_length, 5), _STREAK_FADE_LENGTH_DEFAULT_MULTIPLIER)
    elapsed_mult = _interpolate_streak_elapsed(seconds_elapsed) if seconds_elapsed is not None else 1.0
    p = STREAK_FADE_BASE_PROBABILITY * length_mult * elapsed_mult
    return max(0.0, min(1.0, p))


# ---------------------------------------------------------------------------
# HOUR-CONDITIONED SIZE BOOST. Added 2026-09-25 (research-loop finding):
# his median first-entry size runs measurably higher during the
# 06:00-07:00 UTC hour specifically -- $3.50-3.56 vs $2.77-2.99 baseline
# (~19-26% higher), confirmed on two independent samples (the original
# dataset and a genuinely fresh, later out-of-sample window). The SAME
# hour also shows a much larger per-trade EDGE effect (+11.2pp vs +2.5pp
# baseline, ~4.4x, replicated with the exact spike-then-crash shape
# reproduced independently -- see edge-by-hour-0600-utc-standout.md) --
# but this table deliberately tracks only his OBSERVED SIZE reaction
# (~1.2x), not the much larger edge itself. Chasing the edge instead of
# his real behavior would be "optimizing the bot's own paper PnL," which
# this project's standing rule explicitly rejects in favor of replicating
# his real observed behavior (same principle noted on CROSS_ASSET_
# CARRYOVER_PROBABILITY and STREAK_FADE_BASE_PROBABILITY above).
#
# BOOST-ONLY construction (1.0 clean no-op outside 06:00-07:00 UTC) --
# same "only ever a boost or a no-op" discipline as mid_boundary_edge_
# multiplier/cheap_deep_touch_edge_multiplier/entry_urgency_multiplier.
# Flat across the hour (no sub-hour shape measured for SIZE specifically,
# unlike the edge finding's own 30-minute-bucket breakdown).
HOUR_SIZE_BOOST_UTC_HOURS = (6,)
HOUR_SIZE_BOOST_MULTIPLIER = 1.22  # midpoint of the two independently-measured ratios (~1.19-1.26)


def hour_size_multiplier(hour_utc: Optional[int]) -> float:
    """Boost-only: HOUR_SIZE_BOOST_MULTIPLIER during 06:00-07:00 UTC
    specifically, 1.0 (clean no-op) every other hour and for None (hour
    not known)."""
    if hour_utc is None:
        return 1.0
    if hour_utc in HOUR_SIZE_BOOST_UTC_HOURS:
        return HOUR_SIZE_BOOST_MULTIPLIER
    return 1.0


# ---------------------------------------------------------------------------
# DECISIVENESS-ONSET size scaling. Added 2026-09-13: a market's price
# snapping into a decisive band (>=0.70) is a distinct event from just
# "having a decisive price" -- the trader reacts to the SNAP, not the
# static level, on a timescale set by how much time is left when it
# happens (originally reframed from a pure time-to-close angle;
# see decisiveness-collector-gamma-caching-bug.md's full derivation).
#
# Confirmed real via a ceiling-effect confound check before building:
# early entries are structurally capped at a smaller max-possible delay
# than late ones (a crossing must precede the entry), so "tighter early /
# wider late" delay alone could just be that ceiling. Controlling for it
# -- expressing delay as a FRACTION of the time actually available when
# the crossing happened (window close minus crossing time), not a raw
# second count -- the effect survives: late-crossing markets consistently
# show a SMALLER fraction consumed (more urgent) than early-crossing ones,
# for all 3 assets. This is the deployable quantity; a fixed-second-window
# gate would have miscalibrated for crossings outside the exact buckets
# originally tested.
#
# Measured (35h of dense CLOB-book history, decisive-regime real entries
# only, n=1727/433/228 for BTC/ETH/SOL): density of entries across
# fraction-of-remaining-time-since-crossing buckets, relative to a flat
# no-preference null. Values below are that ratio, capped to [0.5, 2.0]
# (this project's usual safety bound for a frequency-derived multiplier)
# -- Solana's [0.00,0.15) bucket in particular is thin (n=13) and its raw
# 0.38x ratio is floored to the cap rather than trusted at face value.
# The final, open-ended bucket ([0.60, 1.0+)) had ZERO observations in
# ALL THREE assets at every sample size reached so far -- left at a
# neutral 1.0x (no boost, no suppression) rather than extrapolating a
# shape with no supporting data.
#
# Only meaningful for CORE/HIGH entries (price >= DECISIVENESS_THRESHOLD)
# -- see decisiveness_onset_multiplier's own gating. Bucket boundaries
# are fractions in [0, 1]; a fraction outside [0, 1) (crossing happened
# so long ago that more than the entire post-crossing window has already
# elapsed, which shouldn't occur in practice since a market closes at
# fraction==1.0) falls back to the same neutral 1.0x as the empty tail
# bucket.
DECISIVENESS_ONSET_MULTIPLIER = {
    "Bitcoin":  [(0.00, 0.15, 1.74), (0.15, 0.30, 1.55), (0.30, 0.45, 1.08),
                 (0.45, 0.60, 1.35), (0.60, 1.00, 0.71)],
    "Ethereum": [(0.00, 0.15, 1.06), (0.15, 0.30, 1.42), (0.30, 0.45, 0.95),
                 (0.45, 0.60, 1.77), (0.60, 1.00, 1.10)],
    "Solana":   [(0.00, 0.15, 0.50), (0.15, 0.30, 1.05), (0.30, 0.45, 1.05),
                 (0.45, 0.60, 1.87), (0.60, 1.00, 1.73)],
}
_DECISIVENESS_ONSET_MULTIPLIER_CAP = 2.0


def decisiveness_onset_multiplier(asset: str, regime: str, fraction_consumed: Optional[float]) -> float:
    """Size multiplier keyed by how much of the time remaining-at-crossing
    has elapsed since this side's price first snapped decisive. 1.0 (no-op)
    for CHEAP/MID (never measured there -- the whole angle is about a
    market that has already become decisive), for an asset with no table
    entry, or when fraction_consumed is None (no crossing observed yet,
    e.g. a market that opened already-decisive with no pre-crossing tick
    captured, or DECISIVENESS_ONSET_MULTIPLIER's own feature disabled
    upstream)."""
    if regime not in ("CORE", "HIGH") or fraction_consumed is None:
        return 1.0
    buckets = DECISIVENESS_ONSET_MULTIPLIER.get(asset)
    if buckets is None:
        return 1.0
    for lo, hi, mult in buckets:
        if lo <= fraction_consumed < hi:
            return max(1.0 / _DECISIVENESS_ONSET_MULTIPLIER_CAP,
                       min(_DECISIVENESS_ONSET_MULTIPLIER_CAP, mult))
    return 1.0  # fraction_consumed >= 1.0 (or negative, defensively) -- the empty tail case


# ---------------------------------------------------------------------------
# DECISIVENESS x TTC x WITHIN-BAND overshoot correction. Added 2026-09-20,
# closing checklist item #25's first well-powered confirmation: unlike
# the momentum x bankroll pair (tested and found to be a calendar-drift
# artifact, not real), THIS overlap is genuine and structural.
# decisiveness_fraction = (now-crossed_at)/(end_time-crossed_at) and
# seconds_remaining = end_time-now are mechanically related within a
# single market (both derived from the same timestamps) -- confirmed
# r=-0.82 (t=-42 to -78, all three assets independently, n=1030-2876)
# in CORE/HIGH, far stronger than any calendar-drift artifact.
#
# Direct real-vs-predicted test (first decisive-side entry per market,
# HIGH only, decision-collapsed, price_history_dense.jsonl): multiplying
# within_band x ttc x decisiveness together OVERSHOOTS real sizing
# specifically at HIGH decisiveness_fraction (>=0.45) for Ethereum/
# Solana -- Bitcoin actually UNDERSHOOTS across the board (a separate,
# unexplained gap, NOT evidence against this correction, left
# uncalibrated here since correcting an undershoot isn't this finding's
# job). Split-half on the Sep-18 boundary (the exact regime-boundary
# lesson from item #31's own re-derivation, applied here BEFORE shipping
# this time) confirms stability: Ethereum real ratio 1.68 pre-boundary /
# 2.06 post (n=210/57); Solana 1.96 pre / 1.97 post (n=229/59) -- both
# stable across the boundary, unlike item #31's fragile Solana hedge
# hump. config.COMBINED_SIZE_MULTIPLIER_CAP=4.0 does NOT catch this --
# the overshoot bites at ~1.9-2.2x, well under that blanket cap.
#
# Keyed by asset and decisiveness_fraction bucket (only the two buckets
# that actually overshoot; [0.00,0.45) was already close to 1:1 and is
# left uncorrected, 1.0 no-op):
#   Ethereum: [0.45,0.60) predicted/real=1.765 -> correction 0.567
#             [0.60,1.00) predicted/real=1.139 -> correction 0.878
#   Solana:   [0.45,0.60) predicted/real=1.937 -> correction 0.516
#             [0.60,1.00) predicted/real=2.168 -> correction 0.461
#
# BITCOIN ADDED 2026-09-22, in direct response to the live fidelity
# scorecard showing Bitcoin HIGH sizing at 0.29-0.60x the real trader's
# across all three active bots. This function's own docstring had
# already flagged the mechanism ("Bitcoin actually UNDERSHOOTS across
# the board... left uncalibrated here since correcting an undershoot
# isn't this finding's job") -- closing that deferred gap now with the
# same methodology used for Ethereum/Solana above (real/predicted ratio
# of usd size vs median*within_band*ttc*decisiveness_onset, first HIGH
# entries only, price_history_dense-reconstructed decisiveness_fraction).
#
# UNLIKE Ethereum/Solana, Bitcoin's overshoot lives in the LOW bucket
# ([0.00,0.45), the one left uncorrected for the other two assets, well
# powered at n=313) -- direction is a BOOST (>1.0), not a dampener,
# since Bitcoin undershoots rather than overshoots. The [0.45,0.60) and
# [0.60,1.00) buckets are too thin for Bitcoin (n=17, n=3) to trust and
# are left at 1.0, matching this file's usual thin-cell discipline.
#
# HONEST CAVEAT, stronger than usual: this ratio is NOT stable the way
# the Ethereum/Solana values were -- split-half over the full ~9-day
# history gives 1.03 (first half) vs 1.56 (second half), and even
# restricting to just the last 3 days shows 1.41 (first day and a half)
# vs 1.87 (most recent) -- a real, still-ongoing upward drift, not a
# settled step-change. Shipped the last-3-days median (1.567) as the
# best current estimate, flagged "RE-CHECK WITHIN DAYS."
#
# BITCOIN RETIRED 2026-09-23 (systematic post-Sep-18 audit, same-day
# re-check as flagged above): the predicted drift didn't just continue,
# it REVERSED DIRECTION entirely. Fresh reconstruction (n=209, full
# median*within_band*ttc*decisiveness_onset chain, TTC included this
# time -- an earlier same-day check that omitted TTC gave a spuriously
# different number, since fraction_consumed and seconds_remaining are
# mechanically correlated r=-0.82, the exact collinearity this whole
# correction exists to handle): median real/pred(uncorrected) = 0.821,
# split-half 0.725/0.937 -- BOTH halves now below 1.0, meaning the fresh
# data wants a DAMPENER, not the shipped 1.567 BOOST. Keeping a boost
# that reality has flipped away from would be actively wrong, worse than
# no correction. Reverted to 1.0 (no-op) rather than chase a number that
# has now reversed sign within about a day -- this bucket needs a much
# longer, more stable baseline before any correction is trustworthy
# again. Ethereum/Solana checked the same way and stayed reasonably
# consistent with their shipped values (same direction, same order of
# magnitude, more noise than drift) -- left unchanged.
DECISIVENESS_TTC_OVERSHOOT_CORRECTION = {
    "Ethereum": [(0.45, 0.60, 0.567), (0.60, 1.00, 0.878)],
    "Solana": [(0.45, 0.60, 0.516), (0.60, 1.00, 0.461)],
}


def decisiveness_ttc_overshoot_correction(asset: str, regime: str,
                                           fraction_consumed: Optional[float]) -> float:
    """1.0 (no-op) for CHEAP/MID (decisiveness_fraction is never
    populated there), for an asset/bucket combination not covered above
    (e.g. Bitcoin's own [0.45,1.00) range -- too thin to calibrate, see
    this table's docstring), or when fraction_consumed is None. HIGH
    only -- CORE was not part of the confirmed overshoot (its
    predicted/real ratios stayed <=1 in the same joint test)."""
    if regime != "HIGH" or fraction_consumed is None:
        return 1.0
    buckets = DECISIVENESS_TTC_OVERSHOOT_CORRECTION.get(asset)
    if buckets is None:
        return 1.0
    for lo, hi, mult in buckets:
        if lo <= fraction_consumed < hi:
            return mult
    return 1.0


# ---------------------------------------------------------------------------
# DECISIVENESS x WITHIN-BAND overshoot correction. Added 2026-09-21, closing
# a SECOND, distinct double-counting bug found via the on-chain share-sizing
# audit (see onchain-share-sizing-discovery-and-code-audit.md) -- different
# from DECISIVENESS_TTC_OVERSHOOT_CORRECTION above, which corrects
# decisiveness_fraction's collinearity with seconds_remaining (TTC). This
# corrects its SEPARATE collinearity with raw price level (WITHIN_BAND_
# SIZE_SLOPE's own axis), and unlike the TTC correction, hits CORE as well
# as HIGH.
#
# Real-vs-predicted joint test: for each (asset, regime, price sub-band),
# compared real mean entry size at decisiveness_fraction<0.45 ("early")
# vs >=0.45 ("late") against what within_band_size_multiplier x
# decisiveness_onset_multiplier predicts for the same split. Holding price
# fixed isolates the fraction axis's own error from WITHIN_BAND's
# legitimate, already-correct price-driven growth (unlike a naive "high
# price + late fraction vs everything else" comparison, which conflates
# the two). Real size DOES grow somewhat as decisiveness_fraction
# increases (matching decisiveness_onset_multiplier's own intent) but
# consistently less than the model predicts, in every price sub-band
# tested, both assets, both regimes:
#   Ethereum CORE: overshoot 1.19-1.42x across 2 price bands, n-weighted 1.261
#   Ethereum HIGH: overshoot 1.24-1.28x across 2 price bands, n-weighted 1.267
#   Solana CORE:   overshoot 1.76-2.37x across 2 price bands, n-weighted 2.013
#   Solana HIGH:   overshoot 1.77-1.79x across 2 price bands, n-weighted 1.782
# Bitcoin explicitly checked and excluded: shows the OPPOSITE pattern
# (overshoot 0.69-0.75, i.e. UNDERshoots) at every price band tested, same
# "separate unexplained gap, not this correction's job" reasoning as the
# TTC correction above.
#
# Split-half stability (Sep-18 boundary): direction (real overshoot) holds
# in every cell testable on both sides of the boundary, though several
# post-boundary cells (especially Solana CORE) are too thin to independently
# confirm magnitude -- calibrated on the full-sample (pre+post pooled)
# numbers above, same convention as every other split-half-checked table
# in this file.
#
# Coarser than DECISIVENESS_TTC_OVERSHOOT_CORRECTION's two fraction
# sub-buckets ([0.45,0.60)/[0.60,1.00)) -- this uses one combined
# [0.45,1.00) bucket per (asset, regime), since the underlying joint test
# only supports a clean early/late split at current sample sizes, not a
# finer one. [0.00,0.45) is left at the implicit 1.0 no-op, same "close to
# 1:1 already" reasoning as the TTC correction's own early bucket.
# CORRECTED 2026-09-22 (same-day-after-ship self-check, share-based-sizing
# Phase 2 work): the original derivation above tested only within_band x
# decisiveness_onset, but production ALSO multiplies ttc_mult and the
# already-shipped DECISIVENESS_TTC_OVERSHOOT_CORRECTION (HIGH only, fit
# against within_band x ttc x decisiveness). For HIGH that older correction
# had ALREADY absorbed this overshoot, so the HIGH values here (0.789/0.561)
# double-corrected: tested against the full live formula, Ethereum HIGH went
# 1.05 -> 0.83 and Solana HIGH 1.03 -> 0.58 (predicted/real late-vs-early
# ratio; 1.0 = calibrated). HIGH entries REMOVED. CORE has no older
# correction, so its correction is real and was RE-FIT against the full
# formula: Ethereum CORE 1/1.15 = 0.87 (was 0.793, which over-corrected to
# 0.91); Solana CORE 0.497 stays (2.00 -> 0.99). With this table every
# Ethereum/Solana CORE/HIGH cell lands at 0.92-1.05 on both the old base and
# the share-based base (ENABLE_SHARE_BASED_SIZING), so the decisiveness
# corrections need no separate re-derivation for the share-based base.
DECISIVENESS_WITHINBAND_OVERSHOOT_CORRECTION = {
    "Ethereum": {"CORE": 0.87},
    "Solana": {"CORE": 0.497},
}


def decisiveness_withinband_overshoot_correction(asset: str, regime: str,
                                                   fraction_consumed: Optional[float]) -> float:
    """1.0 (no-op) for Bitcoin (undershoots here, a separate unexplained
    gap, same reasoning as decisiveness_ttc_overshoot_correction), for
    CHEAP/MID (decisiveness_fraction is never populated there), or when
    fraction_consumed is None/below 0.45 (already close to 1:1, confirmed
    in the same joint test that found the overshoot above 0.45). Applies
    to CORE only (HIGH's overshoot is already handled by the TTC
    correction -- see the 2026-09-22 note on the table)."""
    if regime != "CORE" or fraction_consumed is None or fraction_consumed < 0.45:
        return 1.0
    return DECISIVENESS_WITHINBAND_OVERSHOOT_CORRECTION.get(asset, {}).get(regime, 1.0)


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
# CHECKED FOR POST-HALT FRESHNESS 2026-09-13 (/loop cycle 14). The
# original audit's own item #11 check ("CONFIRMED, holds up well") used
# a 5d/14d window queried close to the halt's own resumption -- both
# windows substantially straddle the halt, the same blind-window issue
# later found and fixed for SCOUT_PROBABILITY (cycle 7). Redone here
# with a strict post-halt-only (ts>=HALT_END) filter, median usdc per
# TTC-midpoint bucket, weighted-mean-normalized:
#
# CHEAP/MID (all 6 cells, healthy post-halt n=490-4433): SAME SHAPE
# preserved for every cell -- CHEAP still declines late-window, MID
# still rises late-window, exactly as originally found. Bitcoin's
# magnitudes barely moved; Ethereum/Solana's got noticeably MORE
# EXTREME (wider spread) than before, e.g. Solana/MID's late-window
# peak rose from 1.5376 to 1.734, Ethereum/CHEAP's late-window trough
# fell from 0.8513 to 0.6453. Recalibrated below.
#
# CORE/HIGH: same general shape/direction also holds on a first look,
# but NOT recalibrated -- the 270-midpoint bucket (longest time
# remaining) is too thin in the post-halt window to trust a full
# refresh (Bitcoin n=101, Ethereum n=25, Solana n=31), especially in
# HIGH where the whole finding IS the 270-bucket's low value. Left at
# the existing (pre-halt-inclusive) numbers rather than reshape a
# thinly-supported point -- revisit once more post-halt HIGH/CORE
# volume accumulates.
# RETIRED 2026-09-26 (full sizing-mechanism audit, "verify everything
# against the trader" pass): every previous freshness check on this table
# used the project's off-chain "5-second CLOB-fragment collapse" merge
# convention on trades.jsonl/activity_v2.jsonl -- since confirmed
# DECISIVELY WRONG for decision reconstruction (see five-second-merge-
# convention-decisively-wrong in project memory: fits the real share-price
# formula 4.75x WORSE than treating each true on-chain order separately).
# This is the first re-check done against TRUE on-chain order identity
# (OrderFilled events grouped by orderHash, not wall-clock proximity).
#
# Pulled every real maker fill for the last ~37h clean (RPC history
# pruning limited the window; 3,594 true first/add-on/hedge orders across
# 815 markets, 100% maker as expected). Direct ablation on first-entry
# sizing (median*within_band*level_correction, WITH vs WITHOUT this
# table's ttc_mult factor), judged by median|ln(real/pred)|:
#   overall (n=815): WITH=0.4770  WITHOUT=0.4454 -- TTC HURTS
#   10 of 12 (asset, regime) cells individually HURT (only Bitcoin/MID
#   and Solana/HIGH improved with it); per-cell spearman(seconds_
#   remaining, size residual) is weak (|rho|<0.25 in 9/12 cells) and
#   WRONG-SIGNED vs this table's own shape in every MID cell (all three
#   assets), which is supposed to be this table's strongest signal.
#   Split-half stable: BOTH the first half (n=407) and second half
#   (n=408) of the window independently show TTC hurting (0.4997 vs
#   0.4750; 0.4517 vs 0.4292) -- not a one-sided fluke.
# Best explanation: this table was originally built and re-confirmed
# twice (2026-09-10, 2026-09-13 post-halt checks) entirely on the same
# flawed merge convention that's already been shown to corrupt
# POSITION_TIER_SHARE_MULTIPLIER and the original HEDGE_PRICE_SIZING_
# SCALE calibration -- the apparent "holds up well" freshness checks
# were re-measuring the same artifact, not the real relationship.
# Table emptied rather than recalibrated on this ~37h sample (matches
# this file's standing convention for a table whose live effect no
# longer replicates -- see CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER/
# BANKROLL_PNL_SIZE_MULTIPLIER's own "table emptied" precedent just
# below) -- ttc_size_multiplier() already returns 1.0 (no-op) for any
# asset/regime not present, so this is a clean, safe no-op everywhere.
# Re-derive from true on-chain data (not the merge convention) before
# ever re-enabling a TTC-shaped sizing signal.
TTC_SIZE_MULTIPLIER = {}
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


# RECENT_SIDE_PERSISTENCE_OVERRIDE -- ADDED 2026-09-22. SIDE_PERSISTENCE above was fitted on the
# full history (~117k of its 139k decisions are Jul-Sep8 when he flipped sides often). His pooled
# persistence has since risen in steps: 72.5% (Jul-Sep8) -> 80.7% (Sep8-18) -> 90.6% (post-Sep18,
# n=1,792), i.e. his flip rate halved twice. Our bots' two-sided-market rate (22-27%) is ~2x his
# (12.5%), and ~90% of that two-sidedness is ordinary entries flipping sides, not hedge orders.
# Only cells with >=100 post-Sep18 observations are overridden, each shrunk toward the table
# by treating the table as 100 prior observations: new = (n*post + 100*table)/(n+100).
# Bitcoin (n=403/299/208/440): CHEAP .864, MID .943, CORE .942, HIGH .966 (table .773/.825/.866/.884);
# Ethereum CHEAP .767 (n=116), HIGH .936 (n=141). Thin cells (Solana, Ethereum MID/CORE) unchanged.
# Re-fit as post-Sep18 data accumulates; verify via the live scorecard's dual-sided rate.
RECENT_SIDE_PERSISTENCE_OVERRIDE = {
    "Bitcoin": {"CHEAP": 0.846, "MID": 0.913, "CORE": 0.917, "HIGH": 0.951},
    "Ethereum": {"CHEAP": 0.794, "HIGH": 0.882},
}


def side_persistence(asset: str, held_side_regime: str, use_recent_override: bool = True) -> float:
    """P(keep the currently-held side) given the regime that side is
    CURRENTLY trading in (i.e. the regime it would land in if persisted --
    see strategy.decide_side for why this, not the regime a switch would
    resolve to, is the only framing knowable before the decision is made).

    MERGED 2026-09-28 (was side_persistence_for + recent_side_persistence,
    a strict linear pipeline with exactly one caller -- see git history
    if the two need separating again for some new independent caller).
    `use_recent_override` preserves the caller's own
    config.ENABLE_RECENT_SIDE_PERSISTENCE gating (a safety valve, not
    currently disabled on any bot, but kept controllable) -- False skips
    RECENT_SIDE_PERSISTENCE_OVERRIDE entirely and returns the base table
    value, same as the pre-merge two-call pattern with the flag off.
    Base table (SIDE_PERSISTENCE) raises BehaviorLookupError for an
    uncalibrated (asset, regime) -- these should always be one of the 3
    live assets x 4 regimes, a genuinely missing cell means a real bug,
    not an expected no-op. RECENT_SIDE_PERSISTENCE_OVERRIDE, where
    present, takes precedence over the base value -- see its own
    docstring for the shrinkage-toward-the-full-history-table derivation."""
    try:
        base = SIDE_PERSISTENCE[asset][held_side_regime]
    except KeyError as exc:
        raise BehaviorLookupError(asset, held_side_regime) from exc
    if not use_recent_override:
        return base
    return RECENT_SIDE_PERSISTENCE_OVERRIDE.get(asset, {}).get(held_side_regime, base)


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
#
# RECALIBRATED 2026-09-13 (post-halt audit, /loop cycle 9). Unlike almost
# every other table fixed this session, this one held up well: post-halt
# rates are within 1-2pp of the old pooled-whole-history values for all
# 3 assets, not a dramatic level shift. Recalibrated anyway since
# post-halt-only is strictly better methodology (matches this session's
# established discipline) and n is solid (BTC 633/687, ETH 476/747, SOL
# 503/733 after_win/after_loss consecutive pairs, gap<=1h, same causally-
# clean methodology as the original). Bitcoin/Solana's win-vs-loss
# DIFFERENCE still clears this file's own |z|>=2.58 trust bar on its own
# post-halt sample (BTC z=3.478, SOL z=2.999). Ethereum's has weakened to
# JUST below the bar (z=2.191, was z=-3.28 originally) -- flagged
# honestly rather than silently kept at the old, more-significant-looking
# number; direction and rough magnitude still match Bitcoin/Solana
# (persistence stronger after a loss than after a win), so kept as
# measured rather than forced back to a flat rate, but treat Ethereum's
# split here as the weakest-supported of the three going forward.
#
# ALSO ATTEMPTED THIS CYCLE: the standing CHEAP-band edge-gap
# investigation (see cheap-edge-gap-root-cause-persistence-miscalibration
# in project memory) proposed retargeting this table toward "does
# persisting predict a WIN" rather than "matches his revealed frequency,"
# specifically because Ethereum's after-win rate sits below 50% (favors
# switching). Directly tested that on fresh post-halt CHEAP-only data
# (does persist-vs-switch predict winning THIS market, for markets
# landing in CHEAP): BTC edge +2.60pp (z=0.588), ETH edge -1.84pp
# (z=-0.560), SOL edge +2.64pp (z=0.746) -- all three nowhere near the
# trust bar, pure noise at this sample size (n=151-295/cell). CONCLUSION:
# there is no significant win-predicting signal to retarget toward in
# CHEAP: the proposed fix is not viable with current data, and
# frequency-matching (this table's actual design) remains the only
# defensible choice, not a mistake to correct. Closes that memory's
# recommendation #1 as "attempted, not viable" rather than open-ended.
# Bitcoin after_loss RECALIBRATED 2026-09-23 (systematic post-Sep-18
# audit): fresh post-boundary rate (n=302) is 0.4900, a real -12.3pp
# drop from the shipped 0.6128 -- Bitcoin has lost most of its
# persist-after-a-loss bias, now close to a coin flip. after_win barely
# moved (0.5182->0.5333, within noise, left unchanged). Ethereum/Solana
# both checked fresh too and stayed close to shipped (within ~3.5pp on
# both cells) -- left unchanged.
# HONEST CAVEAT: split-half WITHIN the post-boundary window itself shows
# this is still moving, not settled -- 0.525 (first half, n=179) -> 0.439
# (second half, n=123), an ongoing decline rather than a one-time step.
# Shipped the pooled estimate rather than the thinner, more-recent half
# alone. Style-only mechanism (already established non-predictive of win
# rate -- see this section's own docstring above), so this affects
# behavioral-frequency fidelity only, not PnL. RE-CHECK WITHIN DAYS if
# the decline continues, same discipline as ENTRY_SIZE_LEVEL_CORRECTION's
# CHEAP cells.
CROSS_MARKET_SIDE_PERSISTENCE = {
    "Bitcoin": {"after_win": 0.5182, "after_loss": 0.4900},
    "Ethereum": {"after_win": 0.4874, "after_loss": 0.5515},
    "Solana": {"after_win": 0.4990, "after_loss": 0.5853},
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
# RECALIBRATED 2026-09-13 (full-audit root-cause pass, same investigation
# as ENTRY_SIZING_USD's matching note above): the hedge-rate secular-
# decline finding (hedge-rate-secular-decline.md, iter 143) already knew
# his overall hedge propensity had dropped a lot over the TWAP era, but
# every cell below was still measuring a blend that pre-dates the
# 13.6-day halt. Re-derived from ONLY post-halt data (since 2026-09-06
# 14:21 UTC, now ~8 days): live table was off by up to 32pp in the worst
# cell (Solana) against a 3-day snapshot taken during this audit -- this
# is the proper full-post-halt-window replacement, not just a snapshot.
# HIGH cells stay THIN (n=69-80) for all three assets -- first-pass
# estimate, revisit once more post-halt volume accumulates.
#
# RE-VERIFIED 2026-09-13 (/loop cycle 25, same day as ENTRY_SIZING_USD's
# and REENTRY_FATIGUE's own boundary+index fixes): unlike those two
# tables, this one has no position-index dependency (a simple per-market
# rate keyed only on first-entry regime), so it isn't exposed to the
# same real_fill_count bug class -- only the HALT_END boundary bug
# (1788870060 vs the correct 1788704494, see cycle 18) applied here.
# Re-derived with the corrected boundary: BTC/ETH/SOL's well-powered
# cells (n=318-1000) all land within ~1-10% of what's already deployed
# -- CONFIRMS cycle 18's original "no qualitative reversal expected"
# assumption for this table specifically, unlike the deeper problems
# found in ENTRY_SIZING_USD/REENTRY_FATIGUE. Updated the cells that
# clear n>=200 anyway, since the drift, while modest, is real:
#   Bitcoin:  CHEAP 0.6936->0.7193 (n=456), MID 0.7319->0.7390 (n=1000),
#             CORE 0.4506->0.4937 (n=318)
#   Ethereum: CHEAP 0.3838->0.4127 (n=790), MID 0.5751->0.5730 (n=651)
#   Solana:   CHEAP 0.5178->0.5331 (n=756), MID 0.6626->0.6612 (n=667)
# Bitcoin/HIGH, Ethereum/CORE+HIGH, Solana/CORE+HIGH stay UNCHANGED --
# fresh samples (n=80-191) don't clear the trust bar (Solana/HIGH
# looked like a +27% swing at n=113, exactly the kind of thin-sample
# noise this bar exists to guard against).
HEDGE_TRIGGER_PROBABILITY = {
    "Bitcoin":     {"CHEAP": 0.7193, "MID": 0.7390, "CORE": 0.4937, "HIGH": 0.2754},
    "Ethereum":    {"CHEAP": 0.4127, "MID": 0.5730, "CORE": 0.4460, "HIGH": 0.2179},
    "Solana":      {"CHEAP": 0.5331, "MID": 0.6612, "CORE": 0.3967, "HIGH": 0.1250},
    "Dogecoin":    {"CHEAP": 0.3515, "MID": 0.4531, "CORE": 0.6209, "HIGH": 0.3041},
    "Hyperliquid": {"CHEAP": 0.3506, "MID": 0.5819, "CORE": 0.5628, "HIGH": 0.3079},
    "BNB":         {"CHEAP": 0.8389, "MID": 0.9004, "CORE": 0.7806, "HIGH": 0.4884},
}

# REMOVED 2026-09-24 (single-function hedge consolidation -- see
# hedge-consolidation-single-function in project memory): hedge_trigger_
# probability (dead, unused), hedge_liquidity_multiplier (table was
# already empty -- effect vanished post-halt), hedge_size_ratio/
# HEDGE_SIZE_RATIO (replaced entirely by hedge_notional_for_price, the
# validated price+asset formula), adverse_move_size_multiplier,
# absolute_price_hedge_size_multiplier, and weekend_hedge_multiplier were
# all deleted here. Every one is superseded by the two consolidated
# functions this file now exposes for hedging: hedge_probability
# (trigger) and hedge_notional_for_price (size). See those functions'
# own docstrings for what real findings survived the consolidation and
# which were cut for simplicity. (The HEDGE_SIZE_RATIO table itself and
# its _DEFAULT_HEDGE_SIZE_RATIO fallback were accidentally left behind by
# this cleanup, fully orphaned -- caught and actually removed 2026-09-29,
# during a repo-wide zero-call-site dead-code sweep. Its full
# recalibration history, back to 2026-09-08, is preserved in git history
# at that commit if ever needed again.)


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
# REGIME POST-SHIFT HEDGE DAMPENER. Added 2026-09-19 (CHEAP only), in
# direct response to a live PnL investigation: the bot's paper P&L
# cratered in a ~3.4h window specifically because hedges accounted for
# 76% of its CHEAP-band loss (-$233 of -$305) -- driven by the bot
# dual-siding ~6x more often than the real trader currently is (61% vs
# 10.5%, same window). See hedge-rate-secular-decline.md for the full
# derivation: his overall dual-siding rate crashed at a sharp, discrete
# boundary (~2026-09-18 03:00 UTC), holding cleanly for 33+ hours and
# TIGHTENING (not reverting) on every re-check since. HEDGE_TRIGGER_
# PROBABILITY above is fit on the old, pre-shift regime (778k-trade
# historical dataset) and is now stale for several regime cells.
#
# EXTENDED same day to CORE/HIGH: re-measured "does this first-entry
# market EVER go dual-sided" (the exact same definition HEDGE_TRIGGER_
# PROBABILITY itself uses) post-boundary only, per (regime, asset) --
# found the same signature (character unchanged, pure frequency drop)
# in CORE and HIGH too, just not always well-powered enough to trust:
#
#   CHEAP    Bitcoin  n=138 rate=10.87% vs shipped 71.93% -> ratio 0.151
#   CHEAP    Ethereum n=125 rate= 9.60% vs shipped 41.27% -> ratio 0.233
#   CHEAP    Solana   n= 97 rate=12.37% vs shipped 53.31% -> ratio 0.232
#   CORE     Bitcoin  n= 30 rate=13.33% vs shipped 49.37% -> ratio 0.270
#   CORE     Ethereum n=  8 rate=12.50% vs shipped 44.60% -- TOO THIN, not calibrated (left at 1.0)
#   CORE     Solana   n=  8 rate= 0.00% vs shipped 39.67% -- TOO THIN, not calibrated (left at 1.0)
#   HIGH     Bitcoin  n= 48 rate=10.42% vs shipped 27.54% -> ratio 0.378
#   HIGH     Ethereum n= 53 rate= 5.66% vs shipped 21.79% -> ratio 0.260
#   HIGH     Solana   n= 56 rate=14.29% vs shipped 12.50% -- NO drop (ratio 1.14, left at 1.0)
#
# Two real asymmetries preserved rather than smoothed over: (1) Solana's
# HIGH-band hedge rate did NOT drop at all (post-shift rate is actually
# slightly ABOVE the shipped baseline) -- applying a dampener there
# would be actively wrong, not just imprecise; (2) Ethereum/Solana CORE
# have only n=8 post-shift observations each, an order of magnitude
# thinner than every other calibrated cell in this file -- a single
# additional or missing dual-sided market would swing the ratio by
# ~12pp, so these are left uncalibrated (1.0 no-op) rather than guessed
# at.
#
# MID ADDED same day, RETRACTING an earlier same-day claim that "MID's
# shift is a character change, not a frequency change, so a flat
# multiplier is the wrong shape of fix". That claim was measured on the
# wrong population: ALL hedge events (including HEDGE_CONTINUATION_
# PROBABILITY's separate 2nd+ hedges), classified by the HEDGE SIDE's
# own regime -- a real, valid characterization of "what a MID hedge
# looks like when it fires" (and it does still show a real favorable-
# to-defensive composition shift at that level, unexplained, possibly
# living in the untouched continuation mechanism instead), but NOT the
# population HEDGE_TRIGGER_PROBABILITY/hedge_attempt_hazard actually
# governs. Redone with the CORRECT scope (first-entry regime, FIRST
# hedge only, "ever went dual-sided" -- identical methodology to every
# other cell in this table): MID's favorable-first-hedge rate (ratio
# 0.419) and adverse-first-hedge rate (ratio 0.516) dropped by similar
# magnitudes, not divergent ones -- MID's first-hedge TRIGGER fits the
# same simple frequency-drop pattern as everything else:
#
#   MID      Bitcoin  n= 41 rate=34.15% vs shipped 73.90% -> ratio 0.462
#   MID      Ethereum n= 15 rate=20.00% vs shipped 57.30% -> ratio 0.349 (thin)
#   MID      Solana   n= 13 rate=23.08% vs shipped 66.12% -> ratio 0.349 (thin)
#
# Ethereum/Solana MID are thinner than every other calibrated cell
# except CORE/Bitcoin -- direction is unambiguous and consistent with
# Bitcoin's own well-powered result, but treat these two ratios as the
# least-certain calibrated values in this table.
#
# HONEST CAVEAT (applies to every calibrated cell above): the numbers in
# this comment block are the ORIGINAL 2026-09-19 calibration, based on
# only ~33 hours of post-shift data -- SIGN and MAGNITUDE were never in
# doubt, but the exact ratios were flagged as needing a re-derivation
# once more days accumulated, and the uncalibrated cells (CORE
# Ethereum/Solana; Solana HIGH, which looked unshifted at n=56) were
# flagged to revisit then too.
#
# REFRESHED 2026-09-22, in direct response to the live fidelity
# scorecard showing all three active bots dual-siding at 20-32% vs the
# real trader's CURRENT 8.9-10.2% (same live window) -- i.e. the
# 2026-09-19 table was stale, not wrong in direction. Recalibrated
# against the FULL post-boundary window (2026-09-18 03:00 UTC ->
# 2026-09-22, ~4.5 days, every cell now n>=28) instead of the original
# ~33 hours, using the exact same "does this first-entry market EVER go
# dual-sided" definition:
#
#   CHEAP  Bitcoin  n=372 rate=16.67% vs pre-shift 60.70% -> ratio 0.275 (was 0.151 -- UNDER-dampened before)
#   CHEAP  Ethereum n=358 rate=14.25% vs pre-shift 36.21% -> ratio 0.393 (was 0.233 -- under-dampened)
#   CHEAP  Solana   n=291 rate=12.37% vs pre-shift 45.80% -> ratio 0.270 (was 0.232 -- close)
#   CORE   Bitcoin  n=142 rate=16.20% vs pre-shift 43.79% -> ratio 0.370 (was 0.270 -- under-dampened)
#   CORE   Ethereum n= 43 rate= 4.65% vs pre-shift 38.03% -> ratio 0.122 (was 1.0, UNCALIBRATED -- now enough
#                                                                          data to trust: 8x over-hedging before)
#   CORE   Solana   n= 28 rate= 7.14% vs pre-shift 33.33% -> ratio 0.214 (was 1.0, UNCALIBRATED -- same story)
#   HIGH   Bitcoin  n=258 rate= 7.36% vs pre-shift 25.24% -> ratio 0.292 (was 0.378 -- over-dampened, slightly)
#   HIGH   Ethereum n=216 rate= 7.87% vs pre-shift 17.30% -> ratio 0.455 (was 0.260 -- under-dampened)
#   HIGH   Solana   n=206 rate= 7.77% vs pre-shift 16.06% -> ratio 0.484 (was 1.0, UNCALIBRATED -- the earlier
#                                                                          "no drop" read (n=56, ratio 1.14) did
#                                                                          not hold up with 4x the data; it DOES
#                                                                          drop, just slower than other cells)
#   MID    Bitcoin  n=127 rate=22.05% vs pre-shift 68.92% -> ratio 0.320 (was 0.462 -- over-dampened)
#   MID    Ethereum n= 38 rate=10.53% vs pre-shift 55.18% -> ratio 0.191 (was 0.349 -- over-dampened)
#   MID    Solana   n= 39 rate=10.26% vs pre-shift 58.14% -> ratio 0.176 (was 0.349 -- over-dampened)
#
# Genuinely mixed picture, not a uniform "dampen harder" fix: 5 cells
# needed MORE dampening (both previously-uncalibrated CORE cells, the
# previously-uncalibrated Solana HIGH cell, and both previously-
# calibrated MID-Ethereum/Solana cells), while 5 cells actually needed
# LESS dampening than the stale 09-19 table applied (both CHEAP-
# Bitcoin/Ethereum, CORE-Bitcoin, HIGH-Ethereum, MID-Bitcoin). This is
# exactly why the aggregate live dual-sided rate stayed too high even
# though some cells were already over-dampened -- the under-dampened
# cells (especially the two previously-uncalibrated CORE cells running
# at full pre-shift rate) dominated the mismatch. Re-derive again once
# more post-boundary days accumulate; every cell here is still built on
# under a week of data by construction (the boundary itself is only
# ~4.5 days old as of this refresh).
# RECALIBRATED 2026-09-24 (CHEAP/Bitcoin only): live-bot audit found paperbot/
# paperbot-100/coinbase-bot all running CHEAP Bitcoin hedge rates of 56.7-75.4%
# against this cell's own dampened base -- while the real trader's CHEAP
# Bitcoin hedge rate (measured fresh via data-api, n=470 across 6 days
# Sep18-24 including live same-day data, split-half validated at 18.8%/14.5%)
# sits at a stable ~16.8%. That's a real, cross-bot-confirmed ~3.68x
# over-hedge concentrated almost entirely in this one cell (Ethereum/Solana
# CHEAP were only 1.4-1.5x off, within this project's normal calibration
# noise, and left untouched). See cheap-hedge-gap-is-bitcoin-specific-root-
# cause / cheap-hedge-rate-gap-verified-compositional in project memory for
# the full derivation. 0.275 -> 0.075 (a further ~3.7x reduction on top of
# the existing dampener). CODED AND TESTED, NOT YET DEPLOYED to the live
# bots pending explicit go-ahead -- same discipline as every other numeric
# recalibration this session.
REGIME_HEDGE_POST_SHIFT_MULTIPLIER = {
    "CHEAP": {"Bitcoin": 0.075, "Ethereum": 0.393, "Solana": 0.270},
    "MID": {"Bitcoin": 0.320, "Ethereum": 0.191, "Solana": 0.176},
    "CORE": {"Bitcoin": 0.370, "Ethereum": 0.122, "Solana": 0.214},
    "HIGH": {"Bitcoin": 0.292, "Ethereum": 0.455, "Solana": 0.484},
}


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
#
# ATTEMPTED POST-HALT RECHECK 2026-09-13 (/loop cycle 14) -- NOT
# RECALIBRATED, result not trustworthy enough to act on. This table was
# never independently re-checked earlier in the post-halt audit (a real
# gap -- flagged only as "deferred, lower priority" pre-loop, never
# revisited even after its 3 siblings -- ADVERSE_MOVE_SIZE_MULTIPLIER,
# ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER, CROSS_MARKET_HEDGE_RATE_
# MULTIPLIER -- were all fixed in cycles 3 and 5).
#
# The original's attempt_index/TTC-stratified hazard framework can't be
# faithfully reproduced from raw trade data in one cycle (attempt_index
# isn't a directly observable field -- it has to be inferred from the
# sequence of same-side fills before any hedge exists). A simplified
# proxy was tried: per market, walk primary-side fills strictly before
# the first hedge fill (or all of them if never hedged), label only the
# LAST such fill "hedged=1" (an event) and every earlier one "hedged=0"
# (survived to the next attempt) -- a reasonable discrete-hazard
# reduction in principle. Result was a clean U-shape for all 3 assets
# (e.g. Bitcoin bucket-multipliers 1.19 -> 0.69 -> 0.82 -> 1.26), NOT
# the clean monotonic rise the original found and rigorously confound-
# checked. Suspected cause, not confirmed: this proxy's "last fill
# before hedge" selection can attribute a hedge to a near-zero-adverse-
# move fill whenever the real trigger was something else entirely
# (e.g. the base hazard curve simply rising with real_fill_count) --
# exactly the kind of artifact the original's attempt_index/TTC
# stratification was built to rule out, and this simplified version
# doesn't. Rather than ship a possibly-artifactual U-shape in place of
# a well-validated monotonic one, LEFT UNCHANGED. Properly rechecking
# this needs the original's full stratified framework, not a single
# quick proxy pass -- flagged as the next candidate if a future cycle
# has room for that heavier lift.
# RECHECKED AND PARTIALLY RECALIBRATED 2026-09-13 (/loop cycle 34), fixing
# cycle 14's diagnosed proxy flaw. Cycle 14's attempt used the LAST
# primary-side fill's own price as a stand-in for "the price at the
# moment a hedge decision would be made", which produced a spurious
# U-shape -- suspected root cause: that fill can be stale by the time the
# actual next fill (hedge or not) happens, misattributing the adverse
# move. Fixed by using price_history_dense.jsonl (10s-resolution live
# CLOB book, unavailable at cycle 14's time) to look up the hedge side's
# OWN price at each primary fill's exact timestamp, independent of what
# he did next -- giving a genuine "what was the adverse move at this
# moment" value for every attempt, not just the ones immediately
# preceding a hedge.
#
# Result: Bitcoin (n=4773, well-powered) reproduces the SAME monotonic-
# rise shape as the original table (fresh ratios 0.89/0.87/1.21/1.33 vs
# deployed 0.61/0.79/1.08/1.52) -- confirms the underlying effect is
# real, though more compressed/moderate than the original full-history
# measurement (consistent with this whole project's repeated finding
# that his behavior has flattened over time on several other tables).
# RECALIBRATED to the fresh values. Solana (n=488) shows the same
# monotonic shape directionally but its top bucket is thin (n=16) --
# LEFT AT THE DEPLOYED VALUES, not recalibrated, pending more data.
# Ethereum (n=893) is NOT monotonic in the fresh check (a dip at the
# 3rd bucket, n=168) -- also left unchanged; can't distinguish real
# non-monotonicity from bucket-level noise at this n yet.
ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER = {
    "Bitcoin": {0.0: 0.892, 0.07: 0.874, 0.15: 1.207, 0.30: 1.327},
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
# FAVORABLE-MOVE hedge trigger multiplier. Added 2026-09-19: the mirror
# image of ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER above, covering
# adverse_move < 0 (a move IN the primary position's favor) in CHEAP
# specifically -- the exact case that table's own docstring flagged as
# "a real but DIFFERENT, non-monotonic (U-shaped) pattern" and
# deliberately left at a 1.0 no-op, suspecting a SCOUT-bleed
# classification confound (an opportunistic "harvest the now-cheap
# confirmed loser" bet miscounted as a hedge).
#
# Derivation: a Gaussian-Process IRL fit of the hedge-trigger reward
# surface (this project's first fully data-driven, non-parametric-shape
# analysis of this decision -- no threshold or bucket structure assumed
# up front, unlike every other table in this file) found the U-shape is
# real, not the suspected artifact. Checked directly and thoroughly
# before building:
#   - SCOUT-bleed confound tested and REJECTED: hedges at deep-favorable
#     adverse_move (<-0.5) have median size 89% of the position being
#     protected (n=183) -- a properly-scaled real hedge, not a tiny
#     floor-lot/lottery-ticket bet (only 13.1% were "tiny", ratio<0.15).
#   - Time-since-entry confound tested and controlled: adverse_move
#     magnitude correlates with time-since-entry (0.25-0.59 across
#     regimes, mechanically sensible -- bigger moves take longer to
#     develop), so the shape was re-fit on ONLY the early (0-120s)
#     time-since-entry stratum, where it survives cleanly (this same
#     check, done for CORE/HIGH, found THEIR apparent shapes were pure
#     time-confound artifacts and are NOT modeled here as a result).
#   - Bootstrap-confirmed (500 resamples): both tails (adverse_move=-1.0
#     and the adverse side already modeled above) exceed the near-zero
#     valley in 98.6-100% of resamples.
#   - Calendar split-half (first half of the observed week vs second):
#     the valley-at-zero, elevated-at-both-extremes shape holds in BOTH
#     halves; only the exact magnitude/asymmetry drifts, fully
#     explained by the already-documented ~2026-09-18 hedge-rate
#     collapse (see REGIME_HEDGE_POST_SHIFT_MULTIPLIER), applied
#     upstream of this multiplier so it isn't double-counted here.
#   - Per-asset checked (the original U-shape observation that led to
#     this table's ancestor being left unmodeled was never itself
#     re-examined per-asset): all three assets show the same qualitative
#     bathtub, magnitudes differ (Ethereum's deep-favorable tail is the
#     most extreme), so calibrated per-asset like its sibling table.
#
# Same "RATE-based mean-neutral multiplier on hedge_attempt_hazard's
# output" construction as ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER: each
# breakpoint's value is that adverse_move bin's real favorable-side
# hedge rate divided by the OVERALL favorable-side hedge rate for that
# asset (so the multiplier reshapes the distribution across adverse_move
# without changing the overall favorable-side hedge rate on average).
# CHEAP only -- MID showed a real hump for Bitcoin/Ethereum but Solana's
# shape looked closer to monotonic within the same tested range (peak
# possibly at or beyond -1.0, not clearly interior) -- left unmodeled
# pending a asset-specific functional form, same discipline as this
# file's other thin/inconsistent-per-asset exceptions.
FAVORABLE_MOVE_HEDGE_TRIGGER_MULTIPLIER = {
    "Bitcoin": {-1.0: 1.644, -0.5: 1.449, -0.2: 1.051, -0.05: 0.789},
    "Ethereum": {-1.0: 3.629, -0.5: 1.253, -0.2: 0.888, -0.05: 0.767},
    "Solana": {-1.0: 1.632, -0.5: 1.665, -0.2: 0.757, -0.05: 0.905},
}
_FAVORABLE_MOVE_HEDGE_TRIGGER_MULTIPLIER_CAP = 4.0  # measured range tops
# out at 3.629 (Ethereum's deep-favorable bin, n=83) -- comfortably
# inside, same "cap above what's measured" discipline as the sibling
# table's own 3.0 cap.


def favorable_move_hedge_trigger_multiplier(asset: str, adverse_move: Optional[float]) -> float:
    """Continuous, mean-neutral multiplier on hedge_attempt_hazard's
    output as a function of how far price has moved IN the primary
    position's favor since entry -- the mirror image of
    adverse_move_hedge_trigger_multiplier. 1.0 (no-op) for any asset not
    in FAVORABLE_MOVE_HEDGE_TRIGGER_MULTIPLIER, when adverse_move is
    unavailable, OR when adverse_move >= 0 (that side is
    adverse_move_hedge_trigger_multiplier's job, not this one's).
    CHEAP-only by construction -- callers are responsible for scoping
    this to CHEAP the same way they already scope the adverse-move
    sibling to whichever regimes it's calibrated for. Flat beyond the
    measured range, clamped to
    _FAVORABLE_MOVE_HEDGE_TRIGGER_MULTIPLIER_CAP, same discipline as
    every other multiplier in this file."""
    if adverse_move is None or adverse_move >= 0:
        return 1.0
    curve = FAVORABLE_MOVE_HEDGE_TRIGGER_MULTIPLIER.get(asset)
    if curve is None:
        return 1.0
    points = sorted(curve.items())
    lo_move, lo_val = points[0]
    hi_move, hi_val = points[-1]
    move = max(lo_move, min(hi_move, adverse_move))
    result = lo_val
    for i in range(len(points) - 1):
        p_move, p_val = points[i]
        q_move, q_val = points[i + 1]
        if p_move <= move <= q_move:
            frac = (move - p_move) / (q_move - p_move) if q_move > p_move else 0.0
            result = p_val + frac * (q_val - p_val)
            break
    cap = _FAVORABLE_MOVE_HEDGE_TRIGGER_MULTIPLIER_CAP
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


def hedge_continuation_probability(hedge_count: int) -> float:
    """hedge_count is how many hedge-shaped entries this market already
    has (only called with hedge_count >= 1 -- hedge_count == 0 goes
    through hedge_attempt_hazard above instead, unchanged)."""
    return HEDGE_CONTINUATION_PROBABILITY.get(hedge_count, _DEFAULT_HEDGE_CONTINUATION_PROBABILITY)


# ---------------------------------------------------------------------------
# SINGLE CONSOLIDATED HEDGE-TRIGGER FUNCTION. Added 2026-09-24, replacing a
# 13-factor multiplicative stack (liquidity, weekend, cross-market, first-
# entry conviction, after-a-big-loss, MID/CHEAP favorable-move humps, an
# adverse-move x time-remaining interaction, and a late-window hedge-
# urgency hump -- see git history around commit 377f8ac and earlier for
# what those looked like) with the four drivers that carried essentially
# all of the real, well-validated signal:
#
#   1. hedge_attempt_hazard(asset, regime, attempt_index) -- the base
#      per-tick hazard, timed to reproduce the calibrated overall
#      HEDGE_TRIGGER_PROBABILITY frequency with the real attempt-index
#      timing shape (median real hedge lands at attempt_index 4, not 1).
#   2. regime_hedge_post_shift_multiplier(asset, regime) -- the single
#      most impactful, most recently validated calibration in this whole
#      chain: the real trader's hedge rate collapsed sharply at the
#      2026-09-18 boundary in most (asset, regime) cells, and this
#      dampener is what keeps the bot from massively over-hedging
#      relative to his CURRENT behavior. Not safe to drop.
#   3. The adverse/favorable-move shape (via adverse_move_hedge_trigger_
#      multiplier for adverse_move>=0 and favorable_move_hedge_trigger_
#      multiplier for adverse_move<0, CHEAP only) -- the strongest single
#      behavioral driver found in this whole project: a real, monotonic,
#      confound-checked-every-way-possible relationship between how far
#      price has moved and whether he hedges, PLUS (in CHEAP specifically)
#      a genuine bathtub shape showing he also hedges when he's already
#      winning big (profit-locking insurance, not just defense).
#   4. hedge_continuation_probability(hedge_count) for the 2nd+ hedge,
#      once a market is already dual-sided -- structurally necessary for
#      realistic multi-hedge behavior (60%+ of dual-sided markets get a
#      2nd hedge; a single-shot design was tested and found to badly
#      undercount real multi-hedge markets).
#
# CUT for simplicity (real findings, not discredited ones -- each is
# individually well-supported, but none carried anywhere near the
# explanatory weight of the four kept above, and stacking 13 independent
# probability multipliers onto one coin-flip made the whole chain
# impossible to reason about or audit as a unit):
#   - hedge_liquidity_multiplier, cross_market_hedge_rate_multiplier,
#     hedge_trigger_after_big_loss_multiplier -- all three had ALREADY
#     independently decayed to an empty, no-op calibration table by the
#     time of this consolidation (liquidity/cross-market effects
#     vanished post-halt; after-a-big-loss never cleared post-halt
#     re-verification) -- removing them is exactly zero fidelity loss.
#   - weekend_hedge_multiplier -- real but modest (z=9.9, Bitcoin-only).
#   - conviction_hedge_multiplier -- only one (asset, regime) cell
#     survived post-halt recalibration (Solana/MID); the other eleven
#     had already been retracted to no-ops.
#   - mid_favorable_hedge_trigger_multiplier -- real but Solana-only
#     (Bitcoin null, Ethereum an unshipped different shape).
#   - adverse_move_ttc_interaction_multiplier -- a real refinement ON TOP
#     of the adverse-move shape (more time remaining = more sensitive to
#     a given adverse move), but scoped to Bitcoin/Solana x CHEAP/MID/
#     CORE only, and a second-order effect on top of an already-modeled
#     first-order one.
#   - late_window_hedge_urgency_multiplier -- the one genuine sacrifice
#     here: a real, cross-asset-validated hazard hump 150-180s after an
#     early first entry, shipped (commit 377f8ac) mere hours before this
#     consolidation. Cut anyway for the sake of one clean function; see
#     the calibration's own comment (removed just above) for how to
#     bring it back if hedge-TIMING fidelity is ever prioritized again.
# CLOSE-URGENCY boost (2026-09-25, research-loop capstone finding): as a
# market's 5-minute window approaches its close, ALL THREE of his
# decision types (first entry, same-side add-on, opposite-side hedge)
# get sharply more likely to fire -- a single "last chance before the
# outcome locks in" effect, not three separate mechanisms. Confirmed
# split-half stable (the climbing-then-crashing hazard shape reproduces
# independently in both halves of the covered trade history). Distinct
# from and complementary to TTC_SIZE_MULTIPLIER, which governs how BIG
# an entry is (already captures the same regime-dependent direction --
# CHEAP shrinks, MID/HIGH grow, late-window) but is clamped flat below
# 90s remaining; this instead governs how LIKELY an entry/hedge happens
# at all, specifically extending into the final ~90s that
# TTC_SIZE_MULTIPLIER doesn't reach (our own MIN_SECONDS_BEFORE_CLOSE=75
# is the practical floor).
#
# BOOST-ONLY construction (ratio to the 165s-remaining hazard specifically,
# NOT a mean-neutral redistribution) -- deliberately 1.0 (clean no-op) at
# seconds_remaining>=165, same "only ever a boost or a no-op" discipline
# already established for mid_boundary_edge_multiplier/
# cheap_deep_touch_edge_multiplier. A mean-neutral version was tried first
# and rejected: it made the "baseline" (most of an ordinary window) come
# out to ~0.78-0.84 instead of 1.0, silently dampening participation
# everywhere, not just boosting it near close -- exactly the kind of
# unintended interaction with REGIME_ENGAGEMENT_MULTIPLIER's own
# calibration this needed to avoid. Derived from real post-Aug-7 trader
# data:
#   ENTRY (first-entry hazard, pooled across regimes -- checked and
#   confirmed regime-AGNOSTIC: the activity boost itself is uniform
#   across CHEAP/MID/CORE/HIGH; the win-rate CONSEQUENCE differs by
#   regime, but that's a downstream fact of real resolution outcomes,
#   not something this multiplier needs to encode):
#     165s remaining: hazard 15.74% | 135s: 16.85% | 105s: 21.05% | 75s: 27.00%
#   HEDGE (opposite-side flip hazard, steeper climb than entries):
#     165s remaining: hazard 5.66%  | 135s: 6.83%  | 105s: 10.84% | 75s: 14.93%
# RECALIBRATED 2026-09-26 (full-codebase "verify everything against the
# trader" audit, extended to a proper discrete-time hazard/survival check
# instead of a raw entry-count density comparison). Original tables above
# were built by density alone; re-derived here with a real "at risk"
# denominator per 30s bucket (a market only counts toward a bucket's
# denominator if the event -- first entry, or first hedge -- hasn't
# ALREADY happened before that bucket started), over 815 true-on-chain
# markets across BTC/ETH/SOL, ~434 windows/asset:
#   ENTRY, pooled hazard ratio vs the 165s bucket: 135=1.062, 105=1.626,
#     75=2.091 -- same climbing shape as shipped, but the tail (105/75)
#     runs meaningfully steeper than the old table's 1.337/1.715. Stable
#     in direction across a temporal split-half (75-bucket ratio 2.156
#     first half / 2.034 second half; 105-bucket 1.369/1.855 -- both
#     halves clearly >1). The 135 bucket alone is unstable (1.243 vs
#     0.894 split-half) and left close to its prior value.
#   HEDGE, pooled hazard ratio vs the 165s bucket: 135=1.751, 105=3.873,
#     75=6.086 -- a MUCH steeper real climb than the shipped table
#     captures (was 1.207/1.915/2.638, roughly half the real magnitude
#     at the tail). Monotonically climbing in BOTH halves independently
#     (135/105/75 ratios: 2.026/4.784/6.648 first half, 1.522/3.114/5.620
#     second half) -- not a fluke, just previously under-calibrated.
# Standard 80%-of-log-gap shrinkage applied (new = shipped * (measured/
# shipped)**0.8), since this is a single fresh-data pass without the
# extra split-half-fit rigor this file reserves for a direct-ship.
ENTRY_URGENCY_MULTIPLIER = {165: 1.000, 135: 1.064, 105: 1.564, 75: 2.010}
HEDGE_URGENCY_MULTIPLIER = {165: 1.000, 135: 1.625, 105: 3.364, 75: 5.149}
_URGENCY_MULTIPLIER_MIDPOINTS = (165, 135, 105, 75)


def _interpolate_urgency(table: dict, seconds_remaining: float) -> float:
    sr = max(75.0, min(165.0, seconds_remaining))
    mids = _URGENCY_MULTIPLIER_MIDPOINTS
    for i in range(len(mids) - 1):
        hi_mid, lo_mid = mids[i], mids[i + 1]
        if lo_mid <= sr <= hi_mid:
            hi_val, lo_val = table[hi_mid], table[lo_mid]
            frac = (sr - lo_mid) / (hi_mid - lo_mid)
            return lo_val + frac * (hi_val - lo_val)
    return table[mids[0] if sr >= 165 else mids[-1]]


_URGENCY_TABLES = {"entry": ENTRY_URGENCY_MULTIPLIER, "hedge": HEDGE_URGENCY_MULTIPLIER}


def urgency_multiplier(kind: str, seconds_remaining: Optional[float]) -> float:
    """MERGED 2026-09-28 (was entry_urgency_multiplier + hedge_urgency_
    multiplier -- already identical shape, sharing this same
    _interpolate_urgency helper, differing only by which table). `kind`
    is "entry" or "hedge".

    Boost-only on ordinary-entry eligibility (kind="entry") or hedge
    trigger probability (kind="hedge") as the window approaches close:
    exactly 1.0 (clean no-op) at seconds_remaining>=165 (and for None --
    no seconds_remaining known), climbing multiplicatively above 1.0 as
    remaining time shrinks toward 75s. Flat outside [75, 165]s remaining,
    matching TTC_SIZE_MULTIPLIER's own no-extrapolation discipline.

    For kind="entry": applied to REGIME_ENGAGEMENT_MULTIPLIER's own
    result, same composition point as PARTICIPATION_ASSET_SCALE/
    REGIME_DRIFT_CORRECTION. Note the caller composes this under a
    min(1.0, ...) clamp same as the other overlays there -- correct
    here since engagement_mult is a skip-probability gate (ceiling is
    100% chance of not skipping), not a size multiplier; the actual
    magnitude boost for entries lives in TTC_SIZE_MULTIPLIER instead.
    For kind="hedge": calibrated separately on the real hedge-flip
    hazard curve (steeper climb than ordinary entries)."""
    if seconds_remaining is None:
        return 1.0
    return _interpolate_urgency(_URGENCY_TABLES[kind], seconds_remaining)


def hedge_probability(asset: str, regime: str, attempt_index: int, hedge_count: int,
                       adverse_move: Optional[float] = None,
                       seconds_remaining: Optional[float] = None) -> float:
    """The single entry point for 'what's the probability THIS hedge
    attempt fires', for both the first hedge (hedge_count == 0, uses
    attempt_index = activity.real_fill_count) and every continuation
    hedge after it (hedge_count >= 1, attempt_index ignored). Always
    returns a value in [0, 1] -- the caller (decide_hedge) can roll
    against it directly with no further clamping needed.

    seconds_remaining (added 2026-09-25) feeds hedge_urgency_multiplier
    -- see its docstring and the CLOSE-URGENCY comment above. Optional
    (default None -> no-op 1.0) so existing callers/tests keep working
    unchanged. Applied unconditionally, no feature flag, matching this
    function's own established design (validated findings are folded
    in directly, not gated -- see the 2026-09-24 hedge consolidation).

    (streak_length was accepted here 2026-09-25 -> 2026-09-26, removed
    entirely once STREAK_HEDGE_PROBABILITY_MULTIPLIER's on-chain re-check
    reversed the finding it fed -- see git history if reviving a
    streak-conditioned hedge signal is ever warranted.)"""
    urgency = urgency_multiplier("hedge", seconds_remaining)
    if hedge_count >= 1:
        return max(0.0, min(1.0, hedge_continuation_probability(hedge_count) * urgency))
    p = hedge_attempt_hazard(asset, regime, attempt_index)
    p *= table_lookup(REGIME_HEDGE_POST_SHIFT_MULTIPLIER, regime, asset)
    if adverse_move is not None:
        if adverse_move >= 0:
            p *= adverse_move_hedge_trigger_multiplier(asset, adverse_move)
        elif regime == "CHEAP":
            p *= favorable_move_hedge_trigger_multiplier(asset, adverse_move)
    p *= urgency
    return max(0.0, min(1.0, p))


# REMOVED 2026-09-24 (single-function hedge consolidation): HEDGE_
# CONTINUATION_SIZE_RATIO/hedge_continuation_size_ratio and ADVERSE_MOVE_
# CONTINUATION_SIZE_MULTIPLIER/adverse_move_continuation_size_multiplier
# -- both superseded by hedge_notional_for_price, which already covers
# first AND continuation hedges uniformly (see its own docstring).


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
# once-per-market binary measurement). Dogecoin/Hyperliquid/BNB default to
# 0.0 probability (no-op, unchanged first-entry behavior) since there's no
# live data to calibrate them from at all right now.
#
# RECALIBRATED 2026-09-13 (post-halt audit, same 13.6-day-halt root cause
# as the other 9 tables fixed this pass -- see full-behavioral-audit-
# tracker.md). Post-halt-only (ts>=HALT_END), same "eventually-non-
# dominant first entry" definition, collapsed decisions: n=1380/1283/1289
# markets -- comfortably above trust bar now, unlike the original 14-day
# pass. SCOUT_PROBABILITY: 0.390->0.3326 (BTC), 0.285->0.2525 (ETH),
# 0.376->0.3072 (SOL) -- a real but modest ~10-18% relative decline for
# all three, not a collapse like the other 9 tables. SCOUT_SIZE_RATIO:
# same post-halt window, same median(first_entry_usdc / regime's "first"-
# tier post-halt median) definition, n=459/324/396 scout-case markets:
# 0.567->0.9721 (BTC), 0.672->0.9392 (ETH), 0.185->0.6697 (SOL) -- THIS one
# is the dramatic post-halt shift: scouted first entries used to be sized
# at a fraction of a normal first entry (as low as a fifth for Solana);
# post-halt they're sized nearly the SAME as a normal first entry for all
# three assets. The "tentative, small probe" behavior this multiplier was
# built to replicate has almost entirely disappeared -- he still picks the
# eventually-wrong side about a third of the time (SCOUT_PROBABILITY
# above), but no longer discounts its size much when he does.
# RECALIBRATED 2026-09-23 (post-Sep-18-boundary audit): both cells above
# were last touched 2026-09-13, before the Sep-18 shift this project has
# repeatedly found invalidates prior calibrations. Direct on-chain-
# verified re-derivation (268 post-boundary markets sampled, all 268
# matched via eth_getTransactionReceipt against the exact off-chain
# price/size -- a 100% verification rate, so the underlying off-chain
# data itself checks out clean here): dominant side = higher total $ cost
# across the market; scout = first entry lands on the OTHER side.
#
# SCOUT_PROBABILITY roughly DOUBLED for every asset:
#   Bitcoin  n=134: 0.3326 -> 0.5075   Ethereum n=75: 0.2525 -> 0.5600
#   Solana   n=59:  0.3072 -> 0.5085
# Split-half checked on the larger off-chain population (n=59-134 total,
# both halves n=26-69): stable in direction and magnitude, e.g. Bitcoin
# 58.0%/43.1%, Ethereum 59.4%/53.5%, Solana 54.5%/46.2% -- both halves
# comfortably above the old shipped baseline for all three assets.
#
# SCOUT_SIZE_RATIO collapsed even more dramatically -- median(scout
# first-entry $) / median(non-scout first-entry $), same on-chain-
# verified sample:
#   Bitcoin  scout_n=68/dom_n=66: 0.9721 -> 0.3272
#   Ethereum scout_n=42/dom_n=33: 0.9392 -> 0.0825
#   Solana   scout_n=30/dom_n=29: 0.6697 -> 0.3133
# Definition differs slightly from the original ("first_entry_usdc /
# ENTRY_SIZING_USD's regime-specific 'first'-tier median", which pulls in
# a SEPARATE table's own possible staleness) -- this uses a self-
# contained scout-vs-non-scout ratio within the same fresh sample
# instead, deliberately avoiding compounding two independently-drifting
# tables' errors together. Shipped un-shrunk (no 80%-log-gap discount):
# the shift is enormous, consistent in direction across all three
# independently-measured assets, and on-chain verification confirms the
# underlying data quality -- shrinking back toward already-disproven
# values would be conservative for its own sake, not add real safety.
#
# NOT re-validated this pass: ACCURACY_SCOUT_MULTIPLIER (the mean-neutral
# correction composing on top of SCOUT_PROBABILITY below) -- same
# "re-derive corrections against the new base, don't just inherit them"
# risk this file has hit before (see DECISIVENESS_ONSET_MULTIPLIER x
# WITHIN_BAND_SIZE_SLOPE), flagged as a follow-up, not assumed fine.
SCOUT_PROBABILITY = {
    "Bitcoin":  0.5075,
    "Ethereum": 0.5600,
    "Solana":   0.5085,
}

SCOUT_SIZE_RATIO = {
    "Bitcoin":  0.3272,
    "Ethereum": 0.0825,
    "Solana":   0.3133,
}

# Only reachable if SCOUT_PROBABILITY somehow got a nonzero entry for an
# asset without a matching ratio -- shouldn't happen since both dicts are
# maintained together above, but a defined fallback beats a KeyError.
_DEFAULT_SCOUT_SIZE_RATIO = 0.5


def scout_params(asset: str) -> tuple[float, float]:
    """(probability, size_ratio) for this asset's scout mechanism --
    MERGED 2026-09-28 (was scout_probability + scout_size_ratio, always
    called together at both existing call sites: roll the probability,
    then apply the ratio only if it fires). 0.0 probability for an
    uncalibrated asset (no-op, scouting never triggers) with the size
    ratio still returned for interface consistency even though it can
    never be reached in that case."""
    return SCOUT_PROBABILITY.get(asset, 0.0), SCOUT_SIZE_RATIO.get(asset, _DEFAULT_SCOUT_SIZE_RATIO)


# ---------------------------------------------------------------------------
# RE-ENTRY FATIGUE. Added 2026-09-13 (/loop iter 130, generalized same day
# after the MID-only version had been live only minutes -- renamed before
# it settled in anywhere else): a market-OUTCOME dose-response finding, not
# a replicated-sizing finding like every multiplier above -- see the end of
# this comment for why that distinction matters. The more times he's
# already added to the SAME side within one market, the lower that side's
# eventual win rate -- confirmed in 3 of the 4 regime bands, each with its
# OWN shape and cliff point, which is why this is a per-(asset, regime)
# table rather than one flat curve. Real per-trade win rate by live
# position index (real_fill_count BEFORE the trade about to be placed),
# TWAP-era:
#
#   MID   (cliff at idx6+): Ethereum idx1-5 avg ~46.5% -> idx6+ 35.7%
#         (n=1,866-2,381 per bucket / 14,303); Solana idx1-5 avg ~43.0% ->
#         idx6+ 31.0% (n=2,206-2,770 / 19,907). Bitcoin's MID effect is
#         present but far weaker and fails the temporal-stability bar in
#         its second half (z=1.96) -- excluded.
#   CORE  (flat idx1-8, cliff at idx9+): Bitcoin 80.0% avg -> 70.2%
#         (n=672-956 per bucket / 5,404); Ethereum 81.6% avg -> 65.9%
#         (n=235-588 / 1,236). Solana's raw CORE gap does NOT survive
#         controlling for within-CORE price level (partial r -0.0132 ->
#         +0.0016, t=0.09) -- fully explained by idx9+ trades skewing to
#         lower/less-safe CORE prices, not a real fatigue effect. Excluded.
#   CHEAP (gradual decline + cliff at idx9+): Ethereum 19.9% avg -> 14.5%
#         (n=1,040-2,260 / 6,841); Solana 20.7% avg -> 16.0% (n=1,078-2,127
#         / 8,137). Bitcoin's CHEAP shape is NOT monotonic (rises from
#         20.6% at idx1 to 24.6% at idx8, only partially reverting to 22.0%
#         at idx9+) -- not a clean fatigue pattern, excluded.
#   HIGH: inconsistent sign and mostly too thin across all 3 assets --
#         excluded, no clear pattern to build on.
#
# Confound-checked per band before inclusion: MID against entry price
# drifting toward 0.50 (barely moves the partial correlation); CORE/CHEAP
# against the already-known within-band price gradient specifically
# (Solana CORE failed exactly this check and was excluded; the 4 included
# cells all survive it, in most cases getting slightly STRONGER after
# controlling for price, e.g. Bitcoin CORE partial r -0.297 -> -0.282,
# t=-32.4). Temporally stable both halves for every included cell (Ethereum
# MID z=7.55/8.77, Solana MID z=11.06/9.80, Bitcoin CORE z=8.98/10.07,
# Ethereum CORE z=10.75/5.15, Ethereum CHEAP z=9.54/4.20, Solana CHEAP
# z=10.65/2.80). Distinct from the retracted "size_vs_own_prior_trade" and
# "diminishing increments" threads (both were about SIZE, retracted for
# regime/total-trades-in-market composition reasons that don't apply here,
# since this is keyed on the LIVE index as the trade is placed, not a
# post-hoc final total only knowable after the market closed).
#
# UNLIKE every multiplier above, this isn't calibrated to replicate an
# observed real sizing pattern -- there's no evidence he actually sizes
# down at the cliff himself. This is a deliberate risk-management dampener
# for the paper bots, in the same spirit as MAX_CHEAP_REPRICES (config.py):
# both stop leaning further into a zone real outcomes show is reliably
# worse, rather than mimicking an unobserved real behavior. Multiplier
# values are each cell's measured post-cliff / pre-cliff-average win-rate
# ratio, i.e. scale the stake down roughly in proportion to the
# demonstrated drop in edge, not an arbitrarily chosen cut.
# RECALIBRATED 2026-09-13 (post-halt audit, /loop cycle 10). Since this
# table is calibrated on real MARKET-OUTCOME win rates rather than the
# trader's own behavior, it can drift for reasons unrelated to his
# decision process (e.g. the post-halt basket permanently drops BNB,
# and overall volume roughly halved -- real market microstructure
# changed too, not just him). Recomputed each cell's pre-cliff-average
# vs post-cliff win rate on post-halt-only (ts>=HALT_END) raw per-fill
# data (position index = count of same-side real fills so far in that
# market, 1-indexed, matching real_fill_count's own semantics), same
# cliff thresholds as originally derived (not re-run through cliff-
# detection this cycle -- a known scope limit, flagged rather than
# silently assumed unchanged):
#   Bitcoin/CORE:    ratio 0.88->0.9179 (z=-6.479, still clearly real,
#                     but the drop is now milder)
#   Ethereum/CHEAP:  ratio 0.73->0.6480 (z=-5.791, real AND stronger)
#   Solana/CHEAP:    ratio 0.77->0.7412 (z=-2.938, real, roughly similar)
# REMOVED (genuinely vanished post-halt, not just weakened -- z well
# under this file's |z|>=2.58 bar, and 2 of the 3 actually point the
# WRONG direction now):
#   Ethereum/MID:  z=1.482, post-cliff win rate actually HIGHER
#                   (0.4575 vs 0.4309 pre) -- effect reversed to noise.
#   Solana/MID:    z=-0.637, ratio 0.9691 -- indistinguishable from flat.
#   Ethereum/CORE: z=0.533, post-cliff HIGHER again (0.8047 vs 0.7887).
# All 3 removed cells had solid post-halt sample sizes (n=215-2103 per
# side of the split, not a thin-data artifact) -- this is a genuine
# disappearance, not underpowering, so removed from both tables below
# rather than kept at a stale value or forced to a fake curve.
#
# CORRECTED 2026-09-13 (/loop cycle 22, same day as ENTRY_SIZING_USD's
# own 3-part fix): cycle 10's own claim above ("position index...
# matching real_fill_count's own semantics") was WRONG in exactly the
# way cycle 21 found for ENTRY_SIZING_USD -- it counted position among
# SAME-SIDE fills only, but real_fill_count (what this table actually
# receives live) increments for EVERY fill in the market, hedge or
# ordinary alike (bot.py's record_real_fill). Also applied cycle 18's
# corrected HALT_END boundary, never re-run against this table until
# now. Redone with the true combined index (walk ALL decisions
# chronologically, one shared counter, sample only dominant-side
# decisions -- same methodology as ENTRY_SIZING_USD's cycle-21 fix):
#   Bitcoin/CORE:    ratio 0.9179->0.9339 (z=-6.185, still clearly real)
#   Ethereum/CHEAP:  ratio 0.6480->0.6493 (z=-6.632, still clearly real,
#                     essentially unchanged)
# TWO CELLS FLIP TRUST-BAR STATUS under the corrected index+boundary --
# a genuine reversal in BOTH directions, not just a magnitude refresh:
#   Solana/CHEAP: was KEPT in cycle 10 (z=-2.938, ratio 0.7412) -- now
#     z=-0.106, ratio 0.9906, completely flat. REMOVED: the original
#     "real" verdict was itself an artifact of the same-side-only
#     mis-indexing, not a genuine effect.
#   Ethereum/CORE: was REMOVED in cycle 10 (z=0.533, "vanished") -- now
#     z=-2.561, ratio 0.9331, JUST under the |z|>=2.58 bar by a hair
#     (0.019) but matching Bitcoin/CORE's ratio (0.9339) almost exactly
#     and with a healthy n=948/487 split. RESTORED given how closely it
#     tracks a cell already confirmed real at nearly the identical
#     magnitude -- flagged as borderline-but-consistent, not a clean
#     pass of the formal bar, in the interest of transparency.
# Cliff thresholds themselves still not re-derived (same scope limit as
# cycle 10 -- a heavier lift than this pass covers).
REENTRY_FATIGUE_THRESHOLD = {
    ("Bitcoin", "CORE"): 8,
    ("Ethereum", "CHEAP"): 8,
    ("Ethereum", "CORE"): 8,
}
REENTRY_FATIGUE_MULTIPLIER = {
    ("Bitcoin", "CORE"): 0.9339,
    ("Ethereum", "CHEAP"): 0.6493,
    ("Ethereum", "CORE"): 0.9331,
}


def reentry_fatigue_multiplier(asset: str, regime: str,
                                real_fill_count: Optional[int]) -> float:
    """1.0 (no-op) for any (asset, regime) cell not in
    REENTRY_FATIGUE_THRESHOLD (includes Bitcoin/MID, Solana/CORE,
    Bitcoin/CHEAP, and all of HIGH -- each excluded for a specific,
    documented reason, not just uncalibrated), or before that cell's own
    fatigue threshold is reached. See the module-level comment above
    REENTRY_FATIGUE_THRESHOLD for the full derivation, per-band confound
    checks, and why this is a deliberate risk dampener rather than a
    replicated-sizing curve."""
    key = (asset, regime)
    threshold = REENTRY_FATIGUE_THRESHOLD.get(key)
    if threshold is None or real_fill_count is None:
        return 1.0
    if real_fill_count < threshold:
        return 1.0
    return REENTRY_FATIGUE_MULTIPLIER.get(key, 1.0)


# ---------------------------------------------------------------------------
# ADD-ON CONTINUATION PROBABILITY. Added 2026-09-26, closing a real
# structural gap found while auditing every real_fill_count==0-scoped
# mechanism in strategy.py: unlike hedges (which have an explicit
# HEDGE_CONTINUATION_PROBABILITY[k] gate governing whether a 2nd/3rd/etc.
# hedge happens -- see hedge_continuation_probability above), ordinary
# same-side ADD-ON entries have NO equivalent continuation gate anywhere
# in the codebase. Once past the first entry, the bot places another
# same-side entry whenever the existing eligibility gates (regime,
# timing, order cap) pass, with no independent "should I keep adding?"
# throttle -- the first half of hedge's own first/continuation split
# exists for add-ons, not the second half.
#
# Original design pass (2026-09-25, addon-continuation-probability-gap-
# and-design in project memory) measured this on trades.jsonl's 5-second
# merge convention -- since confirmed DECISIVELY WRONG for decision
# reconstruction (five-second-merge-convention-decisively-wrong), so
# those per-k numbers were NOT shipped directly. RE-DERIVED 2026-09-26
# ("implement everything ready, with second-time validation") on TRUE
# on-chain order identity (815 markets, ~37h clean window): the
# qualitative finding fully SURVIVES -- Bitcoin continues adding at a
# dramatically and consistently higher rate than Ethereum/Solana, in
# EVERY regime, not just CHEAP -- but the exact per-k curve shape from
# the original pass could not be independently confirmed (per-k cells in
# the clean sample are thin, n=8-146) and its absolute magnitudes
# disagreed substantially with the clean numbers (e.g. Bitcoin/CHEAP/k=0
# was 59.2% on the merge convention vs 78.3% on true on-chain data) --
# the same direction of distortion this project has now found repeatedly
# from that convention (TTC_SIZE_MULTIPLIER, HEDGE_PRICE_SIZING_SCALE,
# STREAK_HEDGE_PROBABILITY_MULTIPLIER).
#
# Shipped as a coarser POOLED-ACROSS-k rate per (asset, regime) instead
# of a per-k curve -- what the clean on-chain sample actually supports
# well (n=25-458 per cell, all comfortably powered):
#            CHEAP   MID    CORE   HIGH
#   Bitcoin  0.795  0.885  0.869  0.762
#   Ethereum 0.545  0.705  0.672  0.433
#   Solana   0.469  0.571  0.560  0.245
# Bitcoin > {Ethereum, Solana} holds cleanly in every single regime;
# Ethereum vs Solana specifically is less clean-cut (Solana edges out
# Ethereum in CHEAP on this sample, 0.469 vs 0.545 -- close either way)
# but Ethereum > Solana holds clearly in HIGH and MID. Ordering by asset,
# not the exact per-cell numbers, is the well-replicated part of this
# finding; re-derive with more data before trusting the precise values
# past a few weeks, same caution as every other freshly-calibrated table.
#
# Applied as a skip-probability gate (same convention as
# REGIME_ENGAGEMENT_MULTIPLIER/CHEAP_JOINT_ENTRY_MULTIPLIER), scoped to
# ordinary (non-hedge) SAME-SIDE add-ons only (real_fill_count > 0) --
# the complementary case to every real_fill_count==0-scoped first-entry
# gate elsewhere in this file. Latched per (side, k) episode via
# MarketActivityState.rejected_addon_episodes, mirroring
# rejected_regime_episodes/rejected_hedge_episodes exactly, so a resting
# order that gets cancelled and re-attempted at the same k can't
# eventually pass a low probability by chance alone.
ADD_ON_CONTINUATION_PROBABILITY = {
    ("Bitcoin", "CHEAP"): 0.795,
    ("Bitcoin", "MID"): 0.885,
    ("Bitcoin", "CORE"): 0.869,
    ("Bitcoin", "HIGH"): 0.762,
    ("Ethereum", "CHEAP"): 0.545,
    ("Ethereum", "MID"): 0.705,
    ("Ethereum", "CORE"): 0.672,
    ("Ethereum", "HIGH"): 0.433,
    ("Solana", "CHEAP"): 0.469,
    ("Solana", "MID"): 0.571,
    ("Solana", "CORE"): 0.560,
    ("Solana", "HIGH"): 0.245,
}


def addon_continuation_probability(asset: str, regime: str) -> float:
    """1.0 (no-op, i.e. today's unthrottled behavior) for any (asset,
    regime) cell not in ADD_ON_CONTINUATION_PROBABILITY (the three
    dormant assets -- no data). Only ever called for real_fill_count > 0
    (an ordinary same-side add-on being considered, not a first entry or
    a hedge) -- see its call site in build_order_intent for the episode-
    latch this composes with."""
    return ADD_ON_CONTINUATION_PROBABILITY.get((asset, regime), 1.0)


# ---------------------------------------------------------------------------
# HEDGE-COUNT REINFORCEMENT. Added 2026-09-13 (/loop iters 131-133 found and
# validated it, iter 138 verified the LIVE version and built it): the
# counterpart signal to REENTRY_FATIGUE above -- another market-OUTCOME
# dose-response finding, not a replicated-sizing one. Needing 2+ hedges
# against a market's original first-entry side predicts that ORIGINAL side
# ends up winning MORE often than markets that only needed 1 hedge --
# counter-intuitive (more hedging might suggest a shakier original read),
# but real, and the mirror image of REENTRY_FATIGUE's logic: both dampen or
# boost further SAME-SIDE additions based on what real outcomes show at
# this specific juncture, not on what "should" intuitively happen.
#
# CRITICAL: verified against the LIVE hedge-count-so-far (only hedges that
# already happened before this same-side entry's own timestamp), not the
# market's final total hedge count (which is a post-hoc quantity, no
# different a mistake than the one caught and fixed for REENTRY_FATIGUE's
# own live-index reverification). Original-side eventual win rate, live
# hedge-count-so-far == 1 vs >= 2, TWAP-era:
#   Ethereum CHEAP: 11.67% -> 25.92% (n=814/2,812), ratio 2.22x
#   Ethereum MID:   32.27% -> 50.00% (n=1,531/4,588), ratio 1.55x
#   Solana CHEAP:   15.26% -> 25.91% (n=1,376/4,327), ratio 1.70x
#   Solana MID:     31.42% -> 40.86% (n=2,180/7,946), ratio 1.30x
# All 4 cells temporally stable both halves (z range 4.49-12.27). Bitcoin
# excluded -- same recurring pattern as REENTRY_FATIGUE and several other
# findings this session (his tighter, more rule-following behavior leaves
# less room for this class of effect); CORE band excluded (not confirmed
# to the same bar as CHEAP/MID for this specific live-index framing).
#
# Multiplier values are DELIBERATELY damped well below the raw 1.3x-2.2x
# odds-ratio scale above -- consistent with every other multiplier in this
# file staying in a modest ~0.7x-1.3x practical range even when a nominal
# cap would allow more (see COMBINED_SIZE_MULTIPLIER_CAP's docstring in
# strategy.py). This is a reward signal (the mirror of REENTRY_FATIGUE's
# penalty), so the discipline matters just as much in the boosting
# direction as the dampening one.
#
# EXTENDED TO A SECOND TIER (2026-09-13, same day): the full live-index
# dose-response (not just the 1-vs-2+ split above) keeps climbing beyond
# idx=2, though noisily -- pooled idx>=5 win rates (smoothing over
# individual-bucket noise, e.g. Solana CHEAP's idx=3 bucket dips BELOW
# idx=1, n=507, almost certainly sampling noise given idx=2/4/5/6+ are
# all higher) are meaningfully above the idx>=2 tier already shipped:
#   Ethereum CHEAP: idx1 11.67% -> idx2-3 pooled 21.88% -> idx>=4 pooled 27.62%
#   Ethereum MID:   idx1 32.27% -> idx2-3 pooled 49.22% -> idx>=4 pooled 50.34%
#   Solana CHEAP:   idx1 15.26% -> idx2-3 pooled 14.40% (flat/noisy) -> idx>=4 pooled 31.47%
#   Solana MID:     idx1 31.39% -> idx2-3 pooled 35.90% -> idx>=4 pooled 42.82%
# A SECOND threshold at live_hedge_count>=5 (chosen conservatively past
# the noisiest idx=3/4 buckets, into the region every cell agrees is
# higher) with a modestly bigger multiplier than tier 1 -- NOT a smooth
# continuous curve, deliberately: the intermediate buckets are too noisy
# (n=270-960) and non-monotonic in places to trust a fitted curve over a
# second conservative step, matching REENTRY_FATIGUE's own discrete-
# threshold design rather than overfitting the wiggle.
#
# CHECKED FOR POST-HALT FRESHNESS 2026-09-13 (/loop cycle 11), NOT
# RECALIBRATED -- genuinely underpowered right now, a different verdict
# than REENTRY_FATIGUE's clean "3 kept, 3 vanished" split. Re-ran the
# exact same live-hedge-count methodology on post-halt-only
# (ts>=HALT_END) data and got noisy, internally inconsistent results in
# ALL 4 cells (e.g. Ethereum/CHEAP tier1 ratio 2.625x but tier2 ratio
# only 1.067x -- LOWER than tier1, which should never happen if the
# dose-response were real; Ethereum/MID tier2 actually reverses sign,
# z=-2.359). Root cause identified, not just "noisy": only ~4.9 days of
# post-halt data exist as of this check (vs the multi-week TWAP-era
# window this table was built from), and this table's already-narrow
# population (only original-side entries placed AFTER at least one
# hedge already exists) is additionally cut down by the separately-
# documented hedge-rate secular decline (see hedge-rate-secular-decline
# in project memory) -- so the bucket sample sizes collapsed to n=45-149
# per cell/tier, roughly 1/13th to 1/60th of the original calibration's
# n=814-2,812. This is a genuine "can't independently verify YET" verdict,
# not a "vanished" one -- kept at the original TWAP-era values rather
# than recalibrated on underpowered noise or blindly assumed unchanged.
# Revisit once meaningfully more post-halt hedge activity accumulates.
#
# RE-VERIFIED 2026-09-13 (/loop cycle 18): found and fixed a real bug in
# this session's OWN research methodology, not the trader's behavior --
# every prior post-halt-only script this session (including the cycle-11
# check above) used HALT_END=1788870060 (2026-09-08 12:21 UTC), but a
# direct gap-scan of the full trade mirror (853,818 trades) confirms the
# real halt actually ended at 1788704494 (2026-09-06 14:21:34 UTC) --
# every "post-halt-only" filter this session was silently excluding the
# first ~46 hours (~1.9 days) of genuinely valid post-halt data. Re-ran
# this table's check with the corrected boundary: the extra ~2 days
# modestly IMPROVED internal consistency in 2 of 4 cells (Ethereum/CHEAP
# now z=2.156/z=2.189 for tier1/tier2, same direction and much closer
# in magnitude than before; Solana/MID now z=1.122/z=4.449, both
# positive) but Ethereum/MID and Solana/CHEAP remain internally
# inconsistent (tier1/tier2 still disagree in sign or magnitude order).
# Still not confidently actionable -- verdict UNCHANGED (kept at
# original TWAP-era values) -- but the trend with more data is toward
# a real signal, not toward noise, which is itself informative. See
# full-behavioral-audit-tracker.md's cycle 18 entry for the full
# writeup of this boundary-correction discovery and its impact (or lack
# of one) on every other table fixed this session.
HEDGE_COUNT_REINFORCEMENT_TIERS = {
    ("Ethereum", "CHEAP"): [(2, 1.5), (5, 1.8)],
    ("Ethereum", "MID"): [(2, 1.25), (5, 1.4)],
    ("Solana", "CHEAP"): [(2, 1.35), (5, 1.6)],
    ("Solana", "MID"): [(2, 1.15), (5, 1.3)],
}
# Derived, backward-compatible views onto the tier-1 (already-shipped)
# values -- kept so existing callers/tests reading "the" threshold or
# multiplier for a cell still see the original tier-1 numbers.
HEDGE_COUNT_REINFORCEMENT_THRESHOLD = {k: v[0][0] for k, v in HEDGE_COUNT_REINFORCEMENT_TIERS.items()}
HEDGE_COUNT_REINFORCEMENT_MULTIPLIER = {k: v[0][1] for k, v in HEDGE_COUNT_REINFORCEMENT_TIERS.items()}


def hedge_count_reinforcement_multiplier(asset: str, regime: str,
                                          live_hedge_count: Optional[int]) -> float:
    """1.0 (no-op) for any (asset, regime) cell not in
    HEDGE_COUNT_REINFORCEMENT_TIERS (Bitcoin, CORE/HIGH, and all dormant
    assets -- not just uncalibrated, see the module comment above), before
    that cell's first threshold is reached, or for a market's very first
    entry (real_hedge_fill_count starts at 0, always below every
    threshold, so this is naturally a no-op there without a separate
    check). Steps up again at the second threshold. Only ever applies to
    ordinary (non-hedge) same-side entries -- the caller is responsible
    for not applying this to a hedge leg's own sizing, same discipline as
    REENTRY_FATIGUE_MULTIPLIER above."""
    tiers = HEDGE_COUNT_REINFORCEMENT_TIERS.get((asset, regime))
    if tiers is None or live_hedge_count is None:
        return 1.0
    result = 1.0
    for threshold, mult in tiers:  # tiers listed in ascending threshold order
        if live_hedge_count >= threshold:
            result = mult
    return result


# REMOVED 2026-09-24 (single-function hedge consolidation): HEDGE_TTC_
# SIZE_MULTIPLIER/hedge_ttc_size_multiplier and HEDGE_TRIGGER_AFTER_BIG_
# LOSS_MULTIPLIER/hedge_trigger_after_big_loss_multiplier (the latter's
# table was already empty -- effect vanished post-halt, verdict
# unchanged on re-verification). Both cut for simplicity; hedge_notional_
# for_price handles all hedge sizing now with no TTC dimension.
#
# ADDED 2026-09-24: TICK-OFFSET PLACEMENT (queue-position/tick-priority).
#
# See queue-position-reattempt-blocked-marketlens-limit / tick-offset-
# placement-design in project memory for the full derivation. Our bot's
# entry price was, until this addition, unconditionally `side_book.best_bid`
# (strategy.py's build_order_intent) -- joins the touch 100% of the time.
# A real MarketLens L2 pull (n=82 clean, after excluding stale/misaligned
# snapshots via the schema's own as_of/requested_at freshness metadata)
# found the real trader only does that 34.1% of the time:
#
#   <= -3 ticks (deep behind): 15.9%   -1 tick: 14.6%   0 (touch): 34.1%
#   -2 ticks: 1.2%                     +1 tick: 18.3%   +2 ticks: 8.5%
#                                       >= +3 ticks: 7.3%
#
# Of all cases where he improves the price at all (28/82), 15 (53.6%) do
# so by EXACTLY +1 tick -- disciplined, minimum-necessary price
# improvement, not indifferent overpaying. Distribution collapses the
# thin tails to single representative buckets (-3, +3) since the raw
# sample (n=82) is too small to trust a longer per-tick tail; a bucket
# value is applied as its exact tick count when sampled.
#
# Measured on FIRST ENTRIES ONLY (real trader's first-entry decisions) --
# scoped the same way in strategy.py (not applied to hedges or add-ons,
# matching this project's standing discipline of not generalizing a
# finding beyond what was actually measured, e.g. forced_first_entry_side's
# own docstring).
TICK_OFFSET_DISTRIBUTION: list = [
    (-3, 0.159),
    (-2, 0.012),
    (-1, 0.146),
    (0, 0.341),
    (1, 0.183),
    (2, 0.085),
    (3, 0.073),
]


def sample_tick_offset(rng: "random.Random") -> int:
    """Draws an integer tick offset (positive = improves toward the ask,
    negative = trails behind the current best bid) from
    TICK_OFFSET_DISTRIBUTION. Deterministic given a seeded rng, same
    convention as every other stochastic draw in this module (e.g.
    decide_side's persistence roll)."""
    r = rng.random()
    cum = 0.0
    for ticks, p in TICK_OFFSET_DISTRIBUTION:
        cum += p
        if r < cum:
            return ticks
    return TICK_OFFSET_DISTRIBUTION[-1][0]


# REMOVED 2026-09-24 (single-function hedge consolidation): EARLY_ENTRY_
# THRESHOLD_S/LATE_WINDOW_HEDGE_URGENCY_MULTIPLIER/late_window_hedge_
# urgency_multiplier. This WAS a real, cross-asset-validated finding
# (survival-analysis-confirmed hazard hump 150-180s after an early first
# entry, shipped just hours before this consolidation, commit 377f8ac) --
# cut deliberately for simplicity, not because it stopped being real. If
# hedge-timing fidelity matters enough to bring back, this is the one
# mechanism worth re-adding first; the calibration is preserved in git
# history (commit 377f8ac) and in project memory (late-window-hedge-
# urgency-design) if needed.


