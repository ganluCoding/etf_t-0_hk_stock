from datetime import datetime

import pandas as pd
import pytest

from etf_t0.data_pilot import EXPECTED_CORE_TIMES, eastmoney_secid, quality_summary


def test_expected_core_session_has_48_five_minute_bar_ends() -> None:
    assert len(EXPECTED_CORE_TIMES) == 48
    assert "09:35" in EXPECTED_CORE_TIMES
    assert "11:30" in EXPECTED_CORE_TIMES
    assert "13:05" in EXPECTED_CORE_TIMES
    assert "15:00" in EXPECTED_CORE_TIMES


def test_eastmoney_market_identifier_supports_both_etf_exchanges() -> None:
    assert eastmoney_secid("159567") == "0.159567"
    assert eastmoney_secid("513120") == "1.513120"
    assert eastmoney_secid("159567", "SZSE") == "0.159567"
    assert eastmoney_secid("513120", "SSE") == "1.513120"


def test_eastmoney_market_identifier_fails_closed_for_ambiguous_code() -> None:
    with pytest.raises(ValueError, match="cannot infer"):
        eastmoney_secid("000001")
    with pytest.raises(ValueError, match="exchange"):
        eastmoney_secid("159567", "HKEX")
    with pytest.raises(ValueError, match="conflicts"):
        eastmoney_secid("159567", "SSE")
    with pytest.raises(ValueError, match="conflicts"):
        eastmoney_secid("513120", "SZSE")


def test_quality_summary_reports_a_complete_session_and_missing_bar() -> None:
    timestamps = [
        datetime.fromisoformat(f"2026-07-24 {clock}:00") for clock in sorted(EXPECTED_CORE_TIMES)
    ]
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 1.0,
            "close": 1.0,
            "high": 1.0,
            "low": 1.0,
            "volume": 100,
            "turnover": 100.0,
            "amplitude_pct": 0.0,
            "change_pct": 0.0,
            "change_amount": 0.0,
            "turnover_rate_pct": 0.0,
        }
    )

    complete = quality_summary(frame)
    incomplete = quality_summary(frame[frame["timestamp"].dt.strftime("%H:%M") != "10:00"])

    assert complete["complete_core_days"] == 1
    assert incomplete["complete_core_days"] == 0
    assert incomplete["daily_session_detail"][0]["missing_core_times"] == ["10:00"]


def test_quality_summary_rejects_zero_volume_zero_price_and_invalid_ohlc() -> None:
    timestamps = [
        datetime.fromisoformat(f"2026-07-24 {clock}:00") for clock in sorted(EXPECTED_CORE_TIMES)
    ]
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 1.0,
            "close": 1.0,
            "high": 1.0,
            "low": 1.0,
            "volume": 100,
            "turnover": 100.0,
            "amplitude_pct": 0.0,
            "change_pct": 0.0,
            "change_amount": 0.0,
            "turnover_rate_pct": 0.0,
        }
    )
    frame.loc[0, "volume"] = 0
    frame.loc[1, "open"] = 0
    frame.loc[2, "high"] = 0.5

    summary = quality_summary(frame)
    detail = summary["daily_session_detail"][0]

    assert summary["complete_core_days"] == 0
    assert detail["zero_volume_core_bars"] == 1
    assert detail["zero_or_negative_price_core_bars"] == 1
    assert detail["invalid_ohlc_core_bars"] >= 1
