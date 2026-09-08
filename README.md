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

## Dual-sided hedge behavior

Confirmed from the full 778,116-trade historical dataset (`deep_analysis.py`,
committed in this repo): when the trader's early second entry into a
market lands on the OPPOSITE side from the first, it's a deliberate
insurance leg, not noise or a persistence-roll failure --

- **Single-sided markets**: worst-case outcome is always -100% of stake
  (no protection -- trivially true of any pure directional bet).
- **Dual-sided markets**: worst-case outcome averages **-20.5%** of stake
  (median -27.6%), and **24.9%** of them are outright arbitrage --
  guaranteed profit regardless of which side wins.
- The dominant pattern (~31% of dual-sided markets) is a confident
  primary bet in CORE/HIGH paired with a cheap insurance leg in CHEAP on
  the other side. Trigger probability and typical hedge size (as a
  fraction of the primary side's cost) vary by asset and by which regime
  the primary entry landed in -- see `HEDGE_TRIGGER_PROBABILITY` and
  `HEDGE_SIZE_RATIO` in `behavior_config.py`.

**Implementation** (`strategy.decide_hedge`, wired into
`build_order_intent`): modeled as ONE roll, at the market's second entry
only, keyed by (asset, regime of the first entry). If it doesn't trigger
there, the market is not re-rolled at every later entry -- that matches
the real per-market hedge frequency rather than compounding a smaller
probability across many entries into an inflated one. When triggered,
the hedge leg's size is `HEDGE_SIZE_RATIO[asset][primary_regime] *
(dominant side's cumulative cost so far)` -- proportional to what it's
protecting, not the ordinary (asset, regime, position-index) sizing
curve used for every other entry. `OrderIntent.is_hedge` /
`SimulatedOrder.is_hedge` / `SettlementRecord.is_hedge` carry the flag
through placement, fills, and settlement, and `Ledger.hedge_summary()` +
the periodic `PNL_SUMMARY` log line report hedge vs. normal trade P&L
separately -- the live signal for whether the bot's simulated hedges are
actually reproducing the confirmed better-worst-case property, not just
firing.

## Architecture

```
paperbot/
  config.py            endpoints, safety gate, tunable constants
  behavior_config.py   per-asset regime/sizing/persistence/floor-lot/hedge tables
  market_discovery.py  Gamma API polling -> Market objects
  book.py              CLOB REST bootstrap + WS live book state per token
  strategy.py          regime/availability/side/sizing/timing/hedge decisions
  fill_simulation.py   the paper fill model (queueing, drift, expiry)
  ledger.py            settlement + realized P&L (recomputed, never cached)
  bot.py               main loop wiring it all together
tests/                 156 tests covering the above
run_bot.py             CLI entry point
deep_analysis.py       standalone script: win/loss economics + hedge
                        behavior analysis on trade.jsonl (not part of the
                        bot itself -- see "Dual-sided hedge behavior" below)
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

## Deploying on AWS EC2

`deploy/aws/user-data.sh` is the exact cloud-init bootstrap used to set
this up: installs Python + git on a bare Ubuntu 24.04 LTS AMI, clones
this repo, creates a venv, and runs the bot under a systemd unit with
`Restart=on-failure` -- the AWS-side equivalent of Railway's
`restartPolicyType`.

```
aws ec2 run-instances \
  --image-id <latest Ubuntu 24.04 amd64 AMI for your region> \
  --instance-type t3.micro \
  --key-name <your key pair> \
  --security-group-ids <sg allowing 22/tcp> \
  --subnet-id <a subnet in your VPC> \
  --user-data file://deploy/aws/user-data.sh \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":10,"VolumeType":"gp3"}}]'
```

- **Persistence is automatic here**, unlike Railway: an EC2 instance's
  root EBS volume survives stop/start/reboot on its own (just never
  *terminate* it) -- no separate volume-attachment step needed for
  `PAPERBOT_DATA_DIR` to persist across restarts.
- **Access**: no inbound port is required for the bot itself (outbound-
  only, same as always); this setup opens 22/tcp for SSH management,
  restricted to key-based auth. [AWS Systems Manager Session
  Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html)
  is a good alternative if you'd rather not open any inbound port at all
  -- swap the security group for an IAM instance profile with
  `AmazonSSMManagedInstanceCore` instead.
- **Logs**: `journalctl -u paperbot -f` (systemd captures both stdout
  and stderr into the journal, same effect as Railway's log viewer).
- **Redeploying a code change**: SSH in, `cd /opt/paperbot/app && git
  pull && systemctl restart paperbot` -- there's no auto-deploy-on-push
  wired up the way Railway's GitHub integration provides; either re-run
  that manually or set up your own CI hook if you want one.

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

156 tests, `python3 -m pytest tests/ -v`:

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
- **Hedge leg**: fires only at exactly the market's second entry (never
  re-rolled at later entries, never before a first entry exists), uses
  the opposite side from the dominant one, sizes off the dominant side's
  cumulative cost rather than the ordinary position-count curve, and a
  full pipeline test (`TestHedgeLegEndToEnd`) confirming it end to end
  through `decide_hedge -> build_order_intent -> place_order`.

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

**Deploy 5: the price-fallback resolution fix confirmed working end to
end** -- real settlements landed with real P&L (e.g. 12 filled Bitcoin
orders settling to -$11.95 on one market). While confirming that, the WS
"slow consumer" disconnect flagged as benign above recurred twice more,
both times within seconds of a 5-minute market rollover -- enough of a
pattern to fix rather than continue merely documenting:

8. **Onboarding a rollover batch (up to 6 markets x 2 tokens = 12
   blocking REST round trips) awaited them sequentially, one after
   another, all within a single coroutine.** The event loop can
   technically interleave other coroutines during any single await, but a
   long chain of them back-to-back gave the WS reader task repeated
   opportunities to fall behind, and it did -- confirmed live, twice, both
   times seconds after a batch of `ONBOARD` lines.

   **Fix:** `_onboard_markets` (was `_onboard_market`, singular) now
   bootstraps every token across the WHOLE batch concurrently via
   `asyncio.gather` over `asyncio.to_thread`, and subscribes once with the
   complete set of new token ids instead of once per market (which also
   incidentally removed some redundant same-tick WS-close calls from the
   old per-market subscribe pattern). A failure bootstrapping one market's
   token still only halts that market -- covered by
   `TestBatchedOnboarding`.

   **Confirmed live within minutes** -- the very next rollover showed all
   12 `ONBOARD` lines land at the identical millisecond timestamp
   (`21:39:18,860`), proving the batch really does bootstrap concurrently
   now rather than one-token-at-a-time. It also immediately surfaced a
   side effect of its own success:

9. **A burst of `Connection pool is full, discarding connection:
   clob.polymarket.com. Connection pool size: 10` warnings** appeared
   right after the concurrency fix landed. Firing ~12 concurrent
   bootstrap tasks (each making 2 sequential REST calls) against one
   shared `requests.Session` exceeded its default `HTTPAdapter`'s
   10-connections-per-host pool. Not a functional bug -- `urllib3` just
   opens a fresh connection instead of reusing one, and the bot kept
   trading normally through it (`PLACE` orders logged immediately after)
   -- but noisy and easy to fix properly rather than leave as a
   self-inflicted warning storm on every rollover from now on.

   **Fix:** the bot's shared session now mounts an `HTTPAdapter` sized to
   30 connections per host (comfortable headroom over the ~12-connection
   worst case), via `_make_session()`. Covered by
   `TestSessionConnectionPoolSizing`.

**Deploy 6: the most serious bug in this log.** Trading, resolution, and
P&L were all confirmed working, and then a single settlement jumped
realized P&L from -$16.46 to +$1,297.27 -- one order, `entry_cost=$40.63,
payout=$1,354.36`. The order that produced it: `PLACE ... asset=Bitcoin
regime=HIGH ... side=Down price=0.0300 size=1354.360918`. **A "HIGH"-regime
order priced at $0.03 is internally impossible** -- HIGH is 0.90-1.00 --
which is what led to finding this:

10. **Every order's regime, price, and sizing were computed from the Up
    token's book, regardless of which side (`Up`/`Down`) actually got
    traded.** `_evaluate_one_market` fetched only `token_id_up`'s book and
    passed it into `build_order_intent` as *the* book; `build_order_intent`
    used it for `classify_regime`, `availability_check`, and
    `decide_size` unconditionally, then only used the correct Down token
    for the final `token_id`. The Down token's real price is (roughly)
    `1 - Up's price` -- not the same value -- so whenever `decide_side`
    picked "Down" while Up was, say, 0.95 (HIGH), the order was priced and
    sized as a HIGH-band trade using Up's 0.95, while Down's *real* price
    was ~0.05 (CHEAP). The postOnly safety check then silently
    "corrected" the price down to Down's actual book (~0.03) to avoid
    constructing a crossing order -- without correcting the regime or the
    notional that had already been sized off the wrong curve. Result:
    `notional / price` with a HIGH-sized notional and a CHEAP-sized price
    produces a wildly oversized order (1354 shares from what should have
    been roughly a 15-40 unit HIGH-regime notional).

    This affected **every single Down-side order since this bot went
    live** -- roughly half of all trades, given side is close to a coin
    flip on a market's first entry. It's the reason several earlier
    `PLACE ... regime=X ... price=Y` log lines looked internally
    inconsistent in hindsight (X's band and Y didn't match) -- that
    inconsistency was sitting in the logs the whole time; it just took an
    extreme case (a near-1300-share order) to be obvious enough to
    investigate.

    **Fix:** `build_order_intent` now requires both `up_book` and
    `down_book`, decides `side` *first*, then uses whichever book actually
    corresponds to that side for `classify_regime`, `availability_check`,
    and `decide_size` -- not just for the final resting price. Covered by
    `test_regime_price_and_sizing_come_from_the_chosen_sides_book`, which
    reproduces the exact live scenario (Up=0.95/HIGH, Down=0.04/CHEAP,
    side forced to Down) and asserts the intent is correctly CHEAP-priced
    and CHEAP-sized, not HIGH-sized.

    **Not addressed, flagged for awareness:** the still-open Bitcoin
    position this bug produced live did go on to settle profitably (Down
    won), so the paper ledger's +$1,297 isn't itself wrong or reversed --
    but it's a real position that was sized by a bug, not by the intended
    calibration, and shouldn't be read as a signal about strategy
    performance. Anyone reviewing this bot's historical paper P&L should
    treat everything settled before this fix's deploy timestamp as
    unreliable for that reason.

**Deploy 7: the hedge feature deployed cleanly and confirmed firing live**
(`hedge=True` orders observed across four assets, correctly gated to
exactly the market's second entry) -- **and running long enough (~90
minutes) surfaced a real resolution-throughput bug that only shows up at
scale**:

11. **`resolution_tick` iterated its backlog in dict/insertion order,
    which meant a handful of persistently-indecisive OLD markets --
    nothing had ever removed them except eventual resolution or the 2h
    abandon -- sat at the front of the dict forever and won the
    `RESOLUTION_MAX_ATTEMPTS_PER_TICK` budget every single tick.** Every
    market retired after them got starved of attempts entirely.
    Confirmed live: `settled_trades` sat frozen at 355 for 20+ minutes
    while `pending_resolution` grew from 54 to 70, and a cluster of newer
    markets hit `ABANDONED` without ever having been checked once.

    **Fix:** each tick now sorts candidates by least-recently-attempted
    (never-attempted counts as oldest) instead of dict order, so every
    market gets fair rotation through the budget regardless of backlog
    size or how long any single entry has been stuck. Also added explicit
    logging for the previously-silent "Gamma returned nothing for this
    slug" case (distinct from "found it, not decisive yet") -- possibly
    relevant if archived markets stop appearing in this query once fully
    settled, though that's not confirmed, just now visible if it's
    happening. Covered by
    `test_persistently_indecisive_old_markets_do_not_starve_newer_ones`.
    150 tests passing.

**Pre-emptive: an unbounded-memory audit before moving off Railway.**
Railway's frequent restarts (redeploy on every push, plus the trial-plan
restarts observed in deploy 7) accidentally masked a real class of bug:
several structures were never cleaned up as markets came and went, fine
for a process that gets restarted often, a real problem for a
deployment meant to stay up for weeks (the whole point of moving to a
platform with a real persistent disk). Found by audit, not by a live
symptom, before it could become one:

12. **`self.activity`, `self.last_price_by_token`, and
    `self.halted_conditions` in `bot.py` grew by one entry per market
    forever** -- nothing ever removed a market's entries once it retired.
    **`FillSimulator.orders` grew by one entry per order placed, forever**
    -- nothing ever removed a terminal (filled/expired/cancelled) order
    once its market was fully settled or abandoned. At the trading volume
    seen in earlier live deploys (hundreds of orders/hour), this would
    accumulate into a real memory problem over weeks of uptime, not hours.

    **Fix:** `_retire_market` now cleans up the three per-market dicts the
    moment a market leaves `markets_by_condition` (none of it is needed
    again -- `strategy_tick` will never evaluate that market again).
    `FillSimulator.remove_orders_for_condition()` prunes a market's
    orders once `resolution_tick` is actually done with them (settled or
    abandoned) -- not at retire time, since a resting order can still be
    open for a few ticks while expiry catches up. Documented trade-off:
    `stats_by_asset_regime()` (not currently wired into any live logging)
    becomes a rolling view instead of a lifetime one, since it iterates
    `self.orders` directly. Also removed `self._last_price_delta`, a
    duplicate, entirely-unused leftover of `self.last_price_by_token`
    caught by the same audit. Covered by
    `TestBoundedMemoryForLongRunningDeployment` and
    `TestRemoveOrdersForCondition`. 155 tests passing.

13. **Found by a live vs. real-trader comparison, not a symptom in
    isolation: `manage_orders_tick` drained trade prints into
    `on_trade_print` BEFORE calling `manage_open_orders` (drift/cancel).**
    A same-window comparison against the real trader's own trades (see
    `trader_intel/`) surfaced a stark gap: the paperbot's CORE-regime win
    rate was 62% and HIGH-regime was 72%, vs. the real trader's 91%/98%
    in the *identical* time window (and 93%/99.7% historically) --
    CORE/HIGH's entire edge in the real strategy is a win rate far above
    what the entry price implies, so a >15pp shortfall there isn't
    variance, it's a real problem, and it was costing real (simulated)
    money: -$715 realized total after ~5h uptime, with Bitcoin alone at
    -$693 across all four regimes.

    Root cause: `on_trade_print`'s eligibility check
    (`trade.price <= order.price`) is deliberately permissive -- a
    resting bid legitimately fills against any trade at or below it,
    matching real price-time priority. But `book.best_bid` is updated
    immediately and independently by `book`/`price_change` WS events
    (`apply_last_trade_price` never touches it), so by the time a tick
    runs, the book already reflects the latest state either way -- and
    draining trade prints *before* checking whether price had already
    left the order's target regime band meant a stale HIGH/CORE order
    (e.g. resting at 0.95) could get filled by a same-tick trade print
    reflecting a much lower, unrelated price level one tick before
    `manage_open_orders` would have cancelled it for having left its
    band. Fixed by reordering: cancel-on-band-exit now runs first, using
    the already-current book state, closing the same-tick race.
    Regression test (`TestDriftCancelRunsBeforeTradePrintDrain`)
    constructs the exact race and confirms it fails under the old
    ordering (order ends up FILLED) and passes under the new one (order
    ends up CANCELLED). 156 tests passing.

    This was the highest-priority fix from the direct real-trader
    comparison built in `trader_intel/compare_bot_vs_trader.py` -- see
    that tool's README for the full diagnostic (regime-level win rate
    vs. both the live real trader and their full historical numbers).

14. **Deliberately excluded: BNB, Dogecoin, and Hyperliquid.** First
    found for BNB alone (last real trade 2026-08-23) and deployed as
    `--assets Bitcoin Ethereum Solana Dogecoin Hyperliquid` -- but that
    was based on checking only BNB's own last-trade timestamp, not all
    six assets'. **Corrected the same day** after actually checking
    every asset: Dogecoin and Hyperliquid stopped even earlier than BNB
    (both 2026-08-06, 17 days before BNB) -- a progressive narrowing from
    six assets down to three, not a single BNB-specific event. Confirmed
    real, not a mirror artifact (per-asset trade counts all exceed their
    historical totals from continued earlier activity, they just stopped
    accumulating new trades after their respective last dates -- see
    `trader_intel/README.md`'s status log for the full verification).
    The bot is now deployed with `--assets Bitcoin Ethereum Solana`
    (both the live systemd unit and `deploy/aws/user-data.sh`).
    `behavior_config.py`'s tables for all three dormant assets are left
    in place (not deleted) in case the real trader resumes any of them
    -- an automated watch (`trader_intel/fetch_trades.py`'s
    `check_dormant_asset_resumption`, now covering `bnb`, `doge`, `hype`)
    flags any new trade on a watched-dormant asset the moment it
    reappears in the live mirror, at which point re-enabling it here
    would just mean adding it back to `--assets`.

15. **Solana-specific recalibration.** Comparing the real trader's
    RECENT (post-data-gap) behavior against `behavior_config.py`'s
    historical calibration found Bitcoin and Ethereum's own regime
    distributions still close to their historical numbers, but Solana's
    meaningfully drifted (CHEAP -12.5pp, HIGH +6.3pp -- confirmed real
    at this sample size, not noise). Recalibrated only the well-supported
    parameters (n>=500 per cell): `ASSET_REGIME_DISTRIBUTION_PCT`,
    `SIDE_PERSISTENCE` (86.34%->81.64%), `GRADIENT_BIAS_PCT`'s
    `CHEAP_follows_drop` (58.12%->62.14%), and `FLOOR_LOT_PROBABILITY`'s
    CHEAP/MID cells. Deliberately did NOT touch `ENTRY_SIZING_USD`,
    `HEDGE_TRIGGER_PROBABILITY`/`HEDGE_SIZE_RATIO`, or CORE/HIGH-specific
    floor-lot/gradient values for Solana -- every one of those needs a
    finer breakdown (regime x position-tier, or market-level dual-sided
    counts) where the post-gap sample is still too thin to trust (some
    cells as low as n=25). This was a scoped, partial recalibration, not
    a wholesale one -- see `trader_intel/recalibrate_solana.py` and
    `trader_intel/README.md`'s status log for the full per-parameter
    breakdown and trust thresholds used.

16. **Closed the blind spot from #15: checked Bitcoin and Ethereum the
    same rigorous way**, rather than trusting the earlier "their coarse
    regime distribution looked close enough" read. Found a real, smaller
    shift in both -- not as large as Solana's, but genuine, not noise
    (large samples: Bitcoin n=9,248, Ethereum n=2,998 post-gap).
    Recalibrated: both assets' `ASSET_REGIME_DISTRIBUTION_PCT`; Bitcoin's
    `ENTRY_SIZING_USD` 4th_plus cell for all four regimes (a real,
    substantial drop -- CORE -32%, HIGH -37% -- verified NOT a grouping
    bug despite CORE's first/2nd_3rd/4th_plus recent medians briefly
    looking suspiciously identical, traced to heavy value-duplication in
    the raw trade-size data, not a code issue); Ethereum's CHEAP 4th_plus
    cell; Bitcoin's `SIDE_PERSISTENCE` (91.17%->93.48% -- notably the
    OPPOSITE direction from Solana's, which dropped -- confirms this
    isn't one uniform market-wide trend); both assets' `GRADIENT_BIAS_PCT`;
    Ethereum's `FLOOR_LOT_PROBABILITY` CHEAP/MID cells (one increased, one
    decreased -- also not uniform). Left alone where checked-but-thin:
    Bitcoin/Ethereum's first/2nd_3rd entry-sizing cells, Ethereum's
    persistence (barely moved, not worth touching), most CORE/HIGH-
    specific cells, and hedge tables for all three active assets (even
    Bitcoin only has ~340 post-gap markets -- too thin split across 4
    regimes). See `trader_intel/recalibrate_asset.py` (generalized from
    `recalibrate_solana.py`) and `trader_intel/README.md`'s status log.

17. **Investigated what triggers the trader's strategy changes (asset
    add/drop events) -- two plausible-looking hypotheses, both died
    against a baseline check.** "A losing streak triggers the change":
    every one of the 7 known add/drop events was preceded by negative
    10-day PNL -- but so was every one of 6 random non-event 10-day
    windows sampled for comparison (several *more* negative than the
    event windows), so negative PNL is just this trader's normal
    baseline, not a signal. "He only trades while an asset is rising":
    checked both a micro proxy (this trader's own 5-min market
    resolution mix, flat ~48-53% Up in every window, no signal) and
    actual macro price action via news -- and found the opposite of the
    hypothesis in two places: Bitcoin and Solana were both *added*
    mid-crash (Solana during its worst stretch on record, "10
    consecutive down months, unprecedented"), and Solana was *kept*
    trading continuously through that same historic decline while
    Dogecoin/Hyperliquid were dropped during a comparable one. Neither
    hypothesis survived a proper control -- documented in full in
    `trader_intel/README.md`'s status log rather than presented as
    confirmed.

    Built this discipline into the automated pipeline rather than
    leaving it as a one-off manual check: `trader_intel/
    trigger_angle_miner.py` auto-detects asset add/drop events (seeded
    with the 7 known ones, picks up future ones on its own), and for
    each event tests a rotating set of candidate explanations (own PNL,
    own win rate, asset trend, dual-sided rate, regime mix, entry
    confidence, activity level) against a distribution of random
    baseline windows -- a finding only counts as a live candidate if it
    sits in the extreme 10% tail of that baseline, exactly the check
    that ruled out both hypotheses above by hand. Tests 3 (event, angle)
    pairs per 6h cycle (rotation, not a full re-scan every time) so
    different angles get covered over time instead of the bot only ever
    re-confirming the same one or two checks; every pair's most recent
    result -- OUTLIER and WITHIN_NORMAL_RANGE alike -- is kept in
    `trigger_angle_report.md` for transparency. One streaming pass over
    the mirror builds small per-(asset, day) aggregates so every window
    query (event + all baselines, every angle) is answered by summing
    those buckets rather than re-scanning the trade file -- confirmed
    live on AWS at ~9s / ~300MB peak. First real run already surfaced a
    nuance the by-hand check missed: Bitcoin's addition was preceded by
    a win-RATE cold streak (14.3% vs a 25.7% baseline, outlier-low) even
    though the $ PNL in that same window was actually *better* than his
    typical baseline (outlier-high, not low) -- a genuinely mixed
    signal, logged as such rather than flattened into one story.

18. **"Why does the bot place so many fewer trades than the trader?"
    Investigated end to end, found three separate causes, fixed all
    three.** A same-window comparison showed the bot placing ~14x fewer
    trades than the real trader (332 vs 4,762 in one clean 8h window).
    Broke that down into markets touched (2.3x fewer) and entries per
    market (6.2x fewer, avg 3.0 vs 18.5) -- then dug into the order
    lifecycle itself rather than stopping at that split:

    - **Root cause of "fewer markets touched": a Gamma resolution
      reliability bug, not a trading-behavior gap.** Order placement/fill
      was already healthy (48.7% of placed orders filled). The real leak
      was downstream: `fetch_market_by_slug`'s unfiltered `/markets?
      slug=X` query stops returning a market a few minutes after its
      window closes -- confirmed live via direct curl against
      gamma-api.polymarket.com on real stuck slugs -- well before Gamma
      internally flags it `closed=true` (which can take 30+ minutes).
      That left a real gap where a market was invisible to BOTH the
      unfiltered query (aged out) and an explicit `closed=true` query
      (not flagged yet) for several minutes, and once a market aged out
      of the unfiltered listing it NEVER came back -- every retry failed
      forever until the 2h give-up cutoff. Result: 103 of 333 onboarded
      markets (31%) were formally `ABANDONED` in one 8h window, silently
      excluding any real fills there from realized P&L entirely -- which
      also means every PNL/ROI number reported earlier that session
      undercounted actual trading activity. **Fixed**: added
      `fetch_market_for_resolution` (market_discovery.py), which tries
      the unfiltered query first, then falls back to an explicit
      `closed=true` query if that comes back empty -- closing the gap
      between the two without changing the fast-path behavior for
      markets caught early. 4 new unit tests (`TestFetchMarketForResolution`)
      cover both branches plus the "still genuinely unresolved" case.

    - **Root cause of "fewer entries per market": a hard cap of 1
      resting order per market.** `bot.py`'s `strategy_tick` skipped any
      market that already had a resting order at all, so a new entry
      only got a chance once the previous one fully resolved (filled,
      cancelled, or expired) -- nothing like the trader's median 13.5 /
      max 105 entries in a single 5-minute market. **Fixed**: raised the
      cap to a configurable `MAX_OPEN_ORDERS_PER_MARKET` (default 3,
      deliberately conservative -- trade data only has fill timestamps,
      not order-placement times, so there's no way to independently
      confirm how many of the trader's own entries were genuinely
      concurrent vs. fast sequential re-entries, and this project isn't
      going to guess straight to the trader's own ceiling). Required
      turning `resting_order_id` (one order per market) into
      `resting_order_ids` (a set per market) throughout `bot.py`; entry
      sequencing (position tier, side persistence, hedge eligibility)
      already keyed off `MarketActivityState`'s placement-time counters,
      not fill-time ones, so it extends to concurrent orders with no
      special-casing. New tests (`TestOpenOrderCapPerMarketPolicy`)
      cover both "places up to the cap then stops" and a cap-of-1
      regression check reproducing the exact old behavior.

    - **Bonus, from the same investigation: made `SIDE_PERSISTENCE`
      regime-dependent for Bitcoin/Ethereum/Solana**, closing a follow-up
      TRADER_PROFILE.md §8 had explicitly flagged but never built ("would
      need a second full calibration pass"). Computed real P(persist |
      the currently-held side's own regime) per (asset, regime) from the
      live mirror (n=7,937-74,500 per cell) -- deliberately conditioned on
      the regime BEFORE the decision (the currently-held side's price),
      not the resulting trade's own regime, since that's the only framing
      knowable at decision time. Genuinely not uniform: Bitcoin's lowest
      persistence is CHEAP (88.3%), Solana's lowest is CORE (79.2%) -- no
      single "switches more here" rule holds across assets, confirming
      this needed real per-cell data rather than a hand-picked rule.
      `decide_side` now takes the held side's live price (both books
      already available in `build_order_intent`) and looks up
      `behavior_config.side_persistence_for(asset, regime)`.
      Dogecoin/Hyperliquid/BNB reshaped to the same per-regime dict
      structure for interface consistency but left byte-for-byte
      unchanged (dormant, same value in all 4 cells) -- not a
      recalibration for those three, just a type change.

    All three changes are deployed live (156 -> 167 tests passing, no
    old-format test hardcoded), verified importing and running clean on
    AWS before the restart. Full diagnostic trail (the curl evidence,
    the order-lifecycle funnel numbers, the regime-conditional
    persistence table) is in `trader_intel/README.md`'s status log.

## Provenance

`behavior_config.py`'s tables are calibrated on `trade_behavioral_analysis.json`,
produced by `analyze_trades.py` from a 778,116-trade historical dataset
(the raw `trade.jsonl` is gitignored -- ~684MB, not something to version).
