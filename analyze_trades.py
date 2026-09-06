#!/usr/bin/env python3
"""
Behavioral analysis of a Polymarket trader's full trade history.

Streams the JSONL file once, grouping trades by market (slug) into
in-memory lists (small relative to the full file), then computes
per-asset / per-(asset,regime) statistics:

  1. Regime distribution (count + $ volume) per asset per regime
  2. Entry-count sizing: median notional by position-in-market
     (first / 2nd-3rd / 4th+) per asset per regime
  3. Side persistence: % of consecutive same-market trades that repeat
     the prior trade's outcome (Up/Down), per asset
  4. Weakness/strength gradient: rising/falling/flat price deltas
     between consecutive same-market trades, per asset per regime
  5. Intertrade intervals (median/p75/p90) per asset
  6. Daily volume anomaly flags (>2x the dataset's average daily volume)

Also explicitly checks the previously-flagged BNB degenerate-median
signature and reports the actual root cause found in the data.
"""
import json
import sys
import statistics
from collections import defaultdict
from datetime import datetime, timezone

INPUT_PATH = sys.argv[1] if len(sys.argv) > 1 else "trade.jsonl"
LINE_LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else None  # for test slices

REGIME_BANDS = [
    ("CHEAP", 0.00, 0.30),
    ("MID", 0.30, 0.70),
    ("CORE", 0.70, 0.90),
    ("HIGH", 0.90, 1.0001),  # inclusive of 1.0
]


def classify_regime(price):
    for name, lo, hi in REGIME_BANDS:
        if lo <= price < hi:
            return name
    return "HIGH"  # price == 1.0 edge case


def asset_from_title(title):
    if " Up or Down" in title:
        return title.split(" Up or Down")[0]
    return None


def pct(n, d):
    return round(100.0 * n / d, 2) if d else None


def median(vals):
    return round(statistics.median(vals), 6) if vals else None


def quantile(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    idx = q * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return round(s[lo] + (s[hi] - s[lo]) * frac, 2)


def main():
    markets = defaultdict(list)  # slug -> list of trade dicts (in market order not yet sorted)
    daily_volume = defaultdict(float)
    asset_totals = defaultdict(lambda: {"count": 0, "volume": 0.0})
    unknown_asset_count = 0
    total_lines = 0

    with open(INPUT_PATH, "r") as f:
        for i, line in enumerate(f):
            if LINE_LIMIT and i >= LINE_LIMIT:
                break
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            total_lines += 1

            title = d.get("title", "") or ""
            asset = asset_from_title(title)
            if asset is None:
                unknown_asset_count += 1
                continue

            price = d.get("price")
            usdc = d.get("usdcSize")
            size = d.get("size")
            ts = d.get("timestamp")
            outcome = d.get("outcome")
            slug = d.get("slug")

            asset_totals[asset]["count"] += 1
            asset_totals[asset]["volume"] += usdc or 0.0

            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            daily_volume[day] += usdc or 0.0

            markets[slug].append({
                "asset": asset,
                "ts": ts,
                "price": price,
                "usdc": usdc,
                "size": size,
                "outcome": outcome,
            })

    print(f"[info] read {total_lines} lines, {len(markets)} distinct markets, "
          f"{unknown_asset_count} rows with unparseable title/asset", file=sys.stderr)

    # Per-asset accumulators
    regime_dist = defaultdict(lambda: defaultdict(lambda: {"count": 0, "volume": 0.0}))
    entry_sizing = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))  # asset -> regime -> position -> [usdc]
    entry_sizing_ex_floor = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))  # same, excluding size==0.02 floor-lot trades
    side_persist_same = defaultdict(int)
    side_persist_total = defaultdict(int)
    gradient = defaultdict(lambda: defaultdict(lambda: {"rising": 0, "falling": 0, "flat": 0}))
    intervals = defaultdict(list)  # asset -> [seconds]

    # raw-row samples for anomaly investigation: asset -> regime -> position -> list of raw rows
    raw_samples = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    # full-population min-lot-size (size==0.02) counters for root-cause checks
    min_lot_counts = defaultdict(lambda: defaultdict(int))  # asset -> regime -> count
    regime_combo_n = defaultdict(lambda: defaultdict(int))  # asset -> regime -> total trades

    for slug, trades in markets.items():
        trades.sort(key=lambda t: t["ts"])
        asset = trades[0]["asset"]

        for idx, t in enumerate(trades):
            price = t["price"]
            usdc = t["usdc"]
            regime = classify_regime(price)

            # 1. regime distribution
            regime_dist[asset][regime]["count"] += 1
            regime_dist[asset][regime]["volume"] += usdc or 0.0

            # 2. entry-count sizing position bucket
            if idx == 0:
                pos = "first"
            elif idx <= 2:
                pos = "2nd-3rd"
            else:
                pos = "4th+"
            entry_sizing[asset][regime][pos].append(usdc)
            if t["size"] != 0.02:
                entry_sizing_ex_floor[asset][regime][pos].append(usdc)
            if len(raw_samples[asset][regime][pos]) < 20:
                raw_samples[asset][regime][pos].append(t)
            regime_combo_n[asset][regime] += 1
            if t["size"] == 0.02:
                min_lot_counts[asset][regime] += 1

            if idx > 0:
                prev = trades[idx - 1]
                # 3. side persistence
                side_persist_total[asset] += 1
                if t["outcome"] == prev["outcome"]:
                    side_persist_same[asset] += 1

                # 4. weakness/strength gradient (bucket by CURRENT trade's regime)
                delta = price - prev["price"]
                if delta > 0.005:
                    gradient[asset][regime]["rising"] += 1
                elif delta < -0.005:
                    gradient[asset][regime]["falling"] += 1
                else:
                    gradient[asset][regime]["flat"] += 1

                # 5. intertrade interval
                gap = t["ts"] - prev["ts"]
                intervals[asset].append(gap)

    # ---- Data-quality check: BNB-style degenerate median signature ----
    # Flags a (asset, regime) combo where the position-tier medians (first /
    # 2nd-3rd / 4th+) are all suspiciously small AND cluster within a tight
    # band of each other despite very different sample sizes -- the signature
    # is "medians stop moving with position" at near-floor dollar amounts,
    # which erases any real first-vs-4th+ sizing signal. Uses an approximate
    # match (not exact equality) since floor-lot notional varies slightly with
    # price within a regime band.
    dq_flags = []
    DEGENERATE_ABS_THRESHOLD = 0.05  # medians below this are "floor-lot" territory
    for asset, regimes in entry_sizing.items():
        for regime, positions in regimes.items():
            medians_seen = {}
            for pos, vals in positions.items():
                m = median(vals)
                medians_seen[pos] = (m, len(vals))
            vals_only = [v[0] for v in medians_seen.values() if v[0] is not None]
            ns = [v[1] for v in medians_seen.values()]
            if len(vals_only) < 2 or max(ns) <= 500:
                continue
            all_tiny = all(v < DEGENERATE_ABS_THRESHOLD for v in vals_only)
            tight_band = (max(vals_only) - min(vals_only)) <= 0.001 or \
                         (min(vals_only) > 0 and max(vals_only) / min(vals_only) <= 1.3)
            if all_tiny and tight_band and max(ns) / max(1, min(ns)) > 1.5:
                dq_flags.append({
                    "asset": asset,
                    "regime": regime,
                    "medians_by_position": medians_seen,
                    "note": "position-tier medians cluster at near-floor-lot dollar amounts "
                            "despite very different n -- position-based sizing signal is masked"
                })

    # ---- Investigate flagged combos: sample raw rows + root cause check ----
    dq_investigation = []
    for flag in dq_flags:
        asset, regime = flag["asset"], flag["regime"]
        samples = []
        for pos in ("first", "2nd-3rd", "4th+"):
            for row in raw_samples[asset][regime][pos][:20]:
                samples.append({"position": pos, **row})

        # root-cause check #1: is usdcSize actually missing/null for a chunk of rows
        # (the originally-suspected bug)?
        null_usdc_n = sum(
            1 for pos_vals in entry_sizing[asset][regime].values()
            for v in pos_vals if v is None
        )
        total_n = sum(len(v) for v in entry_sizing[asset][regime].values())

        # root-cause check #2: are these medians dominated by exchange-minimum-lot
        # trades (size == 0.02 shares), which legitimately produce tiny usdcSize
        # at low prices? Measured over the FULL population for this (asset, regime).
        combo_n = regime_combo_n[asset][regime]
        min_lot_n_full = min_lot_counts[asset][regime]

        dq_investigation.append({
            "asset": asset,
            "regime": regime,
            "total_n_in_combo": total_n,
            "null_or_missing_usdcSize_count": null_usdc_n,
            "min_lot_size_0.02_count_full_population": min_lot_n_full,
            "min_lot_size_0.02_fraction_full_population": pct(min_lot_n_full, combo_n),
            "sample_rows": samples[:20],
        })

    # ---- Daily volume anomaly ----
    total_volume = sum(daily_volume.values())
    n_days = len(daily_volume)
    avg_daily = total_volume / n_days if n_days else 0
    flagged_days = []
    for day, vol in sorted(daily_volume.items()):
        if avg_daily and vol > 2 * avg_daily:
            flagged_days.append({"date": day, "volume": round(vol, 2), "ratio_to_avg": round(vol / avg_daily, 2)})

    # ---- Assemble per-asset output ----
    assets_out = {}
    for asset in asset_totals:
        total_count = asset_totals[asset]["count"]
        total_vol = asset_totals[asset]["volume"]

        rd_out = {}
        for regime, _, _ in REGIME_BANDS:
            rc = regime_dist[asset][regime]["count"]
            rv = regime_dist[asset][regime]["volume"]
            rd_out[regime] = {
                "count": rc,
                "count_pct": pct(rc, total_count),
                "volume": round(rv, 2),
                "volume_pct": pct(rv, total_vol),
            }

        es_out = {}
        for regime, _, _ in REGIME_BANDS:
            positions = entry_sizing[asset][regime]
            pos_out = {}
            for pos in ("first", "2nd-3rd", "4th+"):
                vals = positions.get(pos, [])
                pos_out[pos] = {"n": len(vals), "median_usdc": median(vals)}

            def compute_direction(pos_out_dict):
                m_first = pos_out_dict["first"]["median_usdc"]
                m_last = pos_out_dict["4th+"]["median_usdc"]
                if m_first is None or m_last is None:
                    return "insufficient data"
                if m_last > m_first * 1.05:
                    return "increasing (4th+ > first)"
                elif m_last < m_first * 0.95:
                    return "decreasing (4th+ < first)"
                return "flat"

            direction_raw = compute_direction(pos_out)

            # Floor-lot-excluded view: strips out size==0.02 exchange-minimum
            # "dust" trades, which for high-floor-lot assets/regimes (BNB,
            # Hyperliquid) dominate the raw median and mask real sizing
            # behavior. See data_quality_flags for the detection.
            ex_floor_positions = entry_sizing_ex_floor[asset][regime]
            ex_pos_out = {}
            for pos in ("first", "2nd-3rd", "4th+"):
                vals = ex_floor_positions.get(pos, [])
                ex_pos_out[pos] = {"n": len(vals), "median_usdc": median(vals)}
            direction_ex_floor = compute_direction(ex_pos_out)

            floor_lot_n = min_lot_counts[asset][regime]
            combo_n = regime_combo_n[asset][regime]
            floor_lot_frac = pct(floor_lot_n, combo_n)

            es_out[regime] = {
                "positions": pos_out,
                "direction_first_to_4th+": direction_raw,
                "floor_lot_fraction_pct": floor_lot_frac,
                "positions_excluding_floor_lot": ex_pos_out,
                "direction_first_to_4th+_excluding_floor_lot": direction_ex_floor,
            }

        grad_out = {}
        for regime, _, _ in REGIME_BANDS:
            g = gradient[asset][regime]
            tot = g["rising"] + g["falling"] + g["flat"]
            grad_out[regime] = {
                "n": tot,
                "rising_pct": pct(g["rising"], tot),
                "falling_pct": pct(g["falling"], tot),
                "flat_pct": pct(g["flat"], tot),
            }

        iv = intervals[asset]
        interval_out = {
            "n": len(iv),
            "median_seconds": quantile(iv, 0.5),
            "p75_seconds": quantile(iv, 0.75),
            "p90_seconds": quantile(iv, 0.90),
        }

        sp_total = side_persist_total[asset]
        sp_same = side_persist_same[asset]

        assets_out[asset] = {
            "trade_count": total_count,
            "dollar_volume": round(total_vol, 2),
            "regime_distribution": rd_out,
            "entry_count_sizing": es_out,
            "side_persistence_pct": pct(sp_same, sp_total),
            "side_persistence_n": sp_total,
            "weakness_strength_gradient": grad_out,
            "intertrade_interval_seconds": interval_out,
        }

    result = {
        "meta": {
            "input_file": INPUT_PATH,
            "total_lines_read": total_lines,
            "distinct_markets": len(markets),
            "unparseable_asset_rows": unknown_asset_count,
            "regime_bands": {name: [lo, hi] for name, lo, hi in REGIME_BANDS},
        },
        "assets": assets_out,
        "daily_volume": {
            "n_days": n_days,
            "average_daily_volume": round(avg_daily, 2),
            "total_volume": round(total_volume, 2),
            "flagged_days_gt_2x_avg": flagged_days,
        },
        "data_quality_flags": {
            "degenerate_median_signature_detected": dq_flags,
            "investigation_samples": dq_investigation,
        },
    }

    return result


if __name__ == "__main__":
    result = main()
    out_path = sys.argv[3] if len(sys.argv) > 3 else "analysis_output.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    # ---- console summary ----
    print("\n" + "=" * 70)
    print(f"TOTAL LINES: {result['meta']['total_lines_read']}  "
          f"MARKETS: {result['meta']['distinct_markets']}  "
          f"UNPARSEABLE: {result['meta']['unparseable_asset_rows']}")
    print("=" * 70)

    for asset, a in sorted(result["assets"].items(), key=lambda x: -x[1]["trade_count"]):
        print(f"\n--- {asset} --- trades={a['trade_count']} volume=${a['dollar_volume']:,.2f} "
              f"side_persistence={a['side_persistence_pct']}% (n={a['side_persistence_n']})")
        print("  Regime dist (count% / vol%):")
        for regime in ("CHEAP", "MID", "CORE", "HIGH"):
            rd = a["regime_distribution"][regime]
            print(f"    {regime:6s} count={rd['count']:7d} ({rd['count_pct']}%)  "
                  f"vol=${rd['volume']:>12,.2f} ({rd['volume_pct']}%)")
        print("  Entry-count sizing (median $ notional) & direction first->4th+:")
        for regime in ("CHEAP", "MID", "CORE", "HIGH"):
            es = a["entry_count_sizing"][regime]
            p = es["positions"]
            print(f"    {regime:6s} first(n={p['first']['n']})=${p['first']['median_usdc']}  "
                  f"2nd-3rd(n={p['2nd-3rd']['n']})=${p['2nd-3rd']['median_usdc']}  "
                  f"4th+(n={p['4th+']['n']})=${p['4th+']['median_usdc']}   => {es['direction_first_to_4th+']}"
                  f"   [floor-lot={es['floor_lot_fraction_pct']}%]")
            if es["floor_lot_fraction_pct"] and es["floor_lot_fraction_pct"] > 20:
                ep = es["positions_excluding_floor_lot"]
                print(f"           ex-floor-lot: first(n={ep['first']['n']})=${ep['first']['median_usdc']}  "
                      f"2nd-3rd(n={ep['2nd-3rd']['n']})=${ep['2nd-3rd']['median_usdc']}  "
                      f"4th+(n={ep['4th+']['n']})=${ep['4th+']['median_usdc']}   => {es['direction_first_to_4th+_excluding_floor_lot']}")
        print("  Weakness/strength gradient (rising/falling/flat %):")
        for regime in ("CHEAP", "MID", "CORE", "HIGH"):
            g = a["weakness_strength_gradient"][regime]
            print(f"    {regime:6s} n={g['n']:7d}  rising={g['rising_pct']}%  falling={g['falling_pct']}%  flat={g['flat_pct']}%")
        iv = a["intertrade_interval_seconds"]
        print(f"  Intertrade interval (sec): median={iv['median_seconds']}  p75={iv['p75_seconds']}  p90={iv['p90_seconds']}  (n={iv['n']})")

    print("\n" + "=" * 70)
    print("DAILY VOLUME")
    dv = result["daily_volume"]
    print(f"  {dv['n_days']} days, avg/day=${dv['average_daily_volume']:,.2f}, total=${dv['total_volume']:,.2f}")
    if dv["flagged_days_gt_2x_avg"]:
        print("  FLAGGED DAYS (>2x average):")
        for fd in dv["flagged_days_gt_2x_avg"]:
            print(f"    {fd['date']}: ${fd['volume']:,.2f} ({fd['ratio_to_avg']}x avg)")
    else:
        print("  No days exceed 2x the average daily volume.")

    print("\n" + "=" * 70)
    print("DATA QUALITY FLAGS (degenerate-median signature)")
    flags = result["data_quality_flags"]["degenerate_median_signature_detected"]
    if flags:
        for f in flags:
            print(f"  FLAGGED: {f['asset']} / {f['regime']}: {f['medians_by_position']}")
        print("\nROOT-CAUSE INVESTIGATION:")
        for inv in result["data_quality_flags"]["investigation_samples"]:
            print(f"  {inv['asset']} / {inv['regime']}: n={inv['total_n_in_combo']}  "
                  f"null_usdcSize={inv['null_or_missing_usdcSize_count']}  "
                  f"size==0.02(min-lot) fraction={inv['min_lot_size_0.02_fraction_full_population']}% "
                  f"(count={inv['min_lot_size_0.02_count_full_population']})")
            print("    sample raw rows:")
            for row in inv["sample_rows"][:20]:
                print(f"      {row}")
    else:
        print("  None detected by the automated identical-median heuristic.")
    print(f"\nWrote full JSON to {out_path}")
