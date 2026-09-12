# Project memory (condensed, current state)

This file is OVERWRITTEN/updated in place to reflect current
understanding — NOT an append-only log (see `loop_log.md` for that). Read
this first if resuming cold; it should always be enough to pick up without
re-reading the whole session. Originally started as loop-iteration
memory, now also used as general project memory per explicit user
instruction ("use your memory file in the same way" for the fix session).

## CURRENT STATUS (2026-09-12): all 8 BUGS_TO_FIX.md entries FIXED, tested, being deployed

The research-angle loop (cron `a31ef787`) was stopped by me after 3
consecutive honestly-flagged diminishing-returns iterations (see loop_log
iterations 22-24 and the angle history further below for what it found).
User then asked to fix every bug rather than start a rewrite -- decision
reasoning: bugs are narrow/well-understood (not architectural), and the
real value is months of calibration work a rewrite would throw away.

**All 7 discrete bugs + 1 systemic note from BUGS_TO_FIX.md are now
fixed in code, covered by tests (435/435 passing, 19 net new tests), and
committed locally.** See `BUGS_TO_FIX.md` itself for the exact fix
applied to each entry (its Status line), and `loop_log.md`'s "FIX
SESSION" heading for the full narrative (including one real editing
mistake made and caught by the test suite -- two orphaned assertion
lines, fixed).

**Deployment status: check `loop_log.md`'s tail / re-verify directly**
(SSH to the server, `sudo -u paperbot git -C /opt/paperbot/app log -1`,
`systemctl status paperbot paperbot-100`) rather than trusting this line
if reading this file after a gap -- deployment was in progress as of this
write.

## Research-angle loop history (reference only, loop is stopped)

Everything below this point is preserved from the earlier research-angle
loop (cron `a31ef787`, now stopped) for reference. Not an active
to-do list.

## BUGS_TO_FIX.md summary (8 entries, all found not yet applied)

1. `MAX_OPEN_ORDERS_PER_MARKET=3` too low vs real trader burst data (~7-9 recommended)
2. Same cap silently blocks the WHOLE hedge subsystem (decide_hedge never runs once hit)
3. `MAX_CHEAP_REPRICES` cancels hedges/scouts despite the validating data excluding them
4. Multiplicative stacking never validated jointly (decide_size: 6 factors; decide_hedge trigger: 5 factors)
5. `entry_count` AND `hedge_count` (extended scope, iter 6) never corrected for unfilled placements — contaminates position_tier/hedge_attempt_hazard/hedge_continuation lookups AND the MAX_HEDGE_COUNT_PER_MARKET gate itself. Compounds with #2 and #3.
6. `BookState._reconcile_best` is a documented no-op, no periodic REST re-sync exists — PLAUSIBLE RISK, not live-confirmed, needs diagnostic logging first
7. `RESUMPTION_SIZE_MULTIPLIER` triggers on OUR BOT's own trading gaps, not the trader's — calibration/trigger mismatch, recommend removing outright
8. Systemic (lower confidence): cross-market momentum/hedge-rate trackers are self-referential (fed from our own bot's state, not the trader's) — bugs #1/#4/#5 propagate forward through them into future markets, not just cause isolated bad decisions

Full detail, file/line references, and fix recommendations for all 8 are
in `BUGS_TO_FIX.md` — don't re-derive, just read that file if a fix is
ever requested.

## Code areas read in full this thread (don't re-audit unless asked to re-verify)

`strategy.py`, `bot.py`, `fill_simulation.py`, `config.py` (relevant
sections), `ledger.py`, `book.py`, `behavior_config.py` (all 28 functions).
`market_discovery.py` read partially (resolution-fetch path only, not
exhaustive — acceptable stopping point).

## Research angles

1. **Platform-wide 60s cutoff — REJECTED (iter 7), cleanly, definitively.**
   Real market-tape data: his own last trade never lands within the final
   60s (0/11, min 63s) but 32.4% of OTHER wallets' last trades do, and the
   market-wide single last trade has median ttc=20s. Not a platform halt —
   confirms his cutoff is genuine personal behavior. Don't re-test.
2. **Real order cancellations (not just fills)** — BLOCKED, confirmed genuinely (iter 9): the CLOB "user" WS channel that streams order-cancel events requires the ACCOUNT's OWN private API credentials (apiKey/secret/passphrase) — structurally impossible to see another wallet's cancellations without their cooperation. Closed permanently unless a fundamentally different data source appears. Don't re-attempt.
3. **On-chain USDC deposit/withdrawal timing** — INFEASIBLE, not blocked (iter 10): checked 2,000 real transfers via Blockscout (only ~1hr of his real activity, that dense); the 79 non-exchange-contract ones found are all internal PUSD mints from the zero address (settlement/redemption), not external deposits. Reaching a genuine external funding event would need 100,000+ transfers of pagination, AND even then a real deposit likely comes from Polymarket's own treasury contract (indistinguishable from settlement on-chain) — same fundamental limit as the closed wallet-clustering angle. Don't re-attempt without a fundamentally cheaper approach.
4. **Multi-instance/Sybil-farm hypothesis** — MAJOR FINDING, upgraded twice (iter 12→16→17), now "very likely confirmed farm." Iter 12: 10+ wallets in ALL 26/26 of his markets. Iter 16: 0/5 of them appear in 6 real markets he SKIPPED (mid-July control period, 89.4% participation) — rules out generic always-present bots. Iter 17 (decisive): all 5 wallets' trade-timing LAG relative to his own first trade is nearly IDENTICAL to each other across 21+ markets (e.g. [192,240,206,215,48...] vs [192,240,203,215,48...], agreeing to 1-3s at every position) — rules out independent reaction to a shared external trigger too (that would show real inter-wallet variance). Best-supported explanation: our target wallet is likely ONE OF AT LEAST 6 near-identical instances run by the same underlying operator/codebase, not an independent single trader. Caveat: lag direction doesn't prove which wallet leads; "coupled system" is confirmed, exact causal structure isn't. Doesn't invalidate anything already built (his calibrated behavior is still real and his own wallet's real history) — reframes the likely nature of the operation, not actionable for our bot.
5. **The unexplained 13.6-day halt** (Aug 23 → Sep 6) — PARTIALLY RESOLVED (iter 8): checked Polymarket's real official status page (status.polymarket.com/history/1) — zero incidents listed Jul 30-Aug 30, a full month covering exactly when his silence began (Aug 23). Rules out a platform-wide outage as the TRIGGER with real evidence. Real incidents (order-read-lag) do cluster Aug 30-Sep 3 (within his silence, just before/around his Sep 6 resumption) but can't explain the initial week. Still doesn't know the actual cause — likely account-specific or undocumented-publicly, ceiling reached without account-level data access. Note: this halt ALREADY fed one shipped feature (RESUMPTION_SIZE_MULTIPLIER, flagged as bug #7 for a DIFFERENT reason)
6. **Multivariate/PCA anomaly scan** — DONE (iter 11, no new finding but a useful confirmation): pure-Python OLS (no numpy on server) jointly controlling price+ttc+regime+asset, residual scan surfaced `is_weekend` (r=0.0589, t=22.86 pooled) but temporal-stability check (3 chronological chunks) shows it decays to null in the most recent era (t=-1.08) — confirms, doesn't overturn, the existing weekend-effect rejection, even under more rigorous joint control. Not worth re-running without a fresh candidate variable.
7. **Reaction to major macro/crypto news events** — REJECTED (iter 13), clean null after ruling out two confounded candidates (June 4 crash confounded with early ramp-up; July 22 event confounded with a known asset-mix reversion). BTC-only isolated check around July 22: before/after daily activity nearly identical (1,982 vs 2,082 trades/day, $24,088 vs $24,325/day notional), both within ordinary day-to-day noise. Consistent with him being reactive to local price action, not macro news.

**ALL 7 ORIGINAL ANGLES NOW RESOLVED** (1 rejected, 2 blocked, 1 infeasible, 1 inconclusive, 1 partial, 1 confirmed-rejection, 1 rejected). No angle remains from the original brainstorm list.

Also available if these run dry: the full ~44-iteration external-bot-
strategy research history lives in project memory
(`external-bot-strategies-loop-progress.md`, outside this repo folder, in
Claude's memory directory) — that thread was extensive; check it before
assuming an angle is untested.

## Angle #8: cross-asset tilt-after-loss — RETRACTED (iter 15), clean confound

Tested whether a loss in one asset predicts smaller sizing in a DIFFERENT
asset's next market. Pooled effect looked real (t=-2.719) and survived a
temporal-stability check (iter 14), but FAILED the regime-confound check
(iter 15): once the current market's own regime is held fixed, none of
the 4 regime cells clear the significance bar (CHEAP t=-2.08, MID t=-0.54,
CORE t=-1.68, HIGH t=-0.02). Composition check confirms why: CORE/HIGH
(naturally bigger $, higher win rate) are overrepresented in the
after-win group (22.9% vs 16.6%) — correlated assets share regime/
decisiveness during correlated market moves, a real compositional
confound, not a behavioral tilt. RETRACTED cleanly.

**Lesson reinforced:** temporal stability and regime/composition checks
test DIFFERENT failure modes (unstable effect vs. stable-but-spurious
one) — passing one doesn't mean the other can be skipped.

## Angle #4 status: CLOSED, final refined conclusion (iter 19)

5 lines of evidence total: all-markets co-occurrence (iter 12),
control-market absence (iter 16), timing lockstep (iter 17), cross-asset
generalization (iter 18), size/side mismatch (iter 19 — REFINES, doesn't
contradict, the earlier framing). Final conclusion: NOT the same operator
running our target wallet + 5 others as one unified strategy. Instead:
several wallets running a SHARED off-the-shelf copy-trading tool that
tracks WHICH markets he enters (explains the tight market-selection
correlation) but applies its own generic fixed-~$0.10-notional
"buy-the-cheap-decisive-longshot-late" logic independently (explains why
their size/side/exact-timing look nothing like his own trades — evidence:
his sizes ranged $0.80-$7.80 in the same sample vs. their fixed ~$0.10;
one example showed his "Down" entry at price 0.510 vs their "Down" entry
at 0.020 in the SAME market — wildly different moments in the window).
This is the final, best-supported, most specific conclusion — don't
re-open without a fundamentally new data source (account-level access).

## Angle #9: loss-streak PARTICIPATION — REJECTED (iter 20), clean null

Does he go quiet (reduced frequency, not just size) after consecutive
market losses? No — gap to next same-asset market is flat across streak
lengths 0-3+ (335.8s to 357.9s, t=1.279, median exactly 300s = one window
in every bucket). Complements the earlier sizing-streak rejection:
neither size nor frequency responds to loss streaks.

## Angle #10: CHEAP-band Up-vs-Down calibration asymmetry — REAL, TWAP-linked, NOT a build candidate

Down longshots win more than Up longshots in CHEAP band, cross-asset
consistent (BTC z=-4.286, ETH z=-4.560, SOL z=-3.300). Temporal check at
the RIGHT boundary (TWAP_SWITCH, not an arbitrary split) shows it's null
pre-TWAP (z=-0.788) and real post-TWAP (z=-4.246) — genuine platform
calibration shift, mechanistically tied to TWAP smoothing, not noise. He
doesn't exploit it himself (his CHEAP volume split ~even/tilted toward
the worse side) — per the reporting-filter rule, NOT a build candidate
(would make us diverge from him, not replicate him better). Real,
interesting, understanding-only finding.

**Methodological lesson (opposite of round-nickel):** when a real known
structural boundary exists (TWAP_SWITCH), test AT that boundary before
concluding an arbitrary-split "instability" means discard as noise — the
right split can reveal a genuine signal an arbitrary one blurred.

## Next concrete step (pick up here)

Angle #10 further strengthened (iter 22): ruled out the simplest
alternative explanation (base-rate resolution shift) — overall Up/Down
resolution rate stayed ~50/50 both pre/post TWAP, confirming the CHEAP
asymmetry is a real longshot-calibration effect, not a directional bias.

## Angle #11: perpetual-futures funding-rate reset timing — REJECTED (iter 23), confound identified not just asserted

Tested the theoretical-only "funding rates too slow to matter" reasoning
from much earlier in the project. First pass looked like a real signature
(intensity drops to ~0.4x at ±10min from 00:00/08:00/16:00 UTC), but
caught the confound before reporting: ±10min from those anchors always
lands on a multiple of 5 (a window boundary), aliasing minute-of-day with
already-known window-phase intensity patterns. Verified with 2 arbitrary
non-multiple-of-5 anchors (03:17, 11:43 UTC) — completely different
pattern there, some values even high at ±10min. Confirms the "signature"
was pure aliasing, not a real funding-rate effect. Clean rejection with
mechanism pinned down, not just "looked unstable."

Confirmatory data point (iter 24, not a new angle): he's not in the top
50 on any of Polymarket's real leaderboards (profit/volume, 30d/all-time)
— his all-time profit ($229,987) sits ~13x below the #50 threshold
($2.93M), all-time volume ($18.7M) ~13x below #50 ($243.5M). Top
platform-wide traders run $1.8B+ all-time volume, multi-product
operations. Confirms/quantifies (doesn't newly discover) that he's a
modest, specialized niche player, not a platform whale.

ALL angles #1-11 are now closed, plus this confirmatory leaderboard
check. **Diminishing returns are real and worth being honest about** —
24 iterations, 11 angles, one full bug sweep, and now 3 straight
iterations (22/23/24) each requiring substantial effort for confirmatory
or negative results. Need a genuinely fresh angle #12 — nothing queued.
Strongly consider pausing for user direction rather than continuing to
grind — the well-motivated, cheaply-testable ideas for THIS trader appear
to be genuinely exhausted for now.
