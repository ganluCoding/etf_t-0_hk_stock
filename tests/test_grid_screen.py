from dataclasses import replace

import pandas as pd
import pytest

from etf_t0.coarse_backtest import (
    EXPECTED_BAR_END_TIMES,
    CostScenario,
    HypotheticalRoundTrip,
)
from etf_t0.grid_screen import (
    _latest_complete_window,
    classify_candidate,
    cost_aware_grid_threshold,
    daily_path_results,
    microstructure_features,
    simulate_cost_aware_grid_proxy,
)


def test_cost_threshold_covers_commission_adverse_execution_and_margin() -> None:
    threshold = cost_aware_grid_threshold(reference_price=1.0, layer_capital=7_500)

    assert threshold["quantity"] == 7_400
    assert threshold["commission_coverage_ticks"] == 2
    assert threshold["required_grid_ticks"] == 4


def test_daily_results_include_zero_trade_days_and_closed_path_drawdown() -> None:
    template = HypotheticalRoundTrip(
        strategy="test",
        trade_date="2026-07-23",
        entry_time="2026-07-23 10:00:00",
        exit_time="2026-07-23 10:05:00",
        assumed_entry_price=1.0,
        assumed_exit_price=1.0,
        quantity=100,
        gross_pnl=0.0,
        explicit_fees=10.0,
        adverse_execution_cost=0.0,
        net_pnl=-10.0,
        forced_exit=False,
    )
    paths = [template, replace(template, exit_time="2026-07-23 10:10:00", net_pnl=15.0)]

    rows = daily_path_results(
        paths, capital=30_000, trade_dates=["2026-07-23", "2026-07-24"]
    )

    assert rows[0]["daily_final_net_pnl_cny"] == 5.0
    assert rows[0]["daily_closed_path_pnl_drawdown_cny"] == -10.0
    assert rows[1]["daily_final_net_pnl_cny"] == 0.0
    assert rows[1]["ending_total_equity_cny"] == 30_005.0


def test_candidate_classification_requires_baseline_and_stress_gates() -> None:
    features = {
        "median_daily_turnover_cny": 200_000_000.0,
        "median_daily_range_ticks": 30.0,
        "median_path_efficiency": 0.4,
        "next_bar_reversal_rate_after_two_tick_move": 0.55,
    }
    threshold = {"required_grid_ticks": 4}
    full = {"net_pnl": 100.0}
    later = {"net_pnl": 20.0}

    classification, failed = classify_candidate(
        features=features,
        threshold=threshold,
        baseline_full_summary=full,
        baseline_later_summary=later,
        stress_full_summary=full,
        stress_later_summary=later,
    )
    rejected, rejected_gates = classify_candidate(
        features={**features, "next_bar_reversal_rate_after_two_tick_move": 0.49},
        threshold=threshold,
        baseline_full_summary=full,
        baseline_later_summary=later,
        stress_full_summary=full,
        stress_later_summary=later,
    )

    assert (classification, failed) == ("candidate_for_next_stage", [])
    assert rejected == "not_qualified"
    assert rejected_gates == ["short_horizon_reversal"]


def test_flat_next_bar_counts_as_not_reversing() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-07-24 09:35", "2026-07-24 09:40", "2026-07-24 09:45"]
            ),
            "open": [1.0, 1.0, 0.998],
            "high": [1.0, 1.0, 0.998],
            "low": [1.0, 0.998, 0.998],
            "close": [1.0, 0.998, 0.998],
            "volume": [100, 100, 100],
            "turnover": [100.0, 100.0, 100.0],
        }
    )

    features = microstructure_features(frame)

    assert features["two_tick_reversal_observations"] == 1
    assert features["next_bar_reversal_rate_after_two_tick_move"] == 0.0


def test_complete_window_rejects_non_core_bar_substituted_for_core_bar() -> None:
    timestamps = pd.to_datetime(
        [f"2026-07-24 {clock}" for clock in EXPECTED_BAR_END_TIMES]
    )
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 100,
            "turnover": 100.0,
        }
    )
    _latest_complete_window(frame, days=1)
    frame.loc[0, "timestamp"] = pd.Timestamp("2026-07-24 12:00")

    with pytest.raises(ValueError, match="observed 0"):
        _latest_complete_window(frame, days=1)


def test_dynamic_entry_gate_is_causal_and_paths_match_across_cost_scenarios() -> None:
    timestamps = pd.to_datetime(
        [f"2026-07-24 {clock}" for clock in EXPECTED_BAR_END_TIMES]
    )
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [1.0, 1.0, 0.992, 1.0] + [1.0] * 44,
            "high": [1.0, 1.0, 1.0, 1.0] + [1.0] * 44,
            "low": [1.0, 0.995, 0.992, 1.0] + [1.0] * 44,
            "close": [1.0, 0.995, 1.0, 1.0] + [1.0] * 44,
            "volume": 100,
            "turnover": 100.0,
        }
    )
    zero_paths = simulate_cost_aware_grid_proxy(
        frame,
        layer_capital=7_500,
        max_round_trips_per_day=4,
        ema_span=2,
        costs=CostScenario("zero", 0.0, 0.0),
    )
    stress_paths = simulate_cost_aware_grid_proxy(
        frame,
        layer_capital=7_500,
        max_round_trips_per_day=4,
        ema_span=2,
        costs=CostScenario("stress", 5.0, 2.0),
    )

    assert len(zero_paths) == len(stress_paths) == 1
    assert zero_paths[0].entry_time == stress_paths[0].entry_time
    assert zero_paths[0].quantity == stress_paths[0].quantity
    prior_ema = frame.loc[:1, "close"].ewm(span=2, adjust=False).mean().iloc[-1]
    threshold = cost_aware_grid_threshold(
        reference_price=zero_paths[0].assumed_entry_price, layer_capital=7_500
    )
    assert prior_ema - zero_paths[0].assumed_entry_price >= threshold["required_grid_delta_cny"]
