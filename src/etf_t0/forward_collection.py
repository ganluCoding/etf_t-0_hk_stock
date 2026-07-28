"""Forward-only minute, quote, depth and IOPV capture for frozen ETF research.

The collector writes local research data only. It never connects to a broker or
submits an order. Provider fields remain source-labelled and missing values are not
replaced with last prices or other proxies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from datetime import time as wall_time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
from curl_cffi import requests

from etf_t0.data_pilot import (
    EASTMONEY_TRENDS_URL,
    _normalize_exchange,
    eastmoney_secid,
    fetch_eastmoney_etf_1m,
    normalize_trends,
)
from etf_t0.universe import confirmed_t0_records, load_universe_ledger

ETF_TICK = 0.001
ETF_LIST_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
ETF_DEPTH_URL = "https://push2.eastmoney.com/api/qt/stock/get"
ETF_LIST_FIELDS = "f2,f5,f6,f12,f13,f14,f17,f18,f31,f32,f124,f297,f402,f441"
ETF_LIST_FILTER = "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827"
DEPTH_FIELDS = (
    "f11,f12,f13,f14,f15,f16,f17,f18,f19,f20,"
    "f31,f32,f33,f34,f35,f36,f37,f38,f39,f40,"
    "f43,f47,f48,f57,f58,f71,f124"
)
JsonFetcher = Callable[[str, dict[str, str]], dict[str, Any]]
FROZEN_STRATEGY = {
    "hypothesis_id": "PROXY_RESIDUAL_L48_Z150_H12_MAX1",
    "target_symbol": "159570",
    "proxy_symbol": "513780",
    "family": "proxy_residual_reversion",
    "signal_bar_interval_minutes": 5,
    "signal_price": "close",
    "residual_formula": "log(target_close/proxy_close)",
    "rolling_window_scope": "continuous_across_sessions_and_trade_dates",
    "rolling_std_ddof": 0,
    "residual_lookback_bars": 48,
    "entry_z": 1.5,
    "exit_z": -0.25,
    "maximum_holding_bars": 12,
    "max_round_trips_per_day": 1,
    "entry_rule": "completed_bar_residual_z_lte_minus_entry_z_then_next_bar_open",
    "exit_rule": "completed_bar_residual_z_gte_exit_z_or_max_hold_then_next_bar_open",
    "end_of_day_rule": "force_exit_at_last_bar_assumed_open",
    "position_side": "long_target_only",
    "total_comparison_capital_cny": 30000,
    "tactical_capital_cny": 15000,
    "lot_size_units": 100,
    "cost_scenarios": [
        {"name": "zero_cost", "commission_per_side_cny": 0.0, "adverse_ticks_round_trip": 0.0},
        {
            "name": "provisional_baseline",
            "commission_per_side_cny": 5.0,
            "adverse_ticks_round_trip": 1.0,
        },
        {
            "name": "provisional_stress",
            "commission_per_side_cny": 5.0,
            "adverse_ticks_round_trip": 2.0,
        },
    ],
    "note": "Frozen for forward observation only; not a live trading instruction.",
}


def load_forward_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    symbols = config.get("symbols", [])
    codes = [item.get("symbol") for item in symbols]
    if len(codes) != 2 or set(codes) != {"159570", "513780"}:
        raise ValueError("forward collection is frozen to 159570 and 513780")
    if len(set(codes)) != len(codes):
        raise ValueError("forward collection symbols must be unique")
    if int(config.get("quote_interval_seconds", 0)) < 1:
        raise ValueError("quote interval must be at least one second")
    if config.get("timezone") != "Asia/Shanghai":
        raise ValueError("forward collection timezone changed")
    if config.get("core_sessions") != [["09:30", "11:30"], ["13:00", "15:00"]]:
        raise ValueError("core session definition changed")
    start = date.fromisoformat(config["forward_sample_start_date"])
    if start != date(2026, 7, 27):
        raise ValueError("v1 forward sample start changed; create a new collection version")
    if config.get("collection_id") != "issue_19_159570_513780_forward_v1":
        raise ValueError("collection identifier changed")
    if config.get("research_source_git_commit") != (
        "23b43d5eec84cddd7e8f848e2418e06d7a8632ad"
    ):
        raise ValueError("research source commit changed")
    if config.get("strategy_source_blob") != "f3dda56a0bb48c47f59e7ff2c38abeb721156879":
        raise ValueError("strategy source blob changed")
    if config.get("frozen_strategy") != FROZEN_STRATEGY:
        raise ValueError("frozen strategy definition changed; create a new collection version")
    return config


def validate_configured_symbols(config: dict[str, Any], ledger_path: Path) -> None:
    records = confirmed_t0_records(load_universe_ledger(ledger_path))
    confirmed = {(record.code, record.exchange) for record in records}
    requested = {(item["symbol"], item["exchange"]) for item in config["symbols"]}
    missing = requested - confirmed
    if missing:
        raise ValueError(f"symbols lack confirmed exchange evidence: {sorted(missing)}")


def _fetch_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(
                url,
                params=params,
                headers={"Referer": "https://quote.eastmoney.com/"},
                impersonate="chrome",
                timeout=20,
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            if payload.get("rc") != 0 or payload.get("data") is None:
                raise ValueError("provider returned an unsuccessful response")
            return payload
        except (requests.RequestsError, ValueError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("quote request failed after 3 attempts") from last_error


def fetch_etf_list_page(page: int, *, fetcher: JsonFetcher = _fetch_json) -> dict[str, Any]:
    if page < 1:
        raise ValueError("page must be positive")
    params = {
        "pn": str(page),
        "pz": "100",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": ETF_LIST_FILTER,
        "fields": ETF_LIST_FIELDS,
    }
    return fetcher(ETF_LIST_URL, params)


def discover_symbol_pages(
    symbols: list[str],
    *,
    page_fetcher: Callable[[int], dict[str, Any]] = fetch_etf_list_page,
) -> dict[str, int]:
    """Find source pages once; fail visibly rather than silently dropping a symbol."""

    first = page_fetcher(1)
    total = int(first["data"]["total"])
    page_count = math.ceil(total / 100)
    payloads: dict[int, dict[str, Any]] = {1: first}
    if page_count > 1:
        with ThreadPoolExecutor(max_workers=min(8, page_count - 1)) as executor:
            pages = range(2, page_count + 1)
            payloads.update(zip(pages, executor.map(page_fetcher, pages)))
    wanted = set(symbols)
    found: dict[str, int] = {}
    for page, payload in payloads.items():
        for row in payload["data"].get("diff", []):
            code = str(row.get("f12", ""))
            if code in wanted:
                found[code] = page
    missing = wanted - set(found)
    if missing:
        raise ValueError(f"configured symbols missing from provider ETF list: {sorted(missing)}")
    return found


def fetch_depth_payload(
    symbol: str,
    exchange: str,
    *,
    fetcher: JsonFetcher = _fetch_json,
) -> dict[str, Any]:
    _normalize_exchange(symbol, exchange)
    return fetcher(
        ETF_DEPTH_URL,
        {
            "secid": eastmoney_secid(symbol, exchange),
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fields": DEPTH_FIELDS,
        },
    )


def _number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _provider_time(value: Any, timezone: ZoneInfo) -> datetime | None:
    epoch = _number(value)
    if epoch is None or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone)


def _provider_date(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    text = str(value).split(".", 1)[0]
    try:
        if len(text) != 8 or not text.isdigit():
            return None
        return date(int(text[:4]), int(text[4:6]), int(text[6:8])).isoformat()
    except ValueError:
        return None


def session_label(observed_at: datetime) -> str:
    if observed_at.weekday() >= 5:
        return "weekend_outside_core"
    clock = observed_at.timetz().replace(tzinfo=None)
    if wall_time(9, 30) <= clock <= wall_time(11, 30):
        return "morning_core"
    if wall_time(13, 0) <= clock < wall_time(14, 57):
        return "afternoon_core"
    if wall_time(14, 57) <= clock <= wall_time(15, 0):
        return "closing_call_auction"
    return "weekday_outside_core"


def is_minute_capture_window(observed_at: datetime) -> bool:
    """Allow short post-session pulls so the 11:30 and 15:00 bars can mature."""

    if observed_at.weekday() >= 5:
        return False
    clock = observed_at.timetz().replace(tzinfo=None)
    return (
        wall_time(9, 30) <= clock <= wall_time(11, 37)
        or wall_time(13, 0) <= clock <= wall_time(15, 7)
    )


def normalize_depth(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    buy_fields = (("f19", "f20"), ("f17", "f18"), ("f15", "f16"), ("f13", "f14"), ("f11", "f12"))
    sell_fields = (("f31", "f32"), ("f33", "f34"), ("f35", "f36"), ("f37", "f38"), ("f39", "f40"))
    result: dict[str, Any] = {}
    available_levels = 0
    for side, fields in (("bid", buy_fields), ("ask", sell_fields)):
        for level, (price_field, size_field) in enumerate(fields, start=1):
            price = _number(data.get(price_field))
            size_lots = _number(data.get(size_field))
            result[f"depth_{side}{level}_price"] = price
            result[f"depth_{side}{level}_size_provider_units"] = size_lots
            if price is not None and size_lots is not None:
                available_levels += 1
    if payload.get("_not_sampled"):
        result["depth_status"] = "not_sampled_this_interval"
    else:
        result["depth_status"] = (
            "five_levels_available" if available_levels == 10 else "partial_or_unavailable"
        )
    result["depth_available_level_sides"] = available_levels
    result["depth_size_unit_status"] = "provider_units_unverified"
    result["depth_capture_error"] = payload.get("_capture_error")
    return result


def _capture_depth_best_effort(
    item: dict[str, str], depth_fetcher: Callable[[str, str], dict[str, Any]]
) -> dict[str, Any]:
    try:
        return depth_fetcher(item["symbol"], item["exchange"])
    except (RuntimeError, ValueError) as error:
        return {
            "rc": -1,
            "data": {},
            "_capture_error": f"{type(error).__name__}: {error}",
        }


def normalize_quote_row(
    raw: dict[str, Any],
    *,
    exchange: str,
    observed_at: datetime,
    capture_id: str,
    forward_start: date,
    freshness_limit_seconds: int,
    depth: dict[str, Any] | None,
) -> dict[str, Any]:
    timezone = ZoneInfo("Asia/Shanghai")
    provider_time = _provider_time(raw.get("f124"), timezone)
    provider_date = _provider_date(raw.get("f297"))
    last_price = _number(raw.get("f2"))
    bid1 = _number(raw.get("f31"))
    ask1 = _number(raw.get("f32"))
    iopv = _number(raw.get("f441"))
    spread = ask1 - bid1 if ask1 is not None and bid1 is not None else None
    delay = (observed_at - provider_time).total_seconds() if provider_time is not None else None
    provider_timestamp_within_limit = delay is not None and -5 <= delay <= freshness_limit_seconds
    label = session_label(observed_at)
    required_fields_valid = (
        last_price is not None
        and last_price > 0
        and bid1 is not None
        and bid1 > 0
        and ask1 is not None
        and ask1 >= bid1
        and iopv is not None
        and iopv > 0
    )
    candidate_forward = (
        observed_at.date() >= forward_start
        and label in {"morning_core", "afternoon_core"}
        and provider_date == observed_at.date().isoformat()
        and provider_timestamp_within_limit
        and required_fields_valid
    )
    row = {
        "capture_id": capture_id,
        "symbol": str(raw.get("f12")),
        "exchange": exchange,
        "name": raw.get("f14"),
        "observed_at": observed_at.isoformat(timespec="milliseconds"),
        "provider_update_time": (
            provider_time.isoformat(timespec="seconds") if provider_time else None
        ),
        "provider_data_date": provider_date,
        "session_label": label,
        "is_provider_timestamp_within_limit": provider_timestamp_within_limit,
        "provider_delay_seconds": delay,
        "is_candidate_forward_quote": candidate_forward,
        "is_candidate_forward_pair": False,
        "last_price": last_price,
        "bid1_price": bid1,
        "ask1_price": ask1,
        "spread_cny": spread,
        "spread_ticks": spread / ETF_TICK if spread is not None else None,
        "iopv": iopv,
        "provider_discount_rate_pct": _number(raw.get("f402")),
        "computed_market_premium_to_iopv_pct": (
            (last_price / iopv - 1) * 100
            if last_price is not None and iopv not in (None, 0)
            else None
        ),
        "cumulative_volume_provider_units": _number(raw.get("f5")),
        "cumulative_turnover_cny": _number(raw.get("f6")),
        "source_endpoint": ETF_LIST_URL,
        "source_delivery_latency_status": "unverified_until_live_session",
    }
    row.update(depth if depth is not None else normalize_depth({"data": {}, "_not_sampled": True}))
    return row


def _append_json_line(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def _append_csv(path: Path, rows: list[dict[str, Any]], keys: list[str]) -> pd.DataFrame:
    path.parent.mkdir(parents=True, exist_ok=True)
    incoming = pd.DataFrame(rows)
    if path.exists():
        combined = pd.concat([pd.read_csv(path), incoming], ignore_index=True)
    else:
        combined = incoming
    combined = combined.drop_duplicates(subset=keys, keep="last").sort_values(keys)
    combined.to_csv(path, index=False)
    return combined


def capture_quote_snapshot(
    *,
    config: dict[str, Any],
    workspace: Path,
    symbol_pages: dict[str, int],
    page_fetcher: Callable[[int], dict[str, Any]] = fetch_etf_list_page,
    depth_fetcher: Callable[[str, str], dict[str, Any]] = fetch_depth_payload,
    include_depth: bool = True,
    observed_at: datetime | None = None,
) -> list[dict[str, Any]]:
    timezone = ZoneInfo(config["timezone"])
    request_started = observed_at or datetime.now(timezone)
    capture_id = f"{request_started.strftime('%Y%m%dT%H%M%S.%f%z')}-{uuid.uuid4().hex[:8]}"
    selected_pages = sorted(set(symbol_pages.values()))
    with ThreadPoolExecutor(max_workers=len(selected_pages)) as executor:
        payloads = dict(zip(selected_pages, executor.map(page_fetcher, selected_pages)))
    quote_received = observed_at or datetime.now(timezone)
    raw_rows = {
        str(row.get("f12")): row
        for payload in payloads.values()
        for row in payload["data"].get("diff", [])
        if str(row.get("f12")) in symbol_pages
    }
    missing = set(symbol_pages) - set(raw_rows)
    if missing:
        raise ValueError(f"symbols moved or disappeared from discovered pages: {sorted(missing)}")
    depth_payloads: dict[str, dict[str, Any]] = {}
    if include_depth:
        with ThreadPoolExecutor(max_workers=2) as executor:
            items = config["symbols"]
            fetched = executor.map(
                lambda item: _capture_depth_best_effort(item, depth_fetcher), items
            )
            depth_payloads = {item["symbol"]: payload for item, payload in zip(items, fetched)}
    depth_received = observed_at or datetime.now(timezone)
    exchange_by_symbol = {item["symbol"]: item["exchange"] for item in config["symbols"]}
    normalized = [
        normalize_quote_row(
            raw_rows[symbol],
            exchange=exchange_by_symbol[symbol],
            observed_at=quote_received,
            capture_id=capture_id,
            forward_start=date.fromisoformat(config["forward_sample_start_date"]),
            freshness_limit_seconds=int(config["provider_freshness_limit_seconds"]),
            depth=normalize_depth(depth_payloads[symbol]) if symbol in depth_payloads else None,
        )
        for symbol in sorted(symbol_pages)
    ]
    provider_times = [
        datetime.fromisoformat(row["provider_update_time"])
        for row in normalized
        if row["provider_update_time"] is not None
    ]
    pair_provider_skew = (
        (max(provider_times) - min(provider_times)).total_seconds()
        if len(provider_times) == len(config["symbols"])
        else None
    )
    pair_is_candidate = (
        len(normalized) == len(config["symbols"])
        and all(row["is_candidate_forward_quote"] for row in normalized)
        and pair_provider_skew is not None
        and pair_provider_skew <= int(config["provider_freshness_limit_seconds"])
    )
    for row in normalized:
        row["is_candidate_forward_pair"] = pair_is_candidate
        row["pair_provider_timestamp_skew_seconds"] = pair_provider_skew
        row["quote_request_started_at"] = request_started.isoformat(timespec="microseconds")
        row["quote_received_at"] = quote_received.isoformat(timespec="microseconds")
        row["depth_received_at"] = (
            depth_received.isoformat(timespec="microseconds") if include_depth else None
        )
    trade_date = quote_received.date().isoformat()
    raw_path = (
        workspace / "data" / "raw" / "forward_capture" / "quotes" / trade_date / "snapshots.jsonl"
    )
    normalized_path = (
        workspace / "data" / "interim" / "forward_capture" / "quotes" / trade_date / "quotes.csv"
    )
    _append_json_line(
        raw_path,
        {
            "capture_id": capture_id,
            "request_started_at": request_started.isoformat(timespec="microseconds"),
            "quote_received_at": quote_received.isoformat(timespec="microseconds"),
            "depth_received_at": (
                depth_received.isoformat(timespec="microseconds") if include_depth else None
            ),
            "list_pages": payloads,
            "depth_payloads": depth_payloads,
        },
    )
    _append_csv(normalized_path, normalized, ["capture_id", "symbol"])
    return normalized


def _expected_one_minute_times() -> set[str]:
    morning = pd.date_range("09:30", "11:30", freq="1min").strftime("%H:%M")
    afternoon = pd.date_range("13:01", "15:00", freq="1min").strftime("%H:%M")
    return set(morning) | set(afternoon)


EXPECTED_ONE_MINUTE_TIMES = _expected_one_minute_times()


def one_minute_quality(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"observed_rows": 0, "candidate_forward_rows": 0, "daily": []}
    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], errors="raise")
    data["trade_date"] = data["timestamp"].dt.date.astype(str)
    valid_ohlc = _valid_ohlc_mask(data)
    candidate = _boolean_series(data, "is_candidate_forward_bar")
    paired = _boolean_series(data, "is_candidate_forward_pair_bar")
    daily = []
    for trade_date, group in data.groupby("trade_date", sort=True):
        clocks = set(group["timestamp"].dt.strftime("%H:%M"))
        group_valid = valid_ohlc.loc[group.index]
        daily.append(
            {
                "trade_date": trade_date,
                "rows": len(group),
                "missing_expected_times": sorted(EXPECTED_ONE_MINUTE_TIMES - clocks),
                "unexpected_times": sorted(clocks - EXPECTED_ONE_MINUTE_TIMES),
                "duplicate_timestamps": int(group["timestamp"].duplicated().sum()),
                "invalid_ohlc_rows": int((~group_valid).sum()),
                "has_241_provider_time_labels": len(group) == 241
                and not (EXPECTED_ONE_MINUTE_TIMES - clocks),
                "candidate_forward_rows": int(candidate.loc[group.index].sum()),
                "candidate_forward_pair_rows": int(paired.loc[group.index].sum()),
            }
        )
    return {
        "observed_rows": len(data),
        "valid_ohlc_rows": int(valid_ohlc.sum()),
        "invalid_ohlc_rows": int((~valid_ohlc).sum()),
        "candidate_forward_rows": int(candidate.sum()),
        "candidate_forward_pair_rows": int(paired.sum()),
        "observed_trade_days": int(data["trade_date"].nunique()),
        "provider_expected_time_labels_per_day": 241,
        "time_label_semantics_status": "09:30 auction_or_partial_bar_semantics_unverified",
        "daily": daily,
    }


def _boolean_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(False, index=frame.index, dtype=bool)
    return frame[column].astype(str).str.lower().eq("true")


def _valid_ohlc_mask(frame: pd.DataFrame) -> pd.Series:
    prices = frame[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    return (
        prices.notna().all(axis=1)
        & prices.gt(0).all(axis=1)
        & prices["high"].ge(prices[["open", "close", "low"]].max(axis=1))
        & prices["low"].le(prices[["open", "close", "high"]].min(axis=1))
    )


def _append_minute_csv(path: Path, incoming: pd.DataFrame) -> pd.DataFrame:
    """Keep the first valid OHLC observation while extending lineage metadata."""

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = pd.read_csv(path) if path.exists() else pd.DataFrame()
    combined = pd.concat([existing, incoming], ignore_index=True)
    combined["is_valid_ohlc"] = _valid_ohlc_mask(combined)
    for column in ("is_candidate_forward_bar", "is_candidate_forward_pair_bar"):
        combined[column] = _boolean_series(combined, column)
    if "first_seen_at" not in combined:
        combined["first_seen_at"] = pd.NA
    if "last_seen_at" not in combined:
        combined["last_seen_at"] = pd.NA
    lineage = combined.groupby("timestamp", sort=False).agg(
        first_seen_at=("first_seen_at", "min"),
        last_seen_at=("last_seen_at", "max"),
    )
    selected = (
        combined.sort_values(
            [
                "timestamp",
                "is_candidate_forward_pair_bar",
                "is_candidate_forward_bar",
                "is_valid_ohlc",
                "first_seen_at",
            ],
            ascending=[True, False, False, False, True],
            na_position="last",
        )
        .drop_duplicates("timestamp", keep="first")
        .set_index("timestamp")
    )
    selected["selected_vintage_received_at"] = selected["first_seen_at"]
    for column in lineage.columns:
        selected[column] = lineage[column]
    selected = selected.reset_index().sort_values("timestamp")
    selected.to_csv(path, index=False)
    return selected


def _write_raw_minute_payload(
    *, workspace: Path, symbol: str, received_at: datetime, value: dict[str, Any]
) -> Path:
    stamp = received_at.strftime("%Y%m%dT%H%M%S.%f%z")
    raw_path = (
        workspace
        / "data"
        / "raw"
        / "forward_capture"
        / "one_minute"
        / symbol
        / f"{stamp}-{uuid.uuid4().hex[:8]}.json"
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return raw_path


def sync_one_minute_bars(
    *,
    config: dict[str, Any],
    workspace: Path,
    fetcher: Callable[..., dict[str, Any]] = fetch_eastmoney_etf_1m,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, dict[str, Any]]:
    timezone = ZoneInfo(config["timezone"])
    now = clock or (lambda: datetime.now(timezone))
    forward_start = date.fromisoformat(config["forward_sample_start_date"])
    frames: dict[str, pd.DataFrame] = {}
    raw_paths: dict[str, Path] = {}
    received_times: dict[str, datetime] = {}
    max_age = int(config["one_minute_sync_interval_seconds"]) + int(
        config["provider_freshness_limit_seconds"]
    )
    for item in config["symbols"]:
        symbol = item["symbol"]
        requested_at = now()
        payload = fetcher(symbol, exchange=item["exchange"])
        received_at = now()
        frame = normalize_trends(payload)
        local_bar_time = frame["timestamp"]
        age_seconds = (pd.Timestamp(received_at).tz_localize(None) - local_bar_time).dt.total_seconds()
        frame["is_valid_ohlc"] = _valid_ohlc_mask(frame)
        frame["bar_age_at_first_observation_seconds"] = age_seconds
        frame["is_candidate_forward_bar"] = (
            (received_at.date() >= forward_start)
            & is_minute_capture_window(received_at)
            & (local_bar_time.dt.date == received_at.date())
            & local_bar_time.dt.strftime("%H:%M").isin(EXPECTED_ONE_MINUTE_TIMES)
            & age_seconds.between(60, max_age, inclusive="both")
            & frame["is_valid_ohlc"]
        )
        frame["first_seen_at"] = received_at.isoformat(timespec="microseconds")
        frame["last_seen_at"] = received_at.isoformat(timespec="microseconds")
        frames[symbol] = frame
        received_times[symbol] = received_at
        raw_paths[symbol] = _write_raw_minute_payload(
            workspace=workspace,
            symbol=symbol,
            received_at=received_at,
            value={
                "requested_at": requested_at.isoformat(timespec="microseconds"),
                "received_at": received_at.isoformat(timespec="microseconds"),
                "source_url": EASTMONEY_TRENDS_URL,
                "payload": payload,
            },
        )

    candidate_sets = {
        symbol: set(frame.loc[frame["is_candidate_forward_bar"], "timestamp"])
        for symbol, frame in frames.items()
    }
    paired_timestamps = set.intersection(*candidate_sets.values()) if candidate_sets else set()
    reports: dict[str, dict[str, Any]] = {}
    for item in config["symbols"]:
        symbol = item["symbol"]
        frame = frames[symbol]
        frame["is_candidate_forward_pair_bar"] = frame["timestamp"].isin(paired_timestamps)
        frame["timestamp"] = frame["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
        normalized_path = (
            workspace / "data" / "interim" / "forward_capture" / "one_minute" / symbol / "bars.csv"
        )
        combined = _append_minute_csv(normalized_path, frame)
        reports[symbol] = {
            "raw_path": str(raw_paths[symbol].relative_to(workspace)),
            "normalized_path": str(normalized_path.relative_to(workspace)),
            "received_at": received_times[symbol].isoformat(timespec="microseconds"),
            "quality": one_minute_quality(combined),
        }
    return reports


def quote_quality(frame: pd.DataFrame, symbols: list[str]) -> dict[str, Any]:
    if frame.empty:
        return {"observed_rows": 0, "candidate_rows": 0}
    data = frame.copy()
    candidate = data[_boolean_series(data, "is_candidate_forward_quote")]
    candidate_pairs = data[_boolean_series(data, "is_candidate_forward_pair")]
    within_limit = _boolean_series(data, "is_provider_timestamp_within_limit")
    delays = pd.to_numeric(data.get("provider_delay_seconds"), errors="coerce").dropna()
    depth_attempted = data.get("depth_status", pd.Series(index=data.index, dtype=object)).ne(
        "not_sampled_this_interval"
    )
    depth_available = data.get("depth_status", pd.Series(index=data.index, dtype=object)).eq(
        "five_levels_available"
    )
    complete_captures = int((data.groupby("capture_id")["symbol"].nunique() == len(symbols)).sum())
    return {
        "observed_rows": len(data),
        "observed_capture_count": int(data["capture_id"].nunique()),
        "complete_two_symbol_capture_count": complete_captures,
        "observed_provider_timestamp_within_limit_rate": float(within_limit.mean()),
        "observed_provider_delay_seconds_median": (
            float(delays.median()) if not delays.empty else None
        ),
        "observed_provider_delay_seconds_max": float(delays.max()) if not delays.empty else None,
        "observed_bid_ask_availability_rate": float(
            (data["bid1_price"].notna() & data["ask1_price"].notna()).mean()
        ),
        "observed_iopv_availability_rate": float(data["iopv"].notna().mean()),
        "depth_attempted_rows": int(depth_attempted.sum()),
        "depth_five_level_success_rate_when_attempted": (
            float(depth_available[depth_attempted].mean()) if depth_attempted.any() else None
        ),
        "candidate_forward_rows": len(candidate),
        "candidate_pair_capture_count": int(candidate_pairs["capture_id"].nunique()),
        "candidate_bid_ask_availability_rate": (
            float((candidate["bid1_price"].notna() & candidate["ask1_price"].notna()).mean())
            if not candidate.empty
            else None
        ),
        "candidate_iopv_availability_rate": (
            float(candidate["iopv"].notna().mean()) if not candidate.empty else None
        ),
        "candidate_invalid_or_crossed_spread_count": (
            int((candidate["spread_cny"] <= 0).sum()) if not candidate.empty else 0
        ),
        "candidate_median_spread_ticks": (
            float(candidate["spread_ticks"].median()) if not candidate.empty else None
        ),
        "note": "Candidate means provider-timestamp and field checks passed for both symbols; real-time delivery and exchange-calendar validation remain blocked.",
    }


def write_run_manifest(
    *,
    config: dict[str, Any],
    workspace: Path,
    latest_rows: list[dict[str, Any]],
    minute: dict[str, Any],
) -> dict[str, Any]:
    quote_frames = []
    quote_root = workspace / "data" / "interim" / "forward_capture" / "quotes"
    for path in sorted(quote_root.glob("*/quotes.csv")):
        quote_frames.append(pd.read_csv(path))
    all_quotes = pd.concat(quote_frames, ignore_index=True) if quote_frames else pd.DataFrame()
    manifest = {
        "collection_id": config["collection_id"],
        "config_sha256": hashlib.sha256(
            json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest(),
        "generated_at": datetime.now(ZoneInfo(config["timezone"])).isoformat(timespec="seconds"),
        "forward_sample_start_date": config["forward_sample_start_date"],
        "frozen_strategy": config["frozen_strategy"],
        "latest_quote_rows": latest_rows,
        "quote_quality": quote_quality(all_quotes, [item["symbol"] for item in config["symbols"]]),
        "one_minute": minute,
        "stage_status": {
            "collection_pipeline_initialized": True,
            "candidate_forward_sample_available": bool(
                not all_quotes.empty
                and _boolean_series(all_quotes, "is_candidate_forward_pair").any()
            ),
            "valid_forward_sample_available": False,
            "G2": "BLOCKED until cross-source and calendar validation",
            "G3": "BLOCKED until live-session delivery latency and executable depth are validated",
        },
        "execution_warning": "Captured public data is research evidence, not a broker quote or trading instruction.",
    }
    report_path = workspace / "reports" / "generated" / "forward_capture" / "latest_manifest.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = report_path.with_name(
        f".{report_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary_path.replace(report_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return manifest


def run_once(
    *,
    config_path: Path,
    ledger_path: Path,
    workspace: Path,
    allow_outside_session: bool = False,
    include_depth: bool = True,
    include_minute: bool = True,
) -> dict[str, Any]:
    config = load_forward_config(config_path)
    validate_configured_symbols(config, ledger_path)
    now = datetime.now(ZoneInfo(config["timezone"]))
    if session_label(now) not in {"morning_core", "afternoon_core"} and not allow_outside_session:
        raise ValueError(
            "outside core session; pass --allow-outside-session for a stale-data probe"
        )
    symbols = [item["symbol"] for item in config["symbols"]]
    pages = discover_symbol_pages(symbols)
    latest = capture_quote_snapshot(
        config=config,
        workspace=workspace,
        symbol_pages=pages,
        include_depth=include_depth,
    )
    minute = sync_one_minute_bars(config=config, workspace=workspace) if include_minute else {}
    return write_run_manifest(config=config, workspace=workspace, latest_rows=latest, minute=minute)


def run_loop(
    *,
    config_path: Path,
    ledger_path: Path,
    workspace: Path,
    duration_minutes: int,
    max_cycles: int | None = None,
    cycle_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if duration_minutes <= 0:
        raise ValueError("duration must be positive")
    config = load_forward_config(config_path)
    validate_configured_symbols(config, ledger_path)
    symbols = [item["symbol"] for item in config["symbols"]]
    pages = discover_symbol_pages(symbols)
    deadline = time.monotonic() + duration_minutes * 60
    last_depth = last_minute = -math.inf
    latest: list[dict[str, Any]] = []
    minute_report: dict[str, Any] = {}
    last_manifest: dict[str, Any] | None = None
    cycles = 0
    while time.monotonic() < deadline:
        now = datetime.now(ZoneInfo(config["timezone"]))
        elapsed = time.monotonic()
        changed = False
        if session_label(now) in {"morning_core", "afternoon_core"}:
            include_depth = elapsed - last_depth >= int(config["depth_interval_seconds"])
            latest = capture_quote_snapshot(
                config=config,
                workspace=workspace,
                symbol_pages=pages,
                include_depth=include_depth,
            )
            changed = True
            if include_depth:
                last_depth = elapsed
        if is_minute_capture_window(now) and elapsed - last_minute >= int(
            config["one_minute_sync_interval_seconds"]
        ):
            minute_report = sync_one_minute_bars(config=config, workspace=workspace)
            last_minute = elapsed
            changed = True
        if changed:
            last_manifest = write_run_manifest(
                config=config,
                workspace=workspace,
                latest_rows=latest,
                minute=minute_report,
            )
            if cycle_callback is not None:
                cycle_callback(last_manifest)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        time.sleep(int(config["quote_interval_seconds"]))
    if last_manifest is not None:
        return last_manifest
    last_manifest = write_run_manifest(
        config=config, workspace=workspace, latest_rows=latest, minute=minute_report
    )
    if cycle_callback is not None:
        cycle_callback(last_manifest)
    return last_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/forward_collection.json"))
    parser.add_argument("--ledger", type=Path, default=Path("config/universe/t0_etf_ledger.json"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--allow-outside-session", action="store_true")
    parser.add_argument("--skip-depth", action="store_true")
    parser.add_argument("--skip-minute", action="store_true")
    parser.add_argument("--duration-minutes", type=int)
    args = parser.parse_args()
    if args.duration_minutes is not None:
        result = run_loop(
            config_path=args.config,
            ledger_path=args.ledger,
            workspace=args.workspace,
            duration_minutes=args.duration_minutes,
        )
    else:
        result = run_once(
            config_path=args.config,
            ledger_path=args.ledger,
            workspace=args.workspace,
            allow_outside_session=args.allow_outside_session,
            include_depth=not args.skip_depth,
            include_minute=not args.skip_minute,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
