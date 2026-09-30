# Bot Functionality Reference

Full function-level inventory of the paper-trading bots, current as of the
2026-09-29 function-minimization pass (196 → 164 functions across the
package). This documents *what exists and what it does* — see git history
and `project memory` for *why* each mechanism was built and its calibration
provenance.

## Architecture

Two parallel decision paths share the same calibration data:

- **`behavior_config.py`** — pure calibration tables + lookup/formula
  functions. No state, no I/O, no side effects. Everything here is
  "confirmed real, measured from the trader's own on-chain/off-chain
  history" — nothing is a guess.
- **`strategy.py`** — decision orchestration for `paperbot`, `paperbot-100`,
  `paperbot-combined`. Threads live per-market state
  (`MarketActivityState`) through `behavior_config.py`'s functions to
  produce order intents.
- **`consolidated_engine.py`** — the same decisions, collapsed into 4
  functions, used only by `coinbase-bot`. No `ENABLE_*` feature flags —
  every mechanism folded in here has already been verified real elsewhere.
- **`bot.py`** — the live asyncio loop: market discovery, WS book state,
  calling into `strategy.py`, fill simulation, settlement.
- **`coinbase_bot.py` / `coinbase_feed.py` / `coinbase_strategy.py`** — a
  separate frozen-control experiment bot that forces its first-entry side
  from Coinbase spot momentum instead of the trader's own patterns.

---

## `behavior_config.py` (44 functions)

### Regime classification & core sizing formulas

| Function | Signature | What it does |
|---|---|---|
| `classify_regime` | `(price) -> str` | Buckets a 0–1 price into CHEAP (0–0.30) / MID (0.30–0.70) / CORE (0.70–0.90) / HIGH (0.90–1.0). Shared boundary logic used everywhere else in the file. |
| `table_lookup` | `(table, *keys, default=1.0) -> float` | Generic nested-dict lookup added 2026-09-28, backing 7 of the simplest per-(asset, regime) calibration tables (see "Merged into `table_lookup`" below). Walks keys in the exact order the caller passes — some tables are asset-first, others regime-first. |
| `floor_lot_probability` | `(asset, regime, position_tier) -> float` | P(this entry is a floor-lot "probe" trade) — a real, deliberate secondary sizing behavior, not noise. Raises `BehaviorLookupError` on an uncalibrated cell (deliberately strict, no silent 1.0 fallback). |
| `position_tier_for_index` | `(entry_index) -> str` | Maps a 0-based entry index to `"first"` / `"2nd_3rd"` / `"4th_plus"`. |
| `median_entry_notional` | `(asset, regime, position_tier) -> float` | The core entry-size curve, pre-share-based-sizing era. Raises on unknown cells — no fallback, since a silent default here would mask a real gap. |
| `shares_for_price` | `(price) -> float` | The bare, asset-independent price→shares curve (confirmed identical BTC/ETH/SOL from on-chain ground truth). Piecewise-exponential in `\|price − 0.5\|`, quantized to 0.1-share lots. Used directly by `hedge_notional_for_price`; the corrected version for entries is `notional_for_price`. |
| `notional_for_price` | `(price, position_tier="first") -> float` | `shares_for_price` corrected for position tier and HIGH-tail behavior. The live entry-sizing formula. |
| `hedge_notional_for_price` | `(asset, price) -> Optional[float]` | **The single entry point for hedge size** (2026-09-24 consolidation). `HEDGE_PRICE_SIZING_SCALE[asset] * (shares_for_price(price)*price) ** HEDGE_PRICE_SIZING_BETA`. Returns `None` for an asset with no calibration (caller skips the hedge). Re-validated fresh on-chain 2026-09-29 — Bitcoin's scale (1.886) confirmed solid; Ethereum/Solana correctly still un-recalibrated (thin, unstable samples). |
| `within_band_size_multiplier` | `(asset, regime, price) -> float` | Continuous within-regime-band size scaling — real positive price↔log(size) relationship inside every (asset, CORE/HIGH, tier) cell, checked cell-by-cell to rule out a position-tier confound. |

### CHEAP / MID / decisive-price edge multipliers

| Function | Signature | What it does |
|---|---|---|
| `cheap_joint_entry_multiplier` | `(asset, price, seconds_since_cheap_crossing) -> float` | Joint price-band × freshness-bucket dampener on CHEAP first entries. Merged 2026-09-19 from two previously-separate mechanisms (price-only and freshness-only) after a joint logistic-regression model found the interaction, not either alone. |
| `cheap_deep_touch_edge_multiplier` | `(price, seconds_into_window) -> float` | Boost for CHEAP entries very close to the extreme edge, early in the window. |
| `mid_boundary_edge_multiplier` | `(regime, price) -> float` | Boost for MID entries near its boundary (0.30/0.70) — the pooled MID null was hiding a real pocket near the edges. |
| `decisive_entry_eligibility_multiplier` | `(asset, seconds_since_decisive_crossing) -> float` | Eligibility/size gate keyed on how long ago price crossed the ≥0.70 "decisive" threshold. |
| `cheap_deep_tail_participation_multiplier` | `(asset, price) -> float` | Boost-only participation multiplier for the deep CHEAP tail (price < 0.08) — shipped 2026-09-28, the newest mechanism in the file. Composes with, doesn't replace, the regime-engagement gate. |
| `cheap_ttc_selectivity_multiplier` | `(asset, price, seconds_remaining) -> float` | Time-to-close-conditioned CHEAP entry selectivity for the assets where it's calibrated. |

### Participation & regime engagement

| Function | Signature | What it does |
|---|---|---|
| `participation_asset_scale` | `(asset) -> float` | Per-asset participation-rate scale (Bitcoin/Solana boosted, Ethereum untouched). One of the 4 confirmed test-isolation "seam" functions — monkeypatched directly in wiring tests, not merged into `table_lookup` despite the simple shape. |
| `regime_engagement_multiplier` | `(asset, regime) -> float` | Whether/how strongly to engage a given (asset, regime) cell at all. The highest-risk merge candidate found this session — has a **global autouse `conftest.py` fixture** (`neutralize_regime_engagement_multiplier`) that monkeypatches it by exact name for the entire test suite. Left as a named function deliberately. |
| `regime_drift_correction` | `(asset, regime, realized_counts, realized_total) -> float` | Closed-loop overlay correcting `regime_engagement_multiplier`'s static targets against what's actually being realized live (`DistributionController`). |

### Contrarian / momentum / carryover side selection

| Function | Signature | What it does |
|---|---|---|
| `mid_contrarian_bias_probability` | `(asset, velocity_magnitude) -> float` | P(override the ordinary side choice with the contrarian side) in MID, conditioned on recent price velocity. |
| `cross_asset_carryover_probability` | `(leader_asset, follower_asset, gap_seconds) -> float` | P(a follower asset's first-entry side should be biased toward the leader asset's own first-entry side), conditioned on the gap between them. |
| `_interpolate_streak_elapsed` | `(seconds_elapsed) -> float` | Internal interpolation helper for `streak_fade_probability`. |
| `streak_fade_probability` | `(streak_length, seconds_elapsed=None) -> float` | P(fade — bet opposite — the asset's own recent same-side outcome streak). Confirmed he over-fades relative to the market's real (weaker) reversal tendency. |
| `hour_size_multiplier` | `(hour_utc) -> float` | Boost-only multiplier for 06:00–07:00 UTC specifically (cleanest single finding of the whole research loop — OOS-replicated). Set-membership check, not a dict lookup — correctly excluded from the `table_lookup` merge. |

### Decisiveness collinearity corrections (3 functions — deliberately kept separate)

| Function | Signature | What it does |
|---|---|---|
| `decisiveness_onset_multiplier` | `(asset, regime, fraction_consumed) -> float` | Size multiplier keyed on how much of the remaining time had elapsed when price first snapped decisive (≥0.70). |
| `decisiveness_ttc_overshoot_correction` | `(asset, regime, fraction_consumed) -> float` | Corrects double-counting between the onset multiplier and the plain time-to-close multiplier for CORE/HIGH. No-op for CHEAP/MID. |
| `decisiveness_withinband_overshoot_correction` | `(asset, regime, fraction_consumed) -> float` | Corrects double-counting between the onset multiplier and within-band size scaling. No-op for Bitcoin (undershoots here for a separate, unexplained reason). |

These three look similar but each corrects a *different* pairwise collinearity — explicitly re-confirmed this session as genuinely distinct, not mergeable.

### Time-to-close / resumption sizing

| Function | Signature | What it does |
|---|---|---|
| `ttc_size_multiplier` | `(asset, regime, seconds_remaining) -> float` | Continuous, mean-neutral size multiplier as a function of time-to-close. Real interpolation logic even though its backing table is currently empty (`TTC_SIZE_MULTIPLIER = {}`, retired after the 5-second-merge-convention fix) — kept as live code, not deleted, since the interpolation itself is still correct/needed if the table is ever repopulated. |
| `resumption_size_multiplier` | `(hours_since_resumption) -> float` | Continuous multiplier on entry size as a function of hours since a qualifying trading-halt resumption. |

### Side persistence

| Function | Signature | What it does |
|---|---|---|
| `side_persistence` | `(asset, held_side_regime, use_recent_override=True) -> float` | P(keep the currently-held side) given the regime it's in. Merged 2026-09-28 from two previously-separate functions (`side_persistence_for` + `recent_side_persistence`) into one, with an override flag threading `config.ENABLE_RECENT_SIDE_PERSISTENCE`. Raises on unknown regime — strict by design. |
| `cross_market_side_persistence` | `(asset, previous_market_won=None) -> float` | P(this market's first entry matches the previous market's first-entry side), conditioned on whether that previous market won. Confirmed-unsolvable to recalibrate further from raw trader data (no ground truth for classifying which raw trades were hedges). |

### Hedge chain

| Function | Signature | What it does |
|---|---|---|
| `_solve_hazard_scale` | `(shape, target_cumulative, lo=0.0, hi=50.0, iters=60) -> float` | Binary search for the odds-scale that makes a hazard shape's cumulative probability match a target. Internal to hazard-curve construction. |
| `_build_hedge_attempt_hazards` | `() -> dict` | Precomputes the full 24-cell (asset × regime × attempt-index) hedge hazard table once at import time. |
| `hedge_attempt_hazard` | `(asset, primary_regime, attempt_index) -> float` | P(the first opposite-side entry happens exactly at this attempt index \| hasn't happened yet). Built from a pooled empirical hazard shape (survival-analysis style, Cox-proportional-hazards-like), rescaled per (asset, regime) cell. |
| `adverse_move_hedge_trigger_multiplier` | `(asset, adverse_move) -> float` | Continuous multiplier on hedge hazard when price has moved against the dominant side. |
| `favorable_move_hedge_trigger_multiplier` | `(asset, adverse_move) -> float` | The CHEAP-only mirror of the above for a *favorable* move (explains the CHEAP "bathtub" hedge-timing shape). |
| `hedge_continuation_probability` | `(hedge_count) -> float` | P(a further hedge follows) once at least one has already happened — a real multi-hedge pattern (39.8% of dual-sided markets go past 1 hedge). One of the 4 confirmed test-seam functions, not merged. |
| `hedge_probability` | `(asset, regime, attempt_index, hedge_count, adverse_move=None, seconds_remaining=None) -> float` | **The single entry point for "what's the probability THIS hedge fires right now"** — composes `hedge_attempt_hazard` (first hedge) or `hedge_continuation_probability` (subsequent hedges) with the adverse/favorable-move multiplier, `regime_hedge_post_shift_multiplier` (via `table_lookup`), and `urgency_multiplier`. |
| `hedge_count_reinforcement_multiplier` | `(asset, regime, live_hedge_count) -> float` | Step-up multiplier once a market's live hedge count crosses calibrated thresholds (tiered loop, not a flat lookup — correctly excluded from the generic merge). |

`REGIME_HEDGE_POST_SHIFT_MULTIPLIER` (regime-first, asset-second key order — note the reversed order vs. every other table in this file) is now read via `table_lookup` directly rather than its own named function.

### Urgency & timing

| Function | Signature | What it does |
|---|---|---|
| `_interpolate_urgency` | `(table, seconds_remaining) -> float` | Shared piecewise-linear interpolation helper for both entry and hedge urgency curves. |
| `urgency_multiplier` | `(kind, seconds_remaining) -> float` | Merged 2026-09-28 from `entry_urgency_multiplier` + `hedge_urgency_multiplier` into one function keyed by `kind ∈ {"entry", "hedge"}` — same shape, previously two near-identical functions. |

### Scout / add-on / reentry

| Function | Signature | What it does |
|---|---|---|
| `scout_params` | `(asset) -> (probability, size_ratio)` | Merged 2026-09-28 from `scout_probability` + `scout_size_ratio` into one tuple-returning function — the scout ("small, tentative first entry") mechanism's probability and relative size. |
| `reentry_fatigue_multiplier` | `(asset, regime, real_fill_count) -> float` | Deliberate risk dampener (not a replicated-sizing curve) once a market's real fill count crosses a calibrated threshold, for specific (asset, regime) cells only. |
| `addon_continuation_probability` | `(asset, regime) -> float` | P(a further add-on entry follows the k-th one) — closes a structural gap found auditing every `real_fill_count==0`-scoped mechanism. Shipped 2026-09-26, re-derived on true on-chain data. One of the 4 confirmed test-seam functions. |
| `sample_tick_offset` | `(rng) -> int` | Draws the tick-level price offset for order placement, replicating the trader's real queue-position/tick-priority behavior (34% of his entries land exactly at touch). |

### Merged into `table_lookup` (2026-09-28, Phase 2)

These 7 tables are now read directly via `bc.table_lookup(bc.TABLE_NAME, ...)` at call sites — their named wrapper functions were deleted after confirming (a) they're pure single/two-key lookups with no other logic, and (b) they're never monkeypatched as test-isolation seams:

- `ENTRY_SIZE_LEVEL_CORRECTION[asset][regime]`
- `DEEP_CHEAP_TAIL_MULTIPLIER[asset]`
- `REGIME_HEDGE_POST_SHIFT_MULTIPLIER[regime][asset]` (reversed key order)

(The other 4 structurally-identical-looking candidates — `regime_engagement_multiplier`, `hedge_continuation_probability`, `participation_asset_scale`, `addon_continuation_probability` — were deliberately **not** merged; see their entries above.)

### Removed this session (dead code)

- `gradient_score` + `GRADIENT_BIAS_PCT`/`GRADIENT_FLAT_EPSILON` — a real, historically-calibrated soft-tiebreak signal for ranking multiple simultaneously-eligible candidates, found fully orphaned (its input was still computed in `bot.py` and threaded through, but nothing ever consumed it — the current architecture evaluates each market independently, with no ranking step left). Confirmed with the user before deleting.
- `HEDGE_SIZE_RATIO` + `_DEFAULT_HEDGE_SIZE_RATIO` — the old dominant-cost-ratio hedge-sizing table, superseded by `hedge_notional_for_price` back in the 2026-09-24 consolidation, but accidentally left behind as an orphaned table.
- `streak_hedge_probability_multiplier` / `streak_hedge_size_multiplier` — retired after true on-chain re-derivation reversed the original 5s-merge-convention finding they were built on.
- `cross_market_size_momentum_multiplier`, `bankroll_pnl_size_multiplier`, `accuracy_scout_multiplier` — permanently dead, removed in an earlier cleanup pass.

---

## `strategy.py` (24 functions/methods)

Decision orchestration for `paperbot`, `paperbot-100`, `paperbot-combined`.

### `MarketActivityState` (dataclass + 10 methods)

Tracks everything already known about one market: position index, which side is "held", cumulative cost by side, first-entry price/regime/side/notional/t_in, decisive/CHEAP crossing timestamps, and the generic episode-latch list.

| Method | What it does |
|---|---|
| `is_episode_rejected(kind, *key_parts)` / `reject_episode(kind, *key_parts)` | Generic episode-latch — prevents a flat per-tick skip-probability roll from "eventually" passing no matter how many times it's retried. Merged 2026-09-28 from 3 separate parallel implementations (`rejected_regime_episodes`, `rejected_hedge_episodes`, `rejected_addon_episodes`) into one `rejected_episodes: list` field, entries stored as `[kind, *key_parts]`. |
| `update_first_prices(up_price, down_price)` | Latches the first non-None price seen for each side. |
| `position_tier()` | `bc.position_tier_for_index(self.real_fill_count)`. |
| `update_decisive_crossings(up_price, down_price, now, threshold=0.70)` | Records the first time each side crosses the decisive threshold. Locks once crossed. |
| `update_cheap_crossings(...)` | Mirror image for the CHEAP threshold. |
| `dominant_side()` | The side with more cumulative $ committed, or `None`. |
| `record_entry(side, notional_usd, regime, is_hedge, price, t_in)` | Records a placed (not necessarily filled) entry. |
| `record_real_fill(is_hedge)` | Called the first time a placed order actually fills. |
| `release_unfilled(side, notional_usd)` | Reverses a `record_entry` reservation if the order never fills. |

### Persistence

| Function | What it does |
|---|---|
| `save_activity_state(activity, path)` | Serializes the whole `{condition_id: MarketActivityState}` map to JSON. |
| `load_activity_state(path)` | Deserializes it back, `{}` on first run. Now runs every entry through `_migrate_legacy_episode_fields` first. |
| `_migrate_legacy_episode_fields(fields)` | **Added 2026-09-29** (live-caught bug fix): converts the 3 old per-type episode-latch field shapes into the new generalized `rejected_episodes` list, so a deploy doesn't crash on old on-disk state and doesn't silently lose already-latched episodes. |

### Decision pipeline

| Function | What it does |
|---|---|
| `decide_side(asset, activity, rng, held_side_price, previous_market_first_side, previous_market_won)` | Side-selection: keep the held side (persistence-gated) or choose fresh. |
| `decide_hedge(asset, activity, rng, price, dominant_current_price, seconds_remaining)` | The full hedge decision — trigger (via `bc.hedge_probability`) and size (via `bc.hedge_notional_for_price`), gated by the hedge episode latch. |
| `mid_contrarian_first_entry_side(...)` | Returns the contrarian side if `bc.mid_contrarian_bias_probability` fires, else `None`. |
| `cross_asset_carryover_side(...)` | Returns the carryover side if `bc.cross_asset_carryover_probability` fires, else `None`. |
| `streak_fade_side(...)` | Returns the fade side if `bc.streak_fade_probability` fires, else `None`. |
| `availability_check(book)` | Uniform liquidity sanity check (two-sided book, spread, depth) — deliberately not regime-scaled. |
| `timing_ok(market, now)` | True iff the market's window has both opened and not yet hit its closing cutoff. |
| `decide_size(...)` | Ordinary-entry sizing: floor-lot roll first, else the full corrected notional curve. Returns `(notional_usd, is_floor_lot)`. |
| `build_order_intent(...)` | **The top-level per-market pipeline** — runs side selection, eligibility gates, sizing, and hedge/add-on/scout logic in sequence, returning an `OrderIntent` or `None`. |
| `would_cross_spread(book, price)` | postOnly semantics check. |
| `safe_postonly_price(book, intended_price)` | Returns a price guaranteed not to cross the current spread. |

---

## `consolidated_engine.py` (4 functions)

`coinbase-bot`'s entire decision surface, deliberately collapsed to 4 functions with no feature flags — everything folded in here has already been verified real elsewhere and is unconditionally live.

| Function | What it does |
|---|---|
| `decide_entry_side(...)` | The entire side-selection decision in one function (mirrors `decide_side` + the 3 override functions from `strategy.py`, inlined). |
| `entry_eligible(...)` | The entire entry-eligibility decision in one function. |
| `decide_size(...)` | The entire ordinary-entry sizing decision (same signature/logic as `strategy.py`'s version). |
| `build_order_intent(...)` | Thin orchestrator wiring the three functions above together, plus hedge sizing reused directly from `strategy.decide_hedge`. |

---

## `bot.py` (17 functions/methods)

The live asyncio trading loop (`PaperBot` class), shared base for `paperbot`, `paperbot-100`, `paperbot-combined` (and subclassed by `CoinbaseMomentumBot`).

| Method | What it does |
|---|---|
| `_make_session()` | Builds the shared `requests.Session` with retry/backoff. |
| `__init__(...)` | Loads persisted activity state, sets up discovery/fill-sim/ledger, wires per-asset trackers. |
| `discovery_tick(now)` | Polls for newly-active markets. |
| `_onboard_markets(markets)` | Bootstraps book state for each token in newly-discovered markets. |
| `_resync_stale_books()` | Re-fetches any `BookState` that's gone quiet (WS gap detection). |
| `_retire_market(market)` | Cleans up all per-market state on window close (activity, halted-conditions — `last_price_by_token` removed 2026-09-29 along with `gradient_score`). |
| `_asset_streak(asset, current_window_start)` | Returns `(streak_side, streak_length)` of the asset's own recent same-side resolution streak. |
| `_hours_since_resumption(now)` | Current value of the resumption-caution clock. |
| `_record_global_trade(now)` | Records a trade for global-rate tracking, called only after a real placement. |
| `_update_drawdown_tracking(now)` | $100-bankroll safety: updates the drawdown circuit breaker. |
| `_new_markets_paused()` | True while the drawdown circuit breaker is active. |
| `_forced_first_entry_side(...)` | Extension point — base class always returns `None`; `CoinbaseMomentumBot` overrides it. |
| `strategy_tick(now)` | Evaluates every active market once. |
| `_evaluate_one_market(market, now, at_open_order_cap)` | Per-market evaluation: builds book state, calls `build_order_intent`, places the resulting order. |
| `_maybe_place_companion(...)` | Experimental (`config.ENABLE_COMPANION_ORDERS`, currently off) — one deeper same-side order per ordinary entry. |
| `available_cash()` | `None` in unconstrained mode, else the $100-bankroll's available cash. |
| `current_equity()` | Total capital including unrealized. |
| `committed_capital()` | Dollars currently tied up in unsettled orders. |
| `manage_orders_tick(now)` | Drives the fill simulator's open-order management every tick. |
| `_forget_resolution_tracking(cid)` | Cleanup after a market resolves. |
| `resolution_tick(now)` | Polls Gamma for outcomes on retired markets, settles the ledger. |
| `log_pnl_summary(now)` | Periodic PnL heartbeat log line. |
| `run_forever(tick_seconds)` | Main loop, runs until SIGTERM/SIGINT. |
| `_ws_supervisor()` | Reconnects the WebSocket with backoff on drop. |
| `main()` | CLI entry point. |

---

## `ledger.py` (12 functions/methods)

Append-only settlement log and PnL accounting.

| Function | What it does |
|---|---|
| `SettlementRecord.pnl_with_rebate()` | One record's directional PnL plus its maker rebate. |
| `Ledger.settle_order(order, winning_side, now)` | Settles one order's filled portion against the resolved outcome. |
| `Ledger.realized_pnl()` | Directional-only PnL, recomputed fresh every call (never a running total). |
| `Ledger.realized_pnl_with_rebates()` | The real total: directional PnL + maker rebate on every fill. |
| `Ledger.total_rebate_usd()` | Sum of rebates across all records. |
| `Ledger.is_settled(order_id)` | Whether this order already has a settlement record. |
| `Ledger.realized_pnl_by_asset()` | PnL grouped by asset. |
| `Ledger.hedge_summary()` | Hedge vs. non-hedge settlement counts and PnL. |
| `Ledger.scout_summary()` | Scout vs. ordinary settlement counts and PnL. |
| `Ledger.save(path)` | Atomic write (fixed 2026-09-25 after a live disk-full corruption incident). |
| `Ledger.load(cls, path)` | Deserializes the ledger back. |

`realized_pnl_by_asset_regime()` was removed 2026-09-29 (zero call sites, zero test coverage).

---

## `book.py` (20 functions/methods)

Order book state and WebSocket message routing.

| Function | What it does |
|---|---|
| `_extract_level(level)` | Normalizes one raw order-book level to `(price, size)`. |
| `BookState.best_bid` / `best_ask` / `spread` / `mid` | Standard book-state properties. |
| `BookState.depth_shares_at_or_better(side, price)` | Sum of resting shares at-or-better than `price`. Used by the fill simulator's queue-ahead model. |
| `BookState.depth_shares_at_price(side, price)` | Raw shares resting at exactly `price` (within half a tick). |
| `BookState.total_depth_usd()` | Total two-sided resting USD notional. |
| `BookState.apply_snapshot(bids, asks, ts)` | Full book replace (REST bootstrap or WS `book` message). |
| `BookState.apply_price_change(...)` | Single price-level update; `size==0` removes the level. |
| `BookState._reconcile_best(best_bid, best_ask)` | Internal best-price bookkeeping. |
| `BookState.apply_last_trade_price(...)` | Applies a trade print for fill-simulation purposes. |
| `BookState.apply_tick_size_change(new_tick_size)` | Updates tick size mid-market. |
| `BookState.drain_pending_trades()` | Pops queued trade prints for the fill simulator to consume. |
| `fetch_book_snapshot(token_id, session, timeout)` | REST GET for a book snapshot. |
| `fetch_tick_size(token_id, session, timeout)` | REST GET for tick size. |
| `bootstrap_book_state(token_id, session)` | Combines the two fetches above into one ready `BookState`. |
| `MarketWebSocketClient.__init__(get_book_state)` | Wires the injected `token_id -> BookState` lookup. |
| `MarketWebSocketClient.connect_and_run()` | Owns the WS connection lifecycle. |
| `MarketWebSocketClient._ping_loop()` | Keepalive. |
| `MarketWebSocketClient.subscribe(token_ids)` | Adds tokens to the WS subscription. |
| `MarketWebSocketClient._send_subscription(token_ids)` | Sends the actual subscribe frame. |
| `MarketWebSocketClient.route_message(raw)` | Pure routing logic — parses one WS text frame. |
| `MarketWebSocketClient._route_one(m)` | Applies one parsed message to the right `BookState`. |

`depth_usd_at_or_better()` was removed 2026-09-29 (zero call sites; `depth_shares_at_or_better` is the one actually used, for shares not USD).

---

## `market_discovery.py` (16 functions/methods)

Polls Gamma for active/resolved markets and parses them.

| Function | What it does |
|---|---|
| `Market.seconds_remaining(now)` / `window_start` / `seconds_until_start(now)` | Window-timing helpers. |
| `_asset_for_slug(slug)` | Maps a market slug prefix to an asset name. |
| `_parse_iso(ts)` | ISO timestamp parsing helper. |
| `parse_market(raw)` | Parses one raw Gamma market object into a `Market`. |
| `_candidate_slugs(asset, now)` | The slug(s) for an asset's current and next 5-minute window. |
| `fetch_active_markets(session, assets, timeout, now)` | Fetches exactly the markets tradeable right now. |
| `fetch_market_by_slug(slug, session, timeout, closed)` | Single-market lookup, used post-close. |
| `fetch_market_for_resolution(slug, session, timeout)` | Resolution-specific lookup wrapper. |
| `parse_resolution(raw)` | Extracts the winning outcome from a raw Gamma object. |
| `infer_resolution_from_price(raw, threshold=0.99)` | Fallback resolution signal when Gamma hasn't marked the market closed yet. |
| `MarketDiscovery.poll()` | Refreshes the tracked active-market set. |
| `MarketDiscovery.due_for_poll(now)` | Rate-limits polling. |

---

## `fill_simulation.py` (15 functions/methods)

Simulates realistic maker-order fills against the live book (queue position, partial fills, reprices, expiry).

| Function | What it does |
|---|---|
| `seed_order_id_counter(start_at)` | Resets the module-level order-id counter (test/restart use). |
| `SimulatedOrder.filled_size` / `time_to_fill` / `is_open` | Order-state properties. |
| `FillSimulator._queue_safety_factor_for(regime, asset)` | Per-(regime, asset) queue-position safety margin. |
| `FillSimulator.place_order(intent, book, now)` | Places a new simulated resting order. |
| `FillSimulator.on_trade_print(token_id, trade)` | Consumes a real trade print against any resting order on that token. |
| `FillSimulator._consume_one(order, trade_size, ts)` | Applies up to `trade_size` shares of consumption to one order. |
| `FillSimulator.manage_open_orders(...)` | Per-tick: reprices, expires, or leaves resting orders. |
| `FillSimulator.cancel_order(order_id, now, reason)` | On-demand cancellation. |
| `FillSimulator._reprice(order, book, now)` | Repricing logic for a stale resting order. |
| `FillSimulator.remove_orders_for_condition(condition_id)` | Prunes every order belonging to one retired market. |
| `FillSimulator.stats_by_asset_regime()` | Fill rate / time-to-fill / expiry rate broken out by (asset, regime). |

---

## `coinbase_bot.py` / `coinbase_feed.py` / `coinbase_strategy.py` (8 functions/methods)

A separate frozen-control experiment bot (`momentum-bot-experiment`) that forces its first-entry side from real Coinbase spot momentum instead of replicating the trader — used as a comparison baseline, not part of the main replication effort.

| Function | What it does |
|---|---|
| `CoinbaseMomentumBot.strategy_tick(now)` / `_forced_first_entry_side(...)` | Overrides the base bot to force momentum-driven sides. |
| `CoinbaseSpotFeed.poll_tick(now)` | Polls Coinbase's public ticker per asset, rate-limited. |
| `CoinbaseSpotFeed._fetch_ticker(product)` | Single ticker fetch. |
| `CoinbaseSpotFeed.trailing_return(asset, now, minutes)` | `(price_now - price_minutes_ago) / price_minutes_ago`. |
| `decide_momentum_side(regime, trailing_return)` | Returns "Up"/"Down" if `regime` is in the validated momentum-following range. |
| `momentum_forced_side(up_book, down_book, trailing_return)` | Returns the side to force for a market's first entry. |

---

## `config.py` (2 functions)

| Function | What it does |
|---|---|
| `get_trading_mode()` | Reads `POLYMARKET_TRADING_MODE` from the environment, defaults to `PAPER`. |
| `maker_rebate_usd(shares, price)` | Rebate earned by the resting (maker) side of one fill. |

---

## Function-count history this session

| Stage | Total |
|---|---|
| Session start | 196 |
| Phase 1 (safe merges: `side_persistence`, `scout_params`, `urgency_multiplier`, episode-latch generalization, streak-hedge deletion) | 189 |
| Phase 2 (`table_lookup` generic dispatcher, 3 of 7 candidates merged) | 186 |
| Dead-code sweep (`gradient_score` + plumbing, `realized_pnl_by_asset_regime`, `depth_usd_at_or_better`, `HEDGE_SIZE_RATIO`) | **164** |

All changes verified via the full test suite (849/849 passing throughout) and live-deployed to all 4 bots with zero intended behavior change. One real backward-compatibility bug was caught and fixed during deployment (a persisted-JSON field-name migration for the episode-latch generalization).

**Assessment: no further safe merges remain** in `behavior_config.py` / `strategy.py` / `consolidated_engine.py` without either breaking a test-isolation seam or merging genuinely distinct business logic just to lower the count. Two marginal candidates in `ledger.py` (`hedge_summary`/`scout_summary`) and `book.py` (`depth_shares_at_or_better`/`depth_shares_at_price`) were considered and declined — the count reduction (1 function each) doesn't justify the clarity/type-safety cost on public, actively-used APIs.
