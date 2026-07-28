from __future__ import annotations

from decimal import Decimal

from etf_t0.trend_research import (
    TrendBar,
    TrendDetectionParameters,
    detect_completed_uptrends,
)


def test_completed_uptrend_is_parameterized_and_never_claims_executable_profit() -> None:
    bars = (
        TrendBar("2026-07-28T09:30:00+08:00", Decimal("1.000")),
        TrendBar("2026-07-28T09:31:00+08:00", Decimal("1.002")),
        TrendBar("2026-07-28T09:32:00+08:00", Decimal("1.004")),
        TrendBar("2026-07-28T09:33:00+08:00", Decimal("1.005")),
        TrendBar("2026-07-28T09:34:00+08:00", Decimal("1.001")),
    )
    parameters = TrendDetectionParameters(
        version="m3-uptrend-v1",
        minimum_duration_bars=3,
        minimum_rise_bps=Decimal(20),
        maximum_pullback_bps=Decimal(20),
    )

    intervals = detect_completed_uptrends(bars, parameters=parameters)

    assert len(intervals) == 1
    interval = intervals[0]
    assert interval.start_at == "2026-07-28T09:30:00+08:00"
    assert interval.end_at == "2026-07-28T09:33:00+08:00"
    assert interval.rise_bps == Decimal("50.0")
    assert interval.maximum_pullback_bps == Decimal(0)
    assert interval.detection_version == "m3-uptrend-v1"
    assert interval.executable_profit_status == "NO_EXECUTABLE_QUOTES"


def test_incomplete_or_too_small_rise_is_not_reported_as_an_uptrend() -> None:
    bars = (
        TrendBar("2026-07-28T09:30:00+08:00", Decimal("1.000")),
        TrendBar("2026-07-28T09:31:00+08:00", Decimal("1.001")),
        TrendBar("2026-07-28T09:32:00+08:00", Decimal("1.001")),
    )
    parameters = TrendDetectionParameters(
        version="m3-uptrend-v1",
        minimum_duration_bars=3,
        minimum_rise_bps=Decimal(20),
        maximum_pullback_bps=Decimal(20),
    )

    assert detect_completed_uptrends(bars, parameters=parameters) == ()


def test_pullback_is_positive_loss_from_peak_and_splits_the_interval() -> None:
    bars = (
        TrendBar("2026-07-28T09:30:00+08:00", Decimal("1.000")),
        TrendBar("2026-07-28T09:31:00+08:00", Decimal("1.010")),
        TrendBar("2026-07-28T09:32:00+08:00", Decimal("1.020")),
        TrendBar("2026-07-28T09:33:00+08:00", Decimal("0.990")),
        TrendBar("2026-07-28T09:34:00+08:00", Decimal("1.040")),
    )
    parameters = TrendDetectionParameters(
        version="m3-uptrend-v1",
        minimum_duration_bars=3,
        minimum_rise_bps=Decimal(20),
        maximum_pullback_bps=Decimal(12),
    )

    intervals = detect_completed_uptrends(bars, parameters=parameters)

    assert len(intervals) == 1
    assert intervals[0].end_at == "2026-07-28T09:32:00+08:00"
    assert intervals[0].maximum_pullback_bps == Decimal(0)


def test_terminal_pullback_is_not_attributed_to_an_interval_ending_at_the_peak() -> None:
    bars = (
        TrendBar("2026-07-28T09:30:00+08:00", Decimal("1.000")),
        TrendBar("2026-07-28T09:31:00+08:00", Decimal("1.010")),
        TrendBar("2026-07-28T09:32:00+08:00", Decimal("1.009")),
    )
    parameters = TrendDetectionParameters("m3-uptrend-v1", 2, Decimal(20), Decimal(20))

    (interval,) = detect_completed_uptrends(bars, parameters=parameters)

    assert interval.end_at == "2026-07-28T09:31:00+08:00"
    assert interval.maximum_pullback_bps == Decimal(0)
