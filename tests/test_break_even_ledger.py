from datetime import datetime, timedelta, timezone
from decimal import Decimal

from etf_t0.break_even_ledger import (
    BreakEvenLedgerRequest,
    ExecutionEvidence,
    ExecutionTier,
    PaperExecutionOutcome,
    PaperExecutionRecord,
    build_break_even_ledger_row,
)
from etf_t0.fees import FeeSchedule, cmb_user_reported_fee_scenarios


def _minimum_fee_schedule() -> FeeSchedule:
    return cmb_user_reported_fee_scenarios()[0]


def _qualified_quote_evidence(**overrides) -> ExecutionEvidence:
    payload = {
        "quote_qualified": True,
        "depth_available": True,
        "quote_symbol": "159567",
        "quote_observed_at": datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc),
        "quote_age_seconds": 15,
        "bid1_price": Decimal("1.399"),
        "ask1_price": Decimal("1.400"),
        "bid1_quantity": 1200,
        "ask1_quantity": 1100,
        "conservative_slippage_bps_round_trip": Decimal(2),
        "queue_partial_fill_haircut_bps_round_trip": Decimal(3),
        "source": "broker-visible manual snapshot",
    }
    payload.update(overrides)
    return ExecutionEvidence(**payload)


def test_ledger_rejects_a_grid_below_its_tick_aligned_full_cost_floor() -> None:
    row = build_break_even_ledger_row(
        BreakEvenLedgerRequest(
            symbol="159567",
            entry_price=Decimal("1.400"),
            order_amount_cny=Decimal(10000),
            grid_spacing_bps=Decimal(5),
            fee_schedule=_minimum_fee_schedule(),
            execution_tier=ExecutionTier.OHLC_CONSERVATIVE,
            fee_scope_confirmed=True,
        )
    )

    assert row.quantity == 7100
    assert row.buy_charged_commission_cny == Decimal("5.00")
    assert row.sell_charged_commission_cny == Decimal("5.00")
    assert row.declared_minimum_commission_per_side_cny == Decimal(5)
    assert row.minimum_round_trip_price_delta_cny == Decimal("0.002")
    assert row.proposed_grid_price_delta_cny == Decimal("0.001")
    assert "RESEARCH_BLOCKED_COST" in row.statuses
    assert row.admissible_to_backtest is False
    assert "NO_EXECUTION_CLAIM" in row.statuses


def test_ledger_reports_each_declared_execution_cost_component_separately() -> None:
    schedule = FeeSchedule(
        name="declared-cost-components",
        commission_rate=Decimal(0),
        minimum_commission=Decimal(5),
        handling_fee_rate=Decimal(0),
        handling_fee_included_in_commission=True,
        spread_bps_per_side=Decimal("2.5"),
        slippage_bps_per_side=Decimal(1),
        provisional=False,
    )
    row = build_break_even_ledger_row(
        BreakEvenLedgerRequest(
            symbol="159567",
            entry_price=Decimal("1.400"),
            order_amount_cny=Decimal(10000),
            grid_spacing_bps=Decimal(50),
            fee_schedule=schedule,
            execution_tier=ExecutionTier.QUOTE_AWARE,
            execution_evidence=_qualified_quote_evidence(),
            fee_scope_confirmed=True,
            trusted_as_of=datetime(2026, 7, 28, 10, 0, 15, tzinfo=timezone.utc),
        )
    )

    assert row.spread_cost_cny > Decimal(0)
    assert row.slippage_cost_cny > Decimal(0)
    assert row.queue_partial_fill_haircut_cny > Decimal(0)
    assert row.minimum_round_trip_bps > Decimal(20)
    assert "QUOTE_AWARE_COST_SCREEN" in row.statuses
    assert "WAIT_FEE_EVIDENCE" not in row.statuses
    assert row.admissible_to_backtest is True


def test_ledger_fails_when_one_legal_lot_cannot_be_funded() -> None:
    row = build_break_even_ledger_row(
        BreakEvenLedgerRequest(
            symbol="159567",
            entry_price=Decimal("1.400"),
            order_amount_cny=Decimal(100),
            grid_spacing_bps=Decimal(100),
            fee_schedule=_minimum_fee_schedule(),
            execution_tier=ExecutionTier.OHLC_CONSERVATIVE,
            fee_scope_confirmed=True,
        )
    )

    assert row.quantity == 0
    assert row.admissible_to_backtest is False
    assert row.statuses == ("RESEARCH_BLOCKED_LOT", "NO_EXECUTION_CLAIM")


def test_quote_and_paper_tiers_cannot_be_promoted_without_their_evidence() -> None:
    quote_row = build_break_even_ledger_row(
        BreakEvenLedgerRequest(
            symbol="159567",
            entry_price=Decimal("1.400"),
            order_amount_cny=Decimal(10000),
            grid_spacing_bps=Decimal(100),
            fee_schedule=_minimum_fee_schedule(),
            execution_tier=ExecutionTier.QUOTE_AWARE,
            fee_scope_confirmed=True,
        )
    )
    paper_row = build_break_even_ledger_row(
        BreakEvenLedgerRequest(
            symbol="159567",
            entry_price=Decimal("1.400"),
            order_amount_cny=Decimal(10000),
            grid_spacing_bps=Decimal(100),
            fee_schedule=_minimum_fee_schedule(),
            execution_tier=ExecutionTier.PAPER_EXECUTION,
            execution_evidence=_qualified_quote_evidence(
                paper_execution_records=tuple(
                    PaperExecutionRecord(
                        symbol="159567",
                        observed_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
                        + timedelta(days=index),
                        normal_overlap_day=True,
                        intended_side="buy",
                        intended_price=Decimal("1.400"),
                        intended_quantity=100,
                        observed_bid1_price=Decimal("1.399"),
                        observed_ask1_price=Decimal("1.400"),
                        quote_source="manual journal",
                        fee_evidence="test fee profile",
                        outcome=PaperExecutionOutcome.UNFILLED,
                        outcome_reason="not reached",
                    )
                    for index in range(19)
                )
            ),
            fee_scope_confirmed=True,
            trusted_as_of=datetime(2026, 7, 28, 10, 0, 15, tzinfo=timezone.utc),
        )
    )

    assert "WAIT_QUOTE_DATA" in quote_row.statuses
    assert "WAIT_FEE_EVIDENCE" in quote_row.statuses
    assert "WAIT_20_PAPER_EXECUTION_DAYS" in paper_row.statuses
    assert "PAPER_EXECUTION_FEASIBILITY_ONLY" in paper_row.statuses


def test_fee_scope_is_visible_when_a_user_reported_scenario_is_reused_elsewhere() -> None:
    row = build_break_even_ledger_row(
        BreakEvenLedgerRequest(
            symbol="159570",
            entry_price=Decimal("1.400"),
            order_amount_cny=Decimal(10000),
            grid_spacing_bps=Decimal(100),
            fee_schedule=_minimum_fee_schedule(),
            execution_tier=ExecutionTier.OHLC_CONSERVATIVE,
            fee_scope_confirmed=False,
        )
    )

    assert "WAIT_FEE_SCOPE" in row.statuses


def test_quote_aware_requires_bound_quote_fields_and_declared_execution_haircuts() -> None:
    row = build_break_even_ledger_row(
        BreakEvenLedgerRequest(
            symbol="159567",
            entry_price=Decimal("1.400"),
            order_amount_cny=Decimal(10000),
            grid_spacing_bps=Decimal(100),
            fee_schedule=FeeSchedule(
                name="verified",
                commission_rate=Decimal(0),
                minimum_commission=Decimal(5),
                handling_fee_rate=Decimal(0),
                handling_fee_included_in_commission=True,
                provisional=False,
            ),
            execution_tier=ExecutionTier.QUOTE_AWARE,
            execution_evidence=ExecutionEvidence(quote_qualified=True, depth_available=True),
            fee_scope_confirmed=True,
            trusted_as_of=datetime(2026, 7, 28, 10, 0, 15, tzinfo=timezone.utc),
        )
    )

    assert "WAIT_QUOTE_DATA" in row.statuses
    assert row.admissible_to_backtest is False


def test_ledger_shows_the_aggregate_cost_of_splitting_total_capital_into_grids() -> None:
    row = build_break_even_ledger_row(
        BreakEvenLedgerRequest(
            symbol="159567",
            entry_price=Decimal("1.000"),
            order_amount_cny=Decimal(2000),
            total_capital_cny=Decimal(10000),
            planned_round_trip_count=5,
            grid_spacing_bps=Decimal(100),
            fee_schedule=_minimum_fee_schedule(),
            execution_tier=ExecutionTier.OHLC_CONSERVATIVE,
            fee_scope_confirmed=True,
        )
    )

    assert row.aggregate_declared_round_trip_cost_cny == Decimal("50.000")
    assert row.aggregate_declared_cost_bps_of_total_capital == Decimal("50.000")


def test_quote_timestamp_must_match_the_trusted_clock_and_declared_age() -> None:
    row = build_break_even_ledger_row(
        BreakEvenLedgerRequest(
            symbol="159567",
            entry_price=Decimal("1.400"),
            order_amount_cny=Decimal(10000),
            grid_spacing_bps=Decimal(100),
            fee_schedule=FeeSchedule(
                name="verified",
                commission_rate=Decimal(0),
                minimum_commission=Decimal(5),
                handling_fee_rate=Decimal(0),
                handling_fee_included_in_commission=True,
                provisional=False,
            ),
            execution_tier=ExecutionTier.QUOTE_AWARE,
            execution_evidence=_qualified_quote_evidence(quote_age_seconds=0),
            fee_scope_confirmed=True,
            trusted_as_of=datetime(2026, 7, 28, 10, 2, 30, tzinfo=timezone.utc),
        )
    )

    assert "WAIT_QUOTE_DATA" in row.statuses
    assert row.admissible_to_backtest is False
