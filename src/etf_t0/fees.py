"""Provisional, auditable transaction-cost calculations for secondary-market ETFs.

These utilities calculate cost coverage only. They neither assert a broker's actual
charging rules nor model whether an order would fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_05UP,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
)
from enum import Enum

CENT = Decimal("0.01")
BASIS_POINT_DENOMINATOR = Decimal(10000)
ETF_PRICE_TICK = Decimal("0.001")


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


VALID_ROUNDING_MODES = {
    ROUND_05UP,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
}


@dataclass(frozen=True)
class FeeSchedule:
    """A declared fee scenario, never an inferred broker fee schedule."""

    name: str
    commission_rate: Decimal
    minimum_commission: Decimal
    handling_fee_rate: Decimal
    handling_fee_included_in_commission: bool
    stamp_duty_rate: Decimal = Decimal(0)
    transfer_fee_rate: Decimal = Decimal(0)
    spread_bps_per_side: Decimal = Decimal(0)
    slippage_bps_per_side: Decimal = Decimal(0)
    fee_rounding_quantum: Decimal = CENT
    fee_rounding_mode: str = ROUND_HALF_UP
    provisional: bool = True
    assumptions: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("fee schedule requires a name")
        values = {
            "commission_rate": self.commission_rate,
            "minimum_commission": self.minimum_commission,
            "handling_fee_rate": self.handling_fee_rate,
            "stamp_duty_rate": self.stamp_duty_rate,
            "transfer_fee_rate": self.transfer_fee_rate,
            "spread_bps_per_side": self.spread_bps_per_side,
            "slippage_bps_per_side": self.slippage_bps_per_side,
        }
        invalid = [name for name, value in values.items() if value < 0]
        if invalid:
            raise ValueError(f"fee schedule values cannot be negative: {', '.join(invalid)}")
        if self.fee_rounding_quantum <= 0:
            raise ValueError("fee rounding quantum must be positive")
        if self.fee_rounding_mode not in VALID_ROUNDING_MODES:
            raise ValueError("fee rounding mode is not supported")

    def round_fee(self, value: Decimal) -> Decimal:
        return value.quantize(self.fee_rounding_quantum, rounding=self.fee_rounding_mode)


@dataclass(frozen=True)
class OrderCost:
    side: OrderSide
    reference_notional: Decimal
    commission: Decimal
    handling_fee: Decimal
    stamp_duty: Decimal
    transfer_fee: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal

    @property
    def explicit_cost(self) -> Decimal:
        return self.commission + self.handling_fee + self.stamp_duty + self.transfer_fee

    @property
    def economic_cost(self) -> Decimal:
        return self.explicit_cost + self.spread_cost + self.slippage_cost


@dataclass(frozen=True)
class RoundTripCost:
    schedule: FeeSchedule
    buy: OrderCost
    sell: OrderCost
    provisional: bool
    assumptions: tuple[str, ...]

    @property
    def explicit_cost(self) -> Decimal:
        return self.buy.explicit_cost + self.sell.explicit_cost

    @property
    def economic_cost(self) -> Decimal:
        return self.buy.economic_cost + self.sell.economic_cost


@dataclass(frozen=True)
class BreakEvenResult:
    entry_price: Decimal
    quantity: int
    reference_notional: Decimal
    round_trip_cost: RoundTripCost
    minimum_tick_price_delta: Decimal
    tick_aligned_exit_price: Decimal

    @property
    def required_return(self) -> Decimal:
        return self.minimum_tick_price_delta / self.entry_price


def cost_for_order(
    *, schedule: FeeSchedule, side: OrderSide, reference_notional: Decimal
) -> OrderCost:
    """Cost one filled order at a declared reference notional."""

    schedule.validate()
    if reference_notional <= 0:
        raise ValueError("reference notional must be positive")

    commission = schedule.round_fee(
        max(reference_notional * schedule.commission_rate, schedule.minimum_commission)
    )
    handling_fee = Decimal(0)
    if not schedule.handling_fee_included_in_commission:
        handling_fee = schedule.round_fee(reference_notional * schedule.handling_fee_rate)

    return OrderCost(
        side=side,
        reference_notional=reference_notional,
        commission=commission,
        handling_fee=handling_fee,
        stamp_duty=schedule.round_fee(reference_notional * schedule.stamp_duty_rate),
        transfer_fee=schedule.round_fee(reference_notional * schedule.transfer_fee_rate),
        spread_cost=schedule.round_fee(
            reference_notional * schedule.spread_bps_per_side / BASIS_POINT_DENOMINATOR
        ),
        slippage_cost=schedule.round_fee(
            reference_notional * schedule.slippage_bps_per_side / BASIS_POINT_DENOMINATOR
        ),
    )


def round_trip_cost(*, schedule: FeeSchedule, reference_notional: Decimal) -> RoundTripCost:
    """Cost a buy and sell separately at the same reference notional."""

    return RoundTripCost(
        schedule=schedule,
        buy=cost_for_order(
            schedule=schedule, side=OrderSide.BUY, reference_notional=reference_notional
        ),
        sell=cost_for_order(
            schedule=schedule, side=OrderSide.SELL, reference_notional=reference_notional
        ),
        provisional=schedule.provisional,
        assumptions=schedule.assumptions,
    )


def break_even_for_round_trip(
    *, schedule: FeeSchedule, entry_price: Decimal, quantity: int
) -> BreakEvenResult:
    """Return gross price movement required to cover a declared round-trip scenario.

    It solves for the smallest valid price tick whose sell proceeds after sell-side
    costs cover the entry cash flow after buy-side costs. It remains cost coverage,
    not an executable target price or a trade recommendation.
    """

    if entry_price <= 0:
        raise ValueError("entry price must be positive")
    if entry_price % ETF_PRICE_TICK != 0:
        raise ValueError("entry price must align to the 0.001 ETF price tick")
    if quantity <= 0 or quantity % 100 != 0:
        raise ValueError("quantity must be a positive multiple of 100 ETF units")

    schedule.validate()
    reference_notional = entry_price * quantity
    buy = cost_for_order(
        schedule=schedule, side=OrderSide.BUY, reference_notional=reference_notional
    )
    required_sell_proceeds = reference_notional + buy.economic_cost
    variable_sell_rate = (
        schedule.commission_rate + schedule.stamp_duty_rate + schedule.transfer_fee_rate
    )
    if not schedule.handling_fee_included_in_commission:
        variable_sell_rate += schedule.handling_fee_rate
    variable_sell_rate += (
        schedule.spread_bps_per_side + schedule.slippage_bps_per_side
    ) / BASIS_POINT_DENOMINATOR
    if variable_sell_rate >= 1:
        raise ValueError("variable sell-side cost rate must be below 100%")

    estimated_exit_price = required_sell_proceeds / (Decimal(quantity) * (1 - variable_sell_rate))
    tick_aligned_exit_price = (
        estimated_exit_price / ETF_PRICE_TICK
    ).to_integral_value(rounding=ROUND_CEILING) * ETF_PRICE_TICK
    while True:
        sell_notional = tick_aligned_exit_price * quantity
        sell = cost_for_order(
            schedule=schedule, side=OrderSide.SELL, reference_notional=sell_notional
        )
        if sell_notional - sell.economic_cost >= required_sell_proceeds:
            break
        tick_aligned_exit_price += ETF_PRICE_TICK

    costs = RoundTripCost(
        schedule=schedule,
        buy=buy,
        sell=sell,
        provisional=schedule.provisional,
        assumptions=schedule.assumptions,
    )
    return BreakEvenResult(
        entry_price=entry_price,
        quantity=quantity,
        reference_notional=reference_notional,
        round_trip_cost=costs,
        minimum_tick_price_delta=tick_aligned_exit_price - entry_price,
        tick_aligned_exit_price=tick_aligned_exit_price,
    )


def provisional_fee_scenarios() -> tuple[FeeSchedule, FeeSchedule]:
    """Return transparent temporary scenarios until a broker statement replaces them."""

    common_assumptions = (
        "ETF secondary-market stamp duty is modelled as zero.",
        "Broker commission rate is unknown; each filled buy and sell uses a provisional 5 yuan minimum.",
        "No broker statement has yet verified the charging method for partial fills or handling fees.",
        "Explicit fees are rounded to 0.01 yuan using ROUND_HALF_UP.",
    )
    return (
        FeeSchedule(
            name="provisional_minimum_commission_all_in",
            commission_rate=Decimal(0),
            minimum_commission=Decimal(5),
            handling_fee_rate=Decimal("0.00004"),
            handling_fee_included_in_commission=True,
            assumptions=common_assumptions
            + ("Handling fee is assumed included in the quoted all-in commission.",),
        ),
        FeeSchedule(
            name="provisional_minimum_commission_plus_handling",
            commission_rate=Decimal(0),
            minimum_commission=Decimal(5),
            handling_fee_rate=Decimal("0.00004"),
            handling_fee_included_in_commission=False,
            assumptions=common_assumptions
            + ("Handling fee is assumed separately charged at 0.04 per mille per side.",),
        ),
    )


def cmb_user_reported_fee_scenarios() -> tuple[FeeSchedule, FeeSchedule]:
    """Return the user-reported CMB minimum-fee scenarios for 159567 research.

    The known terms are a 5-yuan minimum on every filled buy and sell, no stamp
    duty, and no transfer fee. The percentage commission and handling-fee treatment
    remain unknown, so these are lower-bound scenarios rather than final calibration.
    """

    common_assumptions = (
        "User-reported 2026-07-25 CMB A-share account query for 159567: each filled buy and sell has a 5 yuan minimum commission.",
        "User-reported: ETF secondary-market stamp duty and transfer fee are zero.",
        "Commission percentage and partial-fill charging are unknown; commission_rate=0 models the confirmed minimum only.",
        "Explicit fees are rounded to 0.01 yuan using ROUND_HALF_UP.",
    )
    return (
        FeeSchedule(
            name="cmb_user_reported_minimum_all_in_assumption",
            commission_rate=Decimal(0),
            minimum_commission=Decimal(5),
            handling_fee_rate=Decimal("0.00004"),
            handling_fee_included_in_commission=True,
            assumptions=common_assumptions
            + ("Handling fee is assumed included; this has not been verified with CMB.",),
        ),
        FeeSchedule(
            name="cmb_user_reported_minimum_plus_handling_assumption",
            commission_rate=Decimal(0),
            minimum_commission=Decimal(5),
            handling_fee_rate=Decimal("0.00004"),
            handling_fee_included_in_commission=False,
            assumptions=common_assumptions
            + ("Handling fee is assumed separately charged; this has not been verified with CMB.",),
        ),
    )
