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
tests/                 123 tests covering the above
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
- **All six slug prefixes are now live-confirmed, not just BNB and
  Hyperliquid.** Only those two appeared in the raw historical data
  sample used to build `market_discovery.py`; Bitcoin, Ethereum, Solana,
  and Dogecoin were flagged as ASSUMPTION-TO-VERIFY. The second live
  deploy's logs showed clean `ONBOARD`/`RETIRE` cycles for all six assets
  as their 5-minute windows rolled over, confirming every prefix in
  `config.py`'s `ASSET_SLUG_PREFIXES` as-is. Nothing to fix here.
- **The network-facing code paths were implemented against verified
  contracts but genuinely couldn't be exercised from the build
  environment** (no outbound network to any polymarket.com host from that
  sandbox). Real Railway deployments were the actual integration test,
  and found real bugs on both of the first two tries -- see "Bugs found
  and fixed via live Railway deploys" below. Message parsing/application
  logic (`book.py`, `market_discovery.py`) was and remains pure and
  unit-tested with synthetic payloads regardless of live network status.
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

123 tests, `python3 -m pytest tests/ -v`:

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

### Bugs found and fixed via live Railway deploys

This build environment has no outbound network to any polymarket.com
host, so the network-facing layer (`book.py`, `market_discovery.py`) was
implemented against documented contracts but genuinely couldn't be
exercised end to end before shipping. Real deployments then did that
testing for real, and found real bugs -- logged here as they were found
and fixed, deploy by deploy.

**Deploy 1: discovery crashed outright.**

1. **Market discovery paginated the wrong endpoint and crashed.**
   `fetch_active_markets` originally called `GET /markets?active=true&closed=false`
   and paginated with an increasing `offset` until a short page came back.
   That endpoint returns *every* active market on the entire platform, not
   just these six crypto markets, and in production Gamma started
   returning `422 Unprocessable Entity` once the offset got deep enough
   (observed at `offset=2100`) -- likely a pagination-depth cap. Every
   poll cycle crashed, discovery never once succeeded, and the bot never
   placed an order.

   **Fix:** query Gamma by exact `slug` for each asset's current and next
   5-minute window directly (`market_discovery._candidate_slugs`), reusing
   the deterministic `<prefix><epoch-bucket-start>` naming already relied
   on elsewhere in this codebase. This replaced dozens of paginated
   full-table requests per poll with exactly 12 (six assets x two
   buckets), and is structurally immune to the pagination-depth issue
   since there's no `offset` involved at all.

2. **The WebSocket got closed with `1008 policy violation: invalid
   subscription payload`, repeatedly.** Given discovery never succeeded
   even once, `MarketWebSocketClient` never had any token ids to
   subscribe with -- so every reconnect opened a connection and sent
   nothing, which the server closed. `run_forever` now runs one discovery
   pass *before* opening the WebSocket at all, so subscriptions are ready
   the moment it connects instead of racing the first poll.

Covered by `tests/test_market_discovery.py::TestFetchActiveMarketsUsesSlugLookupNotPagination`
(including a direct regression test that a `422` on one asset's slug
lookup doesn't abort the other five).

**Deploy 2: discovery fix confirmed working -- all six slug prefixes
turned out correct, no code changes needed there. Two new bugs surfaced
in the book/WS layer once markets actually started onboarding:**

3. **`ValueError: could not convert string to float: 'size'`, crashing
   every book bootstrap.** `BookState.apply_snapshot` assumed each book
   level was a `[price, size]` pair and did `for p, s in bids`. Real
   Polymarket levels are objects: `{"price": "0.20", "size": "10"}`.
   Unpacking a dict with `p, s = level` binds it to its *keys* -- so
   `p="price"`, `s="size"` -- and `float("size")` throws exactly this
   error. This hit every single market, on both the REST bootstrap and
   the WS `book` snapshot (same code path), so no book state was ever
   built and every market got halted immediately after onboarding.

   **Fix:** `_extract_level()` normalizes either shape (object or pair)
   into a `(price, size)` float tuple. Covered by
   `TestBookStateSnapshot::test_apply_snapshot_handles_real_polymarket_object_shaped_levels`.

4. **The WebSocket kept closing with a plain-text `INVALID OPERATION`
   (not the documented JSON schema) immediately after each new market
   onboarded.** The pattern: one `ONBOARD` log line, immediately followed
   by one `INVALID OPERATION`, repeating per market. `subscribe()` was
   sending a fresh `{"assets_ids": [...], ...}` message on the live
   connection every time a new market appeared, in addition to the one
   sent at connect time. The market channel appears to accept exactly
   *one* subscription message per connection -- any further message on
   the same connection gets rejected.

   **Fix:** `subscribe()` no longer sends anything on an already-open
   connection. When new token ids need to be added, it closes the
   connection instead; `_ws_supervisor`'s existing reconnect loop opens a
   fresh one immediately, which sends a single consolidated message with
   the complete, updated id set (this is what `connect_and_run` already
   did at initial connect time -- now it's also how every subsequent
   addition happens). Covered by
   `TestSubscribeNeverSendsASecondMessageOnALiveConnection`.

Both deploy-2 fixes were confirmed live on the next deploy: clean
onboarding across all six assets, `PLACE`/`REPRICE`/`FILLED` log lines
flowing normally, no further `INVALID OPERATION` or float-parsing errors.

**Deploy 2 logs also caught one more bug** while the pipeline was
otherwise working correctly:

5. **`FILLED order=9 ... time_to_fill=-0.1s`** -- a fill can't take
   negative time. Root cause: `pending_trades` is drained once per
   main-loop tick, *after* `strategy_tick` may have placed a brand-new
   order that same tick. A trade print timestamped before that order
   existed could still be sitting undrained and get applied to it once
   placed -- but that trade is already reflected in the book snapshot the
   order's `queue_ahead` was measured against, so letting it also
   fill/drain-queue for an order it predates is both physically
   impossible and pollutes the fill-rate/time-to-fill calibration stats.

   **Fix:** `on_trade_print` now excludes any order with
   `placed_at > trade.ts` from the candidate list entirely. Covered by
   `TestTradesBeforeOrderPlacementAreIgnored`.

**Deploy 3: the pipeline was placing, repricing, and filling orders
correctly -- but resolution/settlement had never once succeeded, and the
logs revealed why.**

6. **An unbounded resolution backlog self-inflicted a 429 storm and
   flooded Railway's own log ingestion.** `resolution_tick` retried
   *every* pending market on *every* 2-second tick, forever, with no
   backoff or cap. Over several hours of uptime the backlog grew into the
   hundreds (5-minute markets across six assets accumulate fast when
   nothing ever resolves), and each tick fired that many synchronous
   Gamma requests. The result: `429 Too Many Requests` on effectively
   every resolution lookup, `Railway rate limit reached for deployment,
   ... Messages dropped: 483` (our own full-traceback-per-market-per-tick
   logging was itself dense enough to get throttled by Railway's log
   ingestion), and -- since every lookup was failing before it could even
   be inspected -- zero visibility into whether the resolution logic
   itself worked.

   **Fix:** `resolution_tick` now enforces, per market: a minimum
   `RESOLUTION_RETRY_COOLDOWN_SECONDS` (30s) between attempts, growing via
   `RESOLUTION_BACKOFF_ON_FAILURE_SECONDS` on repeated failure; a hard
   `RESOLUTION_MAX_ATTEMPTS_PER_TICK` (3) cap so a large backlog is worked
   down gradually instead of all at once; and a
   `RESOLUTION_MAX_AGE_SECONDS` (2h) give-up so a persistently-broken
   lookup can't grow the backlog (and request rate) forever -- an
   abandoned market's filled orders simply stay unsettled and excluded
   from realized P&L, logged explicitly rather than silently dropped.
   Failure logging switched from a full traceback per attempt to one
   concise warning line. Covered by `TestResolutionThrottling` (attempt
   cap, cooldown, backoff growth, max-age abandonment, and a successful
   end-to-end resolution).

   **This was also the fix for "no P&L visible in the logs"**: since
   resolution never succeeded, `ledger.settle_order`'s existing per-
   settlement log line never fired either. On top of unblocking that, a
   `RESOLVED market=... winning_side=... settled_orders=N
   realized_pnl_total=...` line now logs on every successful resolution,
   and a `PNL_SUMMARY` heartbeat (`PNL_SUMMARY_INTERVAL_SECONDS`, default
   5 min) logs running realized P&L, open-order count, and per-asset
   breakdown independent of any individual settlement -- so P&L is
   visible during quiet stretches too, not just at shutdown.

**Deploy 4: the throttling fix worked perfectly (zero errors, clean ~30s
cadence, sane backlog growth) -- but 29 minutes and 36 pending markets
in, `settled_trades` was still 0. The oldest market's price had been
fully decisive (`["0.9995", "0.0005"]`) and stable for 15+ minutes, and
Gamma's `closed` flag had simply never flipped.**

7. **`parse_resolution`'s hard requirement on Gamma's `closed` flag turned
   out to be the wrong signal for this market type.** Confirmed live:
   `closed` can stay `False` for 30+ minutes past a 5-minute market's
   trading window ending -- possibly much longer, possibly never via this
   specific `/markets?slug=` query -- even while `outcomePrices` are
   already fully decisive and have stopped moving. Gating resolution on
   `closed` alone meant the backlog only grew (36 markets and climbing,
   zero ever settling) and every filled order sat unsettled indefinitely.

   **Fix:** `infer_resolution_from_price()` is a new fallback, used only
   when `parse_resolution` (the `closed`-gated path) returns None. It
   trusts a very decisive price (>=99%, stricter than the 90% bar used
   when `closed` is confirmed True) with no `closed` requirement at all.
   This is safe specifically because `resolution_tick` only ever calls it
   for markets already in `pending_resolution` -- i.e. markets this bot's
   own state machine has independently confirmed finished trading (they
   rolled out of `_candidate_slugs`'s current/next window). It is not
   used, and would not be safe to use, for a market still considered
   active. Covered by `TestInferResolutionFromPrice` and two `bot.py`
   integration tests (resolves via price when `closed=False`; does NOT
   resolve on an indecisive price even without `closed`).

   **Separately observed, not yet acted on:** the WebSocket periodically
   disconnects with `1013 (try again later) slow consumer: send buffer
   full` (a few times over 30 minutes). The existing reconnect-with-
   backoff in `_ws_supervisor` handles this cleanly with no data loss or
   crash, so it isn't blocking anything -- but it suggests the event
   loop occasionally isn't draining incoming WS messages fast enough
   (candidate cause: per-message book-level re-sorting, or onboarding
   bursts of sequential REST calls at market rollovers). Flagged here as
   a known, currently-benign issue rather than a silent one.

## Provenance

`behavior_config.py`'s tables are calibrated on `trade_behavioral_analysis.json`,
produced by `analyze_trades.py` from a 778,116-trade historical dataset
(the raw `trade.jsonl` is gitignored -- ~684MB, not something to version).
