import json

import pytest

from paperbot.fill_simulation import Fill, OrderStatus, SimulatedOrder
from paperbot.ledger import Ledger


def make_filled_order(order_id=1, asset="Bitcoin", regime="CHEAP", side="Up",
                       price=0.20, size=10.0, condition_id="cond-1",
                       is_floor_lot=False, is_hedge=False, is_scout=False):
    order = SimulatedOrder(
        order_id=order_id, condition_id=condition_id, token_id="tok-up",
        asset=asset, regime=regime, position_tier="first", side=side,
        price=price, original_size=size, is_floor_lot=is_floor_lot,
        is_hedge=is_hedge, is_scout=is_scout, placed_at=0.0, remaining_size=0.0,
    )
    order.fills.append(Fill(size=size, price=price, ts=1.0))
    order.status = OrderStatus.FILLED
    order.first_fill_at = 1.0
    return order


class TestHedgeFlagPropagation:
    def test_is_hedge_flows_through_to_the_settlement_record(self):
        ledger = Ledger()
        order = make_filled_order(price=0.15, size=10.0, side="Down", is_hedge=True)
        record = ledger.settle_order(order, winning_side="Down")
        assert record.is_hedge is True

    def test_non_hedge_order_settles_with_is_hedge_false(self):
        ledger = Ledger()
        order = make_filled_order(price=0.15, size=10.0, side="Down", is_hedge=False)
        record = ledger.settle_order(order, winning_side="Down")
        assert record.is_hedge is False

    def test_persists_and_reloads_correctly(self, tmp_path):
        path = tmp_path / "ledger.json"
        ledger = Ledger()
        order = make_filled_order(price=0.15, size=10.0, side="Down", is_hedge=True)
        ledger.settle_order(order, winning_side="Down")
        ledger.save(path)

        reloaded = Ledger.load(path)
        assert reloaded.records[0].is_hedge is True

    def test_hedge_summary_separates_hedge_from_normal_pnl(self):
        ledger = Ledger()
        hedge_order = make_filled_order(order_id=1, price=0.10, size=5.0, side="Down",
                                         condition_id="c1", is_hedge=True)
        normal_order = make_filled_order(order_id=2, price=0.60, size=5.0, side="Up",
                                          condition_id="c2", is_hedge=False)
        ledger.settle_order(hedge_order, winning_side="Down")   # hedge wins
        ledger.settle_order(normal_order, winning_side="Down")  # normal loses

        summary = ledger.hedge_summary()
        assert summary["hedge_trades"] == 1
        assert summary["normal_trades"] == 1
        assert summary["hedge_pnl"] == pytest.approx(5.0 * 1.0 - 5.0 * 0.10)
        assert summary["normal_pnl"] == pytest.approx(0.0 - 5.0 * 0.60)


class TestScoutFlagPropagation:
    def test_is_scout_flows_through_to_the_settlement_record(self):
        ledger = Ledger()
        order = make_filled_order(price=0.15, size=10.0, side="Down", is_scout=True)
        record = ledger.settle_order(order, winning_side="Down")
        assert record.is_scout is True

    def test_non_scout_order_settles_with_is_scout_false(self):
        ledger = Ledger()
        order = make_filled_order(price=0.15, size=10.0, side="Down", is_scout=False)
        record = ledger.settle_order(order, winning_side="Down")
        assert record.is_scout is False

    def test_persists_and_reloads_correctly(self, tmp_path):
        path = tmp_path / "ledger.json"
        ledger = Ledger()
        order = make_filled_order(price=0.15, size=10.0, side="Down", is_scout=True)
        ledger.settle_order(order, winning_side="Down")
        ledger.save(path)

        reloaded = Ledger.load(path)
        assert reloaded.records[0].is_scout is True

    def test_scout_summary_separates_scout_from_normal_pnl(self):
        ledger = Ledger()
        scout_order = make_filled_order(order_id=1, price=0.10, size=5.0, side="Down",
                                         condition_id="c1", is_scout=True)
        normal_order = make_filled_order(order_id=2, price=0.60, size=5.0, side="Up",
                                          condition_id="c2", is_scout=False)
        ledger.settle_order(scout_order, winning_side="Down")   # scout wins
        ledger.settle_order(normal_order, winning_side="Down")  # normal loses

        summary = ledger.scout_summary()
        assert summary["scout_trades"] == 1
        assert summary["normal_trades"] == 1
        assert summary["scout_pnl"] == pytest.approx(5.0 * 1.0 - 5.0 * 0.10)
        assert summary["normal_pnl"] == pytest.approx(0.0 - 5.0 * 0.60)


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


class TestCostBasisAcrossReprices:
    """Direct regression test for a confirmed live bug (2026-09-08, found
    via a direct "is our own PNL system trustworthy" audit): entry_cost
    used to be filled_size * order.price -- order.price is the order's
    CURRENT (final) resting price, but filled_size sums shares that may
    have filled at DIFFERENT prices before a drift-reprice changed it.
    Measured live: 144 of 2,577 completed orders in one ~8h window (5.6%)
    had fills spanning at least one reprice -- real, not a rare edge
    case."""

    def test_entry_cost_uses_each_fills_own_price_not_the_final_resting_price(self):
        order = SimulatedOrder(
            order_id=1, condition_id="cond-1", token_id="tok-up", asset="Bitcoin",
            regime="MID", position_tier="first", side="Up", price=0.55,
            original_size=10.0, is_floor_lot=False, placed_at=0.0, remaining_size=0.0,
        )
        # 4 shares filled at the ORIGINAL price (0.40), then the order
        # repriced to 0.55, then the remaining 6 filled at the NEW price.
        order.fills.append(Fill(size=4.0, price=0.40, ts=1.0))
        order.fills.append(Fill(size=6.0, price=0.55, ts=2.0))
        order.status = OrderStatus.FILLED

        ledger = Ledger()
        record = ledger.settle_order(order, winning_side="Up")

        true_cost = 4.0 * 0.40 + 6.0 * 0.55
        wrong_cost = 10.0 * 0.55  # what the old buggy formula would have given
        assert record.entry_cost == pytest.approx(true_cost)
        assert record.entry_cost != pytest.approx(wrong_cost)
        assert record.entry_price == pytest.approx(true_cost / 10.0)
        assert record.pnl == pytest.approx(10.0 * 1.0 - true_cost)

    def test_single_fill_order_is_unaffected(self):
        """Sanity check: an order that never reprices (the overwhelming
        majority) must settle identically to before -- filled_size * price
        and sum(fill.size * fill.price) agree exactly when there's only
        one fill at the resting price."""
        order = SimulatedOrder(
            order_id=1, condition_id="cond-1", token_id="tok-up", asset="Bitcoin",
            regime="CHEAP", position_tier="first", side="Up", price=0.20,
            original_size=5.0, is_floor_lot=False, placed_at=0.0, remaining_size=0.0,
        )
        order.fills.append(Fill(size=5.0, price=0.20, ts=1.0))
        order.status = OrderStatus.FILLED

        record = Ledger().settle_order(order, winning_side="Up")
        assert record.entry_cost == pytest.approx(5.0 * 0.20)
        assert record.entry_price == pytest.approx(0.20)


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
