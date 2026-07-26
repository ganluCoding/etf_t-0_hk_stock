"""Local-only ETF minute-data acquisition and quality checks.

The pilot preserves the provider response and reports what the source actually
returned. It never fabricates missing minutes or promotes a short retention window
to a 30-trading-day dataset.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from curl_cffi import requests

EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_TRENDS_URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"


def _expected_core_times() -> frozenset[str]:
    """The 48 5-minute bar end times for 09:30-11:30 and 13:00-15:00."""

    morning = [
        f"{hour:02d}:{minute:02d}"
        for hour, minutes in ((9, range(35, 60, 5)), (10, range(0, 60, 5)), (11, range(0, 31, 5)))
        for minute in minutes
    ]
    afternoon = [
        f"{hour:02d}:{minute:02d}"
        for hour, minutes in ((13, range(5, 60, 5)), (14, range(0, 60, 5)), (15, (0,)))
        for minute in minutes
    ]
    return frozenset(morning + afternoon)


EXPECTED_CORE_TIMES = _expected_core_times()


def _fetch_eastmoney_payload(
    url: str,
    symbol: str,
    params: dict[str, str],
    exchange: str | None = None,
    get: Callable[..., Any] = requests.get,
) -> dict[str, Any]:
    """Fetch a public Eastmoney response with bounded, observable retries."""

    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError("symbol must be a six-digit ETF code")
    normalized_exchange = _normalize_exchange(symbol, exchange)
    quote_prefix = "sh" if normalized_exchange == "SSE" else "sz"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = get(
                url,
                params=params,
                headers={"Referer": f"https://quote.eastmoney.com/{quote_prefix}{symbol}.html"},
                impersonate="chrome",
                timeout=30,
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            break
        except Exception as error:  # provider/network failures are retried and remain observable
            last_error = error
            if attempt == 2:
                raise RuntimeError("Eastmoney request failed after 3 attempts") from error
            time.sleep(0.5 * (attempt + 1))
    else:  # pragma: no cover - the final attempt always raises
        raise RuntimeError("Eastmoney request failed") from last_error
    if payload.get("rc") != 0:
        raise ValueError("provider returned an unsuccessful response")
    return payload


def _normalize_exchange(symbol: str, exchange: str | None = None) -> str:
    """Return the exchange used by Eastmoney; fail closed on ambiguous inputs."""

    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError("symbol must be a six-digit ETF code")
    if exchange is not None:
        normalized = exchange.upper()
        if normalized not in {"SSE", "SZSE"}:
            raise ValueError("exchange must be SSE or SZSE")
        if (symbol.startswith("5") and normalized != "SSE") or (
            symbol.startswith("1") and normalized != "SZSE"
        ):
            raise ValueError("ETF symbol prefix conflicts with the explicit exchange")
        return normalized
    if symbol.startswith("5"):
        return "SSE"
    if symbol.startswith("1"):
        return "SZSE"
    raise ValueError("cannot infer ETF exchange from symbol; pass exchange explicitly")


def eastmoney_secid(symbol: str, exchange: str | None = None) -> str:
    """Map an exchange-qualified ETF code to Eastmoney's market identifier."""

    normalized_exchange = _normalize_exchange(symbol, exchange)
    market = "1" if normalized_exchange == "SSE" else "0"
    return f"{market}.{symbol}"


def fetch_eastmoney_etf_5m(
    symbol: str,
    get: Callable[..., Any] = requests.get,
    *,
    exchange: str | None = None,
) -> dict[str, Any]:
    """Fetch native 5-minute bars without provider-side price adjustment."""

    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": "5",
        "fqt": "0",
        "secid": eastmoney_secid(symbol, exchange),
        "beg": "0",
        "end": "20500000",
    }
    payload = _fetch_eastmoney_payload(
        EASTMONEY_KLINE_URL, symbol, params, exchange=exchange, get=get
    )
    if not payload.get("data", {}).get("klines"):
        raise ValueError("provider returned no 5-minute kline data")
    return payload


def fetch_eastmoney_etf_1m(
    symbol: str,
    get: Callable[..., Any] = requests.get,
    *,
    exchange: str | None = None,
) -> dict[str, Any]:
    """Fetch the provider's currently available native 1-minute probe window."""

    params = {
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "ndays": "5",
        "iscr": "0",
        "secid": eastmoney_secid(symbol, exchange),
    }
    payload = _fetch_eastmoney_payload(
        EASTMONEY_TRENDS_URL, symbol, params, exchange=exchange, get=get
    )
    if not payload.get("data", {}).get("trends"):
        raise ValueError("provider returned no 1-minute trend data")
    return payload


def normalize_klines(payload: dict[str, Any]) -> pd.DataFrame:
    """Normalize only fields present in the raw Eastmoney kline response."""

    rows = [item.split(",") for item in payload["data"]["klines"]]
    columns = [
        "timestamp",
        "open",
        "close",
        "high",
        "low",
        "volume",
        "turnover",
        "amplitude_pct",
        "change_pct",
        "change_amount",
        "turnover_rate_pct",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="raise")
    for column in columns[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def normalize_trends(payload: dict[str, Any]) -> pd.DataFrame:
    """Normalize only fields present in the raw Eastmoney 1-minute response."""

    rows = [item.split(",") for item in payload["data"]["trends"]]
    columns = [
        "timestamp",
        "open",
        "close",
        "high",
        "low",
        "volume",
        "turnover",
        "average_price",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="raise")
    for column in columns[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def quality_summary(frame: pd.DataFrame) -> dict[str, Any]:
    """Describe coverage and anomalies without inferring exchange closures."""

    if frame.empty:
        raise ValueError("cannot evaluate an empty minute dataset")
    data = frame.copy()
    data["trade_date"] = data["timestamp"].dt.date.astype(str)
    data["clock"] = data["timestamp"].dt.strftime("%H:%M")
    data["is_core"] = data["clock"].isin(EXPECTED_CORE_TIMES)
    per_day: list[dict[str, Any]] = []
    for trade_date, group in data.groupby("trade_date", sort=True):
        core = group[group["is_core"]]
        observed = set(core["clock"])
        price_columns = ["open", "high", "low", "close"]
        invalid_ohlc = (core["high"] < core[["open", "close", "low"]].max(axis=1)) | (
            core["low"] > core[["open", "close", "high"]].min(axis=1)
        )
        per_day.append(
            {
                "trade_date": trade_date,
                "core_bar_count": len(core),
                "missing_core_times": sorted(EXPECTED_CORE_TIMES - observed),
                "unexpected_times": sorted(set(group["clock"]) - EXPECTED_CORE_TIMES),
                "duplicate_timestamps": int(group["timestamp"].duplicated().sum()),
                "zero_volume_core_bars": int((core["volume"] == 0).sum()),
                "zero_or_negative_price_core_bars": int((core[price_columns] <= 0).any(axis=1).sum()),
                "null_price_core_bars": int(core[price_columns].isna().any(axis=1).sum()),
                "invalid_ohlc_core_bars": int(invalid_ohlc.sum()),
            }
        )
    complete_days = sum(
        item["core_bar_count"] == 48
        and not item["missing_core_times"]
        and item["duplicate_timestamps"] == 0
        and item["zero_volume_core_bars"] == 0
        and item["zero_or_negative_price_core_bars"] == 0
        and item["null_price_core_bars"] == 0
        and item["invalid_ohlc_core_bars"] == 0
        for item in per_day
    )
    return {
        "native_interval_minutes": 5,
        "timezone": "Asia/Shanghai",
        "bar_timestamp_convention": "provider timestamp; treated as 5-minute bar end for session checks",
        "observed_bar_count": len(data),
        "observed_trade_days": int(data["trade_date"].nunique()),
        "complete_core_days": complete_days,
        "expected_core_bars_per_full_day": 48,
        "first_timestamp": data["timestamp"].min().isoformat(sep=" "),
        "last_timestamp": data["timestamp"].max().isoformat(sep=" "),
        "duplicate_timestamps_total": int(data["timestamp"].duplicated().sum()),
        "zero_volume_bars_total": int((data["volume"] == 0).sum()),
        "zero_or_negative_price_bars_total": int(
            (data[["open", "high", "low", "close"]] <= 0).any(axis=1).sum()
        ),
        "invalid_ohlc_bars_total": int(
            ((data["high"] < data[["open", "close", "low"]].max(axis=1)) | (data["low"] > data[["open", "close", "high"]].min(axis=1))).sum()
        ),
        "daily_session_detail": per_day,
    }


def one_minute_probe_summary(frame: pd.DataFrame) -> dict[str, Any]:
    """Describe the returned 1-minute window without session-completeness claims."""

    if frame.empty:
        raise ValueError("cannot describe an empty 1-minute dataset")
    return {
        "native_interval_minutes": 1,
        "provider_ndays_parameter": 5,
        "observed_bar_count": len(frame),
        "observed_trade_days": int(frame["timestamp"].dt.date.nunique()),
        "first_timestamp": frame["timestamp"].min().isoformat(sep=" "),
        "last_timestamp": frame["timestamp"].max().isoformat(sep=" "),
    }


def run_pilot(
    symbol: str,
    workspace: Path,
    *,
    exchange: str | None = None,
    include_one_minute_probe: bool = True,
) -> dict[str, Any]:
    """Fetch once, retain raw/normalized local files, and return a JSON-safe manifest."""

    acquired_at = datetime.now().astimezone().isoformat(timespec="seconds")
    normalized_exchange = _normalize_exchange(symbol, exchange)
    payload = fetch_eastmoney_etf_5m(symbol, exchange=normalized_exchange)
    frame = normalize_klines(payload)
    summary = quality_summary(frame)
    local_name = f"{symbol}_5m_latest"
    raw_dir = workspace / "data" / "raw" / local_name
    interim_dir = workspace / "data" / "interim" / local_name
    report_dir = workspace / "reports" / "generated" / "data_pilots"
    for directory in (raw_dir, interim_dir, report_dir):
        directory.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / "eastmoney_response.json"
    normalized_path = interim_dir / "bars.csv"
    report_path = report_dir / f"{symbol}_5m_quality.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    frame.to_csv(normalized_path, index=False)
    manifest = {
        "symbol": symbol,
        "exchange": normalized_exchange,
        "source": "Direct Eastmoney public endpoint; field definitions compatible with AKShare ETF interfaces",
        "source_url": EASTMONEY_KLINE_URL,
        "acquired_at": acquired_at,
        "adjustment": "none (fqt=0)",
        "units": {"price": "CNY", "volume": "provider-reported shares", "turnover": "CNY"},
        "raw_path": str(raw_path.relative_to(workspace)),
        "normalized_path": str(normalized_path.relative_to(workspace)),
        "quality": summary,
        "acceptance": {
            "target_complete_trading_days": 30,
            "meets_30_day_target": summary["complete_core_days"] >= 30,
            "note": "A false result indicates source coverage is insufficient; it is not filled by resampling or interpolation.",
        },
    }
    if include_one_minute_probe:
        probe_payload = fetch_eastmoney_etf_1m(symbol, exchange=normalized_exchange)
        probe_frame = normalize_trends(probe_payload)
        probe_summary = one_minute_probe_summary(probe_frame)
        probe_name = f"{symbol}_1m_probe"
        probe_raw_dir = workspace / "data" / "raw" / probe_name
        probe_interim_dir = workspace / "data" / "interim" / probe_name
        probe_raw_dir.mkdir(parents=True, exist_ok=True)
        probe_interim_dir.mkdir(parents=True, exist_ok=True)
        probe_raw_path = probe_raw_dir / "eastmoney_response.json"
        probe_normalized_path = probe_interim_dir / "bars.csv"
        probe_raw_path.write_text(
            json.dumps(probe_payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        probe_frame.to_csv(probe_normalized_path, index=False)
        manifest["one_minute_probe"] = {
            "source_url": EASTMONEY_TRENDS_URL,
            "raw_path": str(probe_raw_path.relative_to(workspace)),
            "normalized_path": str(probe_normalized_path.relative_to(workspace)),
            "quality": probe_summary,
            "note": "Provider retention observed in this run; this is a reproducible probe, not a 30-day 1-minute dataset.",
        }
    report_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="159567")
    parser.add_argument("--exchange", choices=("SSE", "SZSE"))
    parser.add_argument("--skip-one-minute-probe", action="store_true")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(
        json.dumps(
            run_pilot(
                args.symbol,
                args.workspace,
                exchange=args.exchange,
                include_one_minute_probe=not args.skip_one_minute_probe,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
