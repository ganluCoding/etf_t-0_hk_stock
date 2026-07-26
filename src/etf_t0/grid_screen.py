"""Cross-sectional grid-suitability screening from native 5-minute ETF bars.

The output is a research ranking, not an executable fill simulation or trade signal.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from etf_t0.coarse_backtest import (
    ETF_TICK,
    EXPECTED_BAR_END_TIMES,
    LOT_SIZE,
    CostScenario,
    HypotheticalRoundTrip,
    performance_summary,
)
from etf_t0.universe import confirmed_t0_records, load_universe_ledger

MIN_MEDIAN_DAILY_TURNOVER_CNY = 100_000_000
MAX_MEDIAN_PATH_EFFICIENCY = 0.60
MIN_TWO_TICK_REVERSAL_RATE = 0.50
MIN_RANGE_TO_COST_MULTIPLE = 5.0


def _latest_complete_window(frame: pd.DataFrame, days: int = 30) -> pd.DataFrame:
    """Select the latest complete 48-bar sessions without filling missing data."""

    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], errors="raise")
    data = data.sort_values("timestamp").reset_index(drop=True)
    data["trade_date"] = data["timestamp"].dt.date.astype(str)
    price_columns = ["open", "high", "low", "close"]
    complete_dates = [
        trade_date
        for trade_date, group in data.groupby("trade_date", sort=True)
        if len(group) == 48
        and group["timestamp"].nunique() == 48
        and set(group["timestamp"].dt.strftime("%H:%M")) == set(EXPECTED_BAR_END_TIMES)
        and not (group["volume"] <= 0).any()
        and not (group[price_columns] <= 0).any(axis=1).any()
        and not group[price_columns].isna().any(axis=1).any()
        and not (
            (group["high"] < group[["open", "close", "low"]].max(axis=1))
            | (group["low"] > group[["open", "close", "high"]].min(axis=1))
        ).any()
    ]
    if len(complete_dates) < days:
        raise ValueError(f"requires {days} complete sessions; observed {len(complete_dates)}")
    selected = complete_dates[-days:]
    return data[data["trade_date"].isin(selected)].copy()


def microstructure_features(frame: pd.DataFrame) -> dict[str, float | None]:
    """Describe volatility, choppiness and short-horizon reversal evidence."""

    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], errors="raise")
    data["trade_date"] = data["timestamp"].dt.date.astype(str)
    data = data.sort_values("timestamp")
    previous_close = data.groupby("trade_date")["close"].shift(1)
    move = data["close"] - previous_close
    next_move = move.groupby(data["trade_date"]).shift(-1)
    two_tick_moves = move.abs() >= 2 * ETF_TICK
    eligible_reversals = two_tick_moves & next_move.notna() & (move != 0)
    reversal_rate = (
        float((move[eligible_reversals] * next_move[eligible_reversals] < 0).mean())
        if eligible_reversals.any()
        else None
    )

    daily_rows: list[dict[str, float]] = []
    for _, day in data.groupby("trade_date", sort=True):
        day_high = float(day["high"].max())
        day_low = float(day["low"].min())
        opening = float(day.iloc[0]["open"])
        closing = float(day.iloc[-1]["close"])
        price_range = day_high - day_low
        daily_rows.append(
            {
                "turnover": float(day["turnover"].sum()),
                "range_ticks": price_range / ETF_TICK,
                "range_pct": price_range / opening if opening > 0 else math.nan,
                "path_efficiency": abs(closing - opening) / price_range
                if price_range > 0
                else 1.0,
            }
        )
    daily = pd.DataFrame(daily_rows)
    valid_abs_moves = move.dropna().abs()
    return {
        "median_price_cny": float(data["close"].median()),
        "median_daily_turnover_cny": float(daily["turnover"].median()),
        "median_daily_range_ticks": float(daily["range_ticks"].median()),
        "median_daily_range_pct": float(daily["range_pct"].median()),
        "median_path_efficiency": float(daily["path_efficiency"].median()),
        "median_absolute_5m_move_bps": float(
            (valid_abs_moves / previous_close[valid_abs_moves.index] * 10_000).median()
        ),
        "two_tick_move_share": float(two_tick_moves.fillna(False).mean()),
        "next_bar_reversal_rate_after_two_tick_move": reversal_rate,
        "two_tick_reversal_observations": int(eligible_reversals.sum()),
    }


def cost_aware_grid_threshold(
    *,
    reference_price: float,
    layer_capital: float,
    commission_per_side: float = 5.0,
    adverse_ticks_round_trip: int = 1,
    safety_ticks: int = 1,
) -> dict[str, float | int]:
    """Convert provisional round-trip costs into a whole-tick trigger floor."""

    quantity = math.floor((layer_capital - commission_per_side) / reference_price / LOT_SIZE)
    quantity *= LOT_SIZE
    if quantity <= 0:
        raise ValueError("layer capital cannot fund one 100-unit ETF lot")
    commission_ticks = math.ceil(
        (2 * commission_per_side) / (quantity * ETF_TICK)
    )
    required_ticks = commission_ticks + adverse_ticks_round_trip + safety_ticks
    return {
        "quantity": quantity,
        "commission_coverage_ticks": commission_ticks,
        "adverse_ticks_round_trip": adverse_ticks_round_trip,
        "safety_ticks": safety_ticks,
        "required_grid_ticks": required_ticks,
        "required_grid_delta_cny": required_ticks * ETF_TICK,
        "required_grid_return_bps": required_ticks * ETF_TICK / reference_price * 10_000,
    }


def _assumed_round_trip_with_quantity(
    *,
    strategy: str,
    trade_date: str,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    entry_price: float,
    exit_price: float,
    quantity: int,
    costs: CostScenario,
    forced_exit: bool,
) -> HypotheticalRoundTrip:
    """Cost one assumed path while preserving its scenario-independent quantity."""

    gross_pnl = (exit_price - entry_price) * quantity
    explicit_fees = costs.commission_per_side * 2
    adverse_execution_cost = costs.adverse_ticks_round_trip * ETF_TICK * quantity
    return HypotheticalRoundTrip(
        strategy=strategy,
        trade_date=trade_date,
        entry_time=entry_time.isoformat(sep=" "),
        exit_time=exit_time.isoformat(sep=" "),
        assumed_entry_price=entry_price,
        assumed_exit_price=exit_price,
        quantity=quantity,
        gross_pnl=gross_pnl,
        explicit_fees=explicit_fees,
        adverse_execution_cost=adverse_execution_cost,
        net_pnl=gross_pnl - explicit_fees - adverse_execution_cost,
        forced_exit=forced_exit,
    )


def simulate_cost_aware_grid_proxy(
    frame: pd.DataFrame,
    *,
    layer_capital: float,
    max_round_trips_per_day: int,
    ema_span: int,
    costs: CostScenario,
    trigger_commission_per_side: float = 5.0,
    trigger_adverse_ticks_round_trip: int = 1,
    trigger_safety_ticks: int = 1,
) -> list[HypotheticalRoundTrip]:
    """Run one causal path policy whose entry gate is recomputed at execution time.

    A completed bar may schedule an entry check for the next bar open. At that
    open, the gate uses only the prior EMA anchor, the observable open print,
    fixed layer capital and 100-unit lot constraint. The trigger policy is held
    constant across cost scenarios so zero/base/stress results use comparable
    paths and quantities.
    """

    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], errors="raise")
    data = data.sort_values("timestamp").reset_index(drop=True)
    data["trade_date"] = data["timestamp"].dt.date.astype(str)
    paths: list[HypotheticalRoundTrip] = []
    for trade_date, day in data.groupby("trade_date", sort=True):
        day = day.reset_index(drop=True)
        day["ema"] = day["close"].ewm(span=ema_span, adjust=False).mean()
        position: dict[str, Any] | None = None
        pending_entry_anchor: float | None = None
        pending_exit = False
        completed = 0
        for bar_index, bar in day.iterrows():
            is_last_bar = bar_index == len(day) - 1
            assumed_open_time = bar["timestamp"] - pd.Timedelta(minutes=5)

            if pending_exit and position is not None:
                paths.append(
                    _assumed_round_trip_with_quantity(
                        strategy="exploratory_dynamic_cost_gate_ema_grid_proxy",
                        trade_date=trade_date,
                        entry_time=position["entry_time"],
                        exit_time=assumed_open_time,
                        entry_price=position["entry_price"],
                        exit_price=float(bar["open"]),
                        quantity=position["quantity"],
                        costs=costs,
                        forced_exit=False,
                    )
                )
                position = None
                pending_exit = False
                completed += 1

            if pending_entry_anchor is not None and position is None and not is_last_bar:
                threshold = cost_aware_grid_threshold(
                    reference_price=float(bar["open"]),
                    layer_capital=layer_capital,
                    commission_per_side=trigger_commission_per_side,
                    adverse_ticks_round_trip=trigger_adverse_ticks_round_trip,
                    safety_ticks=trigger_safety_ticks,
                )
                if (
                    pending_entry_anchor - float(bar["open"])
                    >= float(threshold["required_grid_delta_cny"])
                ):
                    position = {
                        "entry_time": assumed_open_time,
                        "entry_price": float(bar["open"]),
                        "quantity": int(threshold["quantity"]),
                        "entry_bar_index": bar_index,
                    }
                pending_entry_anchor = None
            elif is_last_bar:
                pending_entry_anchor = None

            if position is not None:
                if is_last_bar:
                    paths.append(
                        _assumed_round_trip_with_quantity(
                            strategy="exploratory_dynamic_cost_gate_ema_grid_proxy",
                            trade_date=trade_date,
                            entry_time=position["entry_time"],
                            exit_time=assumed_open_time,
                            entry_price=position["entry_price"],
                            exit_price=float(bar["open"]),
                            quantity=position["quantity"],
                            costs=costs,
                            forced_exit=True,
                        )
                    )
                    position = None
                    completed += 1
                elif (
                    bar_index > position["entry_bar_index"]
                    and float(bar["close"]) >= float(bar["ema"])
                ):
                    pending_exit = True
            elif (
                not is_last_bar
                and completed < max_round_trips_per_day
                and bar_index >= ema_span - 1
                and float(bar["close"]) < float(bar["ema"])
            ):
                pending_entry_anchor = float(bar["ema"])
    return paths


def daily_path_results(
    paths: list[HypotheticalRoundTrip], *, capital: float, trade_dates: list[str]
) -> list[dict[str, Any]]:
    """Return daily final P&L and realized drawdown after assumed path exits."""

    by_date: dict[str, list[HypotheticalRoundTrip]] = {date: [] for date in trade_dates}
    for path in paths:
        by_date.setdefault(path.trade_date, []).append(path)
    cumulative_pnl = 0.0
    rows: list[dict[str, Any]] = []
    for trade_date in trade_dates:
        day_paths = sorted(by_date.get(trade_date, []), key=lambda path: path.exit_time)
        start_equity = capital + cumulative_pnl
        equity = start_equity
        peak = start_equity
        maximum_drawdown_cny = 0.0
        for path in day_paths:
            equity += path.net_pnl
            peak = max(peak, equity)
            maximum_drawdown_cny = min(maximum_drawdown_cny, equity - peak)
        day_pnl = equity - start_equity
        cumulative_pnl += day_pnl
        rows.append(
            {
                "trade_date": trade_date,
                "hypothetical_round_trips": len(day_paths),
                "daily_final_net_pnl_cny": day_pnl,
                "daily_pnl_on_initial_total_capital": day_pnl / capital,
                "daily_closed_path_pnl_drawdown_cny": maximum_drawdown_cny,
                "daily_closed_path_pnl_drawdown_on_initial_total_capital": (
                    maximum_drawdown_cny / capital
                ),
                "ending_total_equity_cny": capital + cumulative_pnl,
            }
        )
    return rows


def classify_candidate(
    *,
    features: dict[str, float | None],
    threshold: dict[str, float | int],
    baseline_full_summary: dict[str, Any],
    baseline_later_summary: dict[str, Any],
    stress_full_summary: dict[str, Any],
    stress_later_summary: dict[str, Any],
) -> tuple[str, list[str]]:
    """Apply this-run fixed exploratory gates; passing means research priority only."""

    reversal_rate = features["next_bar_reversal_rate_after_two_tick_move"]
    checks = {
        "liquidity": features["median_daily_turnover_cny"]
        >= MIN_MEDIAN_DAILY_TURNOVER_CNY,
        "intraday_range": features["median_daily_range_ticks"]
        >= MIN_RANGE_TO_COST_MULTIPLE * threshold["required_grid_ticks"],
        "choppiness": features["median_path_efficiency"] <= MAX_MEDIAN_PATH_EFFICIENCY,
        "short_horizon_reversal": reversal_rate is not None
        and reversal_rate >= MIN_TWO_TICK_REVERSAL_RATE,
        "baseline_full_window_pnl": baseline_full_summary["net_pnl"] > 0,
        "baseline_later_segment_pnl": baseline_later_summary["net_pnl"] > 0,
        "stress_full_window_pnl": stress_full_summary["net_pnl"] > 0,
        "stress_later_segment_pnl": stress_later_summary["net_pnl"] > 0,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return ("candidate_for_next_stage" if not failed else "not_qualified", failed)


def screen_symbol(
    frame: pd.DataFrame,
    *,
    symbol: str,
    capital: float,
    layer_fraction: float = 0.25,
    max_round_trips_per_day: int = 4,
    ema_span: int = 12,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one fixed causal path policy under zero, baseline and stress costs."""

    window = _latest_complete_window(frame)
    trade_dates = sorted(window["trade_date"].unique())
    features = microstructure_features(window)
    layer_capital = capital * layer_fraction
    descriptive_reference_threshold = cost_aware_grid_threshold(
        reference_price=float(features["median_price_cny"]), layer_capital=layer_capital
    )
    scenarios = (
        CostScenario("zero_cost", commission_per_side=0.0, adverse_ticks_round_trip=0.0),
        CostScenario(
            "provisional_baseline_5_per_side_plus_1_tick",
            commission_per_side=5.0,
            adverse_ticks_round_trip=1.0,
        ),
        CostScenario(
            "provisional_stress_5_per_side_plus_2_ticks",
            commission_per_side=5.0,
            adverse_ticks_round_trip=2.0,
        ),
    )
    later_dates = trade_dates[-10:]
    scenario_results: dict[str, Any] = {}
    daily_rows: list[dict[str, Any]] = []
    scenario_paths: dict[str, list[HypotheticalRoundTrip]] = {}
    for costs in scenarios:
        paths = simulate_cost_aware_grid_proxy(
            window,
            layer_capital=layer_capital,
            max_round_trips_per_day=max_round_trips_per_day,
            ema_span=ema_span,
            costs=costs,
        )
        later_paths = [path for path in paths if path.trade_date in later_dates]
        scenario_paths[costs.name] = paths
        scenario_results[costs.name] = {
            "full_30_day_summary": performance_summary(
                paths, capital=capital, trade_dates=trade_dates
            ),
            "later_10_day_description_not_holdout": performance_summary(
                later_paths, capital=capital, trade_dates=later_dates
            ),
        }
        daily_rows.extend(
            {"cost_scenario": costs.name, **row}
            for row in daily_path_results(paths, capital=capital, trade_dates=trade_dates)
        )

    baseline = scenario_results["provisional_baseline_5_per_side_plus_1_tick"]
    stress = scenario_results["provisional_stress_5_per_side_plus_2_ticks"]
    classification, failed_gates = classify_candidate(
        features=features,
        threshold=descriptive_reference_threshold,
        baseline_full_summary=baseline["full_30_day_summary"],
        baseline_later_summary=baseline["later_10_day_description_not_holdout"],
        stress_full_summary=stress["full_30_day_summary"],
        stress_later_summary=stress["later_10_day_description_not_holdout"],
    )
    baseline_paths = scenario_paths["provisional_baseline_5_per_side_plus_1_tick"]
    path_trigger_ticks = sorted(
        {
            int(
                cost_aware_grid_threshold(
                    reference_price=path.assumed_entry_price,
                    layer_capital=layer_capital,
                )["required_grid_ticks"]
            )
            for path in baseline_paths
        }
    )
    report = {
        "symbol": symbol,
        "classification": classification,
        "failed_gates": failed_gates,
        "window": {"start": trade_dates[0], "end": trade_dates[-1], "days": 30},
        "features": features,
        "descriptive_median_price_threshold_not_used_by_signals": (
            descriptive_reference_threshold
        ),
        "fixed_proxy": {
            "description": "completed-bar EMA schedules a next-open check; that open print, fixed layer capital and 100-unit lot rule causally recompute the entry cost gate; exit is assumed at a later open after reversion",
            "ema_span_bars": ema_span,
            "maximum_round_trips_per_day": max_round_trips_per_day,
            "tactical_layer_fraction_of_total_capital": layer_fraction,
            "tactical_layer_capital_cny": layer_capital,
            "trigger_policy": {
                "commission_per_side_cny": 5.0,
                "adverse_ticks_round_trip": 1,
                "safety_ticks": 1,
                "observed_required_grid_tick_values_on_baseline_paths": path_trigger_ticks,
                "future_sample_statistics_used_for_signals": False,
            },
            "scenario_results": scenario_results,
            "drawdown_scope_warning": "summary maximum_drawdown uses day-end closed-path equity; daily CSV drawdown uses only cumulative realized P&L after assumed exits. Neither includes open-position mark-to-market loss.",
        },
        "evidence_status": "insufficient_evidence_for_profitability_or_live_use",
    }
    return report, daily_rows


def run_cross_sectional_screen(
    *, ledger_path: Path, workspace: Path, capital: float = 30_000
) -> dict[str, Any]:
    """Screen every confirmed symbol with a usable local 30-day dataset."""

    records = confirmed_t0_records(load_universe_ledger(ledger_path))
    results: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    for record in records:
        bars_path = workspace / "data" / "interim" / f"{record.code}_5m_latest" / "bars.csv"
        if not bars_path.exists():
            results.append(
                {
                    "symbol": record.code,
                    "trading_name": record.trading_name,
                    "classification": "needs_data",
                    "failed_gates": ["local_5m_dataset_missing"],
                }
            )
            continue
        try:
            symbol_report, symbol_daily = screen_symbol(
                pd.read_csv(bars_path), symbol=record.code, capital=capital
            )
        except ValueError as error:
            results.append(
                {
                    "symbol": record.code,
                    "trading_name": record.trading_name,
                    "classification": "needs_data",
                    "failed_gates": [str(error)],
                }
            )
            continue
        symbol_report["trading_name"] = record.trading_name
        results.append(symbol_report)
        daily_rows.extend(
            {"symbol": record.code, "trading_name": record.trading_name, **row}
            for row in symbol_daily
        )

    ranked = sorted(
        results,
        key=lambda item: (
            item["classification"] != "candidate_for_next_stage",
            -item.get("fixed_proxy", {})
            .get("scenario_results", {})
            .get("provisional_baseline_5_per_side_plus_1_tick", {})
            .get("full_30_day_summary", {})
            .get("net_pnl", -math.inf),
        ),
    )
    output = {
        "mode": "MVP derived cross-sectional calculation; no execution simulator",
        "capital_cny": capital,
        "scope": "exchange-evidenced records with local native 5-minute data",
        "stage_gates": {
            "G0_fee_acceptance": {
                "status": "BLOCKED",
                "reason": "cross-ETF broker scope and statement calibration are unverified",
            },
            "G2_data_acceptance": {
                "status": "BLOCKED",
                "reason": "5-minute coverage is only a sub-gate; cross-source and cross-market calendar checks remain incomplete",
            },
            "G3_execution_data_acceptance": {
                "status": "BLOCKED",
                "reason": "historical executable bid/ask, depth, queue and partial-fill data are unavailable",
            },
            "advancement": "G4-G8 prohibited while G0, G2 or G3 is blocked",
        },
        "fee_warning": "The user-reported CMB minimum CNY 5 per side is applied as a provisional cross-symbol screen only; written broker scope and statement calibration remain unverified.",
        "gate_definitions": {
            "minimum_median_daily_turnover_cny": MIN_MEDIAN_DAILY_TURNOVER_CNY,
            "minimum_daily_range_to_required_grid_multiple": MIN_RANGE_TO_COST_MULTIPLE,
            "maximum_median_path_efficiency": MAX_MEDIAN_PATH_EFFICIENCY,
            "minimum_next_bar_reversal_rate_after_two_tick_move": MIN_TWO_TICK_REVERSAL_RATE,
            "costed_pnl": "positive under both baseline and stress costs in full 30 dates and descriptive later 10 dates",
        },
        "cost_scenarios": {
            "zero_cost": "same causal paths with no explicit fee or adverse execution cost; decomposition only",
            "provisional_baseline_5_per_side_plus_1_tick": "CNY 5 each buy and sell plus one adverse tick per round trip",
            "provisional_stress_5_per_side_plus_2_ticks": "CNY 5 each buy and sell plus two adverse ticks per round trip",
        },
        "multiple_testing_warning": "Cross-sectional selection reuses the same 30-day exploration window. Passing is only a hypothesis priority and requires new out-of-sample data, live bid/ask and broker-calibrated costs.",
        "drawdown_scope_warning": "Reported summary drawdown uses day-end closed-path equity; daily CSV drawdown uses cumulative realized P&L after assumed exits. Open-position mark-to-market drawdown is unavailable from this output.",
        "results": ranked,
        "daily_result_path": "reports/generated/grid_screen/t0_etf_daily_results.csv",
        "evidence_status": "insufficient_evidence_for_profitability_or_live_use",
    }
    output_dir = workspace / "reports" / "generated" / "grid_screen"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "t0_etf_cross_section_latest.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pd.DataFrame(daily_rows).to_csv(output_dir / "t0_etf_daily_results.csv", index=False)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger", type=Path, default=Path("config/universe/t0_etf_ledger.json")
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--capital", type=float, default=30_000)
    args = parser.parse_args()
    print(
        json.dumps(
            run_cross_sectional_screen(
                ledger_path=args.ledger, workspace=args.workspace, capital=args.capital
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
