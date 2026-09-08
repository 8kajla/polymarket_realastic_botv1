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
    pnl: float                # payout - entry_cost
    settled_at: float
    is_floor_lot: bool
    is_hedge: bool = False


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
        entry_cost = order.filled_size * order.price
        payout = order.filled_size * 1.0 if won else 0.0
        pnl = payout - entry_cost

        record = SettlementRecord(
            order_id=order.order_id,
            condition_id=order.condition_id,
            asset=order.asset,
            regime=order.regime,
            side=order.side,
            entry_price=order.price,
            filled_size=order.filled_size,
            entry_cost=entry_cost,
            winning_side=winning_side,
            won=won,
            payout=payout,
            pnl=pnl,
            settled_at=now,
            is_floor_lot=order.is_floor_lot,
            is_hedge=order.is_hedge,
        )
        self.records.append(record)
        self._settled_order_ids.add(order.order_id)
        logger.info(
            "SETTLE order=%d asset=%s side=%s won=%s entry_cost=%.4f payout=%.4f pnl=%+.4f",
            order.order_id, order.asset, order.side, won, entry_cost, payout, pnl,
        )
        return record

    def realized_pnl(self) -> float:
        """Recomputed fresh from the settlement log every call -- see
        module docstring. Never mutate a running total instead of this."""
        return sum(r.pnl for r in self.records)

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
