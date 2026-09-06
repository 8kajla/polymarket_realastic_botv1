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
# Timing
# ---------------------------------------------------------------------------
# Never place a NEW entry, and never let a resting paper order keep sitting
# unmanaged, once fewer than this many seconds remain before market close.
# Confirmed near-hard rule from historical data (violation rate ~0.04% at a
# stricter 60s boundary) -- 90s gives real margin.
MIN_SECONDS_BEFORE_CLOSE = 90

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
# Local paper-trading state (gitignored -- never commit real run state)
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("PAPERBOT_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
LEDGER_PATH = DATA_DIR / "paper_ledger.json"
