"""Target-only detail queries for the multi-ETF local research workbench."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from etf_t0.data_pilot import EXPECTED_CORE_TIMES
from etf_t0.forward_collection import EXPECTED_ONE_MINUTE_TIMES
from etf_t0.research_store import InstrumentCapability, ResearchStore
from etf_t0.trend_research import (
    CompletedUptrend,
    TrendBar,
    TrendDetectionParameters,
    detect_completed_uptrends,
)


@dataclass(frozen=True)
class TargetTrendDetail:
    """One selected target ETF’s native bars and descriptive completed intervals."""

    capability: InstrumentCapability
    status: str
    trade_date: date
    interval_minutes: int | None
    bars: tuple[tuple[str, str, str, str, str], ...]
    completed_uptrends: tuple[CompletedUptrend, ...]


class ResearchWorkbenchService:
    """Read one target at a time while retaining a multi-ETF discovery page."""

    def __init__(
        self,
        *,
        store: ResearchStore,
        parameters: TrendDetectionParameters,
        clock: Callable[[], str],
    ) -> None:
        self._store = store
        self._parameters = parameters
        self._clock = clock

    def list_instruments(self) -> tuple[InstrumentCapability, ...]:
        return self._store.list_instrument_capabilities()

    def target_detail(self, code: str, *, trade_date: date) -> TargetTrendDetail:
        """Prefer native one-minute bars, then labelled native five-minute fallback."""

        capability = next(
            (item for item in self.list_instruments() if item.code == code), None
        )
        if capability is None:
            raise ValueError(f"unknown research ETF: {code}")
        calculated_at = self._clock()
        one_minute_bars = self._store.bars_for_day(code, trade_date, interval_minutes=1)
        five_minute_bars = self._store.bars_for_day(code, trade_date, interval_minutes=5)
        one_minute_complete = _is_completed_core_day(
            bars=one_minute_bars,
            interval_minutes=1,
            trade_date=trade_date,
            calculated_at=calculated_at,
        )
        five_minute_complete = _is_completed_core_day(
            bars=five_minute_bars,
            interval_minutes=5,
            trade_date=trade_date,
            calculated_at=calculated_at,
        )
        interval_minutes: int | None
        bars: tuple[tuple[str, str, str, str, str], ...]
        if one_minute_complete:
            interval_minutes, bars = 1, one_minute_bars
        elif five_minute_complete:
            interval_minutes, bars = 5, five_minute_bars
        elif one_minute_bars:
            interval_minutes, bars = 1, one_minute_bars
        elif five_minute_bars:
            interval_minutes, bars = 5, five_minute_bars
        else:
            interval_minutes, bars = None, ()
        if interval_minutes is None:
            return TargetTrendDetail(
                capability=capability,
                status="WAIT_DATA",
                trade_date=trade_date,
                interval_minutes=None,
                bars=(),
                completed_uptrends=(),
            )
        if not (one_minute_complete or five_minute_complete):
            return TargetTrendDetail(
                capability=capability,
                status="WAIT_COMPLETE_DAY",
                trade_date=trade_date,
                interval_minutes=interval_minutes,
                bars=bars,
                completed_uptrends=(),
            )
        trend_bars = tuple(TrendBar(ended_at=row[0], close=Decimal(row[2])) for row in bars)
        intervals = detect_completed_uptrends(trend_bars, parameters=self._parameters)
        self._store.store_completed_uptrends(
            code=code,
            trade_date=trade_date,
            interval_minutes=interval_minutes,
            parameters=self._parameters,
            input_bar_sha256=self._store.bar_input_fingerprint(
                code, trade_date, interval_minutes
            ),
            input_latest_bar_end=bars[-1][0],
            calculated_at=calculated_at,
            intervals=intervals,
        )
        return TargetTrendDetail(
            capability=capability,
            status="RESEARCH_READY",
            trade_date=trade_date,
            interval_minutes=interval_minutes,
            bars=bars,
            completed_uptrends=intervals,
        )


def load_trend_detection_parameters(path) -> TrendDetectionParameters:
    """Load the versioned, predeclared descriptive interval contract."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported trend detection configuration")
    parameters = TrendDetectionParameters(
        version=str(payload["version"]),
        minimum_duration_bars=int(payload["minimum_duration_bars"]),
        minimum_rise_bps=Decimal(str(payload["minimum_rise_bps"])),
        maximum_pullback_bps=Decimal(str(payload["maximum_pullback_bps"])),
    )
    parameters.validate()
    return parameters


def _is_completed_core_day(
    *,
    bars: tuple[tuple[str, str, str, str, str], ...],
    interval_minutes: int,
    trade_date: date,
    calculated_at: str,
) -> bool:
    """Require an exact native-session label set before calling a day complete.

    The `09:30` one-minute label remains a provider-label convention; this gate
    merely verifies that it is present as required by the retained native series.
    It never promotes a partial intraday capture to an end-of-day result.
    """

    expected = EXPECTED_ONE_MINUTE_TIMES if interval_minutes == 1 else EXPECTED_CORE_TIMES
    clocks = [datetime.fromisoformat(row[0]).strftime("%H:%M") for row in bars]
    if len(clocks) != len(expected) or set(clocks) != expected or len(set(clocks)) != len(clocks):
        return False
    try:
        for _ended_at, open_price, close_price, high_price, low_price in bars:
            opening, closing, high, low = map(
                Decimal, (open_price, close_price, high_price, low_price)
            )
            if min(opening, closing, high, low) <= 0 or low > min(opening, closing):
                return False
            if high < max(opening, closing):
                return False
    except ArithmeticError:
        return False
    completed_at = datetime.fromisoformat(calculated_at)
    if completed_at.date() > trade_date:
        return True
    return completed_at.date() == trade_date and completed_at.time() >= time(15, 7)
