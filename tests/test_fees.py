from decimal import ROUND_DOWN, Decimal

import pytest

from etf_t0.fees import (
    FeeSchedule,
    OrderSide,
    break_even_for_round_trip,
    cmb_user_reported_fee_scenarios,
    cost_for_order,
    provisional_fee_scenarios,
    round_trip_cost,
)


def test_10000_yuan_round_trip_has_ten_yuan_minimum_commission_cost() -> None:
    all_in, _ = provisional_fee_scenarios()

    costs = round_trip_cost(schedule=all_in, reference_notional=Decimal(10000))

    assert costs.buy.commission == Decimal("5.00")
    assert costs.sell.commission == Decimal("5.00")
    assert costs.explicit_cost == Decimal("10.00")
    assert costs.economic_cost == Decimal("10.00")
    assert costs.provisional is True
    assert costs.schedule.name == "provisional_minimum_commission_all_in"


def test_cmb_user_reported_minimum_has_ten_yuan_round_trip_and_no_tax_or_transfer() -> None:
    all_in, separately_charged = cmb_user_reported_fee_scenarios()

    all_in_costs = round_trip_cost(schedule=all_in, reference_notional=Decimal(10000))
    separately_charged_costs = round_trip_cost(
        schedule=separately_charged, reference_notional=Decimal(10000)
    )

    assert all_in_costs.economic_cost == Decimal("10.00")
    assert all_in_costs.buy.stamp_duty == Decimal("0.00")
    assert all_in_costs.buy.transfer_fee == Decimal("0.00")
    assert separately_charged_costs.economic_cost == Decimal("10.80")
    assert all_in.provisional is True


def test_separate_handling_fee_is_not_double_counted_when_all_in() -> None:
    all_in, separate = provisional_fee_scenarios()

    all_in_cost = cost_for_order(
        schedule=all_in, side=OrderSide.BUY, reference_notional=Decimal(10000)
    )
    separate_cost = cost_for_order(
        schedule=separate, side=OrderSide.BUY, reference_notional=Decimal(10000)
    )

    assert all_in_cost.handling_fee == Decimal(0)
    assert separate_cost.handling_fee == Decimal("0.40")


def test_break_even_uses_both_order_fees_and_etf_price_tick() -> None:
    all_in, separate = provisional_fee_scenarios()

    all_in_result = break_even_for_round_trip(
        schedule=all_in, entry_price=Decimal(1), quantity=10000
    )
    separate_result = break_even_for_round_trip(
        schedule=separate, entry_price=Decimal(1), quantity=10000
    )

    assert all_in_result.required_return == Decimal("0.001")
    assert all_in_result.tick_aligned_exit_price == Decimal("1.001")
    assert separate_result.round_trip_cost.economic_cost == Decimal("10.80")
    assert separate_result.tick_aligned_exit_price == Decimal("1.002")


def test_percentage_commission_can_exceed_the_minimum_per_order() -> None:
    schedule = FeeSchedule(
        name="test",
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal(5),
        handling_fee_rate=Decimal(0),
        handling_fee_included_in_commission=True,
        provisional=False,
    )

    cost = cost_for_order(
        schedule=schedule, side=OrderSide.SELL, reference_notional=Decimal(30000)
    )

    assert cost.commission == Decimal("9.00")


def test_break_even_rejects_non_lot_quantity() -> None:
    all_in, _ = provisional_fee_scenarios()

    with pytest.raises(ValueError, match="multiple of 100"):
        break_even_for_round_trip(schedule=all_in, entry_price=Decimal(1), quantity=99)


def test_break_even_rejects_an_entry_price_off_the_etf_tick() -> None:
    all_in, _ = provisional_fee_scenarios()

    with pytest.raises(ValueError, match="price tick"):
        break_even_for_round_trip(schedule=all_in, entry_price=Decimal("1.0001"), quantity=100)


def test_break_even_reprices_sell_side_percentage_fees() -> None:
    schedule = FeeSchedule(
        name="percentage-fee",
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal(0),
        handling_fee_rate=Decimal(0),
        handling_fee_included_in_commission=True,
        provisional=False,
    )

    result = break_even_for_round_trip(
        schedule=schedule, entry_price=Decimal(10000), quantity=100
    )
    sell_notional = result.tick_aligned_exit_price * result.quantity

    assert result.tick_aligned_exit_price == Decimal("10006.002")
    assert sell_notional - result.round_trip_cost.sell.economic_cost >= (
        result.reference_notional + result.round_trip_cost.buy.economic_cost
    )


def test_break_even_reprices_each_tick_until_execution_buffer_is_covered() -> None:
    _, schedule = provisional_fee_scenarios()

    result = break_even_for_round_trip(
        schedule=schedule,
        entry_price=Decimal("1.469"),
        quantity=3400,
        required_net_profit_cny=Decimal("6.80"),
    )
    prior_exit = result.tick_aligned_exit_price - Decimal("0.001")
    prior_sell = cost_for_order(
        schedule=schedule,
        side=OrderSide.SELL,
        reference_notional=prior_exit * result.quantity,
    )
    final_sell_notional = result.tick_aligned_exit_price * result.quantity
    final_net_profit = (
        final_sell_notional
        - result.round_trip_cost.sell.economic_cost
        - result.reference_notional
        - result.round_trip_cost.buy.economic_cost
    )
    prior_net_profit = (
        prior_exit * result.quantity
        - prior_sell.economic_cost
        - result.reference_notional
        - result.round_trip_cost.buy.economic_cost
    )

    assert final_net_profit >= Decimal("6.80")
    assert prior_net_profit < Decimal("6.80")


def test_fee_schedule_can_declare_a_broker_rounding_mode() -> None:
    schedule = FeeSchedule(
        name="round-down",
        commission_rate=Decimal("0.00005"),
        minimum_commission=Decimal(0),
        handling_fee_rate=Decimal(0),
        handling_fee_included_in_commission=True,
        fee_rounding_mode=ROUND_DOWN,
    )

    cost = cost_for_order(schedule=schedule, side=OrderSide.BUY, reference_notional=Decimal(100))

    assert cost.commission == Decimal("0.00")
