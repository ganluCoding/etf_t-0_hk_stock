"""Pre-backtest break-even ledger for manually operated ETF research.

The ledger is deliberately narrower than a backtest.  It answers whether a
particular target, legal order size and declared grid can mathematically cover
the stated round-trip costs.  It never represents an order, a fill probability,
or a live-trading recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from enum import Enum
from zoneinfo import ZoneInfo

from etf_t0.fees import (
    BASIS_POINT_DENOMINATOR,
    ETF_PRICE_TICK,
    FeeSchedule,
    OrderSide,
    break_even_for_round_trip,
    cost_for_order,
)

LOT_SIZE = 100
PAPER_EXECUTION_MINIMUM_VALID_DAYS = 20
QUOTE_MAXIMUM_AGE_SECONDS = 120
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ExecutionTier(str, Enum):
    """Evidence tier; higher tiers never infer lower-tier missing evidence."""

    OHLC_CONSERVATIVE = "OHLC_CONSERVATIVE"
    QUOTE_AWARE = "QUOTE_AWARE"
    PAPER_EXECUTION = "PAPER_EXECUTION"


class PaperExecutionOutcome(str, Enum):
    """Manually observed outcome; no value implies an automated broker action."""

    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNFILLED = "UNFILLED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class PaperExecutionRecord:
    """One manual observation needed to count a paper-execution day."""

    symbol: str
    observed_at: datetime
    normal_overlap_day: bool
    intended_side: OrderSide
    intended_price: Decimal
    intended_quantity: int
    observed_bid1_price: Decimal
    observed_ask1_price: Decimal
    quote_source: str
    fee_evidence: str
    outcome: PaperExecutionOutcome
    filled_price: Decimal | None = None
    filled_quantity: int = 0
    outcome_reason: str | None = None

    def validate(self) -> None:
        if not self.symbol.strip() or not self.quote_source.strip() or not self.fee_evidence.strip():
            raise ValueError("paper record requires symbol, quote source and fee evidence")
        if not self.normal_overlap_day:
            raise ValueError("paper record must belong to a normal-overlap trading day")
        if self.observed_at.tzinfo is None:
            raise ValueError("paper record timestamp must include a timezone")
        if self.intended_price <= 0 or self.observed_bid1_price <= 0 or self.observed_ask1_price <= 0:
            raise ValueError("paper record prices must be positive")
        if self.observed_bid1_price > self.observed_ask1_price:
            raise ValueError("paper record bid cannot exceed ask")
        prices = (self.intended_price, self.observed_bid1_price, self.observed_ask1_price)
        if any(price % ETF_PRICE_TICK != 0 for price in prices):
            raise ValueError("paper record prices must align to the 0.001 ETF price tick")
        if self.intended_quantity <= 0 or self.intended_quantity % LOT_SIZE != 0:
            raise ValueError("paper record intended quantity must be a positive 100-unit lot")
        if self.outcome is PaperExecutionOutcome.FILLED:
            if self.filled_price is None or self.filled_quantity != self.intended_quantity:
                raise ValueError("filled paper record requires full quantity and fill price")
        elif self.outcome is PaperExecutionOutcome.PARTIAL:
            if self.filled_price is None or not 0 < self.filled_quantity < self.intended_quantity:
                raise ValueError("partial paper record requires partial quantity and fill price")
        elif self.filled_price is not None or self.filled_quantity != 0 or not self.outcome_reason:
            raise ValueError("unfilled/cancelled paper record requires zero fill and a reason")
        if self.filled_price is not None and self.filled_price % ETF_PRICE_TICK != 0:
            raise ValueError("paper fill price must align to the 0.001 ETF price tick")


@dataclass(frozen=True)
class ExecutionEvidence:
    """Declared execution evidence used by a single ledger request.

    `queue_partial_fill_haircut_bps_round_trip` is an explicit conservative
    economic haircut.  It is not a fill probability and remains zero only when
    the caller explicitly accepts a lower-bound cost screen.
    """

    quote_qualified: bool = False
    depth_available: bool = False
    quote_symbol: str | None = None
    quote_observed_at: datetime | None = None
    quote_age_seconds: int | None = None
    bid1_price: Decimal | None = None
    ask1_price: Decimal | None = None
    bid1_quantity: int | None = None
    ask1_quantity: int | None = None
    conservative_slippage_bps_round_trip: Decimal | None = None
    queue_partial_fill_haircut_bps_round_trip: Decimal | None = None
    source: str | None = None
    paper_execution_records: tuple[PaperExecutionRecord, ...] = ()
    normal_overlap_calendar_version: str | None = None
    normal_overlap_dates: frozenset[date] = frozenset()

    def validate(self) -> None:
        if (
            self.conservative_slippage_bps_round_trip is not None
            and self.conservative_slippage_bps_round_trip < 0
        ):
            raise ValueError("conservative slippage cannot be negative")
        if (
            self.queue_partial_fill_haircut_bps_round_trip is not None
            and self.queue_partial_fill_haircut_bps_round_trip < 0
        ):
            raise ValueError("queue/partial-fill haircut cannot be negative")
        if self.quote_age_seconds is not None and self.quote_age_seconds < 0:
            raise ValueError("quote age cannot be negative")
        quantities = (self.bid1_quantity, self.ask1_quantity)
        if any(quantity is not None and quantity <= 0 for quantity in quantities):
            raise ValueError("quote depth quantities must be positive when supplied")
        if self.quote_observed_at is not None and self.quote_observed_at.tzinfo is None:
            raise ValueError("quote timestamp must include a timezone")
        for record in self.paper_execution_records:
            record.validate()
        if any(not isinstance(day, date) for day in self.normal_overlap_dates):
            raise ValueError("normal-overlap calendar dates must be date values")


@dataclass(frozen=True)
class BreakEvenLedgerRequest:
    """One target/order/grid/cost/evidence combination in the ledger."""

    symbol: str
    entry_price: Decimal
    order_amount_cny: Decimal
    grid_spacing_bps: Decimal
    fee_schedule: FeeSchedule
    execution_tier: ExecutionTier
    execution_evidence: ExecutionEvidence = ExecutionEvidence()
    fee_scope_confirmed: bool = False
    total_capital_cny: Decimal | None = None
    planned_round_trip_count: int = 1
    trusted_as_of: datetime | None = None

    def validate(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol is required")
        if self.entry_price <= 0:
            raise ValueError("entry price must be positive")
        if self.entry_price % ETF_PRICE_TICK != 0:
            raise ValueError("entry price must align to the 0.001 ETF price tick")
        if self.order_amount_cny <= 0:
            raise ValueError("order amount must be positive")
        if self.grid_spacing_bps <= 0:
            raise ValueError("grid spacing must be positive")
        if self.total_capital_cny is not None and self.total_capital_cny < self.order_amount_cny:
            raise ValueError("total capital cannot be below one planned order amount")
        if self.planned_round_trip_count <= 0:
            raise ValueError("planned round-trip count must be positive")
        if self.trusted_as_of is not None and self.trusted_as_of.tzinfo is None:
            raise ValueError("trusted as-of timestamp must include a timezone")
        self.fee_schedule.validate()
        self.execution_evidence.validate()


@dataclass(frozen=True)
class BreakEvenLedgerRow:
    """Auditable pre-backtest cost result, including all evidence limitations."""

    symbol: str
    execution_tier: ExecutionTier
    fee_schedule_name: str
    entry_price: Decimal
    order_amount_cny: Decimal
    total_capital_cny: Decimal
    planned_round_trip_count: int
    quantity: int
    entry_notional_cny: Decimal
    unused_cash_cny: Decimal
    proposed_grid_bps: Decimal
    proposed_grid_price_delta_cny: Decimal | None
    buy_charged_commission_cny: Decimal | None
    sell_charged_commission_cny: Decimal | None
    declared_minimum_commission_per_side_cny: Decimal | None
    other_explicit_cost_cny: Decimal | None
    spread_cost_cny: Decimal | None
    slippage_cost_cny: Decimal | None
    queue_partial_fill_haircut_cny: Decimal | None
    declared_round_trip_cost_cny: Decimal | None
    aggregate_declared_round_trip_cost_cny: Decimal | None
    aggregate_declared_cost_bps_of_total_capital: Decimal | None
    minimum_round_trip_price_delta_cny: Decimal | None
    minimum_round_trip_ticks: int | None
    minimum_round_trip_bps: Decimal | None
    tick_aligned_break_even_exit_price: Decimal | None
    statuses: tuple[str, ...]
    admissible_to_backtest: bool


def _affordable_quantity(
    request: BreakEvenLedgerRequest, *, effective_entry_price: Decimal, schedule: FeeSchedule
) -> int:
    """Find the largest 100-unit purchase whose buy cash flow fits the budget."""

    quantity = int(
        (request.order_amount_cny / effective_entry_price).to_integral_value(rounding=ROUND_DOWN)
    )
    quantity = quantity // LOT_SIZE * LOT_SIZE
    while quantity > 0:
        notional = effective_entry_price * quantity
        buy_cost = cost_for_order(
            schedule=schedule,
            side=OrderSide.BUY,
            reference_notional=notional,
        )
        if notional + buy_cost.economic_cost <= request.order_amount_cny:
            return quantity
        quantity -= LOT_SIZE
    return 0


def _tick_aligned_grid_delta(*, entry_price: Decimal, grid_spacing_bps: Decimal) -> Decimal:
    raw_delta = entry_price * grid_spacing_bps / BASIS_POINT_DENOMINATOR
    return (raw_delta / ETF_PRICE_TICK).to_integral_value(rounding=ROUND_CEILING) * ETF_PRICE_TICK


def _has_qualified_quote(request: BreakEvenLedgerRequest) -> bool:
    evidence = request.execution_evidence
    return bool(
        evidence.quote_qualified
        and evidence.depth_available
        and evidence.source
        and evidence.quote_symbol == request.symbol
        and evidence.quote_observed_at is not None
        and evidence.quote_age_seconds is not None
        and request.trusted_as_of is not None
        and evidence.bid1_price is not None
        and evidence.ask1_price is not None
        and evidence.bid1_price > 0
        and evidence.ask1_price >= evidence.bid1_price
        and evidence.bid1_quantity is not None
        and evidence.ask1_quantity is not None
        and evidence.bid1_quantity > 0
        and evidence.ask1_quantity > 0
        and _quote_age_seconds(request) == evidence.quote_age_seconds
        and _quote_age_seconds(request) >= 0
        and _quote_age_seconds(request) <= QUOTE_MAXIMUM_AGE_SECONDS
    )


def _quote_age_seconds(request: BreakEvenLedgerRequest) -> int:
    assert request.trusted_as_of is not None
    assert request.execution_evidence.quote_observed_at is not None
    return int((request.trusted_as_of - request.execution_evidence.quote_observed_at).total_seconds())


def _has_declared_execution_haircuts(request: BreakEvenLedgerRequest) -> bool:
    evidence = request.execution_evidence
    return (
        evidence.conservative_slippage_bps_round_trip is not None
        and evidence.queue_partial_fill_haircut_bps_round_trip is not None
    )


def _effective_schedule(request: BreakEvenLedgerRequest) -> FeeSchedule:
    """Use executable ask/bid separately, so never add the quote spread twice."""

    if request.execution_tier is ExecutionTier.OHLC_CONSERVATIVE:
        return request.fee_schedule
    evidence = request.execution_evidence
    if not _has_qualified_quote(request) or not _has_declared_execution_haircuts(request):
        return request.fee_schedule
    assert evidence.conservative_slippage_bps_round_trip is not None
    return replace(
        request.fee_schedule,
        spread_bps_per_side=Decimal(0),
        slippage_bps_per_side=evidence.conservative_slippage_bps_round_trip / 2,
    )


def _paper_execution_valid_days(request: BreakEvenLedgerRequest) -> int:
    evidence = request.execution_evidence
    if not evidence.normal_overlap_calendar_version:
        return 0
    return len(
        {
            record.observed_at.date()
            for record in evidence.paper_execution_records
            if (
                record.symbol == request.symbol
                and record.normal_overlap_day
                and record.observed_at.date() in evidence.normal_overlap_dates
                and _is_continuous_auction_time(record.observed_at)
            )
        }
    )


def _is_continuous_auction_time(observed_at: datetime) -> bool:
    local_time = observed_at.astimezone(SHANGHAI).time()
    return time(9, 30) <= local_time < time(11, 30) or time(13, 0) <= local_time < time(15, 0)


def _execution_statuses(request: BreakEvenLedgerRequest) -> list[str]:
    if request.execution_tier is ExecutionTier.OHLC_CONSERVATIVE:
        return ["NO_EXECUTION_CLAIM"]
    if request.execution_tier is ExecutionTier.QUOTE_AWARE:
        if not _has_qualified_quote(request):
            return ["WAIT_QUOTE_DATA"]
        if not _has_declared_execution_haircuts(request):
            return ["WAIT_EXECUTION_COST_EVIDENCE"]
        if request.entry_price != request.execution_evidence.ask1_price:
            return ["WAIT_ENTRY_PRICE_BASIS"]
        return ["QUOTE_AWARE_COST_SCREEN"]

    statuses = ["PAPER_EXECUTION_FEASIBILITY_ONLY"]
    if not _has_qualified_quote(request):
        statuses.append("WAIT_QUOTE_DATA")
    if not _has_declared_execution_haircuts(request):
        statuses.append("WAIT_EXECUTION_COST_EVIDENCE")
    if _has_qualified_quote(request) and request.entry_price != request.execution_evidence.ask1_price:
        statuses.append("WAIT_ENTRY_PRICE_BASIS")
    if _paper_execution_valid_days(request) < PAPER_EXECUTION_MINIMUM_VALID_DAYS:
        statuses.append("WAIT_20_PAPER_EXECUTION_DAYS")
    return statuses


def build_break_even_ledger_row(request: BreakEvenLedgerRequest) -> BreakEvenLedgerRow:
    """Build one cost-gate row without assuming a broker fill.

    A provisional fee schedule is allowed for research, but preserves
    `WAIT_FEE_EVIDENCE` so the row cannot become an actual-cost conclusion.
    """

    request.validate()
    total_capital = request.total_capital_cny or (
        request.order_amount_cny * request.planned_round_trip_count
    )
    statuses: list[str] = []
    effective_schedule = _effective_schedule(request)
    effective_entry_price = (
        request.execution_evidence.ask1_price
        if _has_qualified_quote(request) and request.execution_tier is not ExecutionTier.OHLC_CONSERVATIVE
        else request.entry_price
    )
    assert effective_entry_price is not None
    quantity = _affordable_quantity(
        request, effective_entry_price=effective_entry_price, schedule=effective_schedule
    )
    proposed_delta = _tick_aligned_grid_delta(
        entry_price=request.entry_price, grid_spacing_bps=request.grid_spacing_bps
    )
    if quantity == 0:
        statuses.extend(["RESEARCH_BLOCKED_LOT", *_execution_statuses(request)])
        return BreakEvenLedgerRow(
            symbol=request.symbol,
            execution_tier=request.execution_tier,
            fee_schedule_name=request.fee_schedule.name,
            entry_price=request.entry_price,
            order_amount_cny=request.order_amount_cny,
            total_capital_cny=total_capital,
            planned_round_trip_count=request.planned_round_trip_count,
            quantity=0,
            entry_notional_cny=Decimal(0),
            unused_cash_cny=request.order_amount_cny,
            proposed_grid_bps=request.grid_spacing_bps,
            proposed_grid_price_delta_cny=proposed_delta,
            buy_charged_commission_cny=None,
            sell_charged_commission_cny=None,
            declared_minimum_commission_per_side_cny=None,
            other_explicit_cost_cny=None,
            spread_cost_cny=None,
            slippage_cost_cny=None,
            queue_partial_fill_haircut_cny=None,
            declared_round_trip_cost_cny=None,
            aggregate_declared_round_trip_cost_cny=None,
            aggregate_declared_cost_bps_of_total_capital=None,
            minimum_round_trip_price_delta_cny=None,
            minimum_round_trip_ticks=None,
            minimum_round_trip_bps=None,
            tick_aligned_break_even_exit_price=None,
            statuses=tuple(statuses),
            admissible_to_backtest=False,
        )

    entry_notional = effective_entry_price * quantity
    queue_haircut = (
        entry_notional
        * (request.execution_evidence.queue_partial_fill_haircut_bps_round_trip or Decimal(0))
        / BASIS_POINT_DENOMINATOR
    )
    result = break_even_for_round_trip(
        schedule=effective_schedule,
        entry_price=effective_entry_price,
        quantity=quantity,
        required_net_profit_cny=queue_haircut,
    )
    costs = result.round_trip_cost
    other_explicit = (
        costs.buy.handling_fee
        + costs.sell.handling_fee
        + costs.buy.stamp_duty
        + costs.sell.stamp_duty
        + costs.buy.transfer_fee
        + costs.sell.transfer_fee
    )
    spread_cost = costs.buy.spread_cost + costs.sell.spread_cost
    movement_base_price = effective_entry_price
    if _has_qualified_quote(request) and request.execution_tier is not ExecutionTier.OHLC_CONSERVATIVE:
        assert request.execution_evidence.bid1_price is not None
        movement_base_price = request.execution_evidence.bid1_price
        spread_cost = (effective_entry_price - movement_base_price) * quantity
    slippage_cost = costs.buy.slippage_cost + costs.sell.slippage_cost
    declared_total = costs.economic_cost + queue_haircut + spread_cost
    aggregate_declared_cost = declared_total * request.planned_round_trip_count
    minimum_delta = result.tick_aligned_exit_price - movement_base_price
    if proposed_delta < minimum_delta:
        statuses.append("RESEARCH_BLOCKED_COST")
    else:
        statuses.append("COST_FLOOR_COVERED")
    if request.fee_schedule.provisional:
        statuses.append("WAIT_FEE_EVIDENCE")
    if not request.fee_scope_confirmed:
        statuses.append("WAIT_FEE_SCOPE")
    statuses.extend(_execution_statuses(request))
    if request.execution_tier is ExecutionTier.OHLC_CONSERVATIVE or (
        request.execution_evidence.conservative_slippage_bps_round_trip == 0
        or request.execution_evidence.queue_partial_fill_haircut_bps_round_trip == 0
    ):
        statuses.append("LOWER_BOUND_ONLY")
    return BreakEvenLedgerRow(
        symbol=request.symbol,
        execution_tier=request.execution_tier,
        fee_schedule_name=request.fee_schedule.name,
        entry_price=request.entry_price,
        order_amount_cny=request.order_amount_cny,
        total_capital_cny=total_capital,
        planned_round_trip_count=request.planned_round_trip_count,
        quantity=quantity,
        entry_notional_cny=entry_notional,
        unused_cash_cny=request.order_amount_cny
        - entry_notional
        - costs.buy.economic_cost,
        proposed_grid_bps=request.grid_spacing_bps,
        proposed_grid_price_delta_cny=proposed_delta,
        buy_charged_commission_cny=costs.buy.commission,
        sell_charged_commission_cny=costs.sell.commission,
        declared_minimum_commission_per_side_cny=request.fee_schedule.minimum_commission,
        other_explicit_cost_cny=other_explicit,
        spread_cost_cny=spread_cost,
        slippage_cost_cny=slippage_cost,
        queue_partial_fill_haircut_cny=queue_haircut,
        declared_round_trip_cost_cny=declared_total,
        aggregate_declared_round_trip_cost_cny=aggregate_declared_cost,
        aggregate_declared_cost_bps_of_total_capital=(
            aggregate_declared_cost / total_capital * BASIS_POINT_DENOMINATOR
        ),
        minimum_round_trip_price_delta_cny=minimum_delta,
        minimum_round_trip_ticks=int(minimum_delta / ETF_PRICE_TICK),
        minimum_round_trip_bps=minimum_delta / effective_entry_price * BASIS_POINT_DENOMINATOR,
        tick_aligned_break_even_exit_price=result.tick_aligned_exit_price,
        statuses=tuple(statuses),
        admissible_to_backtest=not any(
            status
            in {
                "RESEARCH_BLOCKED_COST",
                "RESEARCH_BLOCKED_LOT",
                "WAIT_FEE_EVIDENCE",
                "WAIT_FEE_SCOPE",
                "NO_EXECUTION_CLAIM",
                "WAIT_QUOTE_DATA",
                "WAIT_EXECUTION_COST_EVIDENCE",
                "WAIT_ENTRY_PRICE_BASIS",
                "WAIT_20_PAPER_EXECUTION_DAYS",
                "PAPER_EXECUTION_FEASIBILITY_ONLY",
                "LOWER_BOUND_ONLY",
            }
            for status in statuses
        ),
    )


def build_break_even_ledger(
    requests: list[BreakEvenLedgerRequest],
) -> list[BreakEvenLedgerRow]:
    """Build a complete declared matrix; no row is silently dropped."""

    return [build_break_even_ledger_row(request) for request in requests]
