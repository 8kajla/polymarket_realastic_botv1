# What We Built — The Real Story, In Order

A plain-language, **chronologically accurate** recap of this project,
pulled straight from the actual commit history — not a tidied-up
summary. Corrected after the first draft wrongly implied all three bots
existed together from the start; they didn't. Sections 1-8 below cover
2026-09-06 through 2026-09-11 (67 code changes); section 9 covers the
much larger research-and-build effort that followed on 2026-09-12/13.

---

## 1. The goal

There's a real trader on Polymarket (wallet `0xb0f8...cef5`, nicknamed
"pspspsps5") who trades six crypto "Up/Down" markets that each last only
5 minutes (BTC, ETH, SOL, BNB, DOGE, HYPE — three of which later went
quiet, more below). We wanted a bot that **copies how this trader
behaves**, on paper only — no real money, ever. We had **778,116 of the
trader's real historical trades** to learn from.

## 2. Phase 1 — one single bot, built and hardened (Sept 6–8, ~25 changes)

The project started as **one bot, and only one bot**, on 2026-09-06. For
the first stretch — roughly **25 separate commits** — everything
happened to that single instance:

- Built the core engine: watch live markets, watch real order books,
  simulate realistic paper fills (not instant, not free — actual queue
  position, reprices, timeouts), track a ledger, settle trades.
- Put it on a real server and immediately hit real-deployment problems:
  the price feed disconnecting under load, crashes on first live runs,
  trades briefly double-counted or dropped, an unbounded retry loop that
  could have hammered the exchange's API.
- Started teaching it the trader's actual habits from the 778k-trade
  history: how much he bets at different price levels, whether he keeps
  betting the same side, and — a big one — that he deliberately places a
  second, opposite-side "insurance" bet in about a third of markets
  (confirmed this roughly halves his worst-case loss, and sometimes
  guarantees a profit no matter which side wins).
- Found and fixed a real bug where the bot sized orders using the wrong
  side's price entirely, and noticed the real trader had quietly stopped
  trading three of the six coins (BNB, Dogecoin, Hyperliquid) — so we
  matched that.

**At this point there was still only one bot.**

## 3. Phase 2 — a second bot is added, but development stays on both (Sept 8–10, ~33 more changes)

Late on Sept 8 (commit #26 of 67), we added a **second instance**: a
version with a real, hard bankroll limit — starting at $100 — to answer
a concrete question: *"if I actually put $100 into this, what would
happen?"* The main bot stayed unconstrained.

Importantly, **this did not slow down or pause new development** — the
next stretch (roughly **33 more commits**, Sept 8 through Sept 10) kept
adding and fixing real behavior on top of *both* running instances at
once:

- Continuous, price-sensitive bet-size scaling instead of coarse buckets.
- A proper multi-attempt hedge model (the single-shot version was too
  crude), then a bug where hedging could run away and was confirmed to
  be losing real (paper) money — capped it.
- A "scout" tier: modeling the trader's small, tentative first bets.
- Several real accounting bugs (wrong fill prices used at settlement,
  order-ID collisions after restarts silently dropping trades, capital
  tracking drifting from reality) — each found from live logs, confirmed,
  and fixed.
- Modeled maker rebates properly (confirmed this trader only ever places
  resting orders, never pays taker fees).

So by the time we're roughly **58 commits in**, both bots had already
been through dozens of rounds of real changes — nothing was frozen yet.

## 4. Phase 3 — splitting off a frozen control, so we could actually tell if changes helped (Sept 10)

By Sept 10 we had a problem: with both bots constantly changing, there
was no clean way to answer *"did that last change actually help, or did
it just coincide with a lucky/unlucky market stretch?"*

So we made a deliberate split:

- The existing $100 instance was **frozen as a permanent control** and
  renamed **`paperbot-mini`** — every new behavioral change from this
  point on gets an explicit on/off switch, and `paperbot-mini` is always
  switched **off**, on purpose, so it keeps running the *old* strategy
  as a clean baseline.
- A separate instance kept moving forward and receiving every new
  feature — this became today's **`paperbot-100`**, and its bankroll was
  later raised to a more realistic **$1,000** directly on the server.
- The original unconstrained bot — **`paperbot`** — also keeps receiving
  every new feature, with no bankroll limit at all.

**This is the "way after many changes" moment you're pointing at** — the
three-bot setup we have today wasn't the starting design, it emerged
after ~58 real code changes, specifically so we'd have a trustworthy
before/after comparison going forward.

## 5. Phase 4 — more real behavior shipped on all fronts (Sept 10–11)

With the control in place, work continued on `paperbot` and
`paperbot-100` (with `paperbot-mini` explicitly opted out of each one):

- Bet size that reacts to how close a market is to closing.
- Hedge behavior that reacts to liquidity, weekends, and how far the
  price has already moved against the trader's position.
- Reverse-engineered the bot's own tick timing and a cross-market
  "sticks with his last pick" pattern, and implemented both.

## 6. Phase 5 — the big research pass: hunting for what we were still missing

Once the bot was already replicating a lot of real behavior well, we ran
an extended, mostly autonomous research process that:

1. Proposed a candidate pattern (e.g. "does his bet size in one market
   predict his size in the *next* market?").
2. Tested it against the real 778k-trade data for statistical significance.
3. Checked hard for false positives — is this secretly just another
   known effect in disguise? Is the reasoning circular? Does it still
   hold up both before *and* after Polymarket's own August 7 pricing
   mechanism change (a real structural break in the data)?
4. Only kept it as a confirmed finding if it survived all of that.

This produced **7 validated findings**. One (a "bets less when trading
multiple correlated coins at once" pattern) reversed direction once
split across that August structural break, so we deliberately did **not**
build it, and documented why.

## 7. Phase 6 — implementing the last 6 findings (tonight, Sept 11)

The other six were real and worth building:

1. **Cheap/mid-price size scaling** — smooth price-based bet sizing,
   extended to the two price ranges it wasn't already covering.
2. **Hedge-rate carryover** — a hedge-heavy market predicts the next one
   will be too.
3. **Win/loss-aware persistence** — he sticks with his current pick even
   *more* after a loss than after a win.
4. **Conviction-based hedging** — an unusually small first bet in a
   market predicts *that same market* needs more hedging later.
5. **Size carryover across markets** — bet sizes echo slowly from one
   market to the next.
6. **Accuracy-aware scouting** — after a stretch of wrong calls, he
   scouts (small tentative bets) more; after a good streak, less.

Findings #3 and #6 needed something genuinely new: the bot had never
before fed its own results back into its own decisions in real time. Now
it remembers whether its last bet in a market actually won, and how
accurate its recent calls have been, and uses both going forward.

Every one of these six got its own on/off switch — on by default for
`paperbot`/`paperbot-100`, explicitly off for `paperbot-mini` — following
the same control-group discipline set up back in Phase 3.

## 8. Where things stood after Phase 6 (Sept 11)

- **67 real code changes** since Sept 6, the vast majority of them
  happening before the three-bot control setup even existed.
- All code tested (372 automated tests), committed, and live on the AWS
  server.
- `paperbot` (unconstrained) and `paperbot-100` ($1,000 bankroll) are on
  the latest behavior.
- `paperbot-mini` ($100→$200) is still the clean, deliberately-frozen
  baseline it became in Phase 3 — it has opted out of every single
  behavioral change shipped since then, on purpose.
- Every finding — confirmed or rejected — is written down with the
  reasoning behind it, so future work builds on it instead of
  re-discovering the same things.

## 9. Phase 7 — a much bigger, mostly autonomous research-and-build pass (Sept 12–13)

Starting the evening of Sept 12, work shifted into a long, largely
self-directed research loop (checked in on but not micromanaged),
running continuously across two days. It found, checked, and — where
warranted — built on far more ground than Phase 5's original "7 findings"
research pass:

- **Filled a data-quality gap that had quietly undermined most earlier
  win-rate findings**: the "which side actually won" lookup only covered
  ~9% of all historical markets (populated lazily, not backfilled). Ran a
  ~12.5-hour backfill to bring that to 100% coverage (80,849 markets),
  then re-validated the project's key findings against the complete data
  instead of the earlier partial sample.
- **The single biggest new discovery of this whole project**: real
  exchange spot-price momentum (external to Polymarket, from live
  Coinbase-style 1-minute candles) in the 2-3 minutes before a bet
  predicts whether that bet wins — a strong, cross-asset, statistically
  overwhelming signal, confirmed stable across time, across sessions, and
  independent of a dozen different confounding explanations that were
  each specifically tested and ruled out.
- **Found that Bitcoin gets a measurably different, more attentive
  treatment than Ethereum or Solana** — four independent signals all
  point the same way: which coin's moves "lead" the others when he trades
  several at once, how many times he re-visits the same market, how
  detailed his bet-laddering is, and how precisely he times his insurance
  bets — with Bitcoin consistently the sharpest on every measure.
- **Two new real behaviors were built into the bots**, each only after
  passing the project's full checklist (statistically real, survives
  splitting the data into different time periods, not secretly some other
  known effect in disguise, checked for the specific way it would be
  used before shipping):
  1. A market he's already added to many times without it resolving
     tends to go worse than usual from then on — so the bots now size
     down further additions once that threshold is crossed, in the
     specific coin/price-range combinations where this was actually
     confirmed.
  2. The flip side: a market that's already needed two or more insurance
     bets tends to have his *original* pick come through more often than
     expected, not less — so the bots now size up further same-side bets
     once that happens, again only where confirmed.
- **A live self-correction, caught and fixed mid-stream**: an earlier
  "the closer to closing time, the bigger he bets" finding was built up
  across many checks and repeatedly called the most solid result of the
  whole project — until a standard cross-check (does this hold up
  separately in each price range, not just pooled together) revealed it
  actually reverses in two of the four price ranges. The overclaim was
  explicitly retracted and rewritten as the real, more nuanced
  price-range-dependent picture. The lesson from that mistake —
  "always split by price range before believing a pooled result" — was
  then applied as a standing checklist item for the rest of the pass, and
  caught several more would-be false positives before they were ever
  written up as real.
- **A second, similar near-miss, corrected the same way**: a follow-up
  finding — that a big loss makes the very next bet more likely to be
  insured — looked "unconfirmed" under the project's usual before/after
  check, until a real, separate discovery explained why: overall
  insurance-buying has been quietly declining for months, for reasons not
  yet understood, and that slow decline was distorting the simple
  before/after comparison. Controlling for it properly showed the
  original loss-reaction finding was real all along, just needed the
  right method. Checked the mirror case too (does winning big make him
  bet more aggressively, not just more cautiously after losing?) — no,
  that reaction only exists after losses, not wins.
- **Confirmed the two rotations away from other coins probably weren't
  performance-driven** for one of them (the coins he dropped were
  actually doing *better*, not worse, at the time — consistent with an
  earlier finding that this was likely a platform listing change, not his
  own choice) and genuinely may have been for the other.
- Dozens of other candidate patterns were tested and honestly rejected —
  copying specific public bot strategies people have written up online,
  various calendar/day-of-week effects, an apparent order-book signal
  that turned out to be a data-collection bug, and more — each written
  down so nobody re-tests the same dead end later.

**One correction to Phase 3/8 above, caught while writing this section**:
`paperbot-mini`, described above as the permanent frozen control, is no
longer running as of this pass — only `paperbot` and `paperbot-100` are
live on the server. Its systemd service has been fully removed (not just
stopped), and its own saved records show its last real settlement was
**2026-09-11 09:41 UTC** — so it kept running as intended for roughly a
day after being frozen, then was decommissioned entirely rather than left
running indefinitely as a clean baseline. Nothing in this project's own
notes records the *why*; flagging the *when* honestly since it was
findable, without guessing at the reason.

## 10. Where things stand right now

- Two live bots — `paperbot` (unconstrained) and `paperbot-100` ($100
  bankroll with its own safety limits) — both on the latest behavior,
  460 automated tests passing.
- The historical "who actually won" data is now 100% complete, not ~9%,
  so every win-rate finding from this point on is checked against the
  full picture.
- The single strongest, most-validated finding in the project's history
  is the real-exchange-momentum signal described above.
- A handful of real, well-documented open questions remain deliberately
  unbuilt for now — a slow, unexplained decline in how often he buys
  insurance over the months studied; a confirmed "reacts within about
  20-50 seconds of a market suddenly becoming clear-cut" pattern that
  would need a genuinely new piece of bot architecture, not just a
  calibration tweak, to act on.
- Every finding — confirmed, corrected, or rejected — is written down
  with the reasoning and the actual numbers behind it, continuing the
  same discipline from Phase 5 at much greater scale.
