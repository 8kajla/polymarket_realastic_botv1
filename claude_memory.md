# Claude memory (condensed, current state)

Renamed from `loop_memory.md` (2026-09-12), per explicit user instruction:
this and `claude_logs.md` are now the general working memory/log for this
whole project, not loop-specific — read BOTH before assuming anything
about context, since the point of these files is to let work continue
correctly without relying on Claude's own conversational memory.

This file is OVERWRITTEN/updated in place to reflect current
understanding — NOT an append-only log (see `claude_logs.md` for that,
chronological, append-only). Read this file first if resuming cold; it
should always be enough to pick up without re-reading the whole session.

## MODE (2026-09-12): research angles — all 3 prioritized open threads now resolved to their practical ceiling

Bug-fixing phase (see "PAST STATUS" below) is done and deployed. Then
dug into the 3 remaining open threads from the earlier angle work; ALL
THREE are now resolved to their practical ceiling (see `claude_logs.md`
"RESEARCH MODE RESUMED" section for full detail):

1. **Sybil-farm/copy-tool cluster** — FULLY resolved. Added a 6th line of
   evidence (causal direction: he leads in 17/19 markets, 89.5%) to the
   5 already established. Best-supported conclusion: several wallets run
   a shared copy-trading tool that follows which markets he enters, then
   applies its own generic fixed-notional strategy — not a literal mirror,
   not the same operator as one unified system. Closed for good.
2. **13.6-day halt cause (Aug 23 → Sep 6)** — both obvious rational
   explanations now ruled out with real evidence: platform outage (no
   incidents Jul30-Aug30 on Polymarket's own status page) AND risk-driven
   pause (he was on a genuine HOT STREAK right up to the halt — every day
   Aug18-23 strongly positive, best day the day before it started). True
   cause remains unknown, likely personal/operational, not testable
   further without account-level access. Bonus find along the way:
   precisely dated the previously-uncertain 5-min TWAP window's 30s→60s
   transition to exactly **2026-08-13 00:00 UTC** (via Gamma's
   `resolutionSource` field — confirmed simultaneous across BTC/ETH/SOL).
3. **CHEAP Up/Down asymmetry, why unexploited** — quantified: Down ROI
   +12.12% vs Up +2.44% post-Aug-13 (BTC), a real ~5x gap, direction
   consistent across the two available weekly data chunks. Reasoned
   conclusion (not further testable): either too recent to have
   recalibrated to (the asymmetry only exists post-Aug-13, ~1 month old,
   with the halt eating much of that), or a structural blind spot (his
   CHEAP entries are triggered by price level/momentum, not an
   independent Up/Down preference, so exploiting this would need a new
   mechanism his design never needed before this TWAP change).

**Hypothesis miner re-run (fresh 842k-trade data, first pass):** 68
candidates, ~46 already-known confounds. One genuinely new,
partially-surviving lead: CHEAP-band immediate-next-market win-rate
persistence (after_win 57.35% vs after_loss 50.29%, z=3.325, survives
regime control) — but temporal split shows both halves individually
below the significance bar (z=2.447, z=1.601, same direction though) —
NOT YET CONFIRMED, needs more data. Not a build candidate yet.

**Hypothesis miner RESTRUCTURED (2026-09-12), per user request ("remove
already-filled hypothesis, feed it 80 new angles"):** rewrote
`hypothesis_miner.py` (server + local `hypothesis_miner_v2.py`, original
backed up server-side as `.bak-2026-09-12-original`). 17 settled
dimensions retired from standalone scanning (kept as pairing controls), 2
kept active, 24 new dimensions added (own-pacing, within-market
price-path shape, rolling personal form, per-asset cadence, new calendar
cuts). Two real bugs found and fixed IN the tool itself along the way:
(a) it was using its own stale `infer_winner()` price-heuristic instead
of `resolution_cache.json` — now prefers the cache, heuristic is fallback
only; (b) interaction-pair search was UNCONTROLLED (whole flat
cross-product, always dominated by regime) — added
`_find_best_controlled_pair_*` (group-by-control, search within group).
Re-run: 169 findings. Top new lead looked huge:
streak_of_wins_length/streak_of_losses_length within CHEAP (z=-581/+487).

**That streak-length finding was then checked for temporal stability
(2026-09-12) and does NOT survive as reported** — see `claude_logs.md`
for the full writeup. Two problems found: (1) the miner counts streaks
PER TRADE not per market, so a dual-sided market's own multiple same-side
entries mechanically manufacture "streaks" within ONE market outcome —
deduping to one-row-per-market collapses z from -581/+487 down to a much
more modest -4.10/+4.08 (still a weak, monotonic-looking lead, not the
headline effect); (2) temporal split at market level: wins-streak is
UNDERPOWERED to even test (3+ bucket n=100/155, both under
MIN_BUCKET_N=200), losses-streak IS testable and FAILS (first half
z=+1.56, second half z=+4.16 — inconsistent). **Verdict: not confirmed,
not a build candidate** — generalizes the same fate already documented
for prior_market_streak ("promising but failed a temporal-stability
split").

**Bigger discovery from the same check, now the top-priority open item:**
`resolution_cache.json` covers only ~9% of all markets (7,259/80,797) —
91% of the "missing" ones are 7+ days old, i.e. long since resolved, not
too-recent. Root cause: the cache is populated lazily/on-demand by
`compare_bot_vs_trader.py`/`multi_compare.py` (only for markets inside a
requested since-restart window), never backfilled for full history. This
means the miner's "fixed" winner-source (cache-first) currently only
actually helps ~9% of trades — the other 91% of every win-rate finding to
date, including all 169 from the restructured run, are still built
predominantly on the same flagged-unreliable heuristic. **Fix in
progress:** `resolution_backfill.py` deployed and running in the
background on the server since 2026-09-12 17:26 UTC / 22:56 IST (detached
via nohup, survives SSH disconnect, saves incrementally every 200
fetches, idempotent/safe to re-run), ETA ~407min/~6.8h. **Next session
should check `/tmp/resolution_backfill.log` on the server for completion,
then re-run the full hypothesis_miner.py and re-check the streak-length
finding** — both underpowered buckets above should get real N once
coverage isn't limited to whatever windows happened to get checked
before.

**Depth-imbalance collector BUILT and deployed (2026-09-12)**, per user
request, closing one of the two previously-blocked gaps: `depth_imbalance
_collector.py` (local + `/opt/trader-intel/`, not git-tracked) polls live
CLOB books for BTC/ETH/SOL every 15s, records bid_depth_usd/ask_depth_usd
SEPARATELY for both the Up and Down token (4 independent depth totals per
market per poll) plus a derived imbalance ratio per side — the one thing
no existing collector did. Copies decisiveness_collector.py's proven
architecture (resolve token ids from Gamma once per window, poll CLOB
book directly, hard-deadline request pattern) to avoid re-walking into
its two already-fixed bugs. Deployed as systemd service
`trader-intel-depth-imbalance.service`, verified writing real sane rows
(non-null depth both sides, imbalance in range, Up/Down mid sums to 1.0).
Output: `/opt/trader-intel/data/depth_imbalance.jsonl`. **No usable data
yet — needs real hours/days banked before any imbalance-vs-outcome
hypothesis can be tested.** Remaining blocked gap: wallet-clustering
(still needs an Etherscan API key we don't have).

**NEW ANGLE, validated (2026-09-13): cross-asset synchronized-entry
clustering — the strongest, most novel finding of this whole research
thread.** Nobody had tested whether BTC/ETH/SOL are one portfolio-level
decision vs 3 independent streams (every existing signal is per-asset).
Result: real co-entry rate at 10s is 32.6%, vs a PROPERLY CONTROLLED null
(preserves his own per-asset within-window timing habit + which windows
he trades, only reshuffles the pairing) of 19.7% — z=55. Survives a
temporal-stability split cleanly in BOTH halves (unlike almost everything
else checked recently) and the effect STRENGTHENED over time. Directional:
BTC leads ETH (59%) and SOL (54%), SOL leads ETH (55%) — matches real
crypto market structure (independent corroborating evidence). Critically,
checked whether this is just "shared real market volatility" (which the
bot's existing per-asset triggers would already inherit for free) — NO:
co-entry and solo moments have statistically identical real BTC spot
volatility (ratio 1.048x, medians equal). **This is a genuinely new
mechanism, not already captured by anything in the codebase.** Not yet
implemented — one more check needed first (does clustered vs solo entry
differ in win rate/size, currently blocked by the same thin
resolution_cache coverage the backfill is fixing). Proposed build once
that's done: a CROSS_ASSET_ENTRY_TRIGGER multiplier — entry in one asset
(BTC-weighted) temporarily raises scout/entry likelihood in the other two
for ~30-60s. Full methodology (3 scripts, all confound checks) in
`claude_logs.md`.

**NEW ANGLE, validated (2026-09-13): real multi-price-level order
LADDERING** — resolves a gap `config.py`'s own MAX_OPEN_ORDERS_PER_MARKET
history explicitly flagged as unmeasurable ("no way to confirm genuinely
concurrent orders vs fast sequential re-entries"). Walked 721,775
consecutive same-side trade pairs: 34.8% land within 0.5s of each other
(a batch-placement spike), and of the <=2s "fast" pairs, 53.5% are at a
MEANINGFULLY different price (>=0.005 apart) — since he's confirmed
maker-only, a different price within ~1-2s means a genuinely separate
order at a different level, not repeat fills off one order. 56.2% of all
multi-fill markets show this ladder signature. Real, well-powered,
structurally new (execution-style, not a sizing/trigger question).
**Composition check: inconclusive, not "already free."** Both live bots'
own ledgers show HIGHER loose price-diversity (86.9%/81.5% vs trader's
69.66%) but the ledger has no placement timestamp (only settlement), so
this can't distinguish "genuine tight ladder" from "diversity from slow
reactive re-pricing over many ticks" — a real instrumentation gap, not a
verdict either way. **Concrete next step queued:** add a placement
timestamp to the ledger record (cheap, a few lines in ledger.py), then
re-run the same consecutive-same-side-pair analysis on the bot's own data
for a true time-gated comparison. Full methodology in `claude_logs.md`.

**Cross-asset clustering extends to HEDGES too (2026-09-13)** — same
corrected-null methodology applied to first-hedge timestamps: 18.7% vs
10.1% null at 10s (z=35), temporally stable in both halves (z=11-12 /
z=14-22), strengthens over time — same as the entry-clustering finding.
**Conclusion: this is a general portfolio-level pattern (batch-checking
across all 3 assets), not entry-specific** — the proposed
CROSS_ASSET_ENTRY_TRIGGER build should cover both entry and hedge
readiness, not just entries.

**Loose end CLOSED (2026-09-13): clustered vs solo entries, win rate/size
— using BTC's 69% real resolution_cache coverage (didn't need to wait for
full backfill).** Clustered (n=5690) vs solo (n=1110): entry size $6.51
vs $8.42 mean (t=-3.55, REAL, significant); win rate 46.5% vs 50.6%
(z=-2.52, same direction, just under the bar — re-check once ETH/SOL
coverage lands). **Revises the proposed build: divided-attention
signature, not higher-conviction** — CROSS_ASSET_ENTRY_TRIGGER should
raise entry PROBABILITY but should NOT also raise size (if anything, a
modest downward size adjustment), the opposite of a naive "boost both"
implementation.

**NEW ANGLE, validated (2026-09-13): real SPOT-momentum alignment
predicts win rate, INCLUDING within CHEAP band** — first genuinely new
angle on the long-stuck CHEAP-mismatch thread (3 sizing hypotheses
already rejected there; this is a DIRECTION/signal-quality axis instead).
Uses trailing 3-min REAL exchange spot return (from spot_candles_*.jsonl)
vs his side choice — different from the already-confirmed "window delta"
MID finding (that used the CONTRACT's own within-window price change).
Regime-controlled: CHEAP alone 40.5% vs 14.5% win rate (z=9.78), MID
alone 63.1% vs 36.4% (z=14.58), CHEAP+MID combined z=27.5 — survives
regime control (not a CORE/HIGH decided-market artifact). Temporally
stable at CHEAP+MID level (both halves z>17); CHEAP alone underpowered
to split, not rejected. **Confound-checked against time-in-window
(the exact trap window_delta_test.py already flagged) — survives
untouched, stratified result nearly identical to raw.** Reconciles a
seemingly-contradicting earlier result (window_delta_test.py found CHEAP
"flat-to-opposite") — different underlying signal (real spot vs
contract-derived), CHEAP is evidently where they diverge. **Not yet a
build candidate**: same "does he actively hunt it or passively benefit"
question the MID finding needed (that one resolved to passive, no
active-hunting evidence) hasn't been run for this spot-based version yet
— queued as the next concrete check. BTC-only so far (69% resolution
coverage); ETH/SOL need more backfill coverage to regime-split cleanly.
Full methodology in `claude_logs.md`.

**Spot-momentum "does he hunt it" question: RESOLVED (2026-09-13, /loop
iters 1-2, job d541a715).** Raw check looked like active hunting (gap to
prev trade shrinks sharply with momentum magnitude, t=-6.79) — but after
controlling for the already-known choppiness→intensity driver
(r(momentum,choppiness)=0.585), partial t drops to -1.73, not
significant. **Same conclusion as the earlier MID contract-velocity
finding: passive market truth, not deliberately hunted.** Practical
implication: this stays a CALIBRATION input (does trading with momentum
help, when he happens to trade) not an ENTRY-TIMING trigger — don't build
"detect momentum spike → enter." **CONFIRMED CROSS-ASSET (2026-09-13,
/loop iter 128): ETH t=-0.44, SOL t=1.14, both null, same as BTC's
t=-1.73 (reproduced exactly). Closed for good on all 3 assets.**

**NEW dimension surfaced from final miner re-run, tested and RETRACTED
(2026-09-13, /loop iter 127): `size_vs_own_prior_trade_in_market`.**
Raw pooled looked strong (bigger-than-own-prior-trade wins 49.45% vs
43.18%, n=25,862/24,331, z=14.1) and even passed a temporal-stability
gate (both halves z=11.5/8.4, same direction). But regime-stratified
check kills it completely: CHEAP z=0.75, MID z=0.01, CORE z=0.48, HIGH
z=0.17 — zero signal in every band. Pure regime-composition artifact
("bigger" trades cluster in naturally-high-win-rate CORE/HIGH, "smaller"
skews to naturally-low-win-rate CHEAP) — structurally identical to the
urgency-scaling false-positive (iters 60-83), except caught on the FIRST
pass this time instead of after 4 iterations of overclaiming. Confound
checklist discipline working as intended. Do not re-test without a
different framing.

**/loop running (2026-09-13, job d541a715, every 3min, free-hand
research, auto-expires ~2026-09-20)** — user said "just don't stop until
I tell you to." Each iteration should: check infra health cheaply, dig
one concrete thing (new angle or follow-up on an open thread), log
findings in `claude_logs.md`, update this file's relevant section. Avoid
repeating already-closed threads (see the long list below). Current best
next threads if nothing better comes to mind: (a) ETH/SOL regime-split
replication of the spot-momentum CHEAP+MID finding once backfill reaches
better coverage there, (b) a genuinely fresh angle not yet tried.

**Spread-vs-win-rate: tested and RETRACTED (2026-09-13, /loop iter 3).**
Raw CHEAP result looked real (wide spread wins more, z=-7.4) but is fully
explained by the already-known within-CHEAP price gradient (both spread
and win rate rise smoothly with price 0.00→0.30) — narrow-price-slice
control collapses it to z=-1.61. Same trap as the retracted round-nickel
finding. Not a real signal; don't retest without new data.

**num_distinct_assets_traded_last_hour_bucket vs win rate: tested and
RETRACTED (2026-09-13, /loop iter 4).** Raw pooled result looked real
(more assets/hr = higher win rate) but REVERSES under regime/asset
control (within CHEAP, within Ethereum: MORE diversity = LOWER win rate)
— a regime-composition confound, not real. Don't retest.

**BTC resolution_cache coverage now ~95% (14,001/14,725), full miner
re-run confirms streak-length retraction is structural (z still ~600
even with near-complete coverage), not a data-quality issue. ETH/SOL
still thin (~1255 each) — backfill processes bnb→btc→doge→eth→hype→sol
alphabetically, so ETH/SOL won't be ready for several more hours.**

**Spot-momentum window optimization (2026-09-13, /loop iter 5): 2-3
minutes is the evidence-based optimal trailing window** (peak z=32.6 at
2min, decays to z=11.5 by 15min) for the validated CHEAP+MID finding —
refines the earlier arbitrary 3-min choice, doesn't change the verdict.
`ThinkEnigmatic/polymarket-bot-arena` checked and found underspecified
(no concrete rules) — dead end, don't re-check.

**OPERATIONAL: trader-intel.service crash loop (2026-09-13, /loop iters
6-7) — CORRECTED, real intervention was needed, not self-healing.**
Iter 6 wrongly declared this "self-healed, no action needed" after one
successful restart — it was actually an active crash loop (6+ restarts,
every 1-2 min, since 18:42 UTC). REAL root cause (found by reading
live_poller.py): `last_report` is in-memory only, set to `now` AFTER the
report suite completes — since the OOM happens DURING that suite,
`last_report` never persists, so EVERY restart immediately re-triggers
the full ~300-450MB hypothesis_miner.py subprocess and OOMs again. A
genuine pre-existing bug in live_poller.py, exposed by tonight's added
load (depth-imbalance collector + resolution_backfill.py). **Fixed by
temporarily stopping both new processes** (safely pausable/resumable) to
free ~53MB headroom — one report cycle then succeeded, breaking the
loop; resumed both afterward. Confirmed stable before moving on.
**If this recurs after resolution_backfill.py finishes (it shouldn't),
the real fix is persisting `last_report` to a state file in
live_poller.py** so a crash mid-report doesn't cause infinite immediate
retries — not yet done, just diagnosed. **Lesson: verify an OOM incident
stays fixed with a SECOND check a minute later before declaring it
self-healed.**

**Spot-momentum finding: session-invariant (2026-09-13, /loop iter 8).**
Gap/z near-identical across Asia/Europe/US UTC blocks (39-45pp, z=18-20
all three) — no session-dependence, doesn't need conditioning. Combined
with regime-control + temporal-stability + time-in-window checks already
done, this is now one of the most thoroughly validated findings in the
project.

**RESOLVED (2026-09-13, /loop iter 9): entry size predicts win rate
independent of regime, even within CHEAP.** Closes the miner's own
previously-ACTIVE/open "conviction-correlation" question. Within CHEAP:
under_1 vs under_3 entry-size buckets, ~14-18% vs ~7-12% win rate
depending on snapshot, z well past the bar both times, TEMPORALLY STABLE
(both halves individually clear it). Validates the foundational
assumption behind every conviction-based sizing mechanism already shipped
(CONVICTION_HEDGE_MULTIPLIER, ACCURACY_SCOUT_MULTIPLIER, etc.) — first
direct confirmation the size-accuracy link is real, not just assumed.
**Follow-up RESOLVED (/loop iter 10): bot already replicates this, no
gap.** Both live bots' own ledgers show the same size-accuracy pattern
(paperbot 8.54%→16.39%, paperbot-100 8.42%→16.83%, both ~2x, z~=-8.6) —
closely mirrors the real trader. Emergent from existing sizing logic,
no new mechanism needed. Thread closed cleanly.

**Depth-imbalance collector: early peek only, NOT a finding (2026-09-13,
/loop iter 11).** 844 rows/54 market-series so far (~70min banked).
Directional check (imbalance predicts next-poll price direction) came
back 55.5% (n=713), consistent with published order-flow-imbalance
literature but WAY too early to trust — no temporal split, no regime
breakdown, no trend confound check possible yet. Revisit properly once
days (not hours) of data are banked.

**Ladder-instrumentation gap CLOSED IN CODE, not yet deployed (2026-09-13,
/loop iter 12, commit 6b01f0c pushed).** `SettlementRecord` now persists
per-fill (size, price, ts) from `order.fills` (already tracked in
memory, just never saved) — purely additive, default `[]`, 439/439
tests passing. **Deliberately not deployed** — needs a bot restart,
which would reset the in-progress PnL-tracking window. Flagged for the
user to decide when to deploy (next natural restart) rather than forcing
it. Once live + a few hours of fresh settlements, re-run
`scratch_ladder_check.py`'s methodology against the bot's own ledger —
closes the laddering thread for real.

**Refines "divided attention" finding (2026-09-13, /loop iter 13):**
connected to the spot-momentum edge — the edge itself is just as strong
when clustered (43.4pp vs 36.5pp gap) but clustered entries are far LESS
likely to be momentum-aligned in the first place (25.9% vs 42.2%
detection rate). Precise mechanism: dividing attention across assets
degrades momentum DETECTION, not execution quality once detected — he
doesn't misjudge trend when he notices it, he's just less likely to
notice/act on the right side at all when rushed across multiple assets.

**Boundary condition on divided-attention finding (2026-09-13, /loop
iter 14): no leader/follower asymmetry.** BTC-leads (27.5% aligned) vs
BTC-follows (24.4% aligned) within clusters — only a modest 3pp gap,
both similarly degraded vs the 42.2% solo baseline. The mechanism is
about being in a cluster at all, not about initiating vs reacting within
it. Clean near-null, don't re-test.

**paperbot-100 safety controls: first real stress-test, working as
designed (2026-09-13, /loop iter 15).** MAX_HOURLY_DRAWDOWN_PCT has
tripped 6 times tracing a severe equity decline: peak $402.36 → ~$189-197
now, a ~51-53% drawdown over ~15h, STARTING BEFORE tonight's restart
(first trip 04:07 UTC). Not a bug — process healthy, hedges/existing
positions keep managing normally throughout each pause, exactly as
designed. Explains why trade count looked "stalled" between two PnL
checks (a 30min pause window happened to cover that gap). Context for
interpreting recent PnL numbers: paperbot-100's poor since-restart ROI
partly inherits from this longer pre-existing drawdown, not solely the
~3h window — still use since-restart per the hard rule, just know this
context.

**Rebate-offset magnitude quantified per regime (2026-09-13, /loop iter
16), current era (post-Aug-7) BTC/ETH/SOL only:** CHEAP +$4,743 net
profitable (n=45,518), MID -$1,144 real loss but 88.1% offset by rebate
($1,008) — essentially breakeven, CORE +$3,267, HIGH +$3,910. Doesn't
contradict the Kelly-slope-mismatch thread (that's about sizing SHAPE,
not profitability sign) — adds real precision to "where the edge is."
**Process warning for ongoing backfill work: a pooled cross-asset/era
aggregate right now risks a composition artifact** — first pass (all
assets/eras pooled) wrongly showed CHEAP at +$22,418, 80% of which was
just BTC's disproportionate resolution_cache coverage plus a rotated-out
historical asset (Dogecoin) mixing eras. Always break down by asset +
scope to current era before trusting a pooled number until backfill is
more complete.

**Unifying finding (2026-09-13, /loop iter 17): entry size directly
encodes momentum-reading.** Aligned-with-momentum entries are ~2x the
size of against entries (mean $5.17 vs $2.69, t=30.1), temporally stable
(t=24.4/18.2 both halves). Connects the size-accuracy finding (size
predicts win rate) and the momentum finding (momentum predicts win rate)
into one causal chain: size tracks momentum-reading, which predicts
outcome — not three separate coincidental correlations.

**RESOLVED, retracted — conclusion CORRECTED (2026-09-13, /loop iters
19-20): rolling-accuracy-predicts-size is NOT real, but for a different
reason than first thought.** hypothesis_miner.py was fixed (era-scoped
to current BTC/ETH/SOL, TWAP_SWITCH+ only — same class of fix as the
controlled-pair fix, real and shipped) but the lead STILL appeared after
that fix, disproving iter 19's asset/era-composition diagnosis. Found
the real cause instead: the miner computes this per-TRADE not per-
market, and a genuine POSITION-INDEX confound exists (later trades
within a market skew toward the "low" bucket, mean position 17.5 vs
13.3). Controlling for position (first-entry only): t=-0.53, null —
matches the original BTC-only check. **The follow-up question (recent inaccuracy -> more trades within a
market) was also tested (/loop iter 21) and ALSO doesn't survive** —
mixed by regime (CHEAP null, MID barely) and fails temporal stability
pooled (first half t=-4.58, second half t=-1.00, same fading pattern).
**Whole rolling-accuracy thread now closed for good (iters 18-21)** —
neither size-scaling nor trade-count-persistence versions are real. Does
NOT cast doubt on the shipped ACCURACY_SCOUT_MULTIPLIER (different,
independently-validated mechanism). Don't re-open without a genuinely
new angle.

**hypothesis_miner.py era-scoping fix SHIPPED (2026-09-13, /loop iter
20):** added CURRENT_ERA_ASSETS/TWAP_SWITCH_TS filtering — every scan
now automatically excludes retired-asset/pre-switch data (real fix for
the composition-confound class, even though it didn't turn out to
explain the specific lead above). Bonus: runtime dropped from ~90-100s
to ~36s (less memory pressure too, relevant after tonight's OOM
incident). 160 findings now vs 169 before.

**Real spot trading volume: rejected as a win-rate predictor (2026-09-13,
/loop iter 22).** Clean null (z=0.54 CHEAP, 1.29 MID, 1.88 pooled) —
distinct from momentum (direction, real) and choppiness (magnitude,
tied to intensity) — sheer volume/activity level doesn't matter
independently. Don't retest.

**NEW real finding (2026-09-13, /loop iter 26): hedge TIMING responds to
real spot-momentum reversals.** 60.35% of hedges (n=2280) are timed
exactly when real 2-min trailing spot momentum has turned against the
original side (z=9.88), temporally stable (60.44%/60.26% both halves,
z=7.05/6.93). Connects the hedge-mechanics thread and momentum thread —
shows real exchange price (not just derived contract price/level) drives
WHEN he hedges, not just how big.

**NEW (2026-09-13, /loop iter 31): first-entry momentum alignment
predicts WHETHER he hedges at all — BUT SIGN REVERSES ON ETH (iter 44),
important correction.** BTC: aligned entries hedge LESS (74.4% vs
80.9%, z=-3.38). **ETH: aligned entries hedge MORE (76.0% vs 63.1%,
z=+5.80) — opposite sign, not just non-replication.** Two live,
undistinguished hypotheses at the time, now RESOLVED (/loop iter 49):
confirmed conviction/position-building, not corrective. Among ETH hedged
markets, original entry size is ~2x bigger when aligned ($2.58 vs $1.34,
t=11.12) — opposite of what a corrective mechanism predicts. ETH's
momentum-hedge link is downstream of the already-known size-momentum
link (iter 17), not an independent reversal-detection mechanism like
BTC's.

**Refined final picture (/loop iter 45): hedge-SIZE-scales-with-
magnitude DOES generalize cross-asset (ETH t=6.13, r=0.16, weaker than
BTC's r=0.24 but real) — he reacts to "something real is happening"
fairly universally. But the sharper directional read (reversal-timing,
hedge-existence direction) is BTC-specific/sharper, consistent with BTC
getting his closest attention.** Overall: entry-side win-rate effect of
momentum IS cross-asset-consistent (iter 35); hedge SIZE-vs-magnitude
generalizes weaker; hedge TIMING/EXISTENCE-direction are BTC-specific —
don't generalize those two without SOL data.

**Hedge SIZE also dose-responds to real reversal magnitude (2026-09-13,
/loop iter 27), completing iter 26's finding.** Smooth monotonic scaling:
$6.80→$9.85→$14.28→$23.10 across reversal-magnitude quartiles (3.4x),
r=0.24, temporally stable (r=0.23/0.20 both halves). Real spot momentum
drives both WHETHER/WHEN he hedges (iter 26) AND how much (this iter) —
a genuinely new real-exchange-price-based hedge-sizing component nothing
in the current calibration models. Strongest, most complete new
mechanism found this session on the hedge side. **Good build candidate**
once prioritized — this would be a real addition to hedge sizing, not
just a passive/calibration-only signal like the entry-side momentum
finding. **Confirmed independent of the contract-price signal (2026-09-13,
/loop iter 28)**: partial r=0.16 (t=7.78) controlling for the contract's
own price move, ~67% of raw correlation survives — genuinely new
information, not redundant. **IMPORTANT UPDATE (/loop iter 41): does
NOT clearly generalize to ETH** — ETH hedge-timing vs its own momentum
z=1.86, vs BTC's momentum (shared-signal hypothesis) z=2.19, neither
clears the bar. This mechanism is strongly validated for BTC
specifically, not yet shown universal — likely because BTC gets his
closest attention/has the best-quality real-time signal (consistent
with BTC leading cross-asset clustering). Any future build should scope
to BTC only unless SOL shows otherwise.

**Real spot round-number levels ($1000 marks): rejected (2026-09-13,
/loop iter 29).** z=1.80, doesn't clear the bar. Distinct from and
consistent with the already-rejected contract-price round-nickel
finding. Don't retest.

**Divided-attention pattern possibly extends to hedge-timing accuracy —
SUGGESTIVE, NOT confirmed (2026-09-13, /loop iter 30).** Clustered
hedges: 57.6% reversal-confirmed (n=1062) vs solo 64.7% (n=354), z=-2.34
— just under the bar, same direction as the entry-side finding. Honest
near-miss, not a result. Revisit once ETH/SOL coverage improves (more
hedge samples).

**Cross-asset clustering confirmed as a STABLE trait, not current-basket-
specific (2026-09-13, /loop iter 32).** Replicated the entry-clustering
finding on the historical DOGE/HYPE era (concurrent May 30-Aug 6, zero
overlap with current BTC/ETH/SOL): z=22.8/26.9/29.2 across windows, same
pattern. Rules out "artifact of the current 3-asset combination"
definitively — this is a durable, long-standing behavioral trait present
since the earliest data this project has.

**Laddering also replicates in historical DOGE/HYPE era, weaker
magnitude (2026-09-13, /loop iter 33).** Real in both eras (40.9%
ladder-signature historical vs 56.2% current) — long-standing trait, not
new. Possible strengthening over time OR a price-scale confound (DOGE/
HYPE had different typical prices than BTC/ETH/SOL, affecting the fixed
$0.005 threshold's meaning) — not disentangled, reported honestly as an
open question rather than a picked interpretation.

**Spot-momentum finding CONFIRMED CROSS-ASSET on all 3 current-basket
assets (2026-09-13, /loop iter 35).** ETH coverage jumped to 46%
(backfill), finally enabling this test: ETH CHEAP+MID z=8.14 (54.8% vs
19.5%), SOL CHEAP+MID z=12.65 (74.1% vs 20.6%, and SOL MID alone clears
the bar too, z=8.59). BTC was already z=27.5. Strong, consistent,
cross-asset confirmation — this is now the best-validated finding of the
whole session. **Temporal stability also now confirmed on all 3 assets
(ETH iter 36, SOL iter 116: both halves individually z>8 for SOL)** —
the win-rate effect AND its temporal stability are both independently
triple-confirmed.

**RESOLUTION_CACHE BACKFILL FULLY COMPLETE (2026-09-13, /loop iter 122):
80,849 total entries, only 2 unresolvable in this project's ENTIRE
history.** Definitive full-power re-check: CHEAP+MID z=18.99(BTC)/
19.79(ETH)/21.79(SOL) — remarkably tight, consistent range across all
three assets. Even CHEAP alone now clears the bar on every asset
(z=3.93/8.59/10.20). This is the strongest possible evidentiary standing
this project's data can produce — full coverage, full power, all three
current-basket assets. **Confound battery now essentially cross-asset
complete (/loop iter 123, 125): time-in-window (MH odds ratio
6.56x/8.97x on ETH/SOL) and session-invariance (all z>10 both assets)
both confirmed on ETH/SOL too, matching BTC exactly.** Only "does he
actively hunt it" remains technically BTC-specific, though its
choppiness-based logic is asset-agnostic and likely generalizes. By any
reasonable standard, the single most rigorously validated finding this
project has ever produced.

**Process note: /loop cron interval (3min) is shorter than a full
iteration takes, causing queued duplicate firings.** Treated backlog as
one continuation. If this recurs, worth a longer interval.

**SOL backfill note (2026-09-13, /loop iter 97): SOL has ~8,142 PRE-TWAP
historical markets too (it was in the DOGE/HYPE-era basket, not just
current-era) — backfill sorts chronologically within the sol- prefix, so
it's clearing SOL's OLDEST markets first. Post-TWAP SOL coverage (what
cross-asset checks need) is STILL STUCK AT 1,275 despite total SOL cache
tripling — don't re-test SOL-specific findings until this backlog clears
(~35min more estimated as of this note).**

**Independent corroboration: BTC gets more trades/market, not more
markets (2026-09-13, /loop iter 46).** BTC 20.77 trades/market vs ETH
13.41, SOL 15.87 — market SELECTION nearly identical across assets
(~5,400-5,557 each). Simple, independent metric supporting the "BTC
gets closest attention" picture from a completely different angle than
the momentum-based findings.

**Significant refinement to the shipped hedge-ratio characterization
(2026-09-13, /loop iter 54): it's a blend of two different behaviors.**
Median hedge ratio (hedge/original size) for BTC: aligned-with-momentum
original entries = 0.64x (modest, proportionate hedge); against-momentum
originals = 3.29x (hedge OVERWHELMS the original position, effectively
flipping net exposure). The existing "~45-55% of full arbitrage,
halve-exposure" characterization (trader-mental-model-synthesis) is a
blend obscuring this real bimodality. **Directly actionable**: any
hedge-sizing mechanism should condition on original-entry momentum-
alignment, not use one uniform ratio. **REPLICATES on ETH (/loop iter
55): 0.40x aligned vs 2.83x against — same direction/magnitude as BTC.**
Completes an elegant two-decision synthesis: WHETHER to hedge is asset-
dependent (corrective on BTC, conviction-driven on ETH), but HOW BIG the
hedge is once triggered is universal across assets — the truly
cross-asset-validated piece of the whole hedge-momentum thread.

**SYNTHESIS (2026-09-13): "BTC gets his closest attention" — a named
pattern with 4 converging, independent lines of evidence.** (1) BTC
leads cross-asset entry/hedge clustering (59% BTC→ETH, 54% BTC→SOL, SOL
leads ETH). (2) BTC gets ~1.5x more trades/market than ETH, ~1.3x more
than SOL (20.77 vs 13.41 vs 15.87), despite nearly identical market
SELECTION counts across all three. (3) BTC shows richer laddering (mean
3.19 rungs vs ETH 2.85, SOL 2.75). (4) Hedge-timing-precision (does the
hedge correctly follow a real reversal) shows EXACTLY the same ranking:
BTC 60.35% (z=9.88, strong) > SOL 53.18% (z=3.45, real but modest) >
ETH 51.96% (z=1.86, null) — precisely matching the clustering-leadership
order from (1), an independent metric converging on the identical
hierarchy. Also explains why the hedge-momentum
reversal-detection mechanism is sharp/real on BTC but not ETH/SOL (iter
41/44/49) — the entry-side win-rate momentum effect is genuinely
universal (cross-asset validated), but the precision of REACTING to it
scales with how closely each asset is watched. A coherent, well-
supported behavioral picture, not a single fragile correlation.

**NEW real finding (2026-09-13, /loop iter 60): hedge size scales ~4x
with urgency (time-to-close), independent of momentum.** BTC: mean
hedge size $18.11 (27-111s remaining) down to $4.57 (215-273s
remaining), t=12.09, temporally stable (t=8.00/8.65 both halves).
Nothing currently shipped models this for hedges (TTC_SIZE_MULTIPLIER
is entry-only). **Confirmed genuinely independent of momentum-magnitude
(/loop iter 61): r(urgency,momentum)=0.028 (~zero), partial r(urgency,
hedge_size|momentum)=0.229 vs raw 0.230 — unchanged.** Two real,
additive contributors to hedge size, not one signal in disguise.
**CONFIRMED CROSS-ASSET (/loop iter 62): ETH shows nearly identical
shape (t=9.91, ~3x range $4.89-$14.93).** Unlike the momentum-based
hedge mechanisms (asset-dependent), urgency-based hedge sizing is
cleanly universal on both assets tested — the single cleanest, most
reliable build candidate of the whole session. **GENERALIZES BEYOND
HEDGES (/loop iter 82): same-side re-entries show the identical pooled
pattern** ($3.05→$7.76 across urgency quartiles, t=33.46, n=70,783 — by
far the largest sample of any check tonight), also confirmed on ETH
(iter 83, t=23.68).

**MAJOR CORRECTION (2026-09-13, /loop iter 84): the above "universal"
framing does NOT survive a regime control — never actually tested
across iters 60-83 despite the extensive replication.** Urgency-quartile
composition differs sharply by regime (Q4/not-urgent is 60% MID alone;
Q1/urgent is an even CHEAP/MID/CORE/HIGH spread) — the pooled "bigger
when urgent" reading was substantially a MID-composition artifact.
Within-regime (BTC same-side entries): **HIGH real & strong (t=+10.68,
$18→$29), MID real but weak (t=+4.02), CHEAP REVERSED (t=-28.56, urgent
is SMALLER: $1.49→$1.00), CORE REVERSED (t=-4.74, $7.25→$6.67).** Not
one clean universal mechanism — a mixed, regime-dependent picture where
two bands go the opposite direction from the other two. Likely
explanation: HIGH's version is a genuine decisive last-push scale-up;
CHEAP/CORE's late-window entries may be more like a "small token bet
before it's too late" pattern. **Retract the "most thoroughly validated
mechanism of the session" framing — statistical power, temporal
stability, and cross-asset replication are not substitutes for actually
running the standard confound checklist (here: regime composition).**
Any future build must be regime-specific, not a single flat multiplier.
**CONFIRMED CROSS-ASSET on SOL (/loop iter 115): the corrected regime-
dependent pattern itself replicates almost exactly** (CHEAP reversed
t=-26.42, MID real t=12.30, CORE weak t=2.18, HIGH strongest t=7.05) —
this is a robust, general, now cross-asset-validated mechanism once
properly regime-split, not a BTC-specific artifact of the correction.
**Sanity-checked the OTHER hedge finding (size-vs-reversal-magnitude,
iter 27-28) against the same confound (/loop iter 85): survives cleanly
— CHEAP/MID/CORE all same direction, all clear the bar, no reversal.**
Not everything from tonight has this vulnerability. **Due-diligence
sweep COMPLETE (iters 85-87): timing/reversal-detection also survives
(even stronger in CHEAP), existence-direction also survives (same
direction both testable regimes). 3 of 4 major hedge-momentum mechanisms
are regime-control-clean; only urgency-scaling failed.** Responsible,
complete closure of tonight's biggest research thread's confound-check.

**Liquidity-conditioned sizing thread: DEFINITIVELY RETRACTED
(2026-09-13, /loop iter 67).** Redone with the correct original
methodology (log-transform, time-into-window partial correlation, 45s
lag filter) split chronologically — every single regime band (CHEAP,
MID, CORE, HIGH) shows the same pattern: strong effect in the first
half of the data, collapsing to near-zero or reversing sign in the
second half. A textbook spurious-early-data-artifact signature, uniform
across all 4 bands. Not a real mechanism. Project memory file
(`liquidity-conditioned-sizing-promising.md`) and its MEMORY.md index
line updated directly to reflect the retraction. Closed for good —
don't re-test without a genuinely new angle or data source.

**Cause found for the second, smaller ~26h gap (Aug 8-10) — CORRECTED
framing (2026-09-13, /loop iter 68-70): the gap's existence was already
briefly noted in the persistent project memory, my real contribution was
the cause.** Real, accelerating losses right before it (6h win_rate
37.7%, -$773; worse than the 24h trailing average) — the OPPOSITE
signature from the main 13.6-day halt (which was ruled out as risk-
driven because he was on a hot streak right up to it). Platform-outage
check for THIS gap came back empty (reinforces risk-pause). **Near-miss
caught (iter 70): a search briefly surfaced a real Aug 31 outage that
belongs to the DIFFERENT, already-resolved main halt — not this gap.
Caught before writing anything wrong, but flagged as a reminder to
verify dates match the specific event under investigation.** Both
findings added correctly to the persistent project memory file.

**Size carries real info beyond alignment, now properly confirmed
(2026-09-13, /loop iter 78-79, 95-96).** Same-side re-entries (not just
first entries) extend the momentum-alignment finding with a ~10x bigger
usable sample (26k vs 2k). Within the aligned group, size predicts win
rate cleanly on BTC (47%→58%→66%→66% across quartiles, z=-11.72,
UNIVERSAL across both CHEAP z=-3.77 and MID z=-3.51). **On ETH: CHEAP-
specific only (z=-4.06), MID does not confirm (z=0.90, flat)** — a real
asset-dependent nuance, not a full reversal. Accurate final picture:
size tracks confidence intensity, but the regime-universality of that
signal varies by asset.

**No other angle work currently queued.** Would need either a genuinely
new angle from scratch, or new user direction — don't force weak repeats
of the closed threads below.

Fully closed, don't re-open without new data: #1 (platform cutoff), #2
(order cancellations, blocked by API auth), #3 (on-chain funding,
infeasible at current pagination cost), #6 (PCA/multivariate, confirmed
existing rejection), #7 (macro-news), #8 (cross-asset tilt, retracted
confound), #9 (loss-streak participation), #11 (funding-rate reset).

## PAST STATUS (2026-09-12): all 8 BUGS_TO_FIX.md entries FIXED, tested, deployed — reference only, not active

The research-angle loop (cron `a31ef787`) was stopped by me after 3
consecutive honestly-flagged diminishing-returns iterations (see loop_log
iterations 22-24 and the angle history further below for what it found).
User then asked to fix every bug rather than start a rewrite -- decision
reasoning: bugs are narrow/well-understood (not architectural), and the
real value is months of calibration work a rewrite would throw away.

**All 7 discrete bugs + 1 systemic note from BUGS_TO_FIX.md are now
fixed in code, covered by tests (435/435 passing, 19 net new tests), and
committed locally.** See `BUGS_TO_FIX.md` itself for the exact fix
applied to each entry (its Status line), and `claude_logs.md`'s "FIX
SESSION" heading for the full narrative (including one real editing
mistake made and caught by the test suite -- two orphaned assertion
lines, fixed).

**DEPLOYMENT CONFIRMED COMPLETE (2026-09-12).** Committed (`0000793`),
pushed, pulled on the server as the paperbot user, py_compile-checked
clean, both `paperbot` and `paperbot-100` restarted individually and
verified live: both show `cap=8` in `SKIP_OPEN_ORDER_CAP` log lines
(bug #1), a CHEAP-band hedge placed and surviving normally (bug #3), and
`is_hedge=False` specifically on skip lines (bug #2's non-hedge-only
enforcement). Zero errors/tracebacks in either service's log in the
minute-plus after restart. All 8 BUGS_TO_FIX.md entries are done.

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

**Update (2026-09-13, /loop iters 127-129, job d541a715, still running, free
hand, don't stop):** Miner-report cleanup: `size_vs_own_prior_trade_in_market`
retracted (pure regime-composition artifact, caught on first pass — see
above). Momentum-hunting choppiness-control now confirmed cross-asset
(ETH t=-0.44, SOL t=1.14, both null like BTC) — closes that open item for
good. **New thread opened: order-book depth imbalance** — the two-sided
book collector flagged as blocked in `external-bot-strategies-loop-progress`
is now live and shows a promising-but-underpowered signal (win rate tracks
depth-imbalance direction in CHEAP z=2.20, MID z=1.52; n=261 total markets
so far, needs days more data before it clears the bar either way). Full
details in memory file `depth-imbalance-promising-underpowered.md`. Next
useful checks once more hours bank: (a) re-test depth-imbalance at proper
power, (b) explain ETH's CORE-band divergence in the corrected urgency
finding, (c) decide on deploying the held-back ledger.py fills fix.

**Update (2026-09-13, /loop iter 130): "diminishing increments" hypothesis
RETRACTED.** Tested whether trade size shrinks as he adds more same-side
trades within a market (soft position ceiling) — raw MID-band signal
looked cross-asset real (BTC/ETH/SOL all t<-6, same direction, both
temporal halves negative). But position-index is mechanically correlated
with total-trades-in-market (r=0.75-0.77, structural), and total_n itself
predicts SMALLER average size (chatty/laddered markets get many small
clips, quiet markets get few big entries) — controlling for total_n
REVERSES the sign (BTC/ETH/SOL all flip to small positive, r<0.07). Not a
real "tapering" behavior — just market-chattiness composition wearing a
disguise. **New confound to always check for within-market-sequence
dimensions going forward: total-trades-in-market**, alongside the
established regime/era/asset-composition checks.
