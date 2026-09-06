#!/usr/bin/env python3
"""
Deep, hypothesis-driven analysis of trade.jsonl: win/loss economics,
calibration edge, and every behavioral pattern worth checking for bot
implementation.

Market winner inference: trade.jsonl has no resolution field (it's a
trade log, not a settlement log), so the winning outcome per market is
inferred as the outcome of the single MOST DECISIVE trade observed
anywhere in that market's lifetime (price >= 0.85 -> that outcome wins;
price <= 0.15 -> the complementary outcome wins). This covers ~85% of
markets; the rest are excluded from win/loss-dependent analysis and
reported as a coverage metric, not silently dropped.
"""
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone

INPUT_PATH = "trade.jsonl"
CONFIDENT_THRESHOLD = 0.85
FLOOR_LOT_SIZE = 0.02
REGIME_BANDS = [("CHEAP", 0.0, 0.30), ("MID", 0.30, 0.70), ("CORE", 0.70, 0.90), ("HIGH", 0.90, 1.0001)]


def regime(price):
    for name, lo, hi in REGIME_BANDS:
        if lo <= price < hi:
            return name
    return "HIGH"


def asset_of(title):
    return title.split(" Up or Down")[0] if " Up or Down" in title else None


def pct(n, d):
    return round(100.0 * n / d, 2) if d else None


print("[1/4] Loading and grouping trades by market...", flush=True)
markets = defaultdict(list)
with open(INPUT_PATH) as f:
    for line in f:
        d = json.loads(line)
        markets[d["slug"]].append(d)

print(f"  {sum(len(v) for v in markets.values())} trades, {len(markets)} markets", flush=True)

print("[2/4] Inferring market winners...", flush=True)
market_winner = {}
confident_count = 0
for slug, trades in markets.items():
    trades.sort(key=lambda t: t["timestamp"])
    most_decisive = max(trades, key=lambda t: abs(t["price"] - 0.5))
    price, outcome = most_decisive["price"], most_decisive["outcome"]
    if price >= CONFIDENT_THRESHOLD:
        market_winner[slug] = outcome
        confident_count += 1
    elif price <= (1 - CONFIDENT_THRESHOLD):
        market_winner[slug] = "Down" if outcome == "Up" else "Up"
        confident_count += 1
    # else: leave unresolved (excluded from win/loss analysis)

print(f"  {confident_count}/{len(markets)} markets confidently resolved "
      f"({pct(confident_count, len(markets))}%)", flush=True)

print("[3/4] Computing trade-level economics...", flush=True)

# ---- accumulators ----
overall = {"n": 0, "wins": 0, "cost": 0.0, "payout": 0.0, "pnl": 0.0}
by_asset = defaultdict(lambda: {"n": 0, "wins": 0, "cost": 0.0, "payout": 0.0, "pnl": 0.0})
by_asset_regime = defaultdict(lambda: {"n": 0, "wins": 0, "cost": 0.0, "payout": 0.0, "pnl": 0.0})
by_asset_position = defaultdict(lambda: {"n": 0, "wins": 0, "cost": 0.0, "payout": 0.0, "pnl": 0.0})
price_bucket = defaultdict(lambda: {"n": 0, "wins": 0})  # 0.05-wide global bucket -> calibration
by_side_consistency = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})  # "consistent"/"switch"
by_regime_consistency = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "cost": 0.0})  # (regime, "consistent"/"switch")
dollar_weighted = {"cost": 0.0, "cost_on_wins": 0.0}
floor_lot_stats = defaultdict(lambda: {"n": 0, "wins": 0, "cost": 0.0, "pnl": 0.0})  # "floor_lot"/"normal"
gradient_alignment = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})  # (regime,"aligned"/"not")
by_hour = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
by_dow = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
entries_per_market_bucket = defaultdict(lambda: {"n_markets": 0, "total_pnl": 0.0, "total_cost": 0.0})
prev_trade_win = {"win": {"n": 0, "next_win": 0}, "loss": {"n": 0, "next_win": 0}}
market_level = []  # (asset, n_entries, market_pnl, market_cost) for confidently-resolved markets

GRADIENT_EPS = 0.005

global_trade_sequence = []  # (timestamp, won) for streak analysis

for slug, trades in markets.items():
    winner = market_winner.get(slug)
    if len(trades) == 0:
        continue
    asset = asset_of(trades[0]["title"])
    if asset is None:
        continue

    market_pnl = 0.0
    market_cost = 0.0

    for idx, t in enumerate(trades):
        price = t["price"]
        size = t["size"]
        cost = t["usdcSize"]
        outcome = t["outcome"]
        reg = regime(price)
        is_floor_lot = (size == FLOOR_LOT_SIZE)

        won = None
        pnl = None
        if winner is not None:
            won = (outcome == winner)
            payout = size if won else 0.0
            pnl = payout - cost

            overall["n"] += 1
            overall["wins"] += int(won)
            overall["cost"] += cost
            overall["payout"] += payout
            overall["pnl"] += pnl

            a = by_asset[asset]
            a["n"] += 1; a["wins"] += int(won); a["cost"] += cost; a["payout"] += payout; a["pnl"] += pnl

            ar = by_asset_regime[(asset, reg)]
            ar["n"] += 1; ar["wins"] += int(won); ar["cost"] += cost; ar["payout"] += payout; ar["pnl"] += pnl

            pos = "first" if idx == 0 else ("2nd_3rd" if idx <= 2 else "4th_plus")
            ap = by_asset_position[(asset, pos)]
            ap["n"] += 1; ap["wins"] += int(won); ap["cost"] += cost; ap["payout"] += payout; ap["pnl"] += pnl

            bucket = round(price * 20) / 20  # nearest 0.05
            pb = price_bucket[bucket]
            pb["n"] += 1; pb["wins"] += int(won)

            fl_key = "floor_lot" if is_floor_lot else "normal"
            fl = floor_lot_stats[fl_key]
            fl["n"] += 1; fl["wins"] += int(won); fl["cost"] += cost; fl["pnl"] += pnl

            dt = datetime.fromtimestamp(t["timestamp"], tz=timezone.utc)
            hb = by_hour[dt.hour]
            hb["n"] += 1; hb["wins"] += int(won); hb["pnl"] += pnl
            db = by_dow[dt.weekday()]
            db["n"] += 1; db["wins"] += int(won); db["pnl"] += pnl

            market_pnl += pnl
            market_cost += cost

            global_trade_sequence.append((t["timestamp"], won))
            dollar_weighted["cost"] += cost
            if won:
                dollar_weighted["cost_on_wins"] += cost

            # side persistence consistency
            if idx > 0:
                consistent = (outcome == trades[idx - 1]["outcome"])
                key = "consistent" if consistent else "switch"
                sc = by_side_consistency[key]
                sc["n"] += 1; sc["wins"] += int(won); sc["pnl"] += pnl

                rc = by_regime_consistency[(reg, key)]
                rc["n"] += 1; rc["wins"] += int(won); rc["pnl"] += pnl; rc["cost"] += cost

                # weakness/strength gradient alignment
                delta = price - trades[idx - 1]["price"]
                if reg == "CHEAP":
                    aligned = delta < -GRADIENT_EPS
                    ga = gradient_alignment[("CHEAP", "aligned" if aligned else "not")]
                    ga["n"] += 1; ga["wins"] += int(won); ga["pnl"] += pnl
                elif reg == "HIGH":
                    aligned = delta > GRADIENT_EPS
                    ga = gradient_alignment[("HIGH", "aligned" if aligned else "not")]
                    ga["n"] += 1; ga["wins"] += int(won); ga["pnl"] += pnl

    if winner is not None:
        n_entries = len(trades)
        if n_entries == 1:
            eb = "1"
        elif n_entries <= 3:
            eb = "2-3"
        elif n_entries <= 6:
            eb = "4-6"
        elif n_entries <= 15:
            eb = "7-15"
        else:
            eb = "16+"
        epm = entries_per_market_bucket[eb]
        epm["n_markets"] += 1
        epm["total_pnl"] += market_pnl
        epm["total_cost"] += market_cost
        market_level.append((asset, n_entries, market_pnl, market_cost))

print("[4/5] Per-market net win/loss...", flush=True)
markets_net_positive = sum(1 for (_, _, mpnl, _) in market_level if mpnl > 0)
markets_net_negative = sum(1 for (_, _, mpnl, _) in market_level if mpnl < 0)
markets_net_zero = sum(1 for (_, _, mpnl, _) in market_level if mpnl == 0)
markets_net_total = len(market_level)

by_asset_market_net = defaultdict(lambda: {"pos": 0, "neg": 0, "zero": 0})
for (asset, _, mpnl, _) in market_level:
    k = by_asset_market_net[asset]
    if mpnl > 0:
        k["pos"] += 1
    elif mpnl < 0:
        k["neg"] += 1
    else:
        k["zero"] += 1

print("[5/5] Streak / autocorrelation analysis...", flush=True)
global_trade_sequence.sort(key=lambda x: x[0])
transitions = {"win_then_win": 0, "win_then_loss": 0, "loss_then_win": 0, "loss_then_loss": 0}
for i in range(1, len(global_trade_sequence)):
    prev_won = global_trade_sequence[i - 1][1]
    cur_won = global_trade_sequence[i][1]
    if prev_won and cur_won:
        transitions["win_then_win"] += 1
    elif prev_won and not cur_won:
        transitions["win_then_loss"] += 1
    elif not prev_won and cur_won:
        transitions["loss_then_win"] += 1
    else:
        transitions["loss_then_loss"] += 1

result = {
    "meta": {
        "total_trades": sum(len(v) for v in markets.values()),
        "total_markets": len(markets),
        "confidently_resolved_markets": confident_count,
        "confident_pct": pct(confident_count, len(markets)),
        "trades_in_confidently_resolved_markets": overall["n"],
    },
    "overall": {
        **overall,
        "win_rate_pct": pct(overall["wins"], overall["n"]),
        "roi_pct": pct(overall["pnl"], overall["cost"]),
    },
    "by_asset": {
        a: {**v, "win_rate_pct": pct(v["wins"], v["n"]), "roi_pct": pct(v["pnl"], v["cost"])}
        for a, v in by_asset.items()
    },
    "by_asset_regime": {
        f"{a}|{r}": {**v, "win_rate_pct": pct(v["wins"], v["n"]), "roi_pct": pct(v["pnl"], v["cost"]),
                     "avg_price_vs_win_rate_edge": None}
        for (a, r), v in by_asset_regime.items()
    },
    "by_asset_position": {
        f"{a}|{p}": {**v, "win_rate_pct": pct(v["wins"], v["n"]), "roi_pct": pct(v["pnl"], v["cost"])}
        for (a, p), v in by_asset_position.items()
    },
    "calibration_curve": {
        str(b): {**v, "win_rate_pct": pct(v["wins"], v["n"]), "edge_pct": round(pct(v["wins"], v["n"]) - b*100, 2) if v["n"] else None}
        for b, v in sorted(price_bucket.items())
    },
    "side_persistence_consistency": {
        k: {**v, "win_rate_pct": pct(v["wins"], v["n"])} for k, v in by_side_consistency.items()
    },
    "floor_lot_vs_normal": {
        k: {**v, "win_rate_pct": pct(v["wins"], v["n"]), "roi_pct": pct(v["pnl"], v["cost"])}
        for k, v in floor_lot_stats.items()
    },
    "gradient_alignment": {
        f"{r}|{a}": {**v, "win_rate_pct": pct(v["wins"], v["n"])} for (r, a), v in gradient_alignment.items()
    },
    "by_hour_utc": {
        str(h): {**v, "win_rate_pct": pct(v["wins"], v["n"])} for h, v in sorted(by_hour.items())
    },
    "by_day_of_week": {
        str(d): {**v, "win_rate_pct": pct(v["wins"], v["n"])} for d, v in sorted(by_dow.items())
    },
    "entries_per_market": {
        k: {**v, "avg_pnl_per_market": round(v["total_pnl"]/v["n_markets"], 4) if v["n_markets"] else None,
            "roi_pct": pct(v["total_pnl"], v["total_cost"])}
        for k, v in entries_per_market_bucket.items()
    },
    "streak_transitions": transitions,
    "dollar_weighted_win_rate_pct": pct(dollar_weighted["cost_on_wins"], dollar_weighted["cost"]),
    "by_regime_consistency": {
        f"{r}|{c}": {**v, "win_rate_pct": pct(v["wins"], v["n"]), "roi_pct": pct(v["pnl"], v["cost"])}
        for (r, c), v in by_regime_consistency.items()
    },
    "per_market_net_result": {
        "markets_evaluated": markets_net_total,
        "net_positive": markets_net_positive,
        "net_negative": markets_net_negative,
        "net_zero": markets_net_zero,
        "pct_net_negative": pct(markets_net_negative, markets_net_total),
        "pct_net_positive": pct(markets_net_positive, markets_net_total),
        "by_asset": {
            a: {**v, "pct_net_negative": pct(v["neg"], v["pos"] + v["neg"] + v["zero"])}
            for a, v in by_asset_market_net.items()
        },
    },
}

with open("/private/tmp/claude-501/-Users-deepak-polymarket-bot-ai/27fbe0bc-4ef4-4454-88ee-e76f7d99a013/scratchpad/deep_analysis_output.json", "w") as f:
    json.dump(result, f, indent=2, default=str)

print("\n" + "=" * 70)
print("OVERALL")
print("=" * 70)
print(json.dumps(result["overall"], indent=2))
print(f"\nCoverage: {result['meta']['confident_pct']}% of markets confidently resolved "
      f"({result['meta']['trades_in_confidently_resolved_markets']} trades analyzed)")

print("\n" + "=" * 70)
print("BY ASSET")
print("=" * 70)
for a, v in sorted(result["by_asset"].items(), key=lambda x: -x[1]["n"]):
    print(f"  {a:12s} n={v['n']:7d} win_rate={v['win_rate_pct']:6.2f}%  "
          f"cost=${v['cost']:>12,.2f}  pnl=${v['pnl']:>12,.2f}  roi={v['roi_pct']:>7.2f}%")

print("\n" + "=" * 70)
print("BY (ASSET, REGIME)")
print("=" * 70)
for k, v in sorted(result["by_asset_regime"].items()):
    print(f"  {k:24s} n={v['n']:7d} win_rate={v['win_rate_pct']:6.2f}%  pnl=${v['pnl']:>12,.2f}  roi={v['roi_pct']:>8.2f}%")

print("\n" + "=" * 70)
print("BY (ASSET, POSITION)")
print("=" * 70)
for k, v in sorted(result["by_asset_position"].items()):
    print(f"  {k:24s} n={v['n']:7d} win_rate={v['win_rate_pct']:6.2f}%  pnl=${v['pnl']:>12,.2f}  roi={v['roi_pct']:>8.2f}%")

print("\n" + "=" * 70)
print("CALIBRATION CURVE (price bucket -> empirical win rate; edge = win_rate% - price%)")
print("=" * 70)
for b, v in sorted(result["calibration_curve"].items(), key=lambda x: float(x[0])):
    if v["n"] < 50:
        continue
    print(f"  price~{float(b):.2f}  n={v['n']:7d}  win_rate={v['win_rate_pct']:6.2f}%  edge={v['edge_pct']:>+7.2f}pp")

print("\n" + "=" * 70)
print("SIDE PERSISTENCE: consistent vs switch (RAW, not regime-controlled)")
print("=" * 70)
print(json.dumps(result["side_persistence_consistency"], indent=2))
print(f"\nDollar-weighted win rate (of $ wagered, % on ultimately-winning trades): "
      f"{result['dollar_weighted_win_rate_pct']}%")

print("\n" + "=" * 70)
print("SIDE PERSISTENCE CONTROLLED FOR REGIME (the real test)")
print("=" * 70)
for k, v in sorted(result["by_regime_consistency"].items()):
    print(f"  {k:20s} n={v['n']:7d}  win_rate={v['win_rate_pct']:6.2f}%  pnl=${v['pnl']:>10,.2f}  roi={v['roi_pct']:>7.2f}%")

print("\n" + "=" * 70)
print("PER-MARKET NET RESULT (net pnl across all the trader's entries in one market)")
print("=" * 70)
pm = result["per_market_net_result"]
print(f"markets evaluated: {pm['markets_evaluated']}")
print(f"net negative: {pm['net_negative']} ({pm['pct_net_negative']}%)")
print(f"net positive: {pm['net_positive']} ({pm['pct_net_positive']}%)")
print(f"net zero: {pm['net_zero']}")
print("by asset:")
for a, v in sorted(pm["by_asset"].items()):
    print(f"  {a:12s} pct_net_negative={v['pct_net_negative']}%  (pos={v['pos']} neg={v['neg']} zero={v['zero']})")

print("\n" + "=" * 70)
print("FLOOR-LOT vs NORMAL")
print("=" * 70)
print(json.dumps(result["floor_lot_vs_normal"], indent=2))

print("\n" + "=" * 70)
print("GRADIENT ALIGNMENT (CHEAP-after-drop / HIGH-after-rise vs not)")
print("=" * 70)
print(json.dumps(result["gradient_alignment"], indent=2))

print("\n" + "=" * 70)
print("ENTRIES PER MARKET vs PROFITABILITY")
print("=" * 70)
for k, v in sorted(result["entries_per_market"].items()):
    print(f"  {k:6s} n_markets={v['n_markets']:6d}  avg_pnl/market=${v['avg_pnl_per_market']:>8.3f}  roi={v['roi_pct']:>7.2f}%")

print("\n" + "=" * 70)
print("STREAK TRANSITIONS (global chronological order)")
print("=" * 70)
t = result["streak_transitions"]
p_win_after_win = pct(t["win_then_win"], t["win_then_win"] + t["win_then_loss"])
p_win_after_loss = pct(t["loss_then_win"], t["loss_then_win"] + t["loss_then_loss"])
print(json.dumps(t, indent=2))
print(f"P(win | previous won)  = {p_win_after_win}%")
print(f"P(win | previous lost) = {p_win_after_loss}%")

print("\n" + "=" * 70)
print("BY HOUR (UTC)")
print("=" * 70)
for h, v in sorted(result["by_hour_utc"].items(), key=lambda x: int(x[0])):
    print(f"  hour={h:>2s}  n={v['n']:6d}  win_rate={v['win_rate_pct']:6.2f}%  pnl=${v['pnl']:>10,.2f}")

print("\nDone.")
