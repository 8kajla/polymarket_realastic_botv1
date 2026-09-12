# Loop log (append-only, chronological)

One entry per iteration. Read `claude_memory.md` first for current state —
this file is the detailed history behind it, not the resumption point.

---

## Iteration 1 (2026-09-12, loop start)

Cron `a31ef787` scheduled, every 3 minutes. Set up this log + `claude_memory.md`
per user instruction. Picking up the code bug audit from before the loop
started (5 bugs already in `BUGS_TO_FIX.md`, none applied yet).

This iteration: read `fill_simulation.py`'s `on_trade_print`/`_consume_one`
and `bot.py`'s `_update_drawdown_tracking`/`_new_markets_paused`/
`committed_capital` in full.

**Result: no new bugs found in these areas.**
- `on_trade_print`: SELL-side-only filter and `trade.ts >= o.placed_at`
  filter are both already-fixed (dated 2026-09-10/earlier), price-time
  priority sort (`-o.price, o.placed_at`) is correct for bid-side matching.
- `_consume_one`: queue-then-fill logic is straightforward, no edge case
  found.
- `_update_drawdown_tracking`/`_new_markets_paused`: already correctly
  scoped to pause NEW markets only, explicitly leaves hedges/existing
  positions alone (matches what bug #2's fix would need — this circuit
  breaker got that right already, MAX_OPEN_ORDERS_PER_MARKET didn't).
- `committed_capital()`: CHECKED against the same phantom-cost risk that
  caused bug #5 (`entry_count`)/the earlier `cost_by_side` bug — this one
  is fine. It iterates `is_open()` orders for remaining size and real
  `.fills` for already-filled-unsettled amounts, so cancelled/expired
  orders correctly contribute nothing. Confirms bug #5's contamination is
  specific to `MarketActivityState`'s own bookkeeping, not the
  ledger/equity path.

Next: `ledger.py` (settlement/PnL accounting, not yet opened this thread).

---

## Iteration 2 (2026-09-12)

Read `ledger.py` in full, `resolution_tick` (bot.py) in full, and the
`rolling_accuracy_by_asset` update path. Also chased a theoretical
same-tick ordering race between `_retire_market`'s `dominant_side()`
snapshot and `manage_orders_tick`'s `release_unfilled` calls.

**Result: no new bugs found.**
- `ledger.py`: `settle_order`/`realized_pnl` are careful and already fixed
  for real historical bugs (reprice-spanning fills priced by each Fill's
  own price, not `order.price`; idempotent settlement via
  `_settled_order_ids`; `realized_pnl()` always recomputed fresh, never a
  drifting running counter, per the module's own hard requirement).
- `resolution_tick`: uses `parse_resolution` (real Gamma field) as the
  primary path; `infer_resolution_from_price` is only a deliberate,
  threshold-gated (>=99%) fallback for when `closed` lags a decisive
  price — NOT the same as the deprecated `infer_winner()` heuristic
  already flagged as stale in old analysis scripts (`market_trend_bias.py`).
  Live settlement was never exposed to that stale heuristic.
- `rolling_accuracy_by_asset` (feeds `ACCURACY_SCOUT_MULTIPLIER`): updated
  correctly at resolution time from a snapshot taken at retirement,
  comparing `dominant_side` (by cost) against the real `winning_side`.
- Same-tick ordering race (retirement snapshot vs. expiry/release):
  CHECKED, not a bug — `discovery_tick` (which retires markets) only runs
  every `MARKET_DISCOVERY_POLL_SECONDS=20s`, while `manage_orders_tick`
  (which expires orders and calls `release_unfilled`) runs every 3s tick
  and always finishes expiring a market's orders at least
  `MIN_SECONDS_BEFORE_CLOSE=75s` before window end — comfortably before
  the coarser ~20s-granularity retirement snapshot could ever race it.

Next: `book.py` (WS book-state/trade-print handling, not yet opened) and a
scan of behavior_config.py's remaining ~15+ multiplier functions not yet
read in full.

---

## Iteration 3 (2026-09-12)

Read `book.py` in full.

**Found a new, real (but NOT live-confirmed) architectural risk — bug #6
in BUGS_TO_FIX.md.** `BookState._reconcile_best` is a documented no-op: an
outer comment describes inserting a synthetic level to keep
`best_bid`/`best_ask` correct when a `price_change` message asserts a
price we have no matching level for (a missed earlier update), but the
actual body just `pass`es. Checked where the only real fix
(`apply_snapshot`, which replaces the whole book) gets called: only once
at onboarding, and whenever the EXCHANGE chooses to push a fresh "book"
WS message — no periodic REST re-poll anywhere in `bot.py`. So a missed
price_change update (one that doesn't kill the WS connection outright)
has no bounded correction path; `best_bid`/`best_ask` feed every
order-placement/spread/drift-reprice decision downstream.

Wrote this up as bug #6, explicitly flagged as a plausible risk found by
reading the code, NOT confirmed via live data the way #1-5 were — this
file's own class docstring already admits thin live validation. Recommended
next step (matching #1's precedent): add diagnostic logging inside
`_reconcile_best`'s `if` conditions to see if a mismatch is ever actually
detected live, before deciding whether to build a fix.

Also re-read `MarketWebSocketClient` fully: the one-subscription-per-
connection quirk and the `{"price":.., "size":..}` level-shape bug are
both already fixed/tested, nothing new there. `route_message`'s dispatch
by `event_type` looks complete and defensive (list-wrapped batches
handled, unknown types logged not crashed).

Next: systematic sweep of behavior_config.py's remaining multiplier
functions (grep `^def `, check each one's guards/clamps).

---

## Iteration 4 (2026-09-12)

Started the behavior_config.py sweep (28 functions total via `grep '^def '`).
Read `within_band_size_multiplier`, `ttc_size_multiplier`,
`resumption_size_multiplier` in full, then chased `resumption_size_multiplier`'s
TRIGGER logic in bot.py since the multiplier itself looked clean
(interpolation math checked by hand, correctly oriented, endpoints correct).

**Found a new, real, DIFFERENT CLASS of bug — #7 in BUGS_TO_FIX.md: the
resumption-caution multiplier is calibrated on the real trader's one known
long silence (the 13.6-day gap this session's research thread found), but
its trigger (`bot.py`'s `_record_global_trade`/`_last_trade_at`) fires on
OUR OWN BOT's trading gaps, not the trader's.** Every other cross-market
signal in this codebase (win/loss persistence, rolling accuracy, hedge
rate) is fed from the trader's real observed outcomes; this is the one
exception fed from the bot's own incidental behavior. If our bot ever goes
>=4h between its own trades for ANY reason (quiet market, an outage, or
just being starved by bugs #1/#3/#5), it applies a caution curve modeled
on the trader's specific real silence to an event that has nothing to do
with him. Recommended removing the mechanism outright, since it was also
already fit to a single n=1 real event per its own docstring (fragile even
before this).

`within_band_size_multiplier`/`ttc_size_multiplier`/`resumption_size_multiplier`
themselves: no bugs in the interpolation math (checked frac/endpoint
behavior by hand for ttc_size_multiplier's descending-midpoint loop —
correct, though its final fallback line is dead/unreachable code, harmless).

Next: continue the behavior_config.py sweep — side_persistence_for,
cross_market_side_persistence, cross_market_size_momentum_multiplier,
bankroll_pnl_size_multiplier, hedge_trigger_probability,
hedge_liquidity_multiplier, hedge_size_ratio, weekend_hedge_multiplier,
hedge_continuation_probability/size_ratio, accuracy_scout_multiplier,
scout_probability/size_ratio, cross_market_hedge_rate_multiplier,
conviction_hedge_multiplier — none read in full yet this thread.

---

## Iteration 5 (2026-09-12)

Continued the sweep: read `side_persistence_for`,
`cross_market_side_persistence`, `cross_market_size_momentum_multiplier`,
`hedge_trigger_probability`, `hedge_liquidity_multiplier`,
`hedge_size_ratio`, `adverse_move_size_multiplier` in full.

No new discrete bug (interpolation math in `hedge_liquidity_multiplier`
checked by hand, correct and properly clamped; `ADVERSE_MOVE_SIZE_MULTIPLIER`'s
docstring shows real, already-done confound checks — TTC ruled out as a
proxy, regime-controlled correlation, deliberately non-monotonic where the
real data is non-monotonic).

**But writing up a related, lower-confidence SYSTEMIC observation as #8:**
while re-checking `cross_market_size_momentum_multiplier`'s input source
(`ewma_size_residual_by_asset` in bot.py), confirmed it's fed from OUR OWN
bot's realized first-entry size, not the trader's — necessarily, since the
bot has no live feed of his real trades at runtime. This is a reasonable
design choice on its own, but combined with bug #5's already-noted
downstream effect on `last_hedge_rate_by_asset`, it means bugs #1/#4/#5
don't just cause isolated bad decisions — they get fed FORWARD through
these two self-referential trackers into future markets' sizing/hedging
too. Contrasted this against `last_first_entry_won_by_asset`/
`rolling_accuracy_by_asset`, which are safe from this (market resolution
is an objective fact, identical for the trader and our bot — no
self-reference risk there). Framed explicitly as lower-confidence/systemic,
not a discrete break like #1-7 — no action needed beyond awareness until
#1/#4/#5 are fixed, then re-check if it still matters.

Next: finish the sweep — `bankroll_pnl_size_multiplier`,
`weekend_hedge_multiplier`, `adverse_move_hedge_trigger_multiplier`,
`cross_market_hedge_rate_multiplier`, `conviction_hedge_multiplier`,
`hedge_continuation_probability`/`hedge_continuation_size_ratio`,
`adverse_move_continuation_size_multiplier`, `accuracy_scout_multiplier`,
`scout_probability`/`scout_size_ratio` — still not read in full.

---

## User check-in (mid-loop): confirmed to finish the bug sweep, then move to angles

User asked directly whether this loop was hunting bugs or angles (it had
been 100% bugs since starting). Answered honestly, user said: finish the
bug sweep, THEN move to research angles. Not asked to start fixing bugs
yet.

## Iteration 6 (2026-09-12) — SWEEP COMPLETE

Read the remaining 9 functions: `bankroll_pnl_size_multiplier`,
`weekend_hedge_multiplier`, `_solve_hazard_scale`/`_build_hedge_attempt_hazards`
(the hazard-curve machinery underlying `hedge_attempt_hazard`),
`adverse_move_hedge_trigger_multiplier`, `cross_market_hedge_rate_multiplier`,
`conviction_hedge_multiplier`, `hedge_continuation_probability`/
`hedge_continuation_size_ratio`, `adverse_move_continuation_size_multiplier`,
`accuracy_scout_multiplier`, `scout_probability`/`scout_size_ratio`.

**All 28 functions in behavior_config.py have now been read in full.**
Every interpolation function follows the same correct, consistent pattern
(sorted points, clamp to measured range, linear interpolation, symmetric
multiplier cap) — no new arithmetic/logic bugs found in this batch.

**One real extension of bug #5 found:** `record_entry`'s
`if is_hedge: self.hedge_count += 1` has the IDENTICAL placement-vs-fill
contamination as `entry_count` (incremented at placement, never corrected
on cancel/expire). This reaches further than first scoped — it affects
`decide_hedge`'s own `MAX_HEDGE_COUNT_PER_MARKET` gate (can trip early on
placements that never filled) and both `hedge_continuation_probability`/
`hedge_continuation_size_ratio` (calibrated on real fill-only indices).
Compounds directly with bug #2 (open-order cap blocking hedges) and bug #3
(reprice cap cancelling hedges) — a hedge that places then gets cancelled
still corrupts every later hedge-index lookup. Updated bug #5's writeup in
BUGS_TO_FIX.md with this extended scope rather than creating a duplicate
entry.

**BUG SWEEP DECLARED COMPLETE per user instruction.** Final count: 8
entries in BUGS_TO_FIX.md (7 discrete bugs + 1 systemic note), all fully
written up with file/line references and fix recommendations, NONE applied
yet (not asked to fix, only find). Every file in `paperbot/` has now been
read in full at least once this thread: `strategy.py`, `bot.py`,
`fill_simulation.py`, `config.py`, `ledger.py`, `book.py`,
`behavior_config.py`. `market_discovery.py` was read partially (enough to
confirm the resolution-fetch path is sound) but not exhaustively —
acceptable stopping point, not flagged as an open gap requiring
completion before moving on.

**Per explicit user instruction, next iteration switches to RESEARCH
ANGLES** (external trader-behavior investigation), NOT further bug
hunting. See `claude_memory.md`'s angle list to resume.

---

## Iteration 7 (2026-09-12) — PHASE 2 START: research angles

**Angle #1 tested: platform-wide 60s trading halt — REJECTED, cleanly and
definitively.** Pulled real market tapes (data-api.polymarket.com, 15
markets from his recent activity) and compared his own last-trade timing
against EVERY OTHER wallet active in those same markets:

| | n | mean ttc | median ttc | % within final 60s |
|---|---|---|---|---|
| His own last trade | 11 | 109.5s | 102.0s | **0.0%** (min 63s) |
| Every other wallet's own last trade | 2,538 | 125.0s | 104.0s | **32.4%** |
| Market-wide single last trade (any wallet) | 15 | 61.5s | 20.0s | **60.0%** |

If the ~60s cutoff were a platform-enforced halt, NO wallet could trade
within the final 60 seconds — instead a third of other individual wallets
do, and the single last trade in a market lands with a median of just 20s
before close (sometimes even later per small negative values, likely
clock/settlement-timing nuances). **This confirms his cutoff is a genuine,
deliberate personal behavior, not a platform artifact** — strengthens
rather than undermines the existing `MIN_SECONDS_BEFORE_CLOSE` modeling.
Clean, well-powered, one-pass rejection.

Next: angle #2 (real order cancellations via CLOB order-stream, distinct
from trades.jsonl's fills-only data) or angle #5 (chase the 13.6-day halt
cause) — pick whichever has an easier data path when resuming.

---

## Iteration 8 (2026-09-12)

**Angle #5: chased the 13.6-day halt's cause via Polymarket's own status
page (status.polymarket.com/history/1) — partial answer, real evidence,
rules out one candidate cleanly.**

Web search first surfaced real, documented Polymarket incidents in this
general period: Aug 27 (~10min scheduled CLOB maintenance), Aug 31 (4+
hour outage, order-read-response failures), Sep 2 (~1.5h DB replica-lag
fix), Sep 9 (another reported outage). All too short individually to
explain a 13.6-day gap, but worth checking whether they clustered densely
enough across the period to add up, or whether something undocumented in
search results happened right at the actual start (Aug 23).

Pulled the real status page directly (via server curl with a browser
user-agent, since WebFetch/DNS failed on this specific domain) and parsed
its actual incident list (13 real, dated incidents visible):

| Incident | Date |
|---|---|
| Trading Degraded - Predictions | Sep 11 |
| Perpetuals scheduled maintenance | Sep 09 |
| Delayed open order read responses (x2) | Sep 03 |
| Cancelling orders issue | Sep 03 |
| Delayed open order read responses | Sep 01 |
| Delayed open order read responses (x2) | Aug 31 |
| Delayed open order read responses | Aug 30 |
| Degraded trading performance | Jul 30 |
| Price-history/market-chart lag (x3) | Jul 28-29 |

**Zero incidents listed between July 30 and August 30 — a full month
gap that entirely covers when his silence actually BEGAN (Aug 23).** The
real incident cluster (Aug 30 onward) lands well within his silence
period and slightly after his Sep 6 resumption, but can't explain why he
was ALREADY silent for the week before any of them happened.

**Conclusion: rules out "a platform outage caused the halt" as the
TRIGGER, with real official evidence, not just absence-of-search-results.**
Doesn't fully close the mystery (still don't know his actual reason for
stopping Aug 23), but narrows it usefully: whatever caused the START of
the gap was either account-specific (his own system, a personal decision,
an account-level action) or something not captured on Polymarket's public
status page at all (not every issue gets a public incident, especially a
narrow one affecting only certain markets/wallets) — not a general,
public platform outage. Interesting side-note, not causally connected:
Polymarket really was having a rough infrastructure patch (repeated
order-read-lag incidents) right in the days around his resumption
(Aug 30 - Sep 3), which is suggestive but not proven to be related.

Next: angle #2 (real order cancellations) or angle #6/#7 (PCA scan /
macro-news reaction) — #5 is not fully closed but has hit its likely
ceiling without account-level data we don't have access to.

---

## Iteration 9 (2026-09-12)

**Angle #2: real order cancellations via CLOB order-stream — BLOCKED,
confirmed genuinely (not just difficult), same category as the
already-blocked wallet-clustering angle.**

Researched Polymarket's real CLOB WebSocket API docs
(docs.polymarket.com/developers/CLOB/websocket/user-channel — page itself
is a JS-rendered SPA, unreadable via curl, but WebSearch's own indexed
snippet was sufficient and explicit): the "user" channel that streams
order-placed/updated/canceled events **requires authenticating with
private API credentials (apiKey, secret, passphrase) tied to the specific
account**. This is the account owner's own private order stream, exactly
like any exchange's authenticated user-order-events feed — there is no
public/keyless way to subscribe to a DIFFERENT wallet's order or
cancellation events. We would need the real trader's own API credentials
to ever see his cancellations, which we obviously don't have and can't
legitimately obtain.

**Confirmed genuinely blocked, not abandoned for lack of trying** — same
honest category as the wallet-clustering angle (blocked on a missing
Etherscan API key, but that one is at least theoretically obtainable; this
one is blocked by the exchange's own security model and can never be
obtained without the trader's cooperation). Closing this angle
permanently unless a fundamentally different data source appears (e.g. if
Polymarket ever published aggregate/anonymized cancellation-rate stats
publicly, which no evidence of was found).

Next: angle #6 (multivariate/PCA anomaly scan) or #7 (macro-news
reaction) — both still fully untested.

---

## Iteration 10 (2026-09-12)

**Angle #3: on-chain USDC deposit/withdrawal timing — hit a practical
dead end, marked INFEASIBLE (not BLOCKED like #2 — technically possible,
just impractically expensive with no guarantee of a clean answer).**

Used Blockscout's keyless API (already proven working earlier this
session) to paginate his real ERC-20 token-transfer history and filter out
the already-known exchange-contract noise (0xE111...). Checked 2,000 real
transfers (40 pages) -- these covered only ~1 HOUR of real wall-clock time
(12:06-13:13 UTC), confirming just how extremely high-frequency his real
activity is. Found 79 non-exchange-contract transfers, but **every single
one is a MINT from the zero address (0x000...0)** -- these are PUSD
(Polymarket's internal settlement token) being minted on position
redemption, not a genuine external deposit from a bank, exchange, or
personal wallet.

**Two compounding problems close this angle out practically:**
1. Reaching back to any genuine external funding event (if one exists at
   all beyond his account's original setup) would require pagination
   through potentially 100,000+ transfers given this transaction density
   sustained over his ~3.5-month history -- not a one-iteration task.
2. Even if reached, a "real" deposit likely wouldn't look external anyway:
   Polymarket's own deposit rails (credit card/bank -> Polymarket's
   backend -> an on-chain credit) would plausibly show as a transfer FROM
   POLYMARKET'S OWN TREASURY CONTRACT, not from a personal external
   wallet -- indistinguishable on-chain from ordinary settlement, the
   same fundamental limit that closed the wallet-clustering angle
   (the EOA-to-proxy-wallet mapping lives in Polymarket's own off-chain
   backend, not on public chain state).

**Conclusion: closing this angle as impractical with on-chain data alone,
not rejecting the underlying hypothesis (still genuinely unknown whether
he manages capital via deposits/withdrawals) -- just noting it can't be
answered cheaply the way this session's other on-chain checks could.**

Next: angle #6 (multivariate/PCA scan) or #7 (macro-news reaction) --
both still fully untested, and #4 (Sybil-farm) also still open.

---

## Iteration 11 (2026-09-12)

**Angle #6: multivariate scan — ran a genuine joint-control regression
(no numpy available on server and pip install blocked by PEP 668, so
implemented OLS via normal equations + Gauss-Jordan inversion in pure
Python).** Regressed log(size*price) jointly on price + ttc + regime
dummies + asset dummies simultaneously (n=150,000, subsampled from
489,464 usable rows), then scanned the RESIDUAL against variables not in
the model (hour_of_day, day_of_week, is_weekend) -- the actual point of
"multivariate" here: single-variable partial correlations (already done
extensively this whole project) only control for one or two other things
at a time, this controls for four categories jointly at once.

Found `is_weekend` clears the significance bar strongly on the full
pooled residual (r=0.0589, t=22.86) -- but this project already rejected
weekend/hour-of-day effects for flipping sign across time clusters
(`hour-of-day-and-btc-leadlag-rejected.md`), so ran the SAME temporal-
stability check on this more rigorous, jointly-controlled version before
trusting it:

| Chunk | Date range | n | r | t |
|---|---|---|---|---|
| 1 | 2026-06-23 to 2026-07-29 | 50,000 | 0.0305 | 6.82 |
| 2 | 2026-07-29 to 2026-08-16 | 50,000 | 0.1305 | 29.44 |
| 3 | 2026-08-16 to 2026-09-12 | 50,000 | -0.0048 | -1.08 (null) |

**Confirms, doesn't overturn, the existing rejection: the effect decays to
null in the most recent era even under this more careful joint-control
version.** A useful methodological result even though it's not a new
finding -- shows the earlier rejection wasn't just an artifact of weak
univariate confound-checking; it genuinely doesn't survive under a more
rigorous multivariate control either. `hour_of_day` showed only a weak,
per-asset-inconsistent signal (BTC t=-3.46, ETH t=1.07 null, SOL t=-2.62),
consistent with the same already-established instability.

Next: angle #4 (Sybil-farm hypothesis) or #7 (macro-news reaction) -- both
still fully untested. Angle #6 (PCA/multivariate) can be considered done
for now -- the one signal it surfaced was already-known-unstable, not
worth further multivariate combinations without a fresh candidate
variable to test.

---

## Iteration 12 (2026-09-12)

**Angle #4: Sybil-farm hypothesis, tested with a DIFFERENT approach from
the already-blocked wallet-clustering angle (no EOA-mapping needed) --
found a real, striking pattern, but INCONCLUSIVE, not confirmed or
rejected.**

Instead of chasing the off-chain EOA-to-wallet mapping (already closed as
intractable), tested whether any OTHER wallet's behavior in his own
markets is suspiciously similar to his -- pulled 26 of his recent markets'
full tapes and counted how many of THOSE SAME specific markets each other
wallet also appears in (more than ordinary crowding would predict).

**Found something real: at least 10 distinct wallets appear in ALL 26/26
of his sampled markets, several with near-identical fingerprints to each
other:** median trade size exactly 5.00 shares (matching his own median),
mean price 0.01-0.02 (buying the absolute CHEAP extreme), 100% BUY, across
multiple of these wallets. This looks like either a real cluster of
related bots, or several independent-but-similar high-frequency
market-making bots that happen to converge on the same generic tactics
(exchange minimum floor size, cheap-extreme scalping) purely because
those are common realities of this market's own structure.

**Tried the decisive control test (do these wallets ALSO appear in
markets he SKIPPED) and hit a data-availability wall, not a rejection:**
his current real-time participation rate turned out to be ~100% (28/28
sampled recent windows, and explicitly checking adjacent 5-min windows
found essentially no genuinely-skipped-by-him market to use as a control
right now) -- notably higher than this project's earlier-established
63-65% participation-rate finding from a different time period. Couldn't
construct a clean "does he specifically get followed, or is this wallet
just always present regardless" comparison in this session with current
data.

**Honest conclusion: a real, promising, NOT YET RESOLVED lead -- neither
confirmed nor rejected.** Don't report this as a Sybil-farm finding
without the control test. If revisited: use a window from further back in
his history (during the previously-measured lower-participation period)
to find genuine skipped markets and re-run the same wallet-overlap check
there.

Next: angle #7 (macro-news reaction) -- last remaining untested angle
from the original list.

---

## Iteration 13 (2026-09-12) — ALL 7 ORIGINAL ANGLES NOW COMPLETE

**Angle #7: macro-news reaction — REJECTED, clean null, after two false
starts from confounds.**

First candidate: June 4, 2026's real, documented ~12% BTC intraweek crash
(Fed/inflation-driven). Checked daily trade count/avg-notional around it
-- found a real-looking 2-3x size rise across June 4-7, but this date
falls squarely inside his account's already-known early "ramp-up" period
(first two weeks of his whole studied history) -- already established
elsewhere as a general maturation artifact, not date-specific. Confounded,
discarded.

Second candidate: July 22, 2026's real, documented BTC retreat (oil-price/
inflation-driven). This date ALSO overlapped with an already-known
asset-mix reversion event (the "brief switch back to HYPE/DOGE" already
in the weekly-mix table). Isolated to BTC-ONLY (present throughout every
era, same technique that resolved the earlier size-mystery) to sidestep
that confound:

| Window | mean trades/day | mean notional/day |
|---|---|---|
| Before (Jul 17-21) | 1,982 | $24,088 |
| After (Jul 22-27) | 2,082 | $24,325 |

**Nearly identical before/after, both well within the day-to-day noise
already visible across the whole window (1,490-2,917 trades/day,
$22,185-$26,960/day) -- no detectable discontinuity at the event date.**
Clean null, once properly isolated from confounds. Consistent with
everything else established about him: reactive to LOCAL/live price
action (already the core confirmed mechanism), not macro news headlines.

**LESSON for future angle-testing on this trader's history:** nearly
every calendar date in his ~3.5-month history overlaps with SOME
already-known structural event (the May28-Jul9 ramp/early-rotation era,
the Jul22-23 brief reversion, Aug6-7 TWAP_SWITCH, Aug23-Sep6 halt) --
always check a candidate event date against the full list of already-known
boundaries (see asset-rotation-full-history-and-doge-hype-correction.md
in project memory) BEFORE trusting a raw before/after comparison, and
isolate to BTC-only (present in every era) as the default confound-control
technique when in doubt.

**ALL 7 ORIGINALLY-LISTED RESEARCH ANGLES NOW HAVE RESULTS:** #1 rejected
(platform-wide cutoff), #2 blocked (order cancellations), #3 infeasible
(on-chain funding), #4 inconclusive (Sybil-farm, real unresolved lead),
#5 partial/ceiling (13.6-day halt cause), #6 confirmed-existing-rejection
(multivariate/PCA), #7 rejected (macro-news). No angle list remains from
the original brainstorm -- next iteration needs either (a) a genuinely
new angle found via fresh search/thinking, (b) revisiting #4's Sybil-farm
lead with a proper control window if a clean approach can be found, or
(c) checking in with the user on what to do next now that both the bug
sweep AND the angle list are exhausted.

---

## Iteration 14 (2026-09-12) — user gave no new direction, defaulted to option (a): found a genuinely NEW angle (angle #8)

**Angle #8 (new, not from the original 7-item list): cross-asset tilt-
after-loss.** Never tested before -- every prior "tilt after loss" /
streak check in this project was SAME-asset (does a BTC loss predict his
NEXT BTC market's size). This tests whether a loss in ONE asset (say BTC)
predicts more cautious sizing in a DIFFERENT asset's (ETH/SOL) very next
market -- true cross-asset risk transfer, distinct from the already-
confirmed same-asset persistence/momentum mechanisms.

**Method:** reconstructed 41,973 real markets from trades.jsonl, joined
against resolution_cache.json (7,240 markets with known outcomes) to
determine each market's dominant side and whether it won. For each
market, found the most recent PRIOR market of a DIFFERENT asset within 30
minutes, and bucketed the CURRENT market's total notional by whether that
prior cross-asset market's dominant side won or lost.

| | n | mean notional |
|---|---|---|
| After a different asset's market WON | 2,424 | $59.00 |
| After a different asset's market LOST | 1,421 | $52.54 |

**t=-2.719 (Welch), ratio 0.89 -- barely clears the significance bar.**
Given this project's repeated experience with near-bar effects turning
out to be confounds (round-nickel, weekend effects both flipped/vanished
under scrutiny), ran the standard temporal-stability check before trusting
it: split chronologically (constrained by resolution_cache's known
patchy coverage, heavily skewed recent -- first half only n=230 combined,
second half n=3,614).

| Half | n_after_win | mean | n_after_loss | mean | t |
|---|---|---|---|---|---|
| First (underpowered) | 145 | 52.54 | 85 | 45.33 | -0.915 |
| Second (well-powered) | 2,278 | 59.43 | 1,336 | 53.00 | -2.598 |

**More reassuring than previous near-bar cases: the DIRECTION is
consistent in both halves** (after-loss always smaller), and the first
half's non-significance looks like ordinary low power (n=230) rather than
a genuine sign flip -- different from the weekend effect's clean
reversal-to-null. Still, **NOT YET CONFIRMED** -- the critical missing
check is a REGIME confound: if he tends to enter correlated assets in
similar regimes during correlated market conditions (plausible given
BTC/ETH/SOL move together), "the other asset's market just won" could be
a proxy for "we're currently in a decisive/HIGH-band period," which
independently predicts smaller CURRENT-market size for reasons having
nothing to do with any real cross-asset behavioral tilt. This exact class
of confound is why the round-nickel and MID-velocity investigations both
needed a finer check before being trusted.

**Status: a real, promising, NOT YET CONFIRMED lead -- do not report as a
finding or build candidate until the regime-confound check is done.**
Next concrete step: redo the same before/after comparison holding the
CURRENT market's own regime fixed (needs capturing the dominant-side price
at reconstruction time, not just win/loss -- a small addition to the
existing script).

---

## Iteration 15 (2026-09-12)

**Angle #8 regime-confound check: RETRACTED. Clean, well-explained
confound -- same pattern as round-nickel and MID-velocity before it.**

Added dominant-side price capture to the reconstruction and reran the
before/after comparison holding the CURRENT market's own regime fixed:

| Regime | n_after_win | mean_win | n_after_loss | mean_loss | t |
|---|---|---|---|---|---|
| CHEAP | 852 | 38.101 | 520 | 31.635 | -2.081 |
| MID | 1,017 | 62.249 | 665 | 60.284 | -0.540 |
| CORE | 382 | 91.328 | 176 | 78.664 | -1.675 |
| HIGH | 173 | 71.456 | 60 | 71.283 | -0.016 |

**None of the four regime cells clear the |t|>=2.58 bar.** The composition
check explains exactly why the pooled effect looked real: CORE+HIGH
(naturally bigger $ per the already-known Kelly-like size curve, and
naturally higher win rate by construction) make up 22.9% of the
after-a-different-asset's-WIN group vs only 16.6% of the after-LOSS
group -- a real compositional skew, consistent with correlated assets
sharing regime/decisiveness during correlated market conditions (BTC/ETH/
SOL moving together). The pooled $59.00-vs-$52.54 gap was riding on this
composition difference, not a real cross-asset behavioral tilt.

**RETRACTED, cleanly.** No cross-asset risk-transfer mechanism found --
his sizing in one asset does not respond to a recent win/loss in a
different asset, once you control for the fact that correlated assets
tend to be in similar regimes at similar times. Good confirmation that
the temporal-stability check alone (iteration 14) wasn't sufficient this
time -- consistent direction across time doesn't rule out a stable
confound, only an unstable effect. The regime/composition check was the
one that actually mattered here.

**Lesson reinforced for this whole thread:** temporal stability and
regime/composition confound checks test DIFFERENT failure modes (an
unstable effect vs. a stable-but-spurious one) -- passing one doesn't
mean the other can be skipped. Both are needed before trusting any
raw group comparison.

Angle #8 now CLOSED (retracted). Need a fresh angle #9, or return to
angle #4 (Sybil-farm) with a proper control window.

---

## Iteration 16 (2026-09-12) — angle #4 (Sybil-farm) resolved with a real, positive, well-powered signal

**Got the decisive control test working this time.** Blocked previously
because his CURRENT participation rate is ~100% (no live skipped markets
to test against). Fix: went to an earlier LOCAL trades.jsonl period
(mid-July 2026) where his real BTC participation was 89.4% (592/662
possible windows) -- enough genuinely-skipped windows to sample from.
Also hit and fixed a real API quirk: Gamma's `/markets?slug=X` returns
`[]` for any already-resolved/archived market -- needs `&closed=true`
(or `/markets/slug/{slug}`) to find old markets at all. Without this fix
every "skipped market" lookup silently failed as "not found," which is
exactly what happened on the first attempt.

**Result, 6 real skipped-by-him markets tested (each 400+ distinct
wallets, 1000+ trades -- large, busy markets, not thin ones):**

| Skipped market | n_trades | n_wallets | Top-5 recurring wallets present |
|---|---|---|---|
| btc-updown-5m-1783954800 | 1000 | 403 | 0/5 |
| btc-updown-5m-1783919400 | 1000 | 382 | 0/5 |
| btc-updown-5m-1783968300 | 1000 | 412 | 0/5 |
| btc-updown-5m-1783952400 | 1000 | 403 | 0/5 |
| btc-updown-5m-1784059500 | 1000 | 426 | 0/5 |
| btc-updown-5m-1784043900 | 1000 | 407 | 0/5 |

**ZERO of the 5 top recurring wallets (each present in 26/26 of HIS
markets, iteration 12) appear in ANY of the 6 markets he skipped.**
Combined with iteration 12's finding, this rules out the "generic,
always-present market bot" explanation cleanly -- if these were just
independent high-frequency bots trading every single BTC 5-min market
regardless of him, they'd show up in busy 400+-wallet skipped markets by
sheer chance. They don't. **Their presence is genuinely, specifically
correlated with HIS OWN market selection.**

**Honest interpretation -- confirmed correlation, NOT proof of a single
operator:** this is real and well-powered, but doesn't by itself prove a
literal Sybil farm (one operator running him + these 5 wallets). Equally
consistent alternatives: (a) a genuine coordinated multi-wallet operation
(the original hypothesis), (b) all 6 wallets are clients of some shared
higher-level signal/copy-trading service that picks which specific
markets to enter, or (c) something more mundane -- his own market
SELECTION and these wallets' selection could both be driven by the same
real external trigger (e.g. all reacting to the same live spot-price
signal crossing some threshold) without being the same operator at all.
Can't distinguish between these three with data available -- would need
the still-blocked EOA/API-credential-level access to go further.

**Status: CONFIRMED, real, well-powered correlation. Strongest and most
interesting behavioral discovery from this whole angles thread.** Not
reported as "Sybil farm confirmed" (overclaiming past what the data
shows) -- reported as "his market selection correlates with a specific
small cluster of other wallets, mechanism undetermined among 3 plausible
explanations."

No further build implication (this doesn't change how OUR bot should
behave -- we already select markets independently via our own copy of
his calibrated behavior, not by watching these other wallets). Purely a
research/understanding finding.

---

## Iteration 17 (2026-09-12) — MAJOR UPGRADE: angle #4 goes from "confirmed correlation" to "very likely confirmed farm," via a timing-lockstep test

Tried to distinguish between angle #4's 3 remaining explanations (real
farm / shared signal service / same external trigger) by checking each
top wallet's FIRST-trade lag relative to HIS first trade, in the same
market, across 21-25 real markets.

**Result is decisive, not just suggestive.** All 5 wallets' lag sequences
are NEARLY IDENTICAL to each other, market by market:

```
Wallet 1: [192, 240, 206, 215, 48, 164, 33, 153, 153, 48, 39, 72, 197, 142, 24, 102, 180, 60, -20, 96, 199]
Wallet 2: [192, 240, 203, 215, 48, 167, 33, 153, 153, 48, 39, 72, 197, 142, 24, 102, 182, 60, -20, 96, 199]
Wallet 3: [192, 240, 198, 215, 51, 167, 33, 153, 153, 48, 39, 69, 192, 142, 24, 102, 182, 60, -20, 96, 199]
Wallet 5: [192, 240, 210, 215, 51, 167, 33, 153, 153, 48, 39, 72, 197, 142, 24, 102, 182, 60, -20, 96, 199]
```

(Wallet 4 diverges slightly more but still tracks the same overall shape.)
Every position across 21 independent markets agrees to within 1-3 seconds
across all 5 wallets. **This rules out "independent reaction to the same
external trigger" -- independent bots, even reacting to the identical
real-world signal, would show real variance in their own processing/
latency/decision timing relative to each other. Lag values this tightly
locked together, repeated market after market, is the signature of the
SAME underlying process controlling all 5 (a shared codebase/scheduler),
not 5 separately-operating systems.**

**Reframes the finding significantly: this isn't just "his market
selection correlates with 5 other wallets" (iteration 16's framing) --
these 5 wallets are almost certainly run by ONE operator, likely the SAME
operator as our own target wallet.** The lag is measured relative to HIS
trade specifically and stays tight across dozens of different market
instances -- most parsimonious explanation: our target wallet
(0xb0f85baa...) may not be an independent single trader at all, but ONE
OF AT LEAST 6 near-identical wallet instances run by the same underlying
system, deliberately spread across multiple addresses (a real multi-
wallet farm structure). Combined with iteration 12's fingerprint data
(median size exactly 5.00 matching his own, all buying the cheap extreme,
100% BUY across all 5) -- multiple independent lines of evidence now
point the same direction.

**Still one honest caveat before calling this fully proven:** cannot
rule out that our target wallet is itself just reacting to something the
other 5 do (or vice versa) rather than all 6 being co-equal instances of
one shared system -- the lag being measured FROM him doesn't establish
causal direction, only tight coupling. But "coupled system," in either
direction, is now the best-supported explanation, well past "coincidence"
or "same external trigger."

**Implication for this whole project, flagged honestly, not acted on:**
if pspspsps5 truly is one node of a multi-wallet farm, the "trader" this
whole project has been reverse-engineering and replicating may really be
"one instance of a shared strategy template," not a single individual's
bespoke system. This DOESN'T change the validity of anything already
built (the calibrated behavior is still real, still his own wallet's real
history, still worth replicating) -- but it reframes the likely nature of
the underlying operation. Not actionable for our bot; purely
context-changing for how this whole project's subject should be
understood.

---

## Iteration 18 (2026-09-12) — angle #4 cross-asset generalization: CONFIRMED, holds independently on all 3 assets

Followed up on the user check-in by testing whether iteration 17's
timing-lockstep pattern is BTC-specific or general. Pulled fresh lag data
for BTC, ETH, and SOL separately (his 12 most recent markets per asset):

**BTC** (4-5/8 markets each): lag sequences `[43,33,48,24]` shared
EXACTLY by 4 of the 5 wallets.
**ETH** (7/7 markets each): `[183,240,48,153,39,142,102]` shared almost
exactly (within 1-9s) by all 5.
**SOL** (8/8 markets each): `[192,206,215,164,153,72,197,180]` shared
almost exactly (within 1-8s) by all 5.

**The same near-perfect lockstep holds independently within EACH asset,
not just pooled/BTC-only.** Presence rates are very high and consistent
(7/7 ETH, 8/8 SOL, 4-5/8 BTC — the somewhat lower BTC rate is still
substantial and doesn't undermine the pattern within markets where they
do co-occur). This rules out an asset-specific artifact (e.g. some
BTC-only market-making convention) and confirms the underlying
mechanism/operator relationship generalizes across his whole traded
basket.

**Status: angle #4 is now about as thoroughly investigated as available
data allows** -- 4 independent lines of evidence (all-markets co-
occurrence, control-market absence, timing lockstep, cross-asset
generalization) all point the same direction. Further progress (e.g.
determining causal direction, or finding the actual shared operator)
would need account-level or API-credential access this project doesn't
have. Marking this angle CLOSED at its practical ceiling, not pursuing
further without a fundamentally new data source.

Need a fresh angle #9 for the next iteration -- nothing queued yet.

---

## Iteration 19 (2026-09-12) — angle #4 refined significantly: it's not a literal mirror, it's a shared fixed-notional micro-strategy, most likely a third-party copy-trading tool

Followed up with a size/side comparison (not just timing) across 10 real
markets -- and it changes the interpretation meaningfully.

**Nearly every one of the 5 wallets places a FIXED ~$0.10 notional on
every single trade, via whatever (size, price) combination multiplies to
that** -- e.g. 5.00 shares @ $0.02, 10.00 shares @ $0.01, 3.38 shares @
$0.02. This is NOT a copy of his own size (his sizes ranged $0.80-$7.80
in the same sample, following his own real calibrated curve) -- it's a
generic, fixed, tiny "floor-lot probe" applied uniformly regardless of
his own size.

**Even more telling: in one example (eth-updown-5m-1789219500), his own
entry was "Down" at price 0.510 (a genuine early, undecided-market bet)
while all 5 wallets' "Down" entries were at price 0.020 -- a wildly
different price for the SAME outcome token, meaning they entered at a
completely different, much LATER moment in the window (once the market
had already become decisive) than he did.** This matches the already-
known lag data (24-240+ seconds) -- these wallets aren't reacting
instantly to his trade, they're entering the SAME markets, much later,
specifically to buy whichever side has crashed to the cheap/decisive
extreme by then, for a fixed tiny stake.

**Also notable: in a market where his own trade happened to be the FAVORITE
side (btc-updown-5m-1789218900, Down at 0.960), only ONE of the 5 wallets
appeared at all, and it bought the OPPOSITE (longshot) side** -- further
confirming they're not mirroring his SIDE, they're independently buying
whatever's currently cheapest, in whichever of his markets they happen to
also be active in.

**Refined hypothesis, better-fitting all the evidence now (co-occurrence,
control-absence, timing lockstep, cross-asset generalization, AND this
size/side data):** these are very likely NOT the same operator as our
target wallet running one unified strategy across 6 addresses. More
likely: several wallets running the SAME off-the-shelf tool/codebase that
(a) tracks WHICH markets a chosen "signal" wallet (ours) enters -- a
real, documented category of product (this project found real "Polymarket
Wallet Tracker / copy-trading" services early on) -- and (b) independently
applies its own simple, generic "buy the cheap decisive-extreme longshot
late in the window for a fixed tiny stake" logic on those same markets,
rather than literally copying his trade. This explains BOTH why they only
show up in HIS specific markets (they're following his market SELECTION)
AND why their own trade content (size, side, timing) looks nothing like
his (they're not copying his ACTUAL trade, just piggybacking on his
market-picking).

**Not fully provable beyond this without account-level data (can't
confirm it's literally a specific named third-party tool) -- but this is
now the best-supported, most specific explanation available, a genuine
refinement over iteration 17's blunter "probably the same operator"
framing.** Downgrading "very likely a Sybil farm run by the same
operator" to "very likely several users/bots running a shared
copy-trading tool that watches his wallet specifically" -- a more
precise, better-evidenced conclusion.

This closes out angle #4 for good at a well-supported, nuanced
conclusion. Need a genuinely fresh angle #9 for next iteration.

---

## Iteration 20 (2026-09-12) — angle #9 (new): loss-streak PARTICIPATION (not sizing) — REJECTED, clean null

Loop kept firing without a stop/redirect from the user, so kept pushing
for a genuinely new angle rather than repeat the "I'm stuck" message.
Found one: does he go quiet (skip trading, reduced frequency) after a
streak of consecutive MARKET losses, distinct from the already-rejected
same-asset sizing-streak test (which only checked dollar size, never
participation/frequency)?

**Method:** reconstructed 7,240 resolved markets, tracked per-asset
consecutive-loss streaks chronologically, measured the time gap to the
NEXT same-asset market conditioned on current streak length (0/1/2/3+),
excluding gaps >3h (the known halt-type outliers, not relevant to
ordinary streak behavior).

| Streak | n | mean gap | median gap |
|---|---|---|---|
| 0 losses | 4,874 | 335.8s | 300.0s |
| 1 loss | 1,500 | 338.2s | 300.0s |
| 2 losses | 485 | 328.5s | 300.0s |
| 3+ losses | 368 | 357.9s | 300.0s |

**Welch t (streak=0 vs streak=3+) = 1.279 -- well below the significance
bar. Median is EXACTLY 300s (one 5-min window) in every single bucket.**
Clean, well-powered null -- he essentially always re-enters the very next
window regardless of how many consecutive losses just happened. No
participation-based "cool off after losing" behavior exists.

**REJECTED.** Complements (extends, doesn't duplicate) the earlier
win/loss-streak sizing rejection -- now confirmed neither DOLLAR SIZE nor
TRADING FREQUENCY responds to loss streaks. Consistent with the broader,
repeatedly-confirmed picture of him as a mechanical, streak-agnostic
system (matches the same conclusion already drawn from the session-level
performance-autocorrelation null finding elsewhere in this project).

Need angle #10 for next iteration.

---

## Iteration 21 (2026-09-12) — angle #10 (new): CHEAP-band Up-vs-Down calibration asymmetry — REAL, TWAP-linked, confirmed cross-asset -- but not a build candidate (he doesn't exploit it)

New, never-tested split: within the CHEAP band, does calibration (win% vs
priced probability) differ between "Up" longshots and "Down" longshots,
per asset?

**Pooled result, real and well-powered, SAME direction on all 3 assets:**
Down consistently wins more often than Up in CHEAP band --
BTC z=-4.286, ETH z=-4.560, SOL z=-3.300 (all comfortably clear the bar).
Edge_pp: Up consistently negative (-0.56 to -1.47pp across assets), Down
consistently positive (+0.99 to +3.30pp). His own CHEAP volume is roughly
EQUAL or even slightly tilted toward the WORSE side (Up) -- he is not
currently exploiting this asymmetry.

**Ran the standard temporal-stability check (learned the hard way this
whole thread) before trusting the pooled number -- and it flagged something
real, not noise:** an arbitrary chronological half-split showed FIRST HALF
null (z=0.143, Up/Down win% nearly identical: 16.53%/16.44%) and SECOND
HALF strong (z=-6.216). Rather than treat this as ordinary instability
(the failure mode that killed the weekend effect and the cross-asset-tilt
angle), tested a MECHANISTICALLY MOTIVATED split instead: exactly at
TWAP_SWITCH (2026-08-07), since TWAP smoothing plausibly affects up-moves
and down-moves asymmetrically (already an established mechanism this
project uses to explain the Aug-7 size-halving and hedge-rate-change
findings).

| Period | Up win% (n) | Down win% (n) | z |
|---|---|---|---|
| PRE-TWAP | 13.33% (2,085) | 14.19% (1,888) | -0.788 (null) |
| POST-TWAP | 16.11% (10,884) | 18.30% (10,375) | -4.246 (real) |

**Clean, coherent result: the asymmetry doesn't exist before TWAP_SWITCH
and is real and well-powered after it.** This is NOT the same failure
pattern as the weekend effect (which decayed to null in the MOST RECENT
era) or the round-nickel/cross-asset-tilt effects (which were pure
composition artifacts) -- this is a genuine platform-level calibration
shift that emerged exactly when the settlement mechanism itself changed,
mechanistically consistent with everything already established about
TWAP_SWITCH's real effects on this market's structure.

**Per this project's reporting-filter rule ("does this help replicate the
trader better"), this is NOT a build candidate** -- he doesn't act on
this asymmetry himself (his CHEAP volume split doesn't favor the
better-edge side), so building it into our bot would make us diverge
FROM him, not replicate him more faithfully. Logged as a genuine, real,
well-explained platform-calibration finding (same category as the
already-rejected settlement-discount hypothesis, but this one is REAL,
just not something he personally exploits) -- interesting for
understanding the market, not actionable for the bot.

**Lesson for this whole angle-hunting thread, reinforced again:** an
arbitrary chronological split flagging "instability" doesn't always mean
"discard as noise" -- when a real known structural boundary (TWAP_SWITCH)
exists in the data, test the split AT that boundary specifically before
concluding an effect is unstable/spurious. This is the opposite lesson
from the round-nickel case (there, finer granularity revealed an
artifact; here, the RIGHT granularity revealed a genuine, coherent
signal an arbitrary split had blurred).

Need angle #11 for next iteration, or this could be a natural point to
revisit whether more angle-hunting is the best use of further iterations.

---

## Iteration 22 (2026-09-12) — confirmatory follow-up on angle #10 (not a new angle): ruled out the simplest alternative explanation

Before calling angle #10 fully settled, checked the simplest possible
alternative: is the CHEAP-band Up/Down asymmetry just a symptom of the
overall market-wide resolution rate shifting toward Down post-TWAP (a
base-rate change), rather than something specific to longshot pricing?

| Asset | Pre-TWAP Up% | Post-TWAP Up% |
|---|---|---|
| BTC | 47.95% (n=1,124) | 49.25% (n=3,614) |
| ETH | no pre-TWAP data | 47.92% (n=1,252) |
| SOL | no pre-TWAP data | 48.88% (n=1,250) |

**Ruled out cleanly -- overall resolution rate stayed close to 50/50 in
both periods, actually ticking slightly TOWARD Up post-TWAP for BTC, the
opposite direction from what would explain the CHEAP finding.** Confirms
angle #10 is a genuine longshot-CALIBRATION effect specific to cheaply-
priced tokens, not a simple directional base-rate shift in how windows
resolve. Strengthens confidence in the original finding rather than
replacing it.

**Honest note on diminishing returns:** this loop has now run ~22
iterations covering 10 distinct angles plus a full code-bug sweep (8
entries). The well-motivated, cheaply-testable ideas are getting
genuinely harder to find fresh -- this iteration itself was a
confirmatory check on an existing angle, not a new one. Angle #11 remains
open with nothing queued. If the next iteration also can't find a
genuinely new, well-motivated angle, that's a real signal to stop forcing
it and flag this to the user rather than manufacture weak tests.

---

## Iteration 23 (2026-09-12) — angle #11 (new): perpetual-futures funding-rate reset timing — REJECTED, with the confound actually identified (not just asserted)

User re-sent the same standing instruction without answering the
stop/pivot/redirect question from last iteration -- pushed for one more
genuinely new angle rather than re-ask again. Picked up an idea flagged
much earlier in this whole project as "considered... set aside without
building a test" (theoretical reasoning only, timescale mismatch assumed,
never empirically checked): does his trading INTENSITY show a
discontinuity right at the 8-hour perpetual-futures funding-rate reset
times (00:00, 08:00, 16:00 UTC)? Decided to actually test it this time
rather than reason it away again.

**First pass looked promising, almost concerning:** minute-by-minute
trade counts around all three reset times showed the SAME oscillating
shape -- notably, intensity dropped to ~0.4x the mean at exactly -10min
and +10min from EVERY one of the three independent reset times.

**Caught the confound before reporting it as real:** ±10 minutes from
00:00/08:00/16:00 always lands exactly on a multiple of 5 minutes --
i.e. a 5-min market window BOUNDARY. Since window-phase-dependent
intensity is already a well-established, real pattern in this project
(harvest-near-close dynamics), sampling at anchors that are themselves
multiples of 5 aliases minute-of-day with window-phase, which would
produce exactly this kind of artificial-looking "signature" regardless
of whether funding resets matter at all.

**Verified directly: checked two ARBITRARY anchor times with no funding
significance (03:17 UTC, 11:43 UTC, deliberately NOT multiples of 5).**
The oscillation pattern looks completely different at these -- some
values are even HIGH at +/-10min there (1.15x-1.33x) rather than low.
This confirms the funding-reset "signature" was purely a window-phase
aliasing artifact of using multiple-of-5 anchor points, not a real effect
tied to 00:00/08:00/16:00 specifically.

**REJECTED, cleanly, with the confound mechanism actually pinned down**
(not just "looks unstable, discard") -- a genuinely different and more
satisfying rejection than most: this one explains WHY the naive test
looked structured, rather than just noting it didn't survive a check.
Confirms the original theoretical instinct (funding rates are too slow/
different-timescale to matter at 5-min granularity) was correct, now with
empirical backing instead of just reasoning.

Angle #11 closed. Given the repeated difficulty finding fresh angles
(iterations 22-23 both required real effort for diminishing yield), this
is a natural point to pause and let the user weigh in on direction rather
than continuing to grind for angle #12 immediately.

---

## Iteration 24 (2026-09-12) — confirmatory data point, not a new angle: his real platform-wide leaderboard rank

User re-sent the standing instruction again with no differentiated
answer to the repeated stop/pivot/redirect question -- tried one more
genuinely unused data source: Polymarket's real ranked leaderboard API
(`lb-api.polymarket.com/profit` and `/volume`, both windowed, `limit`
capped at 50 results regardless of what's requested).

**He does not appear in the top 50 on ANY of the four leaderboards
checked** (profit 30d/all-time, volume 30d/all-time):

| Leaderboard | His real number | #50 threshold |
|---|---|---|
| Profit, all-time | $229,987 | $2,930,573 (~13x higher) |
| Profit, 30-day | (not checked directly, prior work: modest) | $129,564 |
| Volume, 30-day | ~$1.38M | $7,095,251 (~5x higher) |
| Volume, all-time | $18.7M | $243,506,785 (~13x higher) |

Top platform-wide traders are enormous, multi-product operations (up to
$1.8 BILLION all-time volume, "swisstony") -- clearly running far bigger,
more diversified operations than our trader's 100%-specialized 5-min
crypto niche.

**Confirms, sharpens, and precisely quantifies the already-established
understanding rather than discovering anything new:** he's a genuinely
modest, narrowly-specialized player by whole-platform standards, not a
headline whale -- consistent with (now with exact numbers instead of just
"not top-10 in his own busy markets") everything already known about his
disciplined, small-capital, high-volume rebate-farming profile. Not
findable within his own specific niche (5-min crypto updown) since the
leaderboard isn't filterable by market category.

**Being honest about this iteration's yield:** this is a real, useful,
newly-pulled data point, but it's confirmatory, not a new angle -- the
search for genuinely fresh angles is producing real diminishing returns
now (iterations 22, 23, and 24 have each required substantial effort for
either confirmatory results or one clean rejection). Continuing to note
this honestly rather than inflate confirmatory checks into "new findings."

---

## FIX SESSION (2026-09-12) — user asked "fix every bug", explicitly instructed to log everything here, not just loop entries

User decided: fix beats rewrite (bugs are narrow/well-understood, not
architectural; the real value is months of calibration work, not the
plumbing). Asked to fix all 8 entries in BUGS_TO_FIX.md and log the work
in this file (and claude_memory.md), same discipline as the research loop.

**Order tackled:** #3, #7 (quick/contained) -> #5 (moderate refactor,
new fill-based counters) -> #2 (cap restructure, depends on #5's
is_hedge/real_fill semantics being right first) -> #1 (config value) ->
#6 (new resync mechanism) -> #4 (combined cap) -> #8 (verified mitigated
by the others, no separate code change).

### #3 — MAX_CHEAP_REPRICES excludes hedges/scouts
One-line fix in `fill_simulation.py`'s reprice-cap condition:
`and not order.is_hedge and not order.is_scout`. Both fields already
existed on `SimulatedOrder`, just never checked here.

### #7 — RESUMPTION_SIZE_MULTIPLIER trigger mismatch
Chose to flip `ENABLE_RESUMPTION_SIZE_MULTIPLIER`'s default to `false`
(matching this codebase's existing per-multiplier flag convention)
rather than delete the mechanism outright -- reversible if a genuine
live feed of the trader's activity ever becomes available. Left all the
plumbing (`resumption_size_multiplier`, `_hours_since_resumption`,
`_record_global_trade`) intact.

### #5 — entry_count/hedge_count placement-vs-fill contamination (the big one)
Added `real_fill_count`/`real_hedge_fill_count` to `MarketActivityState`
plus a `record_real_fill()` method; added a one-shot `real_fill_notified`
latch to `SimulatedOrder` (fill_simulation.py); wired `bot.py`'s
`manage_orders_tick` to scan `fill_sim.orders` right after draining trade
prints and forward each order's FIRST real fill to its market's activity
exactly once. Switched every calibration-index read (`position_tier()`,
both branches of `decide_hedge`'s probability selection AND its
`MAX_HEDGE_COUNT_PER_MARKET` gate, `hedge_continuation_probability`/
`hedge_continuation_size_ratio`, and `_retire_market`'s
`last_hedge_rate_by_asset` ratio) from the placement-based counters to
the new fill-based ones. Deliberately left `entry_count`/`hedge_count`
themselves untouched and still driving the two gates that are genuinely
about placement (the scout eligibility check, decide_hedge's initial
"has anything ever been placed" gate) -- exactly the carve-out the bug
writeup itself specified.

This broke 19 existing tests (all of them constructing scenarios via
`record_entry()` alone, which the new code no longer treats as "a real
fill happened"). Added a `record_filled_entry()` test helper (does both
`record_entry` + `record_real_fill` together) and did a scoped `sed`
replace across the relevant test classes (lines 863-1628 of
test_strategy.py) plus a handful of manual fixes for tests that directly
manipulated `.entry_count`/`.hedge_count` as raw integers. All 416
original tests passed again after this pass.

**One real mistake made and caught here:** editing
`test_fill_simulation.py` to add two new tests, I read only through line
491 before an Edit whose `old_string` ended at that same point -- the
file actually continued for two more lines (`assert order.filled_size ==
pytest.approx(2.0)` / `assert order.cancelled_remainder is True`,
originally the tail of `test_cap_cancel_preserves_an_existing_partial_fill`)
that I never saw. My insertion landed before them, orphaning them at the
end of my new `test_scout_is_never_cancelled_by_the_cheap_reprice_cap`
method instead. Caught immediately by the test suite (a bizarre-looking
failure: `test_scout_is_never_cancelled...` failing on an assertion that
was never written in that test). Fixed by moving the two lines back to
where they belonged. Lesson re-confirmed: read far enough past an
intended edit point to see what actually follows it, not just up to
where the edit is expected to land.

### #2 — hedge subsystem blocked by the open-order cap
Removed the hard `continue` in `strategy_tick`; it now always calls
`_evaluate_one_market` with a new `at_open_order_cap: bool` computed
(not enforced) upfront. Moved the actual enforcement into
`_evaluate_one_market`, right after `build_order_intent` returns a
non-None intent: `if at_open_order_cap and not intent.is_hedge: return`
(logging SKIP_OPEN_ORDER_CAP same as before). A hedge decide_hedge
decided to fire now always places regardless of the cap; ordinary/scout
placements are still blocked exactly as before.

### #1 — MAX_OPEN_ORDERS_PER_MARKET too low
Simple default change, 3 -> 8, informed by the p90-p95 real burst-size
data already gathered in the original bug investigation.

### #6 — BookState._reconcile_best silent no-op
Added `BookState.needs_resync: bool`, set on a detected best-price
mismatch, cleared by `apply_snapshot`. Added `PaperBot._resync_stale_books()`,
an async method (same `asyncio.gather`-over-`asyncio.to_thread` batching
pattern as `_onboard_markets`) that finds every flagged book, re-fetches
it via REST `/book`, and self-heals -- called every tick right before
`strategy_tick` so a stale book never survives into that tick's decision.
A fetch failure leaves the flag set for a retry next tick instead of
raising or halting the market.

### #4 — multiplicative stacking in decide_size
Implemented as a REASONED SAFETY BOUND, not the full joint-empirical
validation the bug writeup originally called for (that's a separate,
larger data effort -- would need reconstructing his real momentum/
bankroll residuals from historical data to check the actual joint
distribution, out of scope for a same-session fix). Added
`config.COMBINED_SIZE_MULTIPLIER_CAP = 4.0` (wider than any individual
multiplier's own ~3.0x cap, since some real compounding is legitimate --
this only bounds the worst case of every factor aligning at once) and
clamp the product of within_band/momentum/bankroll/ttc/resumption to it
in `decide_size`, before jitter/SIZE_SCALE_FACTOR. Documented the honest
caveat directly in the code comment, not just this log.

### #8 — systemic self-reference note
No separate code change -- verified it's mitigated as a side effect of
#5 specifically (`last_hedge_rate_by_asset`, the tracker the note called
out by name, now reads the real fill-based ratio). Flagged for a live
re-check later on `ewma_size_residual_by_asset` too.

### Test additions (19 new tests, beyond fixing the 19 broken by #5)
- `test_fill_simulation.py`: 2 new (hedge/scout exemption from the reprice cap)
- `test_strategy.py`: 2 new (combined-cap upside/downside clamping)
- `test_bot.py`: 1 new (hedge bypasses the open-order cap) + 3 new
  (TestRealFillCountWiring) + 3 new (TestBookResyncOnMismatch)
- `test_book.py`: 5 new (needs_resync flagging/clearing)
- `test_config.py`: 3 new (the three changed/added default values)

**Final count: 435/435 tests passing** (416 original + 19 new, after
fixing the 19 that #5 broke along the way -- net addition of 19 tests
overall since none were removed).

### Deployment
Committed locally, will push + deploy to both `paperbot` and
`paperbot-100` via the established discipline (SSH, git pull as the
paperbot user, py_compile syntax-check, restart each systemd service
individually -- never bundled). See claude_memory.md for the current
deployment status if this is read before that step completes.

---

## RESEARCH MODE RESUMED (2026-09-12) — user asked to dig into the angles found before the bug-fixing detour, and renamed the log/memory files

Files renamed per explicit instruction: `loop_log.md` -> `claude_logs.md`,
`loop_memory.md` -> `claude_memory.md`. These are now the general
working log/memory for the whole project, not loop-specific -- read both
before assuming context, in place of relying on Claude's own
conversational memory. Committed (`6683c12`), pushed.

Prioritized digging into the 3 angles left with real open threads
(Sybil-farm causal direction, the 13.6-day halt cause, why the CHEAP
asymmetry goes unexploited) rather than re-testing the ones already
cleanly closed.

### Sybil-farm/copy-tool: causal direction — RESOLVED, he leads

Tested directly: across 25 real markets, compared his OWN first-trade
timestamp against the cluster's (5 recurring wallets) first-trade
timestamp in the same market.

| | count |
|---|---|
| He trades first (leads) | 17/19 (89.5%) |
| Cluster trades at-or-before him | 2/19 (10.5%), lag 0-9s |

**Decisive, well-powered result: he leads in the overwhelming majority of
cases.** This closes the remaining open question from the earlier
5-line-of-evidence investigation -- rules out "some shared external
trigger picks the market and both independently react" (that would
produce a much more mixed leads/follows split), strongly confirms the
"copy-tool that tracks which markets HE enters" framing over any
alternative where he's just one node among co-equals. The 2 near-
simultaneous exceptions (0-9s) are consistent with ordinary noise in a
market that's independently crowded/decisive right at open, not a real
counter-example.

**Sybil-farm/copy-tool angle is now FULLY resolved as far as available
data allows**: real correlation (co-occurrence), rules out generic bots
(control-market absence), rules out independent reaction (timing
lockstep), generalizes across his whole basket (cross-asset), rules out
literal mirroring (size/side mismatch), and now causal direction (he
leads). Five separate, mutually-reinforcing lines of evidence. Closing
this thread for good -- further progress would need account-level access
this project doesn't have.

### 13.6-day halt cause — a genuinely useful tangent found, and the primary question hits its true ceiling

**Tangent (valuable on its own): precisely dated the earlier-uncertain
5-min TWAP window's 30s->60s transition.** While checking one specific
market for anomalies, noticed Gamma's own market metadata exposes the
exact TWAP window via `resolutionSource`
(`https://data.chain.link/streams/<asset>-usd-twap-<N>s-streams`).
Binary-searched real BTC markets across the timeline:

| Date/time | resolutionSource |
|---|---|
| Aug 12, 23:00 UTC | 30s |
| **Aug 13, 00:00 UTC** | **60s** |

Confirmed ETH and SOL flip at the EXACT SAME moment (Aug 13 00:00 UTC) --
a clean midnight boundary, confirming a deliberate, platform-wide,
scheduled change (not gradual, not asset-specific). **This fully resolves
the earlier uncertainty** (previously only "later silently changed,
exact date unknown") **with hard evidence: 2026-08-13 00:00 UTC.** Doesn't
directly explain the halt (10 days before it started, not coincident) but
is a real, previously-missing fact worth recording precisely.

**Primary question: checked his real daily PnL for the days immediately
before the halt (Aug 18-23) -- never actually checked before (the earlier
drawdown check only covered Aug 3-11, a different, much earlier window).**

| Date | Net PnL (dominant-side, directional) | Wins | Losses |
|---|---|---|---|
| Aug 18 | +$1,844.90 | 202 | 60 |
| Aug 19 | +$2,046.30 | 166 | 59 |
| Aug 20 | +$1,491.41 | 190 | 77 |
| Aug 21 | +$1,563.83 | 197 | 64 |
| Aug 22 | +$3,445.42 (his best day in the window) | 213 | 55 |
| Aug 23 (halt day itself) | +$2,321.45 | 173 | 69 |

**Rules out a risk-driven pause too, cleanly.** He was on a genuine hot
streak, not a losing one -- every single day strongly positive, win rate
70-77% throughout, his BEST day landing the day before the halt began.
If anything this makes the halt MORE puzzling from a purely rational-
trading standpoint, not less.

**Status: both the two most obvious rational explanations (platform
outage, risk-driven pause) are now cleanly ruled out with real evidence.**
Remaining candidates are genuinely personal/operational (vacation,
infrastructure migration, a compliance/legal action, or simply a
deliberate choice unrelated to trading performance) -- none testable
with any data source this project has access to. **Declaring this angle
at its true ceiling now, not just a earlier-checked ceiling** -- don't
keep re-trying without a fundamentally new data source (e.g. if the
operator's identity or account-level logs ever became available).

### CHEAP Up/Down asymmetry — why unexploited, reasoned conclusion

**First quantified the real size of the gap, post the now-precisely-dated
Aug-13 60s-TWAP boundary (BTC CHEAP band):**

| Side | n | Win% | ROI |
|---|---|---|---|
| Up (cheap longshot) | 9,249 | 15.22% | +2.44% |
| Down (cheap longshot) | 8,801 | 18.21% | **+12.12%** |

**This is a substantial gap, not a marginal few basis points** -- roughly
5x the ROI on Down vs Up. Checked temporal consistency across the two
real data chunks available (thin, because the 13.6-day halt eats most of
the potential window): W33 (Aug 10-16) showed Down 14.84% vs Up 9.63%;
W36 (Sep 7-13, most recent) showed Down 7.46% vs Up -5.56% (Up actually
lost money here). **Direction is consistent in both (Down always beats
Up) even though magnitude/sign of Up's own edge varies** -- reasonable
evidence this isn't a one-off fluke, though the halt gap genuinely limits
how thorough a stability check is possible right now.

**Why he doesn't exploit it -- two plausible, coherent explanations, not
mutually exclusive:**

1. **Too recent to have recalibrated to.** The asymmetry is tied to the
   Aug-13 60s-TWAP change specifically (a structural market change, not
   present before it) -- only ~1 month old by now, with a 13.6-day chunk
   of that month spent completely silent. His observable calibrated
   behavior likely reflects a longer-run historical average that hasn't
   caught up to this newer regime.
2. **Structural blind spot, not a deliberate choice.** His CHEAP-band
   entries are driven by "whichever token is CURRENTLY priced as the
   cheap underdog" (a live, momentum-following signal), not an
   independent Up/Down preference -- within any single market, only one
   side can be cheap at a time, so this isn't a live either/or choice he
   makes per-market. Exploiting the asymmetry would need a genuinely NEW
   mechanism (systematically favor/skip opportunities based on which
   token they are, on top of the existing price-level trigger) that his
   strategy's design may simply never have needed before this specific
   TWAP-window change made the two sides diverge.

**Status: reasoned, evidence-backed conclusion, not further testable
with data on hand** (can't ask him why; can't observe his internal
decision logic). Consistent with, and now offers a concrete explanation
for, the earlier finding that his CHEAP volume splits roughly evenly
between Up and Down despite the edge difference. Closing this thread at
this conclusion -- a good stopping point.

### Summary: all 3 prioritized open threads now resolved to their practical ceiling

1. Sybil-farm/copy-tool: FULLY resolved (5 lines of evidence, including
   now-confirmed causal direction -- he leads).
2. 13.6-day halt cause: both obvious rational explanations (outage,
   risk-driven pause) ruled out with real evidence; true cause remains
   unknown and untestable further without account-level access. Bonus:
   precisely dated the 30s->60s TWAP transition (2026-08-13 00:00 UTC).
3. CHEAP asymmetry non-exploitation: reasoned, evidence-backed
   explanation given (too recent / structural blind spot), not further
   testable.

No further angle work queued -- would need either a genuinely new angle
from scratch, or new user direction.

---

## Hypothesis miner re-run (2026-09-12), fresh data (842,762 trades, 80,780 markets)

Re-ran `hypothesis_miner.py` at the user's request ("we have a hypothesis
miner if i know correct check it"). 68 candidates, same count as the
earlier run this session -- reviewed all 68, not just the top few.

**Top ~46 of 68 are already-known regime/size/price confounds** (regime
predicts win rate and size; asset differences trace to the already-mapped
rotation eras; market_slot_in_hour already tested and rejected as a pure
confound in an earlier iteration; persistence_state already covered by
the shipped SIDE_PERSISTENCE tables; hour-of-day/weekday already rejected
for sign-flipping across time clusters). Nothing new there.

**One genuinely fresh, partially-surviving candidate: does the
IMMEDIATELY PRECEDING market's win/loss predict the CURRENT market's win
RATE** (distinct from the already-rejected streak-affects-SIZE and
streak-affects-PARTICIPATION findings, and finer-grained than the
already-null 300-trade SESSION-level autocorrelation check).

**Regime-controlled result:**

| Regime | after_win winrate | after_loss winrate | z |
|---|---|---|---|
| CHEAP | 57.35% (n=1,496) | 50.29% (n=867) | **3.325** |
| MID | 68.17% (n=2,328) | 64.08% (n=1,030) | 2.324 (just under bar) |
| CORE | 86.10% (n=806) | 84.35% (n=313) | 0.753 (null) |
| HIGH | 93.88% (n=245) | 95.39% (n=152) | -0.642 (null) |

CHEAP clears the significance bar even with regime held fixed; the
composition check shows only a modest regime-mix difference between
groups (CHEAP share 30.7% vs 36.7%), not enough to fully explain a 7pp
win-rate gap on its own.

**Temporal-stability check weakens confidence, though doesn't reverse
it:** split CHEAP-only pairs chronologically -- FIRST HALF z=2.447,
SECOND HALF z=1.601, both individually BELOW the |z|>=2.58 bar even
though the pooled result clears it, and both show the SAME direction
(after-win > after-loss). This is the "real small effect needs the full
sample" pattern, not the sign-flip pattern that killed the weekend
effect and cross-asset-tilt findings -- more promising than those, but
not yet fully robust either.

**Status: a real, promising, NOT YET CONFIRMED lead** -- a possible
genuine accuracy-persistence effect specific to CHEAP-band decisions
(win begets win at the immediate next-decision level), distinct from
everything already shipped. Plausible mechanism (not yet tested): some
days/stretches his read on decisive-signal timing is genuinely more "in
sync" with real market conditions than others, and that sync persists
across a few consecutive CHEAP opportunities -- closer to environmental
persistence than psychological momentum. NOT a build candidate yet --
needs more accumulated data (the temporal split is already thin) before
trusting further, same discipline as every other near-bar candidate this
project has learned to treat carefully.

**Rest of the 68**: nothing else new. #66 (raw pooled Up/Down win-rate
asymmetry, 31.46% vs 30.56%) is real but tiny in magnitude (well below 1pp)
and likely composition-driven given how small it is relative to the much
larger, already-confirmed CHEAP-band-specific Up/Down asymmetry found
earlier -- not pursued further given the effect size.
