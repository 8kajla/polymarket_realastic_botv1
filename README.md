# Polymarket Up/Down Paper-Trading Bot

A **paper-trading only** simulator that replicates one trader's confirmed
maker-only behavior across Polymarket's six 5-minute crypto Up/Down
markets (BTC, ETH, SOL, BNB, DOGE, HYPE), using per-asset behavioral
parameters measured from a 778,116-trade historical dataset, and a fill
model that simulates real order-book queueing rather than assuming
instant fills.

**This bot never submits a real order and never handles a private key.**
See "Safety" below.

## Why maker-only

Confirmed via Polymarket's own reward ledger for the modeled wallet: it
has received `MAKER_REBATE` payouts repeatedly and zero `TAKER_REBATE`,
ever. Every real fill came from a resting order a counterparty crossed
into. Every order this bot places is therefore `GTC` + `postOnly` --
modeling a confirmed fact about how this trader operates, not a
stylistic choice.

## Architecture

```
paperbot/
  config.py            endpoints, safety gate, tunable constants
  behavior_config.py   per-asset regime/sizing/persistence/floor-lot tables
  market_discovery.py  Gamma API polling -> Market objects
  book.py              CLOB REST bootstrap + WS live book state per token
  strategy.py          regime/availability/side/sizing/timing decisions
  fill_simulation.py   the paper fill model (queueing, drift, expiry)
  ledger.py            settlement + realized P&L (recomputed, never cached)
  bot.py               main loop wiring it all together
tests/                 96 tests covering the above
run_bot.py             CLI entry point
```

## Running it

```
pip install -r requirements-dev.txt          # runtime deps + pytest
python3 run_bot.py                          # all six assets
python3 run_bot.py --assets Bitcoin BNB      # restrict to a subset
QUEUE_SAFETY_FACTOR=0.3 python3 run_bot.py   # tune the fill model
python3 -m pytest tests/ -v                  # run the test suite
```

For a production/deployment install (no test dependencies), use
`pip install -r requirements.txt` -- see "Deploying on Railway" below.

Paper-trading state (the settlement ledger) is written to `data/`, which
is gitignored -- it's local run output, not code.

## Deploying on Railway

This repo is set up to deploy on Railway as a background worker (no HTTP
port, no public domain needed):

- **`railway.toml`** sets the builder to `RAILPACK` (Railway's current
  default builder) and `startCommand = "python -u run_bot.py"` (`-u` for
  unbuffered output, so logs show up immediately in Railway's log
  viewer). Restart policy is `ON_FAILURE` with up to 10 retries -- the bot
  should run indefinitely; if the whole process crashes on something
  outside a single market's fail-closed isolation, Railway brings it back.
- **`.python-version`** pins Python 3.13 for Railway's Railpack builder.
- **`requirements.txt`** has only runtime deps (`requests`, `websockets`);
  `pytest` lives in `requirements-dev.txt` so it doesn't get installed in
  the deployed container.
- Don't enable "Generate Domain" for this service -- it's a worker that
  opens an outbound WebSocket to Polymarket, it doesn't listen on `$PORT`.

**Environment variables to set in Railway's Variables tab** (all
optional -- sane defaults if you set none of them):
- `QUEUE_SAFETY_FACTOR` -- fill-model tuning, default `0.25`.
- `POLYMARKET_TRADING_MODE` -- leave unset (defaults to `paper`). Setting
  it to anything but `paper` makes the bot refuse to start, on purpose.
- `PAPERBOT_DATA_DIR` -- see persistence note below.

**Persistence note:** Railway's default filesystem is ephemeral --
a redeploy or restart wipes `data/paper_ledger.json` unless you attach a
[Railway Volume](https://docs.railway.com/reference/volumes) and set
`PAPERBOT_DATA_DIR` to its mount path (e.g. mount a volume at `/data` and
set `PAPERBOT_DATA_DIR=/data`). Without a volume, the bot still runs
fine -- it just starts its realized-P&L history over on every redeploy.

**Graceful shutdown:** `run_forever()` installs SIGTERM/SIGINT handlers
and saves the ledger before exiting, so a Railway redeploy's stop signal
doesn't lose recent settlements.

## Safety

- **Paper-only by default.** `paperbot/config.py`'s `get_trading_mode()`
  defaults to `PAPER`. Setting `POLYMARKET_TRADING_MODE=live` does **not**
  enable live trading -- it raises immediately, on purpose, because this
  codebase contains no live order-submission path.
- **No private key handling anywhere in this codebase.** There is no
  wallet, signer, or key-management code to find, because none exists.
- **Fail closed.** Any unexpected exception while processing one market
  is caught, logged clearly, and that market's activity halts
  (`halted_conditions`) rather than continuing in an unknown state. Other
  markets are unaffected.

## What was simplified or approximated, and why

This is a from-scratch build done in an environment with **no outbound
network access to polymarket.com hosts** (verified: DNS resolution for
`docs.polymarket.com`, `gamma-api.polymarket.com`, and
`clob.polymarket.com` all failed from this sandbox; only a pip/PyPI proxy
was reachable). That constrains what could actually be verified end to
end:

- **Platform mechanics** (Gamma API shape, CLOB `/book` and `/tick-size`
  endpoints, WS message schemas, `postOnly` rejection semantics) were
  verified against Polymarket's own documentation and the official
  `py-clob-client` source **via cached/mirrored pages** (GitHub, search
  results), not by hitting the live endpoints directly. One useful detail
  surfaced this way that wasn't in the original spec: the WS market-channel
  subscription needs `"custom_feature_enabled": true` to actually receive
  `last_trade_price` events -- without it you only get `book`/
  `price_change`, and the fill simulator has nothing to consume against.
  This is wired into `book.py`.
- **Four of six slug prefixes are unverified assumptions.** Only BNB
  (`bnb-updown-5m-`) and Hyperliquid (`hype-updown-5m-`) slugs are
  confirmed, because those are the only two that appeared in the raw
  historical data sample used to build `market_discovery.py`. Bitcoin,
  Ethereum, Solana, and Dogecoin follow the same observed
  `<ticker>-updown-5m-<epoch>` convention but are flagged as
  ASSUMPTION-TO-VERIFY in `config.py`. **Verify these against a live
  Gamma pull before trusting discovery for those four assets.**
  Update `paperbot/config.py`'s `ASSET_SLUG_PREFIXES` if they differ.
- **The network-facing code paths (WS connect, Gamma/CLOB REST calls)
  could not be live-tested.** They're implemented per the verified
  contracts above and are structured so the actual decision logic they
  feed (message parsing/application in `book.py`, market parsing in
  `market_discovery.py`) is pure and unit-tested with synthetic payloads
  -- but a live connection is the remaining integration smoke test before
  trusting this unattended. Treat the first real run as that test.
- **First-entry side (Up vs Down) is a 50/50 coin flip.** The historical
  data confirmed side *persistence* for subsequent entries in an
  already-active market, but not what determines the very first entry's
  side -- there was no parameter for that. Documented in
  `strategy.decide_side`.
- **Sizing uses the confirmed medians directly, with a small ±15% jitter**
  (`SIZING_JITTER_FRACTION`), rather than sampling from a full
  distribution -- only medians were measured, not full distributions, so
  reproducing a realistic-looking spread around them is a modeling choice,
  not a second confirmed data point.
- **BNB's floor-lot probability is a position-dependent approximation,
  not a second confirmed measurement.** Only Hyperliquid's rise-with-
  position floor-lot curve was directly measured at each position tier;
  BNB's aggregate float-lot share (~50-70%) was confirmed but not broken
  out by position. BNB's per-position curve in `behavior_config.py` is
  modeled after Hyperliquid's confirmed shape and tuned to blend to the
  right aggregate -- flagged explicitly in a code comment, not presented
  as equally confirmed.
- **Market outcome resolution polls Gamma for `outcomePrices` after
  close** and treats a price >= 0.90 as decisive. This is a reasonable
  reading of how Polymarket exposes resolution via Gamma, but (like the
  rest of the network layer) wasn't exercised against a live resolving
  market.
- **The weakness/strength gradient is used only as a soft tiebreak**
  (`strategy.gradient_score`) for ranking which of several
  simultaneously-eligible opportunities to act on first in a given bot
  cycle -- per spec, it's explicitly not a hard filter, and a single
  token's price can only occupy one regime band at a time, so there was
  no natural hard-filter interpretation to fall back to anyway.

## What the test suite covers

97 tests, `python3 -m pytest tests/ -v`:

- **Band classification** at the exact 0.30/0.70/0.90 boundaries.
- **Per-asset sizing curves are genuinely distinct** -- a test that fails
  if the six per-asset tables were accidentally collapsed into one shared
  table, plus explicit checks that the confirmed Dogecoin/BNB
  increasing-size exceptions are preserved, not smoothed away.
- **`QUEUE_SAFETY_FACTOR` actually discounts the raw summed depth**,
  including a test that reproduces the shape of the real prior bug
  (factor=1.0 -> a trade print equal to the visible depth still produces
  zero fills).
- **Fill simulation**: queue consumption across multiple trade prints, a
  fill that exactly exhausts remaining order size, an order that expires
  unfilled at the 90-second cutoff, and a partially-filled order that
  keeps its fill (for later settlement) instead of being mislabeled
  `EXPIRED_UNFILLED` -- this exact distinction was a bug this build caught
  and fixed during its own smoke testing (see below).
- **Hyperliquid's floor-lot probability strictly increases with entry
  position** in every regime (the clearest confirmed case), plus BNB's
  approximated curve.
- **postOnly semantics**: the bot never constructs an order priced to
  cross the spread, checked via `would_cross_spread`/`safe_postonly_price`
  directly, not just assumed.
- **Realized P&L recomputes correctly from settlement records** and does
  not drift when computed twice with no new settlements, plus a direct
  check that there is no mutable running-total field to accidentally
  increment from two places.
- **Cross-module wiring** (`tests/test_bot.py`): one-order-per-market
  policy, the 90-second timing gate end to end, a fail-closed check that
  an exception in one market halts only that market, and a graceful-
  shutdown test that sends the running process a real `SIGTERM` and
  confirms `run_forever()` stops promptly rather than hanging until
  Railway (or any host) has to `SIGKILL` it.

### Two real bugs this build caught in its own pre-commit testing

Both were caught by running the test suite and an offline integration
smoke test before considering the build done, exactly per Part 7's
instructions:

1. **A dataclass default-sentinel collision** in `SimulatedOrder`:
   `remaining_size` defaulted to `0.0` as an "unset" sentinel, which is
   indistinguishable from a caller legitimately constructing an
   already-fully-filled order with `remaining_size=0.0` explicitly --
   the sentinel logic silently reset it back to the full original size,
   zeroing out real fills. Fixed by using `None` as the sentinel and
   deriving `filled_size` from the fills log directly rather than from
   `original_size - remaining_size`.
2. **A stale `resting_order_id` tracking bug** in `bot.py`: the
   one-order-per-market bookkeeping only cleared an entry when
   `fill_simulation.manage_open_orders` reported a status change
   (expiry/reprice/cancel) -- but an order that fills via a
   `last_trade_price` print during the trade-drain phase never appears in
   that list, so a filled market would stay permanently marked as "has a
   resting order" and never trade again. Fixed with an unconditional
   sweep over tracked orders' actual current state every tick. Covered by
   `tests/test_bot.py::TestRestingOrderIdClearsOnFillViaTradePrint`.

## Provenance

`behavior_config.py`'s tables are calibrated on `trade_behavioral_analysis.json`,
produced by `analyze_trades.py` from a 778,116-trade historical dataset
(the raw `trade.jsonl` is gitignored -- ~684MB, not something to version).
