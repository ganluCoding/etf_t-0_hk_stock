from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from etf_t0.coarse_backtest import EXPECTED_BAR_END_TIMES
from etf_t0.multi_strategy import (
    _base_inventory_account_comparison,
    _candidate_status,
    _dvc_pointer_md5,
    _enforce_stress_cash_ledger,
    _simulate_single_symbol_family,
    load_research_config,
)


def _one_complete_day(closes: list[float]) -> pd.DataFrame:
    timestamps = pd.to_datetime([f"2026-07-24 {clock}" for clock in EXPECTED_BAR_END_TIMES])
    assert len(closes) == len(timestamps)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": closes,
            "high": [value + 0.001 for value in closes],
            "low": [value - 0.001 for value in closes],
            "close": closes,
            "volume": 100,
            "turnover": 100.0,
        }
    )


def test_registry_rejects_duplicate_hypothesis_ids(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        json.dumps(
            {
                "descriptive_split_days": [20, 10],
                "hypotheses": [{"id": "same"}, {"id": "same"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate"):
        load_research_config(config_path)


def test_committed_registry_keeps_all_three_cost_scenarios() -> None:
    config = load_research_config(Path("config/strategy-backtester_config.yaml"))

    assert {item["name"] for item in config["cost_scenarios"]} == {
        "zero_cost",
        "provisional_baseline_5_per_side_plus_1_tick",
        "provisional_stress_5_per_side_plus_2_ticks",
    }
    assert len(config["hypotheses"]) == 13


def test_dvc_pointer_checksum_is_available_for_lineage(tmp_path: Path) -> None:
    pointer = tmp_path / "sample.dvc"
    pointer.write_text("outs:\n- md5: abc123.dir\n  path: sample\n", encoding="utf-8")

    assert _dvc_pointer_md5(pointer) == "abc123.dir"
    assert _dvc_pointer_md5(tmp_path / "missing.dvc") is None


def test_opening_breakout_signal_executes_at_next_bar_open() -> None:
    closes = [1.0] * 6 + [1.006, 1.007, 1.008, 1.009] + [1.009] * 38
    frame = _one_complete_day(closes)
    hypothesis = {
        "id": "breakout",
        "family": "opening_range_breakout",
        "tactical_capital_cny": 15_000,
        "opening_range_bars": 6,
        "exit_ema_span": 6,
        "maximum_holding_bars": 2,
        "safety_ticks": 1,
        "max_round_trips_per_day": 1,
    }

    paths = _simulate_single_symbol_family(frame, hypothesis=hypothesis)

    assert len(paths) == 1
    signal_bar_close = frame.iloc[6]["timestamp"]
    assert pd.Timestamp(paths[0].entry_time) == signal_bar_close
    assert pd.Timestamp(paths[0].exit_time) - pd.Timestamp(paths[0].entry_time) == pd.Timedelta(
        minutes=10
    )


def test_pullback_does_not_enter_if_next_open_gaps_above_anchor() -> None:
    closes = [1 + index * 0.001 for index in range(48)]
    frame = _one_complete_day(closes)
    frame.loc[20, "close"] = frame.loc[20, "close"] - 0.003
    frame.loc[21, ["open", "high", "low", "close"]] = [1.10, 1.101, 1.099, 1.10]
    hypothesis = {
        "id": "pullback",
        "family": "trend_filtered_pullback",
        "tactical_capital_cny": 15_000,
        "fast_ema_span": 6,
        "slow_ema_span": 18,
        "slow_slope_bars": 3,
        "maximum_holding_bars": 6,
        "safety_ticks": 1,
        "max_round_trips_per_day": 1,
    }

    paths = _simulate_single_symbol_family(frame, hypothesis=hypothesis)

    assert all(path.entry_price < 1.10 for path in paths)


def test_candidate_status_keeps_sample_positive_separate_from_priority() -> None:
    def summary(net_pnl: float, paths: int = 5) -> dict[str, float | int]:
        return {"net_pnl": net_pnl, "hypothetical_path_count": paths}

    scenarios = {
        "provisional_baseline_5_per_side_plus_1_tick": {
            "full_30_day": summary(100),
            "earlier_20_day_not_holdout": summary(-10),
            "later_10_day_not_holdout": summary(110),
        },
        "provisional_stress_5_per_side_plus_2_ticks": {
            "full_30_day": summary(50),
            "earlier_20_day_not_holdout": summary(0),
            "later_10_day_not_holdout": summary(0),
        },
    }

    status, failed = _candidate_status(scenarios)

    assert status == "sample_positive_not_stable"
    assert failed == [
        "baseline_earlier_positive",
        "stress_earlier_positive",
        "stress_later_positive",
    ]


def test_priority_requires_stress_cost_to_be_positive_in_both_segments() -> None:
    def summary(net_pnl: float, paths: int = 5) -> dict[str, float | int]:
        return {"net_pnl": net_pnl, "hypothetical_path_count": paths}

    scenarios = {
        "provisional_baseline_5_per_side_plus_1_tick": {
            "full_30_day": summary(100),
            "earlier_20_day_not_holdout": summary(40),
            "later_10_day_not_holdout": summary(60),
        },
        "provisional_stress_5_per_side_plus_2_ticks": {
            "full_30_day": summary(20),
            "earlier_20_day_not_holdout": summary(-1),
            "later_10_day_not_holdout": summary(21),
        },
    }

    status, failed = _candidate_status(scenarios)

    assert status == "sample_positive_not_stable"
    assert failed == ["stress_earlier_positive"]


def test_base_inventory_comparison_separates_passive_and_tactical_pnl() -> None:
    frame = _one_complete_day([1.0] * 47 + [1.1])
    frame["trade_date"] = "2026-07-24"

    comparison = _base_inventory_account_comparison(
        frame, paths=[], capital=30_000, base_inventory_fraction=0.5
    )

    assert comparison["base_inventory_quantity"] == 15_000
    assert comparison["passive_base_inventory_pnl"] == pytest.approx(1_500)
    assert comparison["tactical_incremental_net_pnl"] == 0
    assert comparison["descriptive_total_account_pnl_close_marked_not_executable"] == pytest.approx(
        1_500
    )


def test_stress_cash_ledger_resizes_after_prior_loss() -> None:
    from etf_t0.coarse_backtest import CostScenario
    from etf_t0.multi_strategy import AssumedPricePath

    paths = [
        AssumedPricePath(
            strategy="test",
            trade_date=f"2026-07-{day:02d}",
            entry_time=f"2026-07-{day:02d} 10:00:00",
            exit_time=f"2026-07-{day:02d} 10:05:00",
            entry_price=1.0,
            exit_price=0.99,
            quantity=14_900,
            forced_exit=False,
        )
        for day in (21, 22)
    ]

    adjusted, ledger = _enforce_stress_cash_ledger(
        paths,
        tactical_capital=15_000,
        stress_costs=CostScenario("stress", 5.0, 2.0),
    )

    assert adjusted[0].quantity == 14_900
    assert adjusted[1].quantity < adjusted[0].quantity
    assert adjusted[1].entry_price * adjusted[1].quantity + 5 <= (
        15_000
        + (adjusted[0].exit_price - adjusted[0].entry_price) * adjusted[0].quantity
        - 10
        - 2 * 0.001 * adjusted[0].quantity
    )
    assert ledger["resized_path_count"] == 1
