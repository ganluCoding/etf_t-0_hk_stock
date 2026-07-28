from __future__ import annotations

from datetime import date
from pathlib import Path

from etf_t0.data_pilot import EXPECTED_CORE_TIMES
from etf_t0.forward_collection import EXPECTED_ONE_MINUTE_TIMES
from etf_t0.research_store import ResearchStore
from etf_t0.trend_research import TrendDetectionParameters
from etf_t0.universe import load_universe_ledger
from etf_t0.workbench_service import (
    ResearchWorkbenchService,
    load_trend_detection_parameters,
)

LEDGER_PATH = Path("config/universe/t0_etf_ledger.json")
TREND_CONFIG_PATH = Path("config/trend_detection.json")


def _write_complete_one_minute_day(path: Path) -> None:
    rows = ["timestamp,open,close,high,low,volume,turnover"]
    for index, clock in enumerate(sorted(EXPECTED_ONE_MINUTE_TIMES)):
        close = "1.000"
        if index == 1:
            close = "1.002"
        elif index == 2:
            close = "1.004"
        elif index == 3:
            close = "1.005"
        elif index == 4:
            close = "1.001"
        rows.append(
            f"2026-07-28 {clock}:00,1.000,{close},1.005,0.999,100,100"
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_complete_five_minute_day(path: Path) -> None:
    rows = ["timestamp,open,close,high,low,volume,turnover"]
    for clock in sorted(EXPECTED_CORE_TIMES):
        rows.append(f"2026-07-28 {clock}:00,1.000,1.000,1.000,1.000,100,100")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_versioned_trend_configuration_is_loadable() -> None:
    parameters = load_trend_detection_parameters(TREND_CONFIG_PATH)

    assert parameters.version == "m3-uptrend-close-v1"
    assert parameters.minimum_duration_bars == 5


def test_target_detail_prefers_native_one_minute_bars_and_persists_uptrends(
    tmp_path: Path,
) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    bars = tmp_path / "159567_1m.csv"
    raw_payload = tmp_path / "159567.json"
    _write_complete_one_minute_day(bars)
    raw_payload.write_text('{"source":"fixture"}', encoding="utf-8")
    store.ingest_native_bar_csv(
        code="159567",
        interval_minutes=1,
        csv_path=bars,
        raw_payload_path=raw_payload,
        acquired_at="2026-07-28T15:10:00+08:00",
        source_name="fixture",
    )
    service = ResearchWorkbenchService(
        store=store,
        parameters=TrendDetectionParameters(
            version="m3-uptrend-v1",
            minimum_duration_bars=3,
            minimum_rise_bps=20,
            maximum_pullback_bps=20,
        ),
        clock=lambda: "2026-07-28T15:10:00+08:00",
    )

    detail = service.target_detail("159567", trade_date=date(2026, 7, 28))

    assert detail.status == "RESEARCH_READY"
    assert detail.interval_minutes == 1
    assert len(detail.bars) == len(EXPECTED_ONE_MINUTE_TIMES)
    assert len(detail.completed_uptrends) == 1
    assert detail.completed_uptrends[0].executable_profit_status == "NO_EXECUTABLE_QUOTES"
    assert store.completed_uptrends_for_day("159567", date(2026, 7, 28)) == ()
    service.persist_completed_trends("159567", trade_date=date(2026, 7, 28))
    assert len(store.completed_uptrends_for_day("159567", date(2026, 7, 28))) == 1


def test_partial_session_never_creates_completed_trend_intervals(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    bars = tmp_path / "159567_partial.csv"
    raw_payload = tmp_path / "159567.json"
    bars.write_text(
        "timestamp,open,close,high,low,volume,turnover\n"
        "2026-07-28 09:30:00,1,1,1,1,100,100\n",
        encoding="utf-8",
    )
    raw_payload.write_text('{"source":"fixture"}', encoding="utf-8")
    store.ingest_native_bar_csv(
        code="159567", interval_minutes=1, csv_path=bars, raw_payload_path=raw_payload,
        acquired_at="2026-07-28T09:31:00+08:00", source_name="fixture"
    )
    service = ResearchWorkbenchService(
        store=store,
        parameters=TrendDetectionParameters("m3-uptrend-v1", 3, 20, 20),
        clock=lambda: "2026-07-28T10:00:00+08:00",
    )

    detail = service.target_detail("159567", trade_date=date(2026, 7, 28))

    assert detail.status == "WAIT_COMPLETE_DAY"
    assert detail.completed_uptrends == ()


def test_complete_time_labels_with_zero_ohlc_return_data_quality_wait_state(
    tmp_path: Path,
) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    bars, raw_payload = tmp_path / "zero_open.csv", tmp_path / "raw.json"
    _write_complete_one_minute_day(bars)
    bars.write_text(
        bars.read_text(encoding="utf-8").replace(
            "2026-07-28 09:30:00,1.000,1.000", "2026-07-28 09:30:00,0.000,1.000"
        ),
        encoding="utf-8",
    )
    raw_payload.write_text('{"source":"fixture"}', encoding="utf-8")
    store.ingest_native_bar_csv(
        code="159567", interval_minutes=1, csv_path=bars, raw_payload_path=raw_payload,
        acquired_at="2026-07-28T15:10:00+08:00", source_name="fixture"
    )
    service = ResearchWorkbenchService(
        store=store,
        parameters=TrendDetectionParameters("m3-uptrend-v1", 3, 20, 20),
        clock=lambda: "2026-07-28T15:10:00+08:00",
    )

    detail = service.target_detail("159567", trade_date=date(2026, 7, 28))

    assert detail.status == "WAIT_DATA_QUALITY"
    assert detail.completed_uptrends == ()


def test_complete_five_minute_day_beats_incomplete_one_minute_day(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    one_minute, five_minute, raw = tmp_path / "one.csv", tmp_path / "five.csv", tmp_path / "raw.json"
    one_minute.write_text(
        "timestamp,open,close,high,low,volume,turnover\n2026-07-28 09:30:00,1,1,1,1,1,1\n",
        encoding="utf-8",
    )
    _write_complete_five_minute_day(five_minute)
    raw.write_text('{"source":"fixture"}', encoding="utf-8")
    for interval_minutes, path in ((1, one_minute), (5, five_minute)):
        store.ingest_native_bar_csv(
            code="159567", interval_minutes=interval_minutes, csv_path=path,
            raw_payload_path=raw, acquired_at="2026-07-28T15:10:00+08:00", source_name="fixture"
        )
    service = ResearchWorkbenchService(
        store=store,
        parameters=TrendDetectionParameters("m3-uptrend-v1", 3, 20, 20),
        clock=lambda: "2026-07-28T15:10:00+08:00",
    )

    detail = service.target_detail("159567", trade_date=date(2026, 7, 28))

    assert detail.status == "RESEARCH_READY"
    assert detail.interval_minutes == 5


def test_target_detail_returns_wait_data_without_borrowing_another_etf_series(
    tmp_path: Path,
) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    service = ResearchWorkbenchService(
        store=store,
        parameters=TrendDetectionParameters(
            version="m3-uptrend-v1",
            minimum_duration_bars=3,
            minimum_rise_bps=20,
            maximum_pullback_bps=20,
        ),
        clock=lambda: "2026-07-28T15:10:00+08:00",
    )

    detail = service.target_detail("159920", trade_date=date(2026, 7, 28))

    assert detail.status == "WAIT_DATA"
    assert detail.bars == ()
    assert detail.completed_uptrends == ()
