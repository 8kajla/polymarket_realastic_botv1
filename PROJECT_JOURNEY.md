# What We Built — The Real Story, In Order

A plain-language, **chronologically accurate** recap of this project,
pulled straight from the actual commit history (67 code changes total,
spanning 2026-09-06 to 2026-09-11) — not a tidied-up summary. Corrected
after the first draft wrongly implied all three bots existed together
from the start; they didn't.

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

## 8. Where things stand right now

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

**In short:** this wasn't "build three bots and calibrate them all
together." It was one bot, hardened and calibrated through dozens of real
fixes first; then a second, bankroll-limited bot rode along through
another few dozen changes; and only after that did we deliberately freeze
one copy as a permanent control so the *next* round of changes — including
tonight's six — could be judged against something that never moved.
