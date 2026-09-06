import json

import pytest

from paperbot.fill_simulation import Fill, OrderStatus, SimulatedOrder
from paperbot.ledger import Ledger


def make_filled_order(order_id=1, asset="Bitcoin", regime="CHEAP", side="Up",
                       price=0.20, size=10.0, condition_id="cond-1",
                       is_floor_lot=False):
    order = SimulatedOrder(
        order_id=order_id, condition_id=condition_id, token_id="tok-up",
        asset=asset, regime=regime, position_tier="first", side=side,
        price=price, original_size=size, is_floor_lot=is_floor_lot,
        placed_at=0.0, remaining_size=0.0,
    )
    order.fills.append(Fill(size=size, price=price, ts=1.0))
    order.status = OrderStatus.FILLED
    order.first_fill_at = 1.0
    return order


class TestRealizedPnlRecompute:
    def test_pnl_recomputes_from_records_not_a_running_counter(self):
        ledger = Ledger()
        order = make_filled_order(price=0.20, size=10.0, side="Up")
        ledger.settle_order(order, winning_side="Up")

        expected_pnl = 10.0 * 1.0 - 10.0 * 0.20  # payout - entry_cost
        assert ledger.realized_pnl() == pytest.approx(expected_pnl)

        # Calling it again with no new settlements must not drift.
        assert ledger.realized_pnl() == pytest.approx(expected_pnl)
        assert ledger.realized_pnl() == pytest.approx(expected_pnl)

    def test_there_is_no_mutable_running_total_field(self):
        """Guards the architectural requirement directly: realized_pnl must
        be a method that derives its value, not a stored attribute that
        could be incremented from multiple places."""
        ledger = Ledger()
        assert not hasattr(ledger, "realized_pnl_total")
        assert not hasattr(ledger, "_realized_pnl")
        assert callable(ledger.realized_pnl)

    def test_losing_side_produces_negative_pnl(self):
        ledger = Ledger()
        order = make_filled_order(price=0.60, size=5.0, side="Up")
        ledger.settle_order(order, winning_side="Down")
        assert ledger.realized_pnl() == pytest.approx(0.0 - 5.0 * 0.60)

    def test_multiple_settlements_sum_correctly(self):
        ledger = Ledger()
        win = make_filled_order(order_id=1, price=0.30, size=10.0, side="Up")
        lose = make_filled_order(order_id=2, price=0.40, size=5.0, side="Down")
        ledger.settle_order(win, winning_side="Up")
        ledger.settle_order(lose, winning_side="Up")

        expected = (10.0 * 1.0 - 10.0 * 0.30) + (0.0 - 5.0 * 0.40)
        assert ledger.realized_pnl() == pytest.approx(expected)

    def test_settling_the_same_order_twice_does_not_double_count(self):
        ledger = Ledger()
        order = make_filled_order(price=0.20, size=10.0, side="Up")
        ledger.settle_order(order, winning_side="Up")
        first_pnl = ledger.realized_pnl()
        ledger.settle_order(order, winning_side="Up")  # duplicate call
        assert ledger.realized_pnl() == pytest.approx(first_pnl)
        assert len(ledger.records) == 1


class TestOnlyFilledOrdersSettle:
    def test_zero_fill_order_produces_no_settlement(self):
        ledger = Ledger()
        order = SimulatedOrder(
            order_id=99, condition_id="c", token_id="t", asset="Bitcoin",
            regime="CHEAP", position_tier="first", side="Up", price=0.2,
            original_size=5.0, is_floor_lot=False, placed_at=0.0,
        )
        order.status = OrderStatus.EXPIRED_UNFILLED
        record = ledger.settle_order(order, winning_side="Up")
        assert record is None
        assert ledger.realized_pnl() == 0.0

    def test_partially_filled_expired_order_still_settles_its_fill(self):
        ledger = Ledger()
        order = SimulatedOrder(
            order_id=5, condition_id="c", token_id="t", asset="Bitcoin",
            regime="CHEAP", position_tier="first", side="Up", price=0.2,
            original_size=5.0, is_floor_lot=False, placed_at=0.0,
            remaining_size=0.0,
        )
        order.fills.append(Fill(size=2.0, price=0.2, ts=1.0))
        order.status = OrderStatus.PARTIALLY_FILLED
        order.expired_remainder = True

        record = ledger.settle_order(order, winning_side="Up")
        assert record is not None
        assert record.filled_size == pytest.approx(2.0)
        assert ledger.realized_pnl() == pytest.approx(2.0 * 1.0 - 2.0 * 0.2)


class TestPersistence:
    def test_save_and_load_round_trips_pnl_exactly(self, tmp_path):
        path = tmp_path / "ledger.json"
        ledger = Ledger()
        order = make_filled_order(price=0.25, size=4.0, side="Down")
        ledger.settle_order(order, winning_side="Down")
        pnl_before = ledger.realized_pnl()
        ledger.save(path)

        reloaded = Ledger.load(path)
        assert reloaded.realized_pnl() == pytest.approx(pnl_before)
        assert len(reloaded.records) == 1

        data = json.loads(path.read_text())
        assert "records" in data
        # No cached total is persisted -- it's always recomputed on load.
        assert "realized_pnl" not in data

    def test_load_missing_file_returns_empty_ledger(self, tmp_path):
        ledger = Ledger.load(tmp_path / "does_not_exist.json")
        assert ledger.records == []
        assert ledger.realized_pnl() == 0.0
