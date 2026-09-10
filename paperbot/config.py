"""
Central configuration: endpoints, safety gate, and tunable constants.

SAFETY
------
This codebase is PAPER-TRADING ONLY. There is no code path anywhere in
this package that constructs a signed order, holds a private key, or
submits anything to Polymarket's order-submission endpoints. "Live mode"
below is a placeholder switch that, if ever set, raises immediately and
explains why -- it is intentionally never implemented. Do not add a live
order path to this file or anywhere else without a much broader
conversation about custody, key management, and risk controls than this
project was scoped for.
"""
import os
from enum import Enum
from pathlib import Path


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"  # never implemented -- see get_trading_mode() below


def get_trading_mode() -> TradingMode:
    """
    Reads POLYMARKET_TRADING_MODE from the environment. Defaults to PAPER.
    Setting it to "live" does NOT enable live trading -- it deliberately
    raises, because this codebase contains no live order-submission
    implementation and no private-key handling. There is no environment
    variable that will make this function return LIVE successfully.
    """
    raw = os.environ.get("POLYMARKET_TRADING_MODE", "paper").strip().lower()
    if raw in ("paper", ""):
        return TradingMode.PAPER
    if raw == "live":
        raise RuntimeError(
            "POLYMARKET_TRADING_MODE=live was requested, but this codebase "
            "intentionally contains no live order-submission path and no "
            "private-key handling. This is a paper-trading simulator only. "
            "Refusing to start."
        )
    raise ValueError(f"Unknown POLYMARKET_TRADING_MODE={raw!r}; expected 'paper'.")


# ---------------------------------------------------------------------------
# Platform endpoints (verified against Polymarket's own documentation and
# py-clob-client source as of this build -- re-check docs.polymarket.com
# before relying on these long-term, platform details have changed before).
# ---------------------------------------------------------------------------
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_REST_BASE_URL = "https://clob.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# The market-channel WS subscription must include custom_feature_enabled:true
# to receive last_trade_price events -- without it, only book/price_change
# messages arrive, and the fill simulator has nothing to consume against.
WS_SUBSCRIBE_CUSTOM_FEATURE_ENABLED = True
WS_PING_INTERVAL_SECONDS = 10
WS_PING_MESSAGE = "PING"

# ---------------------------------------------------------------------------
# Market discovery. Slug prefixes are confirmed for BNB and Hyperliquid from
# the real historical trade data this bot's behavior is calibrated on
# ("bnb-updown-5m-<epoch>", "hype-updown-5m-<epoch>"). The other four follow
# the same observed "<ticker>-updown-5m-<epoch>" convention but were not
# directly present in that sample -- VERIFY these against a live Gamma
# markets pull before relying on discovery for those four assets.
# ---------------------------------------------------------------------------
ASSET_SLUG_PREFIXES = {
    "Bitcoin": "btc-updown-5m-",       # ASSUMPTION: verify live
    "Ethereum": "eth-updown-5m-",      # ASSUMPTION: verify live
    "Solana": "sol-updown-5m-",        # ASSUMPTION: verify live
    "Dogecoin": "doge-updown-5m-",     # ASSUMPTION: verify live
    "Hyperliquid": "hype-updown-5m-",  # CONFIRMED from historical data
    "BNB": "bnb-updown-5m-",           # CONFIRMED from historical data
}

ALL_ASSETS = list(ASSET_SLUG_PREFIXES.keys())

MARKET_DISCOVERY_POLL_SECONDS = 20

# ---------------------------------------------------------------------------
# Market resolution polling (post-close, waiting for Gamma to report the
# winning outcome). Confirmed live: without these limits, a growing backlog
# of unresolved markets gets retried on EVERY tick with no backoff, which
# both self-inflicts a 429 storm against Gamma and floods Railway's own log
# ingestion (dropped messages) with one full traceback per market per tick.
# ---------------------------------------------------------------------------
RESOLUTION_RETRY_COOLDOWN_SECONDS = 30.0   # don't re-hit the same market more often than this
RESOLUTION_MAX_ATTEMPTS_PER_TICK = 3       # spread the backlog across ticks instead of blasting all of it
RESOLUTION_BACKOFF_ON_FAILURE_SECONDS = 60.0  # extra cooldown per consecutive failure, capped below
RESOLUTION_MAX_BACKOFF_MULTIPLIER = 5
RESOLUTION_MAX_AGE_SECONDS = 2 * 60 * 60   # give up and log an explicit abandonment after this long

# How often to log a running realized-P&L summary, independent of any
# individual settlement.
PNL_SUMMARY_INTERVAL_SECONDS = 300

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
# Never place a NEW entry, and never let a resting paper order keep sitting
# unmanaged, once fewer than this many seconds remain before market close.
# Confirmed near-hard rule from historical data (violation rate ~0.04% at a
# stricter 60s boundary) -- was 90 (chosen as "real margin" above that 60s
# boundary, not derived from anything about this bot's own fill mechanics --
# see the git history for the original reasoning).
#
# TIGHTENED 2026-09-09 (found chasing the confirmed-live HIGH-regime
# participation gap -- bot lands on HIGH-band entries at roughly half the
# real trader's rate in a same-window comparison). Root cause: HIGH-band
# opportunities concentrate very late in the window (median 119s
# remaining, vs 168-204s for the other three regimes), so the 90s cutoff
# was disproportionately excluding them -- 26.4% of his real HIGH-regime
# trades land with 60-90s remaining, a window this bot could never enter
# at 90s. That's not HIGH-specific plumbing -- CHEAP/MID/CORE lose
# 8.7-15.0% of their own real trades to the same 60-90s band, just less
# severely since they aren't clustered as late. Confirmed exactly ZERO
# real trades (any regime, n=37,860 checked) fall under 60s remaining --
# matches the original 60s boundary finding exactly, so that boundary
# itself isn't in question, only how much margin to keep above it.
# Tightened to 75 -- keeps a real 15s buffer above his empirical 60s
# limit (half the original 30s margin, not zero), while recovering most
# of the excluded opportunity: HIGH's excluded share drops from 26.4% to
# 10.7% at this threshold. Applies symmetrically to both usages below
# (new-entry gate in strategy.timing_ok, and the open-order expiry check
# in fill_simulation.manage_open_orders) since both exist for the same
# reason and should move together.
MIN_SECONDS_BEFORE_CLOSE = 75

# ---------------------------------------------------------------------------
# Availability / liquidity sanity check -- deliberately SIMPLE and UNIFORM
# across all regime bands. An earlier version of a similar bot scaled these
# thresholds up with price band and it starved CORE/HIGH almost entirely;
# do not reintroduce that.
# ---------------------------------------------------------------------------
MAX_SPREAD = 0.05          # best_ask - best_bid, in price units (0-1 scale)
MIN_BOOK_DEPTH_USD = 5.0   # total resting USD notional at-or-better on both sides

# ---------------------------------------------------------------------------
# Fill simulation
# ---------------------------------------------------------------------------
# Raw queue-ahead (sum of resting bid depth at-or-better than our order's
# price) is a pessimistic upper bound -- real queue position is usually much
# shorter because not all of that depth is genuinely ahead of us in time
# priority, and some of it cancels before ever trading. Discount it. Do NOT
# default this to 1.0: that models our order as queued behind the ENTIRE
# visible book and produces near-zero fill rates.
QUEUE_SAFETY_FACTOR = float(os.environ.get("QUEUE_SAFETY_FACTOR", "0.25"))

# If the best bid drifts more than this many ticks away from our resting
# order's price (while remaining in the same regime band), cancel and
# re-place at the new price rather than leaving a stale order unmanaged.
DRIFT_REPRICE_TICKS = 3

# How many resting orders this bot will hold OPEN SIMULTANEOUSLY in the same
# market. CONFIRMED LIVE (2026-09-08): with this at its old implicit value of
# 1 (a market was skipped entirely in strategy_tick once it had any resting
# order at all), the bot averaged 3.0 entries/market in an 8h live window
# vs. the real trader's 18.5 average / 13.5 median / 105 max in the same
# window -- a ~6x gap, the dominant driver (bigger than market-participation
# rate) of the bot trading far fewer total positions than the trader.
#
# Raising this to >1 lets strategy_tick keep placing new orders in a market
# that already has one resting, rather than waiting for it to fill/cancel/
# expire first. MarketActivityState already tracks entry_count/last_side/
# cost_by_side at PLACEMENT time (not fill time), so decide_side/decide_hedge/
# decide_size treat a second concurrent order exactly like a second
# sequential one -- no special-casing needed there.
#
# Deliberately conservative, not an attempt to match the trader's 13.5-
# median/105-max directly: trade.jsonl only has fill TIMESTAMPS, not order
# placement times, so there's no way to independently confirm how many of
# the trader's own entries were genuinely concurrent (multiple resting
# orders at once) vs. fast sequential re-entries after quick fills -- that
# distinction matters a lot for how high this should safely go, and isn't
# measurable from the data this project has. Start small, watch the live
# entries/market number after deploying, raise later with real evidence
# rather than guessing straight to the trader's own ceiling.
MAX_OPEN_ORDERS_PER_MARKET = int(os.environ.get("MAX_OPEN_ORDERS_PER_MARKET", "3"))

# ---------------------------------------------------------------------------
# Floor-lot "probe" tier (see behavior_config.py)
# ---------------------------------------------------------------------------
FLOOR_LOT_SIZE_SHARES = 0.02

# ---------------------------------------------------------------------------
# Sizing jitter: the confirmed historical numbers are MEDIANS, not full
# distributions. To avoid every simulated order at a given (asset, regime,
# position) tier being bit-for-bit identical, apply a small symmetric
# jitter around the median. This is a modeling simplification -- documented,
# not hidden.
# ---------------------------------------------------------------------------
SIZING_JITTER_FRACTION = 0.15

# ---------------------------------------------------------------------------
# Optional fixed-bankroll mode. Added 2026-09-08 for a second, small-capital
# instance meant to answer a real question -- "if I deposited $100 real
# money and ran this exact strategy, what would actually happen?" -- not to
# change the strategy at all.
#
# None (the default) means UNCONSTRAINED: every order the strategy decides
# to place gets placed, no matter the cumulative notional -- today's
# behavior, exactly. This MUST stay the main bot's mode: that instance
# exists to track how closely the strategy matches the real trader's
# behavior at the real trader's own scale, not to model one account size.
#
# Setting BANKROLL_USD turns on a hard "can't spend money we don't have"
# gate in PaperBot._evaluate_one_market: before placing an order, available
# cash is recomputed FROM SOURCE every time (starting balance + realized
# P&L - capital currently committed to unsettled orders -- see
# PaperBot.available_cash()) -- never an incrementally-mutated running
# total, matching this project's existing hard rule for
# Ledger.realized_pnl(). If an order's notional exceeds what's available it
# is skipped entirely (logged), exactly like a real exchange rejecting an
# order for insufficient buying power -- never silently downsized, which
# would be an actual (unrequested) strategy change.
BANKROLL_USD = os.environ.get("BANKROLL_USD")
if BANKROLL_USD is not None:
    BANKROLL_USD = float(BANKROLL_USD)

# Global multiplier applied to every non-floor-lot entry's notional (see
# strategy.decide_size). 1.0 (default) is a no-op -- today's behavior,
# unchanged. Exists so a small-bankroll instance can run the EXACT SAME
# decision logic (side/regime/hedge/timing all untouched -- this only
# scales dollars) at a proportionally smaller dollar scale, so a $100
# account can still afford to trade near the real trader's ENTRY FREQUENCY
# instead of exhausting its bankroll on a handful of whale-sized entries
# and then sitting out most opportunities. Deliberately NOT applied to the
# floor-lot probe tier (FLOOR_LOT_SIZE_SHARES): that tier is already tiny
# and independently calibrated, and scaling it down further risks pushing
# it below the exchange's real per-market orderMinSize, which would
# silently kill those trades outright -- the opposite of "not less trades".
#
# LIMIT CONFIRMED 2026-09-09 ($100-bankroll capacity work): this can't
# scale a bankroll down arbitrarily far. The real orderMinSize=5-share
# floor (strategy.py's own BUMP path) sets a hard per-trade dollar floor
# that doesn't move with this factor -- roughly $0.75 (CHEAP) to $4.75
# (HIGH) at typical in-band prices. Below the scale where a regime's
# calibrated median would clear that floor on its own, every trade in
# that regime just gets bumped to the same flat floor value regardless of
# how low this factor goes -- shrinking it further stops reducing
# concurrent commitment and only flattens size differentiation. Verified
# live: paperbot-100 at BANKROLL_USD=1000, SIZE_SCALE_FACTOR=1.0 (this
# knob unused) already peaks around $450-460 committed concurrently
# across 3 assets -- that peak is a near-fixed property of the strategy's
# calibrated sizing x hedge depth, not something BANKROLL_USD alone
# controls (BANKROLL_USD only gates whether a trade is AFFORDABLE, not
# how big it is). Getting a $100 instance to a safe concurrent-commitment
# ceiling needs this factor AND a tighter MAX_HEDGE_COUNT_PER_MARKET
# together, not either alone.
SIZE_SCALE_FACTOR = float(os.environ.get("SIZE_SCALE_FACTOR", "1.0"))

# ---------------------------------------------------------------------------
# Maker rebates. Added 2026-09-09: every order this bot places is postOnly
# (see bot.py's "postOnly order would cross spread" skip path -- it never
# crosses as a taker, only ever rests as a maker), but nothing in the
# ledger has ever modeled the real economics that comes with that.
# Polymarket's taker-fee/maker-rebate program means a maker isn't just
# fee-free, it actively EARNS a share of the taker's fee on every fill --
# real, additive income this bot's PNL has been silently leaving out of
# every dollar figure this whole project has ever reported.
#
# CORRECTED 2026-09-09 (same night, few hours after first shipping this):
# the original CRYPTO_TAKER_FEE_RATE=0.018 came from secondary aggregator
# sites (docs.polymarket.com / help.polymarket.com were unreachable from
# this sandbox -- DNS resolution failed both times tried) and was WRONG.
# Pulled the live feeSchedule directly off real, currently-open BTC/ETH/
# SOL 5-min markets via Gamma (a PRIMARY source, not an aggregator's
# summary) and all three agreed exactly:
#   {"exponent": 1, "rate": 0.07, "takerOnly": true, "rebateRate": 0.2}
# rebateRate=0.2 matches CRYPTO_MAKER_REBATE_SHARE below exactly --
# that part of the aggregator research held up. But the real taker rate
# is 0.07 (7%), not 0.018 (1.8%) -- nearly 4x off, and understated (this
# bot's rebate income has been running ~4x too low since the first
# deploy tonight). "exponent": 1 is consistent with the same p*(1-p)
# SHAPE the aggregators described (peaks at price=0.50, tapers toward
# 0/1) -- read as fee = shares * rate * (price*(1-price))^exponent,
# which collapses to the formula below when exponent=1. Not
# independently confirmed against Polymarket's own docs (still
# unreachable), but directly confirmed identical across BTC, ETH, and
# SOL live market data, which is the authoritative source available.
#   taker_fee(shares, price) = shares * CRYPTO_TAKER_FEE_RATE * price * (1-price)
#   makers pay ZERO fees, and are paid CRYPTO_MAKER_REBATE_SHARE of the
#   taker's fee on that same fill.
CRYPTO_TAKER_FEE_RATE = 0.07
CRYPTO_MAKER_REBATE_SHARE = 0.20


def maker_rebate_usd(shares: float, price: float) -> float:
    """Rebate earned by the RESTING (maker) side of one fill -- this
    bot's only execution mode. Always additive income, never a cost (the
    taker pays the fee; the maker receives a share of it)."""
    if shares <= 0 or price <= 0.0 or price >= 1.0:
        return 0.0
    taker_fee = shares * CRYPTO_TAKER_FEE_RATE * price * (1.0 - price)
    return taker_fee * CRYPTO_MAKER_REBATE_SHARE

# ---------------------------------------------------------------------------
# Feature flag for TTC_SIZE_MULTIPLIER (see its docstring in
# behavior_config.py). Added 2026-09-10 alongside that feature itself.
# Default TRUE -- this is meant to be the new, correct default behavior
# everywhere, same as every other calibration shipped tonight.
#
# Explicit exception, per direct instruction: the $100 real-viability
# instance (paperbot-mini) is deliberately being kept on the OLD (no ttc
# scaling) behavior for now, as a running control -- to see how that
# config performs over a real long run BEFORE this change is layered on
# top of it, rather than changing two things (the $100 config's own
# tuning AND this new feature) at once and losing the ability to
# attribute results to either one cleanly. Set via env var (same pattern
# as BANKROLL_USD/SIZE_SCALE_FACTOR/MAX_HEDGE_COUNT_PER_MARKET) so this
# survives every future restart of that instance, not just "whichever
# deploy happened to not restart it" -- a crash-restart or a future
# unrelated redeploy must not silently pull mini back onto the new
# behavior.
ENABLE_TTC_SIZE_MULTIPLIER = os.environ.get(
    "ENABLE_TTC_SIZE_MULTIPLIER", "true"
).strip().lower() not in ("false", "0", "no")

# ---------------------------------------------------------------------------
# Feature flag for HEDGE_LIQUIDITY_MULTIPLIER (see its docstring in
# behavior_config.py). Added 2026-09-10, same night and same reasoning as
# ENABLE_TTC_SIZE_MULTIPLIER directly above -- default TRUE everywhere,
# explicit FALSE for paperbot-mini (the $100 control instance), same
# env-var-driven, restart-durable mechanism, same rationale: don't change
# the control's behavior while it's specifically being watched to isolate
# what its OWN config change (SIZE_SCALE_FACTOR/MAX_HEDGE_COUNT_PER_MARKET)
# does on its own.
ENABLE_HEDGE_LIQUIDITY_MULTIPLIER = os.environ.get(
    "ENABLE_HEDGE_LIQUIDITY_MULTIPLIER", "true"
).strip().lower() not in ("false", "0", "no")

# ---------------------------------------------------------------------------
# Feature flag for RESUMPTION_SIZE_MULTIPLIER (see its docstring in
# behavior_config.py). Added 2026-09-10, same night, same pattern and
# reasoning as ENABLE_TTC_SIZE_MULTIPLIER/ENABLE_HEDGE_LIQUIDITY_MULTIPLIER
# above -- default TRUE everywhere, explicit FALSE for paperbot-mini (the
# $100 control instance).
ENABLE_RESUMPTION_SIZE_MULTIPLIER = os.environ.get(
    "ENABLE_RESUMPTION_SIZE_MULTIPLIER", "true"
).strip().lower() not in ("false", "0", "no")

# ---------------------------------------------------------------------------
# Feature flag for WEEKEND_HEDGE_MULTIPLIER (see its docstring in
# behavior_config.py). Added 2026-09-10, same pattern and reasoning as
# ENABLE_TTC_SIZE_MULTIPLIER/ENABLE_HEDGE_LIQUIDITY_MULTIPLIER/
# ENABLE_RESUMPTION_SIZE_MULTIPLIER above -- default TRUE everywhere,
# explicit FALSE for paperbot-mini (the $100 control instance).
ENABLE_WEEKEND_HEDGE_MULTIPLIER = os.environ.get(
    "ENABLE_WEEKEND_HEDGE_MULTIPLIER", "true"
).strip().lower() not in ("false", "0", "no")

# ---------------------------------------------------------------------------
# Feature flag for ADVERSE_MOVE_SIZE_MULTIPLIER (see its docstring in
# behavior_config.py). Added 2026-09-10, same pattern and reasoning as
# ENABLE_TTC_SIZE_MULTIPLIER/ENABLE_HEDGE_LIQUIDITY_MULTIPLIER/
# ENABLE_RESUMPTION_SIZE_MULTIPLIER/ENABLE_WEEKEND_HEDGE_MULTIPLIER above --
# default TRUE everywhere, explicit FALSE for paperbot-mini (the $100
# control instance).
ENABLE_ADVERSE_MOVE_SIZE_MULTIPLIER = os.environ.get(
    "ENABLE_ADVERSE_MOVE_SIZE_MULTIPLIER", "true"
).strip().lower() not in ("false", "0", "no")

# ---------------------------------------------------------------------------
# Feature flag for ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER (see its
# docstring in behavior_config.py). Added 2026-09-10, same night, same
# pattern as ENABLE_ADVERSE_MOVE_SIZE_MULTIPLIER above (which this one
# extends to hedge_count >= 1) -- default TRUE everywhere, explicit FALSE
# for paperbot-mini (the $100 control instance).
ENABLE_ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER = os.environ.get(
    "ENABLE_ADVERSE_MOVE_CONTINUATION_SIZE_MULTIPLIER", "true"
).strip().lower() not in ("false", "0", "no")

# ---------------------------------------------------------------------------
# Feature flag for ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER (see its
# docstring in behavior_config.py). Added 2026-09-10, same night --
# distinct from the two SIZE flags above, this one scales WHETHER a hedge
# triggers at all (hedge_attempt_hazard's output), not how big it is once
# one happens. Default TRUE everywhere, explicit FALSE for paperbot-mini
# (the $100 control instance).
ENABLE_ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER = os.environ.get(
    "ENABLE_ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER", "true"
).strip().lower() not in ("false", "0", "no")

# ---------------------------------------------------------------------------
# Hard cap on hedge_count. Added 2026-09-09, hours after
# HEDGE_CONTINUATION_PROBABILITY shipped (behavior_config.py) let
# decide_hedge fire more than once per market -- CONFIRMED LIVE this
# same night that the per-opportunity probability model runs away: our
# bot's own hedge-count-per-market distribution came back badly bimodal
# (61.4% stop at 1, vs the real 39.8%; 15.1% reach 7+, vs the real
# 2.2% -- nearly 7x too common), and markets with 2+ hedges accounted
# for 89% of a confirmed net-negative hedging effect (-$474 of -$535
# total, on the bot's own ledger, dominant-side-alone vs combined PNL
# across 251 paired markets). Root cause not yet fixed properly (the
# per-opportunity roll doesn't account for how much more often our
# bot's tick cadence offers a "continuation opportunity" than the real
# trader's actual decision cadence, so a 50-60% per-opportunity
# probability compounds into runaway chains) -- this cap is a direct,
# conservative mitigation to bound the damage NOW while that gets
# redesigned properly. 6 is chosen because the real distribution's 7+
# bucket is only 2.2% (a thin tail, not where the real mass lives) --
# capping here cuts off almost none of genuine behavior while
# eliminating the runaway-chain failure mode entirely.
#
# RE-CHECKED 2026-09-10 (prompted by a research pass that initially
# proposed raising this cap): a fresh count using RAW trade fills (not
# decision-collapsed) found 16.08% of real markets exceed 6 hedges --
# looked like a real gap worth fixing. Redone properly with the same
# 5s-same-side-fragment-merge this file already uses for
# HEDGE_TRIGGER_PROBABILITY/HEDGE_SIZE_RATIO: the real figure is 1.57%
# (over 10: 0.12%), closely matching the original 2.2% figure above.
# The raw-count version was inflated by exactly the fragmentation
# artifact this file has hit before -- multiple partial fills of ONE
# real hedge decision counted as separate hedges. Cap left at 6,
# confirmed still correct; not raised.
#
# Env-overridable (2026-09-09, added for the $100-bankroll capacity
# work): a small-bankroll instance needs a tighter cap than 6 to bound
# worst-case concurrent commitment -- see SIZE_SCALE_FACTOR's own
# docstring for why size scaling alone can't do this (exchange
# orderMinSize floors don't scale down). Default stays 6, unchanged, for
# every existing instance.
MAX_HEDGE_COUNT_PER_MARKET = int(os.environ.get("MAX_HEDGE_COUNT_PER_MARKET", "6"))

# ---------------------------------------------------------------------------
# Local paper-trading state (gitignored -- never commit real run state)
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("PAPERBOT_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
LEDGER_PATH = DATA_DIR / "paper_ledger.json"
