"""Predeclared multi-strategy exploration on native five-minute ETF bars.

This module records every tested symbol/strategy combination. It is a coarse,
assumed-price research calculation, not an execution backtest or trading signal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pandas as pd

from etf_t0.coarse_backtest import (
    ETF_TICK,
    LOT_SIZE,
    CostScenario,
    HypotheticalRoundTrip,
    performance_summary,
)
from etf_t0.grid_screen import (
    _latest_complete_window,
    cost_aware_grid_threshold,
    simulate_cost_aware_grid_proxy,
)
from etf_t0.universe import confirmed_t0_records, load_universe_ledger

BASELINE_COST_NAME = "provisional_baseline_5_per_side_plus_1_tick"
STRESS_COST_NAME = "provisional_stress_5_per_side_plus_2_ticks"


@dataclass(frozen=True)
class AssumedPricePath:
    """A causal next-open price path before transaction costs are applied."""

    strategy: str
    trade_date: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: int
    forced_exit: bool


def load_research_config(path: Path) -> dict[str, Any]:
    """Load the JSON-compatible YAML registry and fail closed on duplicate IDs."""

    config = json.loads(path.read_text(encoding="utf-8"))
    hypotheses = config.get("hypotheses", [])
    identifiers = [item.get("id") for item in hypotheses]
    if not hypotheses or any(not identifier for identifier in identifiers):
        raise ValueError("strategy registry requires non-empty hypothesis IDs")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("strategy registry contains duplicate hypothesis IDs")
    if config.get("descriptive_split_days") != [20, 10]:
        raise ValueError("this exploration requires the declared 20/10 descriptive split")
    return config


def _dvc_pointer_md5(path: Path) -> str | None:
    """Read the directory checksum from a small tracked DVC pointer."""

    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- md5:"):
            return stripped.split(":", 1)[1].strip()
    return None


def _quantity_for_tactical_capital(tactical_capital: float, entry_price: float) -> int:
    """Reserve the declared 5 yuan entry fee and enforce 100-unit lots."""

    if tactical_capital <= 5 or entry_price <= 0:
        return 0
    return math.floor((tactical_capital - 5) / entry_price / LOT_SIZE) * LOT_SIZE


def _cost_path(path: AssumedPricePath, costs: CostScenario) -> HypotheticalRoundTrip:
    gross_pnl = (path.exit_price - path.entry_price) * path.quantity
    explicit_fees = 2 * costs.commission_per_side
    adverse_execution_cost = costs.adverse_ticks_round_trip * ETF_TICK * path.quantity
    return HypotheticalRoundTrip(
        strategy=path.strategy,
        trade_date=path.trade_date,
        entry_time=path.entry_time,
        exit_time=path.exit_time,
        assumed_entry_price=path.entry_price,
        assumed_exit_price=path.exit_price,
        quantity=path.quantity,
        gross_pnl=gross_pnl,
        explicit_fees=explicit_fees,
        adverse_execution_cost=adverse_execution_cost,
        net_pnl=gross_pnl - explicit_fees - adverse_execution_cost,
        forced_exit=path.forced_exit,
    )


def _enforce_stress_cash_ledger(
    paths: list[AssumedPricePath],
    *,
    tactical_capital: float,
    stress_costs: CostScenario,
) -> tuple[list[AssumedPricePath], dict[str, float | int]]:
    """Resize every path to cash available after all prior stress-cost outcomes.

    Quantities fixed by this conservative ledger are reused in lower-cost scenarios,
    so scenario comparisons retain identical prices, timestamps and lot sizes.
    """

    available_cash = tactical_capital
    minimum_cash = tactical_capital
    adjusted: list[AssumedPricePath] = []
    resized_count = 0
    skipped_count = 0
    for path in sorted(paths, key=lambda item: item.entry_time):
        budget = min(tactical_capital, available_cash)
        affordable_quantity = _quantity_for_tactical_capital(budget, path.entry_price)
        quantity = min(path.quantity, affordable_quantity)
        if quantity <= 0:
            skipped_count += 1
            continue
        if quantity != path.quantity:
            resized_count += 1
        adjusted_path = AssumedPricePath(
            strategy=path.strategy,
            trade_date=path.trade_date,
            entry_time=path.entry_time,
            exit_time=path.exit_time,
            entry_price=path.entry_price,
            exit_price=path.exit_price,
            quantity=quantity,
            forced_exit=path.forced_exit,
        )
        entry_cash_required = (
            adjusted_path.entry_price * quantity + stress_costs.commission_per_side
        )
        if entry_cash_required > available_cash + 1e-9:
            raise AssertionError("stress cash ledger allowed an unaffordable entry")
        available_cash += _cost_path(adjusted_path, stress_costs).net_pnl
        minimum_cash = min(minimum_cash, available_cash)
        adjusted.append(adjusted_path)
    return adjusted, {
        "initial_tactical_cash_cny": tactical_capital,
        "ending_stress_cash_cny": available_cash,
        "minimum_stress_cash_after_closed_path_cny": minimum_cash,
        "resized_path_count": resized_count,
        "skipped_path_count": skipped_count,
    }


def _raw_paths_from_costed(paths: list[HypotheticalRoundTrip]) -> list[AssumedPricePath]:
    return [
        AssumedPricePath(
            strategy=path.strategy,
            trade_date=path.trade_date,
            entry_time=path.entry_time,
            exit_time=path.exit_time,
            entry_price=path.assumed_entry_price,
            exit_price=path.assumed_exit_price,
            quantity=path.quantity,
            forced_exit=path.forced_exit,
        )
        for path in paths
    ]


def _simulate_single_symbol_family(
    frame: pd.DataFrame, *, hypothesis: dict[str, Any]
) -> list[AssumedPricePath]:
    """Simulate one long-only family with completed-bar signals and next-open fills."""

    family = hypothesis["family"]
    if family == "ema_cost_gated_reversion":
        zero_paths = simulate_cost_aware_grid_proxy(
            frame,
            layer_capital=float(hypothesis["tactical_capital_cny"]),
            max_round_trips_per_day=int(hypothesis["max_round_trips_per_day"]),
            ema_span=int(hypothesis["ema_span"]),
            costs=CostScenario("path_only", 0.0, 0.0),
            trigger_safety_ticks=int(hypothesis["safety_ticks"]),
        )
        return _raw_paths_from_costed(zero_paths)

    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], errors="raise")
    data = data.sort_values("timestamp").reset_index(drop=True)
    data["trade_date"] = data["timestamp"].dt.date.astype(str)
    eligible_dates: set[str] | None = None
    if family == "prior_day_volatility_filtered_reversion":
        daily_extremes = data.groupby("trade_date").agg(
            day_high=("high", "max"), day_low=("low", "min")
        )
        daily_range = daily_extremes["day_high"] - daily_extremes["day_low"]
        lookback = int(hypothesis["prior_day_lookback"])
        threshold = (
            daily_range.shift(1)
            .rolling(lookback)
            .quantile(float(hypothesis["prior_day_range_quantile"]))
        )
        prior_range = daily_range.shift(1)
        eligible_dates = set(prior_range[prior_range >= threshold].dropna().index)

    paths: list[AssumedPricePath] = []
    tactical_capital = float(hypothesis["tactical_capital_cny"])
    max_per_day = int(hypothesis["max_round_trips_per_day"])
    safety_ticks = int(hypothesis.get("safety_ticks", 1))
    for trade_date, source_day in data.groupby("trade_date", sort=True):
        day = source_day.reset_index(drop=True).copy()
        if family in {"trend_filtered_pullback", "opening_range_breakout"}:
            fast_span = int(hypothesis.get("fast_ema_span", hypothesis.get("exit_ema_span", 6)))
            day["fast_ema"] = day["close"].ewm(span=fast_span, adjust=False).mean()
        if family == "trend_filtered_pullback":
            slow_span = int(hypothesis["slow_ema_span"])
            day["slow_ema"] = day["close"].ewm(span=slow_span, adjust=False).mean()
        if family == "prior_day_volatility_filtered_reversion":
            day["ema"] = day["close"].ewm(span=int(hypothesis["ema_span"]), adjust=False).mean()

        position: dict[str, Any] | None = None
        pending_entry: dict[str, Any] | None = None
        pending_exit = False
        completed = 0
        for bar_index, bar in day.iterrows():
            is_last_bar = bar_index == len(day) - 1
            assumed_open_time = bar["timestamp"] - pd.Timedelta(minutes=5)

            if pending_exit and position is not None:
                paths.append(
                    AssumedPricePath(
                        strategy=hypothesis["id"],
                        trade_date=trade_date,
                        entry_time=position["entry_time"].isoformat(sep=" "),
                        exit_time=assumed_open_time.isoformat(sep=" "),
                        entry_price=position["entry_price"],
                        exit_price=float(bar["open"]),
                        quantity=position["quantity"],
                        forced_exit=False,
                    )
                )
                position = None
                pending_exit = False
                completed += 1

            if pending_entry is not None and position is None and not is_last_bar:
                entry_price = float(bar["open"])
                quantity = _quantity_for_tactical_capital(tactical_capital, entry_price)
                threshold = cost_aware_grid_threshold(
                    reference_price=entry_price,
                    layer_capital=tactical_capital,
                    safety_ticks=safety_ticks,
                )
                anchor = pending_entry.get("anchor")
                gate_passed = anchor is None or float(anchor) - entry_price >= float(
                    threshold["required_grid_delta_cny"]
                )
                if quantity > 0 and gate_passed:
                    position = {
                        "entry_time": assumed_open_time,
                        "entry_price": entry_price,
                        "quantity": quantity,
                        "entry_bar_index": bar_index,
                    }
                pending_entry = None
            elif is_last_bar:
                pending_entry = None

            if position is not None:
                held_bars = bar_index - int(position["entry_bar_index"]) + 1
                maximum_holding = int(hypothesis.get("maximum_holding_bars", 10_000))
                if is_last_bar:
                    paths.append(
                        AssumedPricePath(
                            strategy=hypothesis["id"],
                            trade_date=trade_date,
                            entry_time=position["entry_time"].isoformat(sep=" "),
                            exit_time=assumed_open_time.isoformat(sep=" "),
                            entry_price=position["entry_price"],
                            exit_price=float(bar["open"]),
                            quantity=position["quantity"],
                            forced_exit=True,
                        )
                    )
                    position = None
                    completed += 1
                elif (
                    (
                        family == "trend_filtered_pullback"
                        and (
                            float(bar["close"]) >= float(bar["fast_ema"])
                            or float(bar["close"]) < float(bar["slow_ema"])
                            or held_bars >= maximum_holding
                        )
                    )
                    or (
                        family == "opening_range_breakout"
                        and (
                            float(bar["close"]) < float(bar["fast_ema"])
                            or held_bars >= maximum_holding
                        )
                    )
                    or (
                        family == "prior_day_volatility_filtered_reversion"
                        and float(bar["close"]) >= float(bar["ema"])
                    )
                ):
                    pending_exit = True
            elif not is_last_bar and completed < max_per_day:
                if family == "trend_filtered_pullback":
                    slope_bars = int(hypothesis["slow_slope_bars"])
                    if (
                        bar_index >= int(hypothesis["slow_ema_span"]) - 1
                        and bar_index >= slope_bars
                        and float(bar["slow_ema"])
                        > float(day.iloc[bar_index - slope_bars]["slow_ema"])
                        and float(bar["close"]) > float(bar["slow_ema"])
                        and float(bar["close"]) < float(bar["fast_ema"])
                    ):
                        pending_entry = {"anchor": float(bar["fast_ema"])}
                elif family == "opening_range_breakout":
                    opening_bars = int(hypothesis["opening_range_bars"])
                    if bar_index >= opening_bars:
                        opening_high = float(day.iloc[:opening_bars]["high"].max())
                        threshold = cost_aware_grid_threshold(
                            reference_price=float(bar["close"]),
                            layer_capital=tactical_capital,
                            safety_ticks=safety_ticks,
                        )
                        if float(bar["close"]) >= opening_high + float(
                            threshold["required_grid_delta_cny"]
                        ):
                            pending_entry = {"anchor": None}
                elif family == "prior_day_volatility_filtered_reversion":
                    if (
                        eligible_dates is not None
                        and trade_date in eligible_dates
                        and bar_index >= int(hypothesis["ema_span"]) - 1
                        and float(bar["close"]) < float(bar["ema"])
                    ):
                        pending_entry = {"anchor": float(bar["ema"])}
    return paths


def _simulate_proxy_family(
    target: pd.DataFrame, proxy: pd.DataFrame, *, hypothesis: dict[str, Any]
) -> list[AssumedPricePath]:
    """Use a synchronized causal log-price residual as a long-only target anchor."""

    target_data = target.copy()
    proxy_data = proxy.copy()
    target_data["timestamp"] = pd.to_datetime(target_data["timestamp"], errors="raise")
    proxy_data["timestamp"] = pd.to_datetime(proxy_data["timestamp"], errors="raise")
    merged = target_data.merge(
        proxy_data[["timestamp", "close"]],
        on="timestamp",
        how="inner",
        suffixes=("", "_proxy"),
        validate="one_to_one",
    ).sort_values("timestamp")
    if len(merged) != len(target_data) or len(merged) != len(proxy_data):
        raise ValueError("proxy pair does not have exactly synchronized five-minute bars")
    merged["trade_date"] = merged["timestamp"].dt.date.astype(str)
    merged["log_ratio"] = (merged["close"] / merged["close_proxy"]).map(math.log)
    lookback = int(hypothesis["residual_lookback_bars"])
    rolling_mean = merged["log_ratio"].rolling(lookback, min_periods=lookback).mean()
    rolling_std = merged["log_ratio"].rolling(lookback, min_periods=lookback).std(ddof=0)
    merged["residual_z"] = (merged["log_ratio"] - rolling_mean) / rolling_std.replace(0, pd.NA)

    paths: list[AssumedPricePath] = []
    tactical_capital = float(hypothesis["tactical_capital_cny"])
    entry_z = float(hypothesis["entry_z"])
    exit_z = float(hypothesis["exit_z"])
    maximum_holding = int(hypothesis["maximum_holding_bars"])
    max_per_day = int(hypothesis["max_round_trips_per_day"])
    for trade_date, source_day in merged.groupby("trade_date", sort=True):
        day = source_day.reset_index(drop=True)
        position: dict[str, Any] | None = None
        pending_entry = False
        pending_exit = False
        completed = 0
        for bar_index, bar in day.iterrows():
            is_last_bar = bar_index == len(day) - 1
            assumed_open_time = bar["timestamp"] - pd.Timedelta(minutes=5)
            if pending_exit and position is not None:
                paths.append(
                    AssumedPricePath(
                        strategy=hypothesis["id"],
                        trade_date=trade_date,
                        entry_time=position["entry_time"].isoformat(sep=" "),
                        exit_time=assumed_open_time.isoformat(sep=" "),
                        entry_price=position["entry_price"],
                        exit_price=float(bar["open"]),
                        quantity=position["quantity"],
                        forced_exit=False,
                    )
                )
                position = None
                pending_exit = False
                completed += 1
            if pending_entry and position is None and not is_last_bar:
                entry_price = float(bar["open"])
                quantity = _quantity_for_tactical_capital(tactical_capital, entry_price)
                if quantity > 0:
                    position = {
                        "entry_time": assumed_open_time,
                        "entry_price": entry_price,
                        "quantity": quantity,
                        "entry_bar_index": bar_index,
                    }
                pending_entry = False
            elif is_last_bar:
                pending_entry = False
            if position is not None:
                held_bars = bar_index - int(position["entry_bar_index"]) + 1
                if is_last_bar:
                    paths.append(
                        AssumedPricePath(
                            strategy=hypothesis["id"],
                            trade_date=trade_date,
                            entry_time=position["entry_time"].isoformat(sep=" "),
                            exit_time=assumed_open_time.isoformat(sep=" "),
                            entry_price=position["entry_price"],
                            exit_price=float(bar["open"]),
                            quantity=position["quantity"],
                            forced_exit=True,
                        )
                    )
                    position = None
                    completed += 1
                elif (
                    pd.notna(bar["residual_z"]) and float(bar["residual_z"]) >= exit_z
                ) or held_bars >= maximum_holding:
                    pending_exit = True
            elif (
                not is_last_bar
                and completed < max_per_day
                and pd.notna(bar["residual_z"])
                and float(bar["residual_z"]) <= -entry_z
            ):
                pending_entry = True
    return paths


def _one_sided_normal_p(t_stat: float | None) -> float | None:
    if t_stat is None or not math.isfinite(t_stat):
        return None
    return 1 - NormalDist().cdf(t_stat)


def _summaries(
    raw_paths: list[AssumedPricePath],
    *,
    scenarios: list[CostScenario],
    capital: float,
    trade_dates: list[str],
) -> dict[str, Any]:
    earlier_dates = trade_dates[:20]
    later_dates = trade_dates[-10:]
    result: dict[str, Any] = {}
    for scenario in scenarios:
        paths = [_cost_path(path, scenario) for path in raw_paths]
        result[scenario.name] = {
            "full_30_day": performance_summary(paths, capital=capital, trade_dates=trade_dates),
            "earlier_20_day_not_holdout": performance_summary(
                [path for path in paths if path.trade_date in earlier_dates],
                capital=capital,
                trade_dates=earlier_dates,
            ),
            "later_10_day_not_holdout": performance_summary(
                [path for path in paths if path.trade_date in later_dates],
                capital=capital,
                trade_dates=later_dates,
            ),
        }
    return result


def _base_inventory_account_comparison(
    frame: pd.DataFrame,
    *,
    paths: list[HypotheticalRoundTrip],
    capital: float,
    base_inventory_fraction: float = 0.5,
) -> dict[str, Any]:
    """Add a day-end close-marked 50/50 account view without changing tactical alpha."""

    data = frame.sort_values("timestamp").copy()
    trade_dates = sorted(data["trade_date"].unique())
    first_reference_price = float(data.iloc[0]["open"])
    base_budget = capital * base_inventory_fraction
    base_quantity = math.floor(base_budget / first_reference_price / LOT_SIZE) * LOT_SIZE
    daily_close = data.groupby("trade_date")["close"].last().reindex(trade_dates)
    base_pnl = (daily_close - first_reference_price) * base_quantity
    if paths:
        tactical_daily = (
            pd.DataFrame(asdict(path) for path in paths)
            .groupby("trade_date")["net_pnl"]
            .sum()
            .reindex(trade_dates, fill_value=0.0)
        )
    else:
        tactical_daily = pd.Series(0.0, index=trade_dates)
    account_equity = capital + base_pnl + tactical_daily.cumsum()
    equity_with_initial = pd.concat(
        [pd.Series([capital], index=["initial"], dtype=float), account_equity]
    )
    drawdown = equity_with_initial / equity_with_initial.cummax() - 1
    tactical_net = float(tactical_daily.sum())
    passive_base_pnl = float(base_pnl.iloc[-1])
    return {
        "base_inventory_fraction": base_inventory_fraction,
        "base_inventory_quantity": base_quantity,
        "base_inventory_start_reference_price": first_reference_price,
        "passive_base_inventory_pnl": passive_base_pnl,
        "tactical_incremental_net_pnl": tactical_net,
        "descriptive_total_account_pnl_close_marked_not_executable": (
            passive_base_pnl + tactical_net
        ),
        "descriptive_total_account_return_close_marked_not_executable": (
            passive_base_pnl + tactical_net
        )
        / capital,
        "descriptive_day_end_drawdown_not_prd_compliant": float(drawdown.min()),
        "marking_warning": "Base inventory is marked at five-minute day-end close, not executable bid. Acquisition cost before the window and intraday mark-to-market drawdown are excluded.",
    }


def _candidate_status(summaries: dict[str, Any]) -> tuple[str, list[str]]:
    baseline = summaries[BASELINE_COST_NAME]
    stress = summaries[STRESS_COST_NAME]
    checks = {
        "at_least_one_path": baseline["full_30_day"]["hypothetical_path_count"] > 0,
        "baseline_full_positive": baseline["full_30_day"]["net_pnl"] > 0,
        "baseline_earlier_positive": baseline["earlier_20_day_not_holdout"]["net_pnl"] > 0,
        "baseline_later_positive": baseline["later_10_day_not_holdout"]["net_pnl"] > 0,
        "stress_full_positive": stress["full_30_day"]["net_pnl"] > 0,
        "stress_earlier_positive": stress["earlier_20_day_not_holdout"]["net_pnl"] > 0,
        "stress_later_positive": stress["later_10_day_not_holdout"]["net_pnl"] > 0,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if not failed:
        return "exploratory_priority_not_validated", []
    if checks["baseline_full_positive"]:
        return "sample_positive_not_stable", failed
    return "not_positive_after_baseline_cost", failed


def run_multi_strategy_research(
    *, config_path: Path, ledger_path: Path, workspace: Path
) -> dict[str, Any]:
    """Run and log every predeclared hypothesis against each eligible local dataset."""

    config = load_research_config(config_path)
    capital = float(config["capital_cny"])
    scenarios = [
        CostScenario(
            item["name"],
            float(item["commission_per_side_cny"]),
            float(item["adverse_ticks_round_trip"]),
        )
        for item in config["cost_scenarios"]
    ]
    names = {scenario.name for scenario in scenarios}
    if {"zero_cost", BASELINE_COST_NAME, STRESS_COST_NAME} - names:
        raise ValueError("zero, baseline and stress cost scenarios are required")
    stress_costs = next(scenario for scenario in scenarios if scenario.name == STRESS_COST_NAME)

    ledger = confirmed_t0_records(load_universe_ledger(ledger_path))
    trading_names = {record.code: record.trading_name for record in ledger}
    windows: dict[str, pd.DataFrame] = {}
    data_lineage: dict[str, dict[str, str | None]] = {}
    excluded: dict[str, str] = {}
    for record in ledger:
        bars_path = workspace / "data" / "interim" / f"{record.code}_5m_latest" / "bars.csv"
        if not bars_path.exists():
            excluded[record.code] = "local_5m_dataset_missing"
            continue
        try:
            windows[record.code] = _latest_complete_window(
                pd.read_csv(bars_path), days=int(config["window_complete_days"])
            )
            dvc_pointer = workspace / "data" / "interim" / f"{record.code}_5m_latest.dvc"
            data_lineage[record.code] = {
                "bars_path": str(bars_path.relative_to(workspace)),
                "dvc_pointer_path": str(dvc_pointer.relative_to(workspace)),
                "dvc_directory_md5": _dvc_pointer_md5(dvc_pointer),
            }
        except ValueError as error:
            excluded[record.code] = str(error)

    trials: list[dict[str, Any]] = []
    for hypothesis in config["hypotheses"]:
        family = hypothesis["family"]
        if family == "proxy_residual_reversion":
            targets = [tuple(pair) for pair in hypothesis["pairs"]]
        else:
            targets = [(symbol, None) for symbol in sorted(windows)]
        for symbol, proxy_symbol in targets:
            if symbol not in windows or (proxy_symbol is not None and proxy_symbol not in windows):
                continue
            window = windows[symbol]
            trade_dates = sorted(window["trade_date"].unique())
            if proxy_symbol is None:
                raw_paths = _simulate_single_symbol_family(window, hypothesis=hypothesis)
            else:
                raw_paths = _simulate_proxy_family(
                    window, windows[proxy_symbol], hypothesis=hypothesis
                )
            tactical_capital = float(hypothesis["tactical_capital_cny"])
            raw_paths, stress_cash_ledger = _enforce_stress_cash_ledger(
                raw_paths,
                tactical_capital=tactical_capital,
                stress_costs=stress_costs,
            )
            summaries = _summaries(
                raw_paths,
                scenarios=scenarios,
                capital=tactical_capital,
                trade_dates=trade_dates,
            )
            account_comparisons = {
                scenario.name: _base_inventory_account_comparison(
                    window,
                    paths=[_cost_path(path, scenario) for path in raw_paths],
                    capital=capital,
                )
                for scenario in scenarios
            }
            status, failed_checks = _candidate_status(summaries)
            baseline_full = summaries[BASELINE_COST_NAME]["full_30_day"]
            trials.append(
                {
                    "hypothesis_id": hypothesis["id"],
                    "family": family,
                    "purpose": hypothesis["purpose"],
                    "symbol": symbol,
                    "trading_name": trading_names[symbol],
                    "proxy_symbol": proxy_symbol,
                    "parameters": {
                        key: value
                        for key, value in hypothesis.items()
                        if key not in {"id", "family", "purpose", "pairs"}
                    },
                    "window": {
                        "start": trade_dates[0],
                        "end": trade_dates[-1],
                        "complete_days": len(trade_dates),
                    },
                    "status": status,
                    "failed_checks": failed_checks,
                    "sample_size_warning": (
                        "fewer_than_30_paths"
                        if baseline_full["hypothetical_path_count"] < 30
                        else None
                    ),
                    "naive_full_window_one_sided_normal_p_not_selection_safe": (
                        _one_sided_normal_p(baseline_full["daily_pnl_t_stat"])
                    ),
                    "scenario_results": summaries,
                    "stress_cash_ledger": stress_cash_ledger,
                    "account_comparison_50pct_base_inventory": account_comparisons,
                }
            )

    trial_count = len(trials)
    bonferroni_alpha = 0.05 / trial_count if trial_count else None
    for trial in trials:
        p_value = trial["naive_full_window_one_sided_normal_p_not_selection_safe"]
        trial["passes_naive_bonferroni_screen"] = (
            p_value is not None and bonferroni_alpha is not None and p_value < bonferroni_alpha
        )
    trials.sort(
        key=lambda trial: trial["scenario_results"][BASELINE_COST_NAME]["full_30_day"]["net_pnl"],
        reverse=True,
    )

    output = {
        "research_id": config["research_id"],
        "mode": "MVP derived calculation from native five-minute OHLCV; no executable quote or fill simulator",
        "capital_cny": capital,
        "tactical_result_scope": "long-only intraday tactical sleeve; every open path is forced out before the session ends; candidate ranking uses incremental tactical P&L",
        "window_policy": "latest 30 complete dates per symbol; earlier 20 and later 10 are descriptive reuse, not train/test or out-of-sample validation",
        "causal_timing": "completed-bar signals execute no earlier than the next bar open print",
        "cost_policy": "same raw paths across zero, baseline and stress scenarios; each buy and sell is charged separately",
        "hypothesis_registry_path": str(config_path.relative_to(workspace)),
        "hypothesis_registry_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "data_lineage": data_lineage,
        "eligible_symbols": sorted(windows),
        "excluded_symbols": excluded,
        "unique_symbol_strategy_trials": trial_count,
        "multiple_testing": {
            "all_trials_retained": True,
            "family_count": len({trial["family"] for trial in trials}),
            "naive_bonferroni_alpha": bonferroni_alpha,
            "warning": "Normal-approximation p-values are descriptive only. The same 30 days informed the research question, so even a corrected pass would not be out-of-sample evidence.",
        },
        "frequency_question": "Compare FREQ_EMA12_MAX1/MAX2/MAX4 at fixed symbol, capital layer, anchor and trigger; this isolates the cap on daily round trips.",
        "candidate_definition": {
            "sample_positive_not_stable": "baseline full-30-day tactical net P&L is positive but one or more segment/stress checks fail",
            "exploratory_priority_not_validated": "baseline and stress costs are positive in the earlier 20, later 10 and full 30 descriptions; still not validated",
        },
        "trials": trials,
        "stage_gates": {
            "G0_fee_acceptance": "BLOCKED",
            "G2_data_acceptance": "BLOCKED",
            "G3_execution_data_acceptance": "BLOCKED",
            "G4_hypothesis_acceptance": "BLOCKED: 30-day exploratory reuse and no fair-value/executable-quote validation",
            "G5_holdout_acceptance": "BLOCKED: no untouched 60-day holdout",
        },
        "drawdown_warning": "Tactical maximum drawdown uses the declared tactical sleeve as denominator and only day-end realized path P&L. The separate 50% base-inventory close-marked description is not a PRD-compliant account return or drawdown; both exclude intrapath mark-to-market loss and executable bid/ask valuation.",
        "risk_control_warning": "The PRD daily-loss, invested-capital, spread, stale-data and portfolio-drawdown stops are intentionally omitted from this hypothesis-generation calculation. It is not the formal strategy baseline and cannot advance G4-G8.",
        "evidence_quality": "insufficient_evidence_for_profitability_or_live_use",
    }

    output_dir = workspace / "reports" / "generated" / "multi_strategy"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "t0_etf_multi_strategy_latest.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    flat_rows: list[dict[str, Any]] = []
    for trial in trials:
        for scenario_name, segments in trial["scenario_results"].items():
            for segment_name, summary in segments.items():
                flat_rows.append(
                    {
                        "hypothesis_id": trial["hypothesis_id"],
                        "family": trial["family"],
                        "purpose": trial["purpose"],
                        "symbol": trial["symbol"],
                        "proxy_symbol": trial["proxy_symbol"],
                        "status": trial["status"],
                        "cost_scenario": scenario_name,
                        "segment": segment_name,
                        **summary,
                    }
                )
    pd.DataFrame(flat_rows).to_csv(output_dir / "t0_etf_multi_strategy_trials.csv", index=False)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path, default=Path("config/strategy-backtester_config.yaml")
    )
    parser.add_argument("--ledger", type=Path, default=Path("config/universe/t0_etf_ledger.json"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(
        json.dumps(
            run_multi_strategy_research(
                config_path=args.config,
                ledger_path=args.ledger,
                workspace=args.workspace,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
