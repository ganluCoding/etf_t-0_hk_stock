"""Descriptive, post-close continuous-uptrend research over native bar closes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TrendBar:
    """One completed native bar represented only by its known close."""

    ended_at: str
    close: Decimal


@dataclass(frozen=True)
class TrendDetectionParameters:
    """Predeclared descriptive thresholds retained with every result."""

    version: str
    minimum_duration_bars: int
    minimum_rise_bps: Decimal
    maximum_pullback_bps: Decimal

    def validate(self) -> None:
        if not self.version:
            raise ValueError("trend detection version is required")
        if self.minimum_duration_bars < 2:
            raise ValueError("minimum duration requires at least two bars")
        if self.minimum_rise_bps <= 0 or self.maximum_pullback_bps < 0:
            raise ValueError("trend thresholds must be non-negative and rise positive")


@dataclass(frozen=True)
class CompletedUptrend:
    """A completed descriptive interval, not an order or future-price forecast."""

    start_at: str
    end_at: str
    duration_bars: int
    start_close: Decimal
    end_close: Decimal
    rise_bps: Decimal
    maximum_pullback_bps: Decimal
    detection_version: str
    executable_profit_status: str = "NO_EXECUTABLE_QUOTES"


def detect_completed_uptrends(
    bars: tuple[TrendBar, ...], *, parameters: TrendDetectionParameters
) -> tuple[CompletedUptrend, ...]:
    """Find non-overlapping rising close-to-close intervals after a completed day.

    The function deliberately uses completed closes only.  It cannot assert that
    a bar high or low was executable, and therefore labels every result as lacking
    executable-profit evidence until a separate quote-aware calculation exists.
    """

    parameters.validate()
    if len(bars) < parameters.minimum_duration_bars:
        return ()
    _validate_bars(bars)
    intervals: list[CompletedUptrend] = []
    start_index = 0
    peak_index = 0
    maximum_pullback_bps = Decimal(0)
    maximum_pullback_at_peak_bps = Decimal(0)
    for index in range(1, len(bars)):
        if bars[index].close >= bars[peak_index].close:
            peak_index = index
            maximum_pullback_at_peak_bps = maximum_pullback_bps
        pullback_bps = _drawdown_bps(bars[peak_index].close, bars[index].close)
        maximum_pullback_bps = max(maximum_pullback_bps, pullback_bps)
        if pullback_bps > parameters.maximum_pullback_bps:
            _append_if_qualifying(
                intervals,
                bars,
                start_index=start_index,
                end_index=peak_index,
                maximum_pullback_bps=maximum_pullback_at_peak_bps,
                parameters=parameters,
            )
            start_index = index
            peak_index = index
            maximum_pullback_bps = Decimal(0)
            maximum_pullback_at_peak_bps = Decimal(0)
    _append_if_qualifying(
        intervals,
        bars,
        start_index=start_index,
        end_index=peak_index,
        maximum_pullback_bps=maximum_pullback_at_peak_bps,
        parameters=parameters,
    )
    return tuple(intervals)


def _append_if_qualifying(
    intervals: list[CompletedUptrend],
    bars: tuple[TrendBar, ...],
    *,
    start_index: int,
    end_index: int,
    maximum_pullback_bps: Decimal,
    parameters: TrendDetectionParameters,
) -> None:
    duration_bars = end_index - start_index + 1
    rise_bps = _basis_points(bars[start_index].close, bars[end_index].close)
    if (
        duration_bars >= parameters.minimum_duration_bars
        and rise_bps >= parameters.minimum_rise_bps
    ):
        intervals.append(
            CompletedUptrend(
                start_at=bars[start_index].ended_at,
                end_at=bars[end_index].ended_at,
                duration_bars=duration_bars,
                start_close=bars[start_index].close,
                end_close=bars[end_index].close,
                rise_bps=rise_bps,
                maximum_pullback_bps=maximum_pullback_bps,
                detection_version=parameters.version,
            )
        )


def _basis_points(base: Decimal, value: Decimal) -> Decimal:
    if base <= 0:
        raise ValueError("bar close must be positive")
    return (value - base) / base * Decimal(10_000)


def _drawdown_bps(peak: Decimal, value: Decimal) -> Decimal:
    if peak <= 0:
        raise ValueError("bar close must be positive")
    return (peak - value) / peak * Decimal(10_000)


def _validate_bars(bars: tuple[TrendBar, ...]) -> None:
    previous: str | None = None
    for bar in bars:
        if bar.close <= 0:
            raise ValueError("bar close must be positive")
        if previous is not None and bar.ended_at <= previous:
            raise ValueError("completed bars must be strictly time ordered")
        previous = bar.ended_at
