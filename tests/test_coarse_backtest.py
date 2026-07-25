from datetime import datetime, timedelta

import pandas as pd

from etf_t0.coarse_backtest import (
    CostScenario,
    performance_summary,
    run_mvp_report,
    simulate_causal_mean_reversion,
    simulate_mechanical_round_trips,
)


def _session(prices: list[tuple[float, float]], date: str = "2026-07-24") -> pd.DataFrame:
    start = datetime.fromisoformat(f"{date} 09:35:00")
    return pd.DataFrame(
        {
            "timestamp": [start + timedelta(minutes=5 * index) for index in range(len(prices))],
            "open": [item[0] for item in prices],
            "close": [item[1] for item in prices],
        }
    )


def _complete_session(date: str) -> pd.DataFrame:
    morning = pd.date_range(f"{date} 09:35:00", f"{date} 11:30:00", freq="5min")
    afternoon = pd.date_range(f"{date} 13:05:00", f"{date} 15:00:00", freq="5min")
    timestamps = morning.append(afternoon)
    return pd.DataFrame({"timestamp": timestamps, "open": 1.0, "close": 1.0})


def test_mechanical_baseline_completes_declared_round_trips_and_costs_each_one() -> None:
    frame = _session([(1.0, 1.001)] * 20)
    costs = CostScenario("commission", commission_per_side=5, adverse_ticks_round_trip=0)

    trades = simulate_mechanical_round_trips(
        frame, capital=30_000, round_trips_per_day=20, costs=costs
    )
    summary = performance_summary(trades, capital=30_000, trade_dates=["2026-07-24"])

    assert len(trades) == 20
    assert summary["explicit_fees"] == 200
    assert summary["hypothetical_path_count"] == 20
    assert all(trade.quantity == 29_900 for trade in trades)


def test_execution_stress_applies_declared_round_trip_ticks() -> None:
    frame = _session([(1.0, 1.001)])
    costs = CostScenario("stress", commission_per_side=5, adverse_ticks_round_trip=1)

    trade = simulate_mechanical_round_trips(
        frame, capital=30_000, round_trips_per_day=1, costs=costs
    )[0]

    assert round(trade.gross_pnl, 6) == 29.9
    assert round(trade.adverse_execution_cost, 6) == 29.9
    assert round(trade.net_pnl, 6) == -10


def test_drawdown_includes_initial_capital_peak() -> None:
    frame = _session([(1.0, 0.999)])
    costs = CostScenario("zero", commission_per_side=0, adverse_ticks_round_trip=0)
    paths = simulate_mechanical_round_trips(
        frame, capital=30_000, round_trips_per_day=1, costs=costs
    )

    summary = performance_summary(paths, capital=30_000, trade_dates=["2026-07-24"])

    assert summary["maximum_drawdown"] < 0


def test_mean_reversion_signal_executes_only_at_next_bar_open() -> None:
    frame = _session(
        [
            (1.000, 1.000),
            (1.000, 1.000),
            (1.000, 0.990),
            (0.991, 0.995),
            (0.996, 1.001),
            (1.002, 1.002),
        ]
    )
    costs = CostScenario("zero", commission_per_side=0, adverse_ticks_round_trip=0)

    trades = simulate_causal_mean_reversion(
        frame,
        capital=30_000,
        max_round_trips_per_day=20,
        ema_span=3,
        entry_deviation_bps=20,
        costs=costs,
    )

    assert len(trades) == 1
    assert trades[0].entry_time.endswith("09:45:00")
    assert trades[0].assumed_entry_price == 0.991
    assert trades[0].exit_time.endswith("09:55:00")
    assert trades[0].assumed_exit_price == 1.002


def test_mean_reversion_forces_final_close_without_overnight_inventory() -> None:
    frame = _session(
        [
            (1.000, 1.000),
            (1.000, 1.000),
            (1.000, 0.990),
            (0.991, 0.989),
            (0.989, 0.988),
        ]
    )
    costs = CostScenario("zero", commission_per_side=0, adverse_ticks_round_trip=0)

    trades = simulate_causal_mean_reversion(
        frame,
        capital=30_000,
        max_round_trips_per_day=20,
        ema_span=3,
        entry_deviation_bps=20,
        costs=costs,
    )

    assert len(trades) == 1
    assert trades[0].forced_exit is True
    assert trades[0].assumed_exit_price == 0.989
    assert trades[0].exit_time.endswith("09:50:00")


def test_30_day_split_is_explicitly_descriptive_not_out_of_sample() -> None:
    dates = pd.bdate_range("2026-06-01", periods=30)
    frame = pd.concat(
        [_complete_session(date.strftime("%Y-%m-%d")) for date in dates],
        ignore_index=True,
    )

    result = run_mvp_report(frame)
    split = result["chronological_descriptive_split_not_validation"]

    assert "warning" in split
    assert "not holdout or out-of-sample" in split["warning"]
    assert "chronological_split" not in result
    assert all(
        metrics["assumed_fill_only"]
        for scenario in result["results"].values()
        for metrics in scenario.values()
    )


def test_report_rejects_incomplete_latest_session() -> None:
    dates = pd.bdate_range("2026-06-01", periods=30)
    sessions = [_complete_session(date.strftime("%Y-%m-%d")) for date in dates]
    sessions[-1] = sessions[-1].iloc[:-1]

    with pd.option_context("mode.chained_assignment", None):
        frame = pd.concat(sessions, ignore_index=True)

    try:
        run_mvp_report(frame)
    except ValueError as error:
        assert "incomplete 5-minute sessions" in str(error)
    else:
        raise AssertionError("incomplete latest session must fail closed")
