# Known bugs / interaction issues to fix

Running list from an audit of how the bot's many shipped multipliers/caps
interact with each other, not just whether each was individually validated.
Each entry: what's wrong, why, and a recommended fix.

**STATUS (2026-09-12): all 7 discrete bugs (+1 systemic note) FIXED,
tested (435/435 passing, 19 new tests added), and deployed to both
paperbot and paperbot-100.** See each entry below for the exact fix
applied. Full narrative log of the fix work is in `loop_log.md`; condensed
current state in `loop_memory.md`.

---

## 1. `MAX_OPEN_ORDERS_PER_MARKET` is too low relative to real trader behavior

**File:** `paperbot/config.py`

Currently `MAX_OPEN_ORDERS_PER_MARKET = 3`. Live diagnostic (2026-09-12,
~2.5min sample after adding `SKIP_OPEN_ORDER_CAP`/`SKIP_NO_BOOK` logging):
**273 skips from hitting this cap vs only 9 actual trades placed** — a 30:1
ratio, by far the dominant reason our main (unconstrained) bot trades far
fewer times per market (3.9) than the real trader (14.2) in a matched
since-restart window.

Real-data calibration check (max trades landing in any rolling 5-second
window, across 74,665 of his real markets):

| Percentile | Burst size |
|---|---|
| p50 | 3 |
| p75 | 5 |
| p90 | 7 |
| p95 | 9 |
| p99 | 16 |
| max | 95 (outlier) |

Our cap of 3 matches only his *median*. Caveat: this can't fully separate
genuinely-simultaneous resting orders from very fast sequential fills (we
only have fill timestamps, not order-placement timestamps) — but even under
the sequential-fill interpretation, our 3-second tick can't cycle
fill→replace fast enough to hit 5+ fills in 5 seconds on its own, so more
concurrent slots are needed either way.

**Fix:** raise `MAX_OPEN_ORDERS_PER_MARKET` from 3 to ~7-9 (covers p90-p95
of his real behavior without jumping to the rare extreme tail). Redeploy,
then re-measure entries/market and the skip:trade ratio after some live
time, same "raise with evidence, don't guess" discipline the original
constant's own docstring specified.

**Status:** FIXED 2026-09-12. Raised default from 3 to 8 in
`paperbot/config.py`. New test: `test_config.py::TestBugAuditDefaults::test_max_open_orders_per_market_raised_to_eight`.

---

## 2. `MAX_OPEN_ORDERS_PER_MARKET` silently blocks the entire hedge subsystem

**File:** `paperbot/bot.py`, `strategy_tick` (~line 451)

```python
open_here = self.resting_order_ids.get(cid)
if open_here and len(open_here) >= config.MAX_OPEN_ORDERS_PER_MARKET:
    continue
```

This check runs **before** `_evaluate_one_market` → `build_order_intent` →
`decide_hedge` is ever called. If a market already has
`MAX_OPEN_ORDERS_PER_MARKET` resting orders — which is exactly when price is
active and a hedge is most likely needed — the whole tick is skipped and
`decide_hedge` never runs. There is no reserved capacity or priority for
hedges; they compete for the same fixed slot budget as ordinary and scout
entries.

**Effect:** the entire hedge-calibration suite —
`ADVERSE_MOVE_HEDGE_TRIGGER_MULTIPLIER`, `CONVICTION_HEDGE_MULTIPLIER`,
`ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER`, `CROSS_MARKET_HEDGE_RATE_MULTIPLIER`
— can be completely bypassed by a cap tuned only with entry/fill-rate in
mind, never with the hedge subsystem in mind. Likely a real contributor to
our bots' hedge/dual-sided rates running below the trader's real rates.

**Fix options (pick one):**
- (a) Reserve one slot for hedge-only placement: if `open_here` is at cap
  but the market has a dominant side established, still allow
  `build_order_intent`'s hedge path to be evaluated and placed (past the
  cap) when `decide_hedge` says yes.
- (b) Check hedge eligibility BEFORE the cap gate, and only apply the cap to
  non-hedge (ordinary/scout) entries.

Option (a) is closer to "reserve capacity for hedges" without changing
`decide_hedge`'s own logic; option (b) is a smaller code change. Needs a
decision before implementing.

**Status:** FIXED 2026-09-12 (a hybrid of both options). The hard
pre-check in `strategy_tick` was removed; `_evaluate_one_market` now
always evaluates (computing `at_open_order_cap` instead of skipping), and
the cap is enforced only AFTER `build_order_intent` returns, rejecting
the placement only `if at_open_order_cap and not intent.is_hedge`. A
hedge `decide_hedge` actually decided on always places regardless of the
cap. New tests: `test_bot.py::TestOpenOrderCapPerMarketPolicy::test_a_hedge_places_even_at_the_cap_ordinary_entries_do_not`.

---

## 3. `MAX_CHEAP_REPRICES` cancels hedges and scouts despite the finding behind it excluding them

**Files:** `paperbot/fill_simulation.py` (`manage_open_orders`), validated
against `cheap-fill-calibration-gap.md` (project memory)

```python
if (config.ENABLE_CHEAP_REPRICE_CAP and order.regime == "CHEAP"
        and order.reprice_count >= config.MAX_CHEAP_REPRICES):
    ...  # cancel
```

No check on `order.is_hedge` or `order.is_scout` — even though
`SimulatedOrder` already carries both fields (set from `intent.is_hedge` /
`intent.is_scout` at placement, confirmed in code). But the real-data
finding that justified this cap was measured explicitly **"excluding
hedges and scouts (not directional edge bets)"** (memory file, `Method:`
section). The cap has never been validated against hedge/scout behavior at
all, yet it ships applying to them.

**Effect:** a hedge that lands in CHEAP band — exactly the case
`ABSOLUTE_PRICE_HEDGE_SIZE_MULTIPLIER`'s U-shaped curve deliberately sizes
*up* — can get cancelled by this cap before it ever fills. Two shipped,
carefully-calibrated mechanisms directly undermining each other.

**Fix:** add `and not order.is_hedge and not order.is_scout` to the cap
condition, matching the original validation's scope exactly.

**Status:** FIXED 2026-09-12, exactly as recommended. New tests:
`test_fill_simulation.py::TestCheapRepriceCap::test_hedge_is_never_cancelled_by_the_cheap_reprice_cap`
and `test_scout_is_never_cancelled_by_the_cheap_reprice_cap`.

---

## 4. Multiplicative stacking in `decide_size` never validated jointly

**File:** `paperbot/strategy.py`, `decide_size` (~line 484)

```python
return max(median * jitter * within_band * momentum_mult * bankroll_mult
           * ttc_mult * resumption_mult * config.SIZE_SCALE_FACTOR, 0.0), False
```

Up to six independent multipliers compound on the same first-entry notional
for ETH/SOL (`within_band`, `momentum_mult`, `bankroll_mult`, `ttc_mult`,
`resumption_mult`, plus jitter). Each was fit **marginally** — controlling
for other already-known variables individually at the time it was built —
but the combined, multiplied-together output has never been checked against
his real observed size distribution as a joint quantity.

Specific risk: `momentum_mult` (EWMA of size residual) and `bankroll_mult`
(realized P&L-conditioned sizing) both plausibly pick up overlapping
"recent performance" signal. Multiplying them risks double-counting the
same real effect into a combined swing more extreme than anything actually
observed.

**Fix:** not urgent, but before trusting this further: pull real sizing
data for markets where multiple of these conditions fire simultaneously
(e.g. ETH/SOL first entries during a size-momentum streak AND a bankroll
drawdown at once) and check whether the ACTUAL combined multiplier implied
by his real size in those cells matches the product of the two independently
-fit multipliers, or overshoots it. If it overshoots, consider a dampened
combination (e.g. geometric mean of the two, or capping the joint product)
instead of a flat product.

**Status:** FIXED 2026-09-12, as a reasoned safety bound rather than the
full joint-empirical validation (that remains a separate, larger analysis
not yet done -- see the honest caveat in the fix itself). Added
`config.COMBINED_SIZE_MULTIPLIER_CAP` (4.0, wider than any individual
multiplier's own ~3.0x cap) and clamp the product of
within_band/momentum/bankroll/ttc/resumption to it in `decide_size`
before applying jitter/SIZE_SCALE_FACTOR. New tests:
`test_strategy.py::TestSizingDecision::test_combined_multiplier_cap_bounds_worst_case_compounding`
and `test_combined_multiplier_cap_also_bounds_the_downside`.

---

## 5. `entry_count` still counts unfilled placements, even though `cost_by_side` was already fixed for the identical bug

**File:** `paperbot/strategy.py`, `MarketActivityState.record_entry` /
`release_unfilled`

`record_entry()` increments `self.entry_count` at **placement** time, for
every order placed regardless of whether it ever fills. A 2026-09-09 fix
(`release_unfilled`, see its own docstring) already discovered and
corrected the identical problem for `cost_by_side`: *"38.6% of placed
orders get cancelled, 93% of those fully unfilled... roughly a third of
tracked cost_by_side was phantom exposure that never actually happened."*
`release_unfilled` is called from `bot.py`'s `manage_orders_tick` whenever
an order goes CANCELLED/EXPIRED_UNFILLED/partially-unfilled, and correctly
subtracts the never-filled notional back out of `cost_by_side`.

**But `entry_count` itself is never touched by `release_unfilled` or
anywhere else** — grepped the whole codebase, no decrement exists. Given
the same ~36% non-fill rate the 2026-09-09 fix measured, `entry_count`
still runs systematically ahead of how many of THIS market's entries
actually filled.

**Why this matters:** `entry_count` (or `entry_count - 1` as `attempt_index`
/ `entry_index`) directly indexes into:
- `position_tier_for_index` → `decide_size`'s `ENTRY_SIZING_USD` lookup
  (first / 2nd_3rd / 4th_plus)
- `hedge_attempt_hazard(asset, regime, attempt_index)` in `decide_hedge`

Both tables were calibrated from the trader's real trade log, which (as
already established elsewhere in this project — see the
`QUEUE_SAFETY_FACTOR` docstring's own "his trade log only contains fills"
caveat) contains only **fills**, i.e. the index in the calibration data
means "this was his Nth real fill in this market," not "the Nth order he
attempted." Our bot's `entry_count` means the latter. Every market that
has had at least one cancelled/expired placement drifts these two
definitions apart — the bot ends up looking up a HIGHER position tier /
hedge-hazard index than the market has actually earned via real fills,
systematically shifting size and hedge-probability decisions away from
what the calibration intended.

**Fix:** add a `real_fill_count` (or similar) to `MarketActivityState`,
incremented only when an order actually fills (fully or partially) rather
than at placement, and use that — not `entry_count` — everywhere
`position_tier`/`hedge_attempt_hazard`'s index is read. `entry_count` can
stay as-is if it's still needed for the "is this the market's first
placement" scout gate (`activity.entry_count == 0` in `build_order_intent`),
since that check is about placement, not fill history — but every
calibration-table lookup keyed by "which numbered entry" should switch to
the fill-based counter.

**Status:** FIXED 2026-09-12. Added `real_fill_count`/`real_hedge_fill_count`
to `MarketActivityState` plus `record_real_fill()`; added a one-shot
`real_fill_notified` latch to `SimulatedOrder`; `bot.py`'s
`manage_orders_tick` now forwards each order's first-ever fill to its
market's activity right after draining trade prints. Every calibration-
index lookup (`position_tier()`, `hedge_attempt_hazard`,
`hedge_continuation_probability`/`hedge_continuation_size_ratio`, the
`MAX_HEDGE_COUNT_PER_MARKET` gate, and `last_hedge_rate_by_asset`'s ratio)
now reads the fill-based counters instead of the placement-based ones.
`entry_count`/`hedge_count` themselves are unchanged and still drive the
placement-time-only gates (scout eligibility, decide_hedge's initial
"anything ever placed" check). New tests:
`test_bot.py::TestRealFillCountWiring` (3 tests).

**Downstream consequence, same root cause:** `bot.py` computes
`last_hedge_rate_by_asset[asset] = activity.hedge_count / activity.entry_count`
per market (feeds `CROSS_MARKET_HEDGE_RATE_MULTIPLIER` for the *next*
market). Both the numerator (`hedge_count`, separately ceiling-capped by
`MAX_HEDGE_COUNT_PER_MARKET`) and the denominator (`entry_count`, inflated
by unfilled placements per #5) are systematically distorted versions of
what a real-fill-only ratio would show — so this ratio understates the
true hedge rate the calibration (`CROSS_MARKET_HEDGE_RATE_MULTIPLIER`) was
fit against on the trader's real, fill-only data. Worth re-checking once
#5 is fixed, before assuming this needs a separate fix of its own.

**Extended scope, found during the full behavior_config.py sweep:**
`record_entry`'s `if is_hedge: self.hedge_count += 1` has the IDENTICAL
placement-vs-fill contamination as `entry_count` — incremented
unconditionally at placement, never decremented by `release_unfilled` (which
only touches `cost_by_side`) when that specific hedge order never fills.
This means the bug reaches further than first scoped:
- `decide_hedge`'s own gate, `activity.hedge_count >= config.MAX_HEDGE_COUNT_PER_MARKET`
  — the safety ceiling can trip on placement attempts that never filled,
  cutting off genuine further hedging earlier than the real cap intends.
- `hedge_continuation_probability(activity.hedge_count)` and
  `hedge_continuation_size_ratio(activity.hedge_count + 1)` — both keyed
  directly by `hedge_count`, both calibrated on real, fill-only hedge
  indices (`HEDGE_CONTINUATION_PROBABILITY`/`HEDGE_CONTINUATION_SIZE_RATIO`'s
  own docstrings confirm "decision-collapsed" REAL trade data).

Compounds directly with bug #2: a hedge that gets blocked from ever
placing by the open-order cap obviously can't inflate `hedge_count` (never
placed at all), but a hedge that DOES place and then gets cancelled/expires
unfilled (e.g. via bug #3's reprice-cap misfire, or an ordinary drift-cancel)
still counts toward `MAX_HEDGE_COUNT_PER_MARKET` and shifts every later
continuation lookup to the wrong index — on top of, not instead of, #2's
separate blocking effect.

**Fix, extended:** the same `real_fill_count`-style correction proposed
above for `entry_count` should track hedge fills separately too (or the
existing `hedge_count` should only increment on a real fill, matching
`cost_by_side`'s discipline) — not just position_tier/hedge_attempt_hazard
lookups, but every hedge_count-keyed lookup and the MAX_HEDGE_COUNT_PER_MARKET
gate itself.

---

## 6. `BookState._reconcile_best` is a documented no-op — book staleness has no bounded correction path (plausible risk, NOT confirmed via live data like #1-5)

**File:** `paperbot/book.py`, `BookState._reconcile_best` (~line 158) and
`apply_price_change` (~line 136)

```python
def _reconcile_best(self, best_bid, best_ask) -> None:
    # If the message asserts a best price we don't have a matching
    # level for (e.g. we missed an earlier update), insert a synthetic
    # level so best_bid/best_ask stay correct until the next snapshot.
    if best_bid is not None and (not self.bids or self.bids[0][0] != float(best_bid)):
        pass  # don't fabricate size; rely on next full snapshot to correct depth
    if best_ask is not None and (not self.asks or self.asks[0][0] != float(best_ask)):
        pass
```

The outer comment describes an intended fix (insert a synthetic level so
`best_bid`/`best_ask` stay correct); the actual body does nothing, per a
different inline comment explaining the decision not to fabricate a
sizeless level. Reasonable on its own — you can't safely simulate queue
position against a level of unknown size — but the practical consequence
stands regardless of why: **if a `price_change` WS message ever asserts a
best price our local book has no matching level for (a missed earlier
update), `best_bid`/`best_ask` keep reading the stale value with no
correction until something replaces the whole book via `apply_snapshot`.**

Checked where `apply_snapshot` (the only real fix) is ever called:
1. Once per market, at onboarding (`bootstrap_book_state` via REST).
2. Whenever the exchange itself chooses to push a `"book"`-type WS message.

**There is no periodic REST re-poll of `/book` mid-market-life anywhere in
`bot.py`.** So the only correction paths are an exchange-initiated
snapshot push (frequency/guarantee not documented in this codebase) or a
full WS reconnect (which does happen, via `_ws_supervisor`'s backoff loop,
but only on a genuine disconnect, not on an ordinary missed/dropped
message that doesn't kill the connection). `best_bid`/`best_ask` feed
literally every order-placement, spread, and drift-reprice decision in
`strategy.py`/`fill_simulation.py` — a silent, uncorrected staleness here
would be invisible everywhere downstream.

**Honest caveat:** this is a **plausible architectural risk found by
reading the code**, not something confirmed via live data the way issues
#1-5 were (no live log evidence of an actual missed price_change /
resulting stale best_bid has been checked yet). The class docstring itself
already flags this whole file's live validation as thin ("this layer's
only real validation is one live deployment... treat further live runs as
still-active integration testing"). Worth checking for live evidence
before prioritizing a fix — e.g., logging whenever `_reconcile_best`'s
`if` conditions are true (i.e. a mismatch WAS detected) to see if this
ever actually fires in practice, the same "add a diagnostic before
assuming it's real" discipline already used for issues #1/#5.

**Fix options, once/if confirmed real:** (a) trigger an on-demand REST
`/book` re-fetch specifically when a mismatch is detected (self-healing
within one tick instead of waiting for luck), or (b) a periodic
(every N seconds) full REST re-sync per tracked token regardless of any
detected mismatch, as a time-bounded safety net.

**Status:** FIXED 2026-09-12, fix option (a) implemented directly (given
the instruction to fix everything, skipped the diagnostic-only
intermediate step). Added `BookState.needs_resync`, set by
`_reconcile_best` on a detected mismatch and cleared by `apply_snapshot`;
added `PaperBot._resync_stale_books()`, called every tick right before
`strategy_tick`, which batches concurrent REST `/book` re-fetches (same
`asyncio.gather`-over-`asyncio.to_thread` pattern as `_onboard_markets`)
for every flagged token and self-heals within one tick. A transient fetch
failure leaves the flag set for a retry next tick rather than raising.
New tests: `test_book.py` (5 new tests on `_reconcile_best`/`needs_resync`)
and `test_bot.py::TestBookResyncOnMismatch` (3 tests).

---

## 7. `RESUMPTION_SIZE_MULTIPLIER` triggers on OUR BOT's own trading gaps, not the real trader's — a calibration/trigger mismatch, different class of bug from #1-6

**Files:** `paperbot/bot.py` (`_record_global_trade`, `_hours_since_resumption`,
`__init__` lines ~226-237), `paperbot/behavior_config.py`
(`resumption_size_multiplier`, `RESUMPTION_SIZE_MULTIPLIER`)

`RESUMPTION_SIZE_MULTIPLIER`'s whole 3-phase curve (suppressed 0-6h →
overshoot 7-9h → back to baseline 10h+) was measured from studying how the
**real trader** behaved in the hours after his one genuine long silence
ended (the 327.46h/13.6-day gap, Aug 23 → Sep 6 — the same gap this
session's research thread found and flagged as still causally unexplained).

But the trigger condition that decides when to APPLY this curve in our own
bot has nothing to do with the trader's activity at all:

```python
def _record_global_trade(self, now: float) -> None:
    if self._last_trade_at is not None:
        gap_hours = (now - self._last_trade_at) / 3600.0
        if gap_hours >= bc.RESUMPTION_GAP_THRESHOLD_HOURS:
            self._resumption_started_at = now
    self._last_trade_at = now
```

`_last_trade_at` tracks **our own bot's** last placed trade (any
market/asset), purely in-memory, never persisted or compared against the
trader's real trade timing at all. Whenever OUR bot itself goes
`>= RESUMPTION_GAP_THRESHOLD_HOURS` (4h) between two of its OWN trades —
for ANY reason: a genuinely quiet market with no qualifying entries across
all three assets/four regimes for that long, an extended outage, or just
being starved by bugs #1/#3/#5 above making real fills rarer than they
should be — the bot applies the trader's-real-13.6-day-silence-shaped
caution curve to itself, even though the trader may have been trading
completely normally the whole time.

**Why this matters:** this doesn't just risk a wrong multiplier value — it
actively works AGAINST the goal of replicating the trader, per this
project's own stated filter ("does this help replicate the trader better,"
not just "is it statistically real"). The trigger event (our bot's
incidental gap) and the calibrated event (his real, specific silence) are
different things that happen to share a shape by coincidence of
measurement, not by any causal link. Every other cross-market/global
signal in this codebase (`last_first_entry_won_by_asset`,
`rolling_accuracy_by_asset`, `last_hedge_rate_by_asset`, etc.) is fed from
the TRADER's real observed outcomes; this is the one exception that's fed
from the BOT's own incidental behavior instead.

**Fix options:**
- (a) Remove the mechanism entirely — it was fit to a single n=1 real
  event (the code's own docstring already admits "necessarily built from
  ONE large real gap event... no basis to split this by asset"), and its
  trigger can never faithfully reproduce "the trader had a long silence"
  without an actual live feed of the trader's own trades to compare
  against (which this paper-trading bot doesn't have at runtime — it only
  mirrors calibrated statistics, not his live feed).
- (b) If kept, at minimum gate it more conservatively — e.g. only apply it
  following an actual bot RESTART with a long uptime gap (a proxy for "the
  bot was down," closer in spirit to a real outage) rather than any
  incidental gap between fills during continuous operation.

**Status:** FIXED 2026-09-12, a variant of fix (a) that keeps the
plumbing intact for future use rather than deleting it: flipped
`config.ENABLE_RESUMPTION_SIZE_MULTIPLIER`'s default from `"true"` to
`"false"`, matching this codebase's existing convention (every multiplier
already has an `ENABLE_*` flag) rather than ripping out
`resumption_size_multiplier`/`_hours_since_resumption`/
`_record_global_trade` — if a genuine live feed of the trader's own
activity is ever available, the mechanism can be correctly re-enabled
with a real trigger source later. New test:
`test_config.py::TestBugAuditDefaults::test_resumption_multiplier_disabled_by_default`.
Existing tests that cover the multiplier FUNCTION's own behavior in
isolation (`test_resumption_multiplier_suppresses_size_early_and_overshoots_later`)
now opt back in explicitly via `monkeypatch`.

---

## 8. Systemic note (lower confidence, not a discrete break): self-referential cross-market trackers can propagate our OWN bugs forward instead of anchoring to the trader

**Files:** `paperbot/bot.py` (`ewma_size_residual_by_asset`,
`last_hedge_rate_by_asset`), `paperbot/behavior_config.py`
(`cross_market_size_momentum_multiplier`, `cross_market_hedge_rate_multiplier`)

Different in kind from bug #7 (a clean event-type mismatch) — this is a
softer, structural observation. `CROSS_MARKET_SIZE_MOMENTUM_MULTIPLIER` was
calibrated on the real trader's own serial correlation (his size at market
N, relative to typical, predicts his size at N+1). Our bot necessarily
feeds it **our own bot's** previous first-entry-size residual
(`ewma_size_residual_by_asset`, updated from `intent.notional_usd` — our
own decided size), not the trader's, because the bot has no live feed of
his actual trades at runtime — only offline calibration tables. Reasonable
as a design choice (assumes: if we're already tracking him well on
average, our own residual sequence approximates his), but it means **any
existing bias in our own sizing (e.g. from bugs #1/#4/#5) doesn't stay
isolated to one bad decision — it gets fed forward through this momentum
tracker into future first-entry sizing too.** Same structural pattern
already noted as a downstream consequence under bug #5 for
`last_hedge_rate_by_asset` (fed from our own possibly-contaminated
`hedge_count`/`entry_count`, not his real ratio).

Contrast with `last_first_entry_won_by_asset`/`rolling_accuracy_by_asset`:
those are safe from this issue because "did this market's chosen side
actually win" is an objective fact about the real world, identical for the
trader and our bot (it's the same real market resolving the same way) —
no self-reference risk there.

**Not a fix in itself** — flagging so that fixing bugs #1/#4/#5 gets
weighed as having a bigger effect than their direct impact alone: they
also stop propagating bias through momentum/hedge-rate feedback into
future markets. No action needed beyond awareness until #1/#4/#5 are
addressed; re-check whether this still matters afterward.

**Status:** MITIGATED as a side effect of fixing #1/#4/#5 (2026-09-12) —
#5's fix in particular means `last_hedge_rate_by_asset` (the tracker this
note specifically called out) now reads the real, fill-based ratio
instead of the placement-contaminated one. Not a separate code change of
its own; re-check with live data after some runtime whether residual bias
still propagates through `ewma_size_residual_by_asset` now that its
upstream sizing decisions are less distorted.

---

## Still looking

This file will be updated as more issues are found during an ongoing code
audit. All 7 discrete bugs + the systemic note are now fixed and deployed
(see loop_log.md for the full narrative of the fix session, including one
editing mistake -- two orphaned assertion lines from an incomplete Read
before an Edit -- caught by the test suite and fixed during the process).
Not necessarily exhaustive -- a future audit pass could still find more.
