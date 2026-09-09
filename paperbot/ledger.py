"""
Paper positions, settlement, and realized P&L.

HARD REQUIREMENT (per project spec, backed by a real prior accounting-drift
bug): realized P&L is NEVER a running counter that gets incremented in
multiple places. It is always recomputed FROM the settlement log, every
time it's needed. There is no `self.realized_pnl` field anywhere in this
class -- only a method that sums the records.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import config
from .fill_simulation import OrderStatus, SimulatedOrder

logger = logging.getLogger("paperbot.ledger")


@dataclass(frozen=True)
class SettlementRecord:
    order_id: int
    condition_id: str
    asset: str
    regime: str
    side: str               # "Up" or "Down" -- the side we held
    entry_price: float
    filled_size: float
    entry_cost: float        # filled_size * entry_price
    winning_side: str
    won: bool
    payout: float            # filled_size * 1.0 if won else 0.0
    pnl: float                # payout - entry_cost -- DIRECTIONAL only,
                              # deliberately excludes rebate_usd (see its
                              # docstring below) so every earlier comparison
                              # this project has ever made against this
                              # field stays meaningful and comparable.
    settled_at: float
    is_floor_lot: bool
    is_hedge: bool = False
    is_scout: bool = False
    # Maker rebate earned on this order's fills -- see
    # config.maker_rebate_usd's docstring. Real, additive income (this bot
    # only ever places postOnly/maker orders), but kept as a SEPARATE field
    # rather than folded into `pnl` above: every prior analysis this
    # project has done (and everything in bot_vs_trader_history.jsonl)
    # compares against directional pnl alone, and silently redefining it
    # would make every one of those comparisons wrong retroactively. Use
    # pnl_with_rebate() for the real, total economic result.
    rebate_usd: float = 0.0

    def pnl_with_rebate(self) -> float:
        return self.pnl + self.rebate_usd


class Ledger:
    """
    Append-only settlement log. `realized_pnl()` sums `record.pnl` across
    every record on every call -- it is intentionally never cached or
    mutated incrementally. Persist/reload round-trips through the same
    recompute path, so there is no stale cached total to drift.
    """

    def __init__(self):
        self.records: list[SettlementRecord] = []
        self._settled_order_ids: set[int] = set()

    def settle_order(self, order: SimulatedOrder, winning_side: str,
                      now: Optional[float] = None) -> Optional[SettlementRecord]:
        """
        Settle one order's FILLED portion against the market's resolved
        winning side. Only orders with an actual fill (status FILLED, or
        PARTIALLY_FILLED with a cancelled remainder after the timing
        cutoff) produce a settlement record -- an order that never filled
        at all contributes nothing, matching "only settle FILLED orders".
        Idempotent: calling this twice for the same order_id is a no-op
        the second time (no double-counted P&L).
        """
        if order.order_id in self._settled_order_ids:
            return None
        if order.filled_size <= 0:
            return None
        if order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            return None

        now = now if now is not None else time.time()
        won = order.side == winning_side
        # CONFIRMED LIVE (2026-09-08): order.filled_size * order.price is
        # WRONG whenever an order reprices between fills -- order.price is
        # the order's CURRENT (final) resting price, but filled_size sums
        # shares that may have filled at DIFFERENT prices before a
        # drift-reprice changed it (each Fill already records its own
        # actual price; order.price does not retroactively apply to
        # earlier fills). Measured live: 144 of 2,577 completed orders in
        # one ~8h window (5.6%) had fills spanning at least one reprice --
        # a real, non-negligible fraction, not a rare edge case. True cost
        # is the sum of each fill's own size*price, which this project's
        # existing convention (weighted-average entry_price, matching
        # ENTRY_SIZING_USD's own "size * price" invariant used throughout
        # every analysis script this session) requires getting right.
        entry_cost = sum(f.size * f.price for f in order.fills)
        entry_price = entry_cost / order.filled_size if order.filled_size else order.price
        payout = order.filled_size * 1.0 if won else 0.0
        pnl = payout - entry_cost
        # CONFIRMED 2026-09-09 (see config.maker_rebate_usd's docstring):
        # every order this bot places is postOnly -- it never crosses as a
        # taker, only ever rests as a maker -- so every fill earns a real
        # rebate that was never modeled before now. Summed per-fill (each
        # Fill has its own price, same reasoning as entry_cost above: the
        # rebate formula is price-dependent, and order.price is only the
        # CURRENT resting price, not what earlier fills actually cleared at).
        rebate_usd = sum(config.maker_rebate_usd(f.size, f.price) for f in order.fills)

        record = SettlementRecord(
            order_id=order.order_id,
            condition_id=order.condition_id,
            asset=order.asset,
            regime=order.regime,
            side=order.side,
            entry_price=entry_price,
            filled_size=order.filled_size,
            entry_cost=entry_cost,
            winning_side=winning_side,
            won=won,
            payout=payout,
            pnl=pnl,
            settled_at=now,
            is_floor_lot=order.is_floor_lot,
            is_hedge=order.is_hedge,
            is_scout=order.is_scout,
            rebate_usd=rebate_usd,
        )
        self.records.append(record)
        self._settled_order_ids.add(order.order_id)
        logger.info(
            "SETTLE order=%d asset=%s side=%s won=%s entry_cost=%.4f payout=%.4f pnl=%+.4f rebate=%.4f",
            order.order_id, order.asset, order.side, won, entry_cost, payout, pnl, rebate_usd,
        )
        return record

    def realized_pnl(self) -> float:
        """Recomputed fresh from the settlement log every call -- see
        module docstring. Never mutate a running total instead of this.
        DIRECTIONAL only -- see realized_pnl_with_rebates() for the real,
        total economic result including maker rebates."""
        return sum(r.pnl for r in self.records)

    def realized_pnl_with_rebates(self) -> float:
        """The real total: directional pnl PLUS the maker rebate earned on
        every fill (this bot only ever places postOnly/maker orders -- see
        config.maker_rebate_usd's docstring). Recomputed fresh every call,
        same discipline as realized_pnl() above."""
        return sum(r.pnl_with_rebate() for r in self.records)

    def total_rebate_usd(self) -> float:
        return sum(r.rebate_usd for r in self.records)

    def is_settled(self, order_id: int) -> bool:
        """Whether this order_id has already produced a settlement record.
        Exposed so callers (PaperBot.available_cash()) don't need to reach
        into the private _settled_order_ids set directly."""
        return order_id in self._settled_order_ids

    def realized_pnl_by_asset(self) -> dict:
        out: dict[str, float] = {}
        for r in self.records:
            out[r.asset] = out.get(r.asset, 0.0) + r.pnl
        return out

    def realized_pnl_by_asset_regime(self) -> dict:
        out: dict[tuple, float] = {}
        for r in self.records:
            key = (r.asset, r.regime)
            out[key] = out.get(key, 0.0) + r.pnl
        return out

    def hedge_summary(self) -> dict:
        """Hedge vs. non-hedge settlement counts and P&L -- the live
        signal for whether the bot's simulated hedge legs are actually
        reproducing the confirmed historical property (dual-sided markets
        have a far better worst-case outcome than single-sided ones)."""
        hedge = [r for r in self.records if r.is_hedge]
        normal = [r for r in self.records if not r.is_hedge]
        return {
            "hedge_trades": len(hedge),
            "hedge_pnl": round(sum(r.pnl for r in hedge), 4),
            "normal_trades": len(normal),
            "normal_pnl": round(sum(r.pnl for r in normal), 4),
        }

    def scout_summary(self) -> dict:
        """Scout (small, tentative first-entry) vs. ordinary settlement
        counts and P&L -- see SCOUT_PROBABILITY/SCOUT_SIZE_RATIO's
        docstring in behavior_config.py. The live signal for whether this
        new tier is actually landing at the frequency/size it was
        calibrated for, same role hedge_summary() plays for hedges."""
        scout = [r for r in self.records if r.is_scout]
        normal = [r for r in self.records if not r.is_scout]
        return {
            "scout_trades": len(scout),
            "scout_pnl": round(sum(r.pnl for r in scout), 4),
            "normal_trades": len(normal),
            "normal_pnl": round(sum(r.pnl for r in normal), 4),
        }

    # -- persistence ---------------------------------------------------

    def save(self, path: Optional[Path] = None) -> None:
        path = path or config.LEDGER_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"records": [asdict(r) for r in self.records]}
        path.write_text(json.dumps(payload, indent=2))

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Ledger":
        path = path or config.LEDGER_PATH
        ledger = cls()
        if not path.exists():
            return ledger
        payload = json.loads(path.read_text())
        for raw in payload.get("records", []):
            record = SettlementRecord(**raw)
            ledger.records.append(record)
            ledger._settled_order_ids.add(record.order_id)
        return ledger
