"""Conservative MVP screening with native 5-minute ETF bars.

This module is intentionally a coarse screening tool. It does not infer bid/ask
fills, queue priority, or an intrabar limit-order path from OHLC bars.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, stdev
from typing import Any

import pandas as pd

ETF_TICK = 0.001
LOT_SIZE = 100
EXPECTED_BAR_END_TIMES = tuple(
    [f"09:{minute:02d}" for minute in range(35, 60, 5)]
    + [f"10:{minute:02d}" for minute in range(0, 60, 5)]
    + [f"11:{minute:02d}" for minute in range(0, 31, 5)]
    + [f"13:{minute:02d}" for minute in range(5, 60, 5)]
    + [f"14:{minute:02d}" for minute in range(0, 60, 5)]
    + ["15:00"]
)


@dataclass(frozen=True)
class CostScenario:
    """Declared round-trip costs applied independently to every assumed path."""

    name: str
    commission_per_side: float
    adverse_ticks_round_trip: float


@dataclass(frozen=True)
class HypotheticalRoundTrip:
    """An assumed bar-price path, not evidence that an order would fill."""

    strategy: str
    trade_date: str
    entry_time: str
    exit_time: str
    assumed_entry_price: float
    assumed_exit_price: float
    quantity: int
    gross_pnl: float
    explicit_fees: float
    adverse_execution_cost: float
    net_pnl: float
    forced_exit: bool
    assumed_fill: bool = True


def _quantity_for_capital(capital: float, entry_price: float, buy_fee: float) -> int:
    """Return the largest valid 100-unit lot whose entry cash flow fits capital."""

    if capital <= buy_fee or entry_price <= 0:
        return 0
    return math.floor((capital - buy_fee) / entry_price / LOT_SIZE) * LOT_SIZE


def _assume_round_trip(
    *,
    strategy: str,
    trade_date: str,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    entry_price: float,
    exit_price: float,
    capital: float,
    costs: CostScenario,
    forced_exit: bool,
) -> HypotheticalRoundTrip:
    quantity = _quantity_for_capital(capital, entry_price, costs.commission_per_side)
    if quantity <= 0:
        raise ValueError("capital cannot fund one valid ETF lot")
    gross_pnl = (exit_price - entry_price) * quantity
    explicit_fees = costs.commission_per_side * 2
    adverse_execution_cost = costs.adverse_ticks_round_trip * ETF_TICK * quantity
    net_pnl = gross_pnl - explicit_fees - adverse_execution_cost
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
        net_pnl=net_pnl,
        forced_exit=forced_exit,
    )


def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "open", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")
    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], errors="raise")
    data = data.sort_values("timestamp").reset_index(drop=True)
    data["trade_date"] = data["timestamp"].dt.date.astype(str)
    return data


def _validate_complete_5m_sessions(data: pd.DataFrame, dates: list[str]) -> None:
    """Fail closed unless every selected date has the expected 48 unique bars."""

    selected = data[data["trade_date"].isin(dates)].copy()
    duplicate_count = int(selected["timestamp"].duplicated().sum())
    if duplicate_count:
        raise ValueError(f"selected dates contain {duplicate_count} duplicate timestamps")
    expected = set(EXPECTED_BAR_END_TIMES)
    problems: list[str] = []
    for trade_date in dates:
        day = selected[selected["trade_date"] == trade_date]
        observed = set(day["timestamp"].dt.strftime("%H:%M"))
        if len(day) != 48 or observed != expected:
            problems.append(
                f"{trade_date}: bars={len(day)}, missing={sorted(expected - observed)}, "
                f"unexpected={sorted(observed - expected)}"
            )
    if problems:
        raise ValueError("incomplete 5-minute sessions: " + "; ".join(problems))


def simulate_mechanical_round_trips(
    frame: pd.DataFrame,
    *,
    capital: float,
    round_trips_per_day: int,
    costs: CostScenario,
) -> list[HypotheticalRoundTrip]:
    """Assume entry at selected bar opens and exit at closes, with no signal.

    Bars are selected deterministically and evenly across each session. Open occurs
    before close, so this baseline does not invent an intrabar price sequence.
    """

    data = _prepare(frame)
    if round_trips_per_day <= 0:
        raise ValueError("round_trips_per_day must be positive")
    paths: list[HypotheticalRoundTrip] = []
    account_equity = capital
    for trade_date, day in data.groupby("trade_date", sort=True):
        day = day.reset_index(drop=True)
        if len(day) < round_trips_per_day:
            raise ValueError(f"{trade_date} does not contain enough bars")
        if round_trips_per_day == 1:
            selected = [0]
        else:
            selected = [
                round(index * (len(day) - 1) / (round_trips_per_day - 1))
                for index in range(round_trips_per_day)
            ]
        for bar_index in selected:
            bar = day.iloc[bar_index]
            usable_capital = min(capital, account_equity)
            if _quantity_for_capital(
                usable_capital, float(bar["open"]), costs.commission_per_side
            ) <= 0:
                return paths
            path = _assume_round_trip(
                strategy="mechanical_evenly_spaced",
                trade_date=trade_date,
                entry_time=bar["timestamp"] - pd.Timedelta(minutes=5),
                exit_time=bar["timestamp"],
                entry_price=float(bar["open"]),
                exit_price=float(bar["close"]),
                capital=usable_capital,
                costs=costs,
                forced_exit=False,
            )
            paths.append(path)
            account_equity += path.net_pnl
    return paths


def simulate_causal_mean_reversion(
    frame: pd.DataFrame,
    *,
    capital: float,
    max_round_trips_per_day: int,
    ema_span: int,
    entry_deviation_bps: float,
    costs: CostScenario,
) -> list[HypotheticalRoundTrip]:
    """Single-position causal EMA mean-reversion screening baseline.

    A signal is evaluated only after a bar closes and executes at the next bar open.
    An open position exits at the next open after a close at or above the causal EMA.
    Any remaining position is assumed to exit at the final bar's open print; no
    inventory is carried in the calculation. This is not proof of an executable fill.
    """

    data = _prepare(frame)
    if ema_span < 2:
        raise ValueError("ema_span must be at least 2")
    if entry_deviation_bps <= 0:
        raise ValueError("entry_deviation_bps must be positive")
    threshold = entry_deviation_bps / 10_000
    paths: list[HypotheticalRoundTrip] = []
    account_equity = capital
    for trade_date, day in data.groupby("trade_date", sort=True):
        day = day.reset_index(drop=True)
        day["ema"] = day["close"].ewm(span=ema_span, adjust=False).mean()
        position: dict[str, Any] | None = None
        pending_entry = False
        pending_exit = False
        completed = 0
        for bar_index, bar in day.iterrows():
            is_last_bar = bar_index == len(day) - 1
            if pending_exit and position is not None:
                path = _assume_round_trip(
                strategy="exploratory_causal_ema_mean_reversion_without_risk_controls",
                    trade_date=trade_date,
                    entry_time=position["entry_time"],
                    exit_time=bar["timestamp"] - pd.Timedelta(minutes=5),
                    entry_price=position["entry_price"],
                    exit_price=float(bar["open"]),
                    capital=min(capital, account_equity),
                    costs=costs,
                    forced_exit=False,
                )
                paths.append(path)
                account_equity += path.net_pnl
                completed += 1
                position = None
                pending_exit = False
            if pending_entry and position is None and not is_last_bar:
                usable_capital = min(capital, account_equity)
                if _quantity_for_capital(
                    usable_capital, float(bar["open"]), costs.commission_per_side
                ) <= 0:
                    return paths
                position = {
                    "entry_time": bar["timestamp"] - pd.Timedelta(minutes=5),
                    "entry_price": float(bar["open"]),
                    "entry_bar_index": bar_index,
                }
                pending_entry = False
            elif pending_entry and is_last_bar:
                pending_entry = False

            if position is not None:
                if is_last_bar:
                    path = _assume_round_trip(
                    strategy="exploratory_causal_ema_mean_reversion_without_risk_controls",
                        trade_date=trade_date,
                        entry_time=position["entry_time"],
                        exit_time=bar["timestamp"] - pd.Timedelta(minutes=5),
                        entry_price=position["entry_price"],
                        exit_price=float(bar["open"]),
                        capital=min(capital, account_equity),
                        costs=costs,
                        forced_exit=True,
                    )
                    paths.append(path)
                    account_equity += path.net_pnl
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
                and float(bar["close"]) <= float(bar["ema"]) * (1 - threshold)
            ):
                pending_entry = True
    return paths


def performance_summary(
    paths: list[HypotheticalRoundTrip], *, capital: float, trade_dates: list[str]
) -> dict[str, Any]:
    """Summarize assumed price paths without claiming executable fills."""

    if not trade_dates:
        raise ValueError("trade_dates cannot be empty")
    path_frame = pd.DataFrame(asdict(path) for path in paths)
    if path_frame.empty:
        daily = pd.Series(0.0, index=trade_dates, dtype=float)
        gross_profit = gross_loss = 0.0
    else:
        daily = (
            path_frame.groupby("trade_date")["net_pnl"]
            .sum()
            .reindex(trade_dates, fill_value=0.0)
        )
        gross_profit = float(path_frame.loc[path_frame["net_pnl"] > 0, "net_pnl"].sum())
        gross_loss = float(-path_frame.loc[path_frame["net_pnl"] < 0, "net_pnl"].sum())
    equity = capital + daily.cumsum()
    equity_with_initial = pd.concat(
        [pd.Series([capital], index=["initial"], dtype=float), equity]
    )
    drawdown = equity_with_initial / equity_with_initial.cummax() - 1
    daily_values = [float(value) for value in daily]
    standard_error = (
        stdev(daily_values) / math.sqrt(len(daily_values)) if len(daily_values) > 1 else 0.0
    )
    average_daily = mean(daily_values)
    return {
        "hypothetical_path_count": len(paths),
        "average_hypothetical_paths_per_day": len(paths) / len(trade_dates),
        "maximum_hypothetical_paths_on_one_day": (
            int(path_frame.groupby("trade_date").size().max()) if not path_frame.empty else 0
        ),
        "positive_hypothetical_path_rate": (
            float((path_frame["net_pnl"] > 0).mean()) if not path_frame.empty else None
        ),
        "positive_hypothetical_day_rate": float((daily > 0).mean()),
        "gross_pnl": float(path_frame["gross_pnl"].sum()) if not path_frame.empty else 0.0,
        "explicit_fees": (
            float(path_frame["explicit_fees"].sum()) if not path_frame.empty else 0.0
        ),
        "adverse_execution_cost": (
            float(path_frame["adverse_execution_cost"].sum())
            if not path_frame.empty
            else 0.0
        ),
        "net_pnl": float(daily.sum()),
        "ending_equity": float(equity.iloc[-1]),
        "minimum_equity": float(equity.min()),
        "capital_depleted": bool((equity <= 0).any()),
        "sample_return_on_recycled_capital": float(daily.sum() / capital),
        "average_daily_pnl": average_daily,
        "worst_day_pnl": float(daily.min()),
        "maximum_drawdown": float(drawdown.min()),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "daily_pnl_t_stat": average_daily / standard_error if standard_error > 0 else None,
        "daily_mean_95pct_normal_interval": [
            average_daily - 1.96 * standard_error,
            average_daily + 1.96 * standard_error,
        ],
        "forced_exit_count": (
            int(path_frame["forced_exit"].sum()) if not path_frame.empty else 0
        ),
        "assumed_fill_only": True,
    }


def run_mvp_report(
    frame: pd.DataFrame,
    *,
    capital: float = 30_000,
    round_trip_limit: int = 20,
    ema_span: int = 12,
    entry_deviation_bps: float = 30,
) -> dict[str, Any]:
    """Run predeclared scenarios on the latest 30 complete dates."""

    data = _prepare(frame)
    all_dates = sorted(data["trade_date"].unique())
    if len(all_dates) < 30:
        raise ValueError("MVP report requires at least 30 trade dates")
    selected_dates = all_dates[-30:]
    _validate_complete_5m_sessions(data, selected_dates)
    data = data[data["trade_date"].isin(selected_dates)].copy()
    scenarios = (
        CostScenario("zero_cost", 0.0, 0.0),
        CostScenario("commission_floor", 5.0, 0.0),
        CostScenario("commission_plus_1_tick_round_trip", 5.0, 1.0),
        CostScenario("commission_plus_2_ticks_round_trip", 5.0, 2.0),
    )
    results: dict[str, Any] = {}
    for scenario in scenarios:
        mechanical = simulate_mechanical_round_trips(
            data,
            capital=capital,
            round_trips_per_day=round_trip_limit,
            costs=scenario,
        )
        mean_reversion = simulate_causal_mean_reversion(
            data,
            capital=capital,
            max_round_trips_per_day=round_trip_limit,
            ema_span=ema_span,
            entry_deviation_bps=entry_deviation_bps,
            costs=scenario,
        )
        results[scenario.name] = {
            "mechanical": performance_summary(
                mechanical, capital=capital, trade_dates=selected_dates
            ),
            "exploratory_causal_mean_reversion_without_risk_controls": performance_summary(
                mean_reversion, capital=capital, trade_dates=selected_dates
            ),
        }
    later_dates = selected_dates[-10:]
    earlier_dates = selected_dates[:-10]
    baseline_costs = scenarios[1]
    earlier_data = data[data["trade_date"].isin(earlier_dates)]
    later_data = data[data["trade_date"].isin(later_dates)]
    earlier_segment_paths = simulate_causal_mean_reversion(
        earlier_data,
        capital=capital,
        max_round_trips_per_day=round_trip_limit,
        ema_span=ema_span,
        entry_deviation_bps=entry_deviation_bps,
        costs=baseline_costs,
    )
    later_segment_paths = simulate_causal_mean_reversion(
        later_data,
        capital=capital,
        max_round_trips_per_day=round_trip_limit,
        ema_span=ema_span,
        entry_deviation_bps=entry_deviation_bps,
        costs=baseline_costs,
    )
    sensitivity: dict[str, Any] = {}
    for sensitivity_span in (6, 12, 18):
        for sensitivity_deviation in (20.0, 30.0, 40.0):
            key = f"ema_{sensitivity_span}_entry_{int(sensitivity_deviation)}bps"
            sensitivity_paths = simulate_causal_mean_reversion(
                data,
                capital=capital,
                max_round_trips_per_day=round_trip_limit,
                ema_span=sensitivity_span,
                entry_deviation_bps=sensitivity_deviation,
                costs=baseline_costs,
            )
            sensitivity[key] = performance_summary(
                sensitivity_paths, capital=capital, trade_dates=selected_dates
            )
    return {
        "mode": "MVP derived calculation; no dedicated execution simulator",
        "symbol": "159567",
        "window": {"start": selected_dates[0], "end": selected_dates[-1], "days": 30},
        "capital": capital,
        "round_trip_limit_per_day": round_trip_limit,
        "assumptions": {
            "fill_status": "Every entry and exit is an assumed OHLC price path; no executable fill is claimed.",
            "mechanical_schedule": f"{round_trip_limit} deterministic bars evenly spaced across each session; assume entry at the bar open print and exit at the same bar close print",
            "exploratory_causal_mean_reversion_without_risk_controls": {
                "ema_span_bars": ema_span,
                "entry_deviation_bps": entry_deviation_bps,
                "signal_execution": "completed-bar signal executes at next bar open",
                "exit": "assume exit at the next open print after close reaches causal EMA; otherwise assume exit at the final bar open print",
            },
            "positioning": "one full-capital long position at a time; no overnight inventory; not a position recommendation",
            "risk_controls": "intentionally omitted for this denial-oriented pressure calculation; this is not the PRD strategy baseline and cannot advance G4-G8",
            "fee_floor": "5 CNY per filled buy and 5 CNY per filled sell",
            "bid_ask_and_slippage": "not observed; adverse tick scenarios are sensitivity tests, not measured execution costs",
        },
        "results": results,
        "chronological_descriptive_split_not_validation": {
            "earlier_20_dates": [earlier_dates[0], earlier_dates[-1]],
            "later_10_dates": [later_dates[0], later_dates[-1]],
            "warning": "Both segments remain part of the same 30-day exploration sample. The later segment is not holdout or out-of-sample evidence and is reused in parameter sensitivity.",
            "commission_floor_exploratory_path": {
                "earlier_20_day_description": performance_summary(
                    earlier_segment_paths,
                    capital=capital,
                    trade_dates=earlier_dates,
                ),
                "later_10_day_description": performance_summary(
                    later_segment_paths,
                    capital=capital,
                    trade_dates=later_dates,
                ),
            },
        },
        "parameter_sensitivity_commission_floor": sensitivity,
        "evidence_status": "insufficient_evidence_for_profitability_or_live_use",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/interim/159567_5m_latest/bars.csv"),
    )
    parser.add_argument("--capital", type=float, default=30_000)
    parser.add_argument("--round-trip-limit", type=int, default=20)
    args = parser.parse_args()
    frame = pd.read_csv(args.input)
    print(
        json.dumps(
            run_mvp_report(
                frame,
                capital=args.capital,
                round_trip_limit=args.round_trip_limit,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
