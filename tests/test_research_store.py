from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from etf_t0.break_even_ledger import PaperExecutionOutcome, PaperExecutionRecord
from etf_t0.fees import OrderSide
from etf_t0.research_store import ResearchStore
from etf_t0.research_workbench import bootstrap_workspace_database
from etf_t0.trend_research import (
    TrendBar,
    TrendDetectionParameters,
    detect_completed_uptrends,
)
from etf_t0.universe import load_universe_ledger
from etf_t0.workbench_service import ResearchWorkbenchService

LEDGER_PATH = Path("config/universe/t0_etf_ledger.json")


def _write_bars(path: Path, *, start_price: str) -> None:
    path.write_text(
        "timestamp,open,close,high,low,volume,turnover\n"
        f"2026-07-28 09:35:00,{start_price},1.002,1.003,0.999,1000,1002\n"
        "2026-07-28 09:40:00,1.002,1.004,1.005,1.001,1200,1204\n",
        encoding="utf-8",
    )


def test_store_lists_the_full_research_universe_and_isolates_target_bars(
    tmp_path: Path,
) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    first = tmp_path / "159567.csv"
    second = tmp_path / "159920.csv"
    first_raw = tmp_path / "159567.json"
    second_raw = tmp_path / "159920.json"
    _write_bars(first, start_price="1.000")
    _write_bars(second, start_price="2.000")
    first_raw.write_text('{"source":"159567"}', encoding="utf-8")
    second_raw.write_text('{"source":"159920"}', encoding="utf-8")

    store.ingest_native_bar_csv(
        code="159567",
        interval_minutes=5,
        csv_path=first,
        raw_payload_path=first_raw,
        acquired_at="2026-07-28T15:10:00+08:00",
        source_name="fixture",
    )
    store.ingest_native_bar_csv(
        code="159920",
        interval_minutes=5,
        csv_path=second,
        raw_payload_path=second_raw,
        acquired_at="2026-07-28T15:10:00+08:00",
        source_name="fixture",
    )

    instruments = store.list_instrument_capabilities()

    assert len(instruments) == 16
    assert {item.code for item in instruments} >= {"159567", "159920", "513780"}
    assert store.bars_for_day("159567", date(2026, 7, 28), interval_minutes=5) == (
        ("2026-07-28T09:35:00+08:00", "1.000", "1.002", "1.003", "0.999"),
        ("2026-07-28T09:40:00+08:00", "1.002", "1.004", "1.005", "1.001"),
    )
    assert store.bars_for_day("159920", date(2026, 7, 28), interval_minutes=5)[0][1] == "2.000"


def test_read_only_store_queries_without_mutating_or_accepting_writes(tmp_path: Path) -> None:
    database_path = tmp_path / "research.sqlite3"
    writer = ResearchStore(database_path)
    writer.sync_instruments(load_universe_ledger(LEDGER_PATH))
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()

    reader = ResearchStore.open_read_only(database_path)

    assert len(reader.list_instrument_capabilities()) == 16
    assert reader.bars_for_day("159567", date(2026, 7, 28), interval_minutes=1) == ()
    service = ResearchWorkbenchService(
        store=reader,
        parameters=TrendDetectionParameters("m3-uptrend-v1", 3, 20, 20),
        clock=lambda: "2026-08-01T09:00:00+08:00",
        expected_instrument_codes=frozenset(
            record.code for record in load_universe_ledger(LEDGER_PATH)
        ),
    )
    assert service.target_detail("159567", trade_date=date(2026, 7, 28)).status == "WAIT_DATA"
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.sync_instruments(load_universe_ledger(LEDGER_PATH))


def test_reingesting_the_same_bar_file_is_idempotent_and_keeps_lineage(
    tmp_path: Path,
) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    bars = tmp_path / "159567.csv"
    raw_payload = tmp_path / "159567.json"
    _write_bars(bars, start_price="1.000")
    raw_payload.write_text('{"source":"fixture"}', encoding="utf-8")

    for _ in range(2):
        store.ingest_native_bar_csv(
            code="159567",
            interval_minutes=5,
            csv_path=bars,
            raw_payload_path=raw_payload,
            acquired_at="2026-07-28T15:10:00+08:00",
            source_name="fixture",
        )

    assert len(store.bars_for_day("159567", date(2026, 7, 28), interval_minutes=5)) == 2
    lineage = store.bar_lineage("159567", "2026-07-28T09:35:00+08:00", 5)
    assert lineage.source_name == "fixture"
    assert lineage.raw_payload_path == str(raw_payload)
    assert len(lineage.raw_payload_sha256) == 64


def test_identical_raw_contents_do_not_overwrite_another_target_path(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    first_bars, second_bars = tmp_path / "first.csv", tmp_path / "second.csv"
    first_raw, second_raw = tmp_path / "first.json", tmp_path / "second.json"
    _write_bars(first_bars, start_price="1.000")
    _write_bars(second_bars, start_price="2.000")
    first_raw.write_text('{"same":"content"}', encoding="utf-8")
    second_raw.write_text('{"same":"content"}', encoding="utf-8")
    for code, bars, raw in (
        ("159567", first_bars, first_raw),
        ("159920", second_bars, second_raw),
    ):
        store.ingest_native_bar_csv(
            code=code, interval_minutes=5, csv_path=bars, raw_payload_path=raw,
            acquired_at="2026-07-28T15:10:00+08:00", source_name="fixture"
        )

    assert store.bar_lineage("159567", "2026-07-28T09:35:00+08:00", 5).raw_payload_path == str(first_raw)
    assert store.bar_lineage("159920", "2026-07-28T09:35:00+08:00", 5).raw_payload_path == str(second_raw)


def test_workspace_bootstrap_imports_quality_and_native_bars(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = workspace / "config/universe/t0_etf_ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(LEDGER_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    bars = workspace / "data/interim/159567_5m_latest/bars.csv"
    raw_payload = workspace / "data/raw/159567_5m_latest/eastmoney_response.json"
    report = workspace / "reports/generated/data_pilots/159567_5m_quality.json"
    bars.parent.mkdir(parents=True)
    raw_payload.parent.mkdir(parents=True)
    report.parent.mkdir(parents=True)
    _write_bars(bars, start_price="1.000")
    raw_payload.write_text('{"source":"fixture"}', encoding="utf-8")
    report.write_text(
        """{
          "acquired_at": "2026-07-28T15:10:00+08:00",
          "raw_path": "data/raw/159567_5m_latest/eastmoney_response.json",
          "normalized_path": "data/interim/159567_5m_latest/bars.csv",
          "quality": {"observed_trade_days": 31, "complete_core_days": 30}
        }""",
        encoding="utf-8",
    )

    store = bootstrap_workspace_database(workspace=workspace)
    capability = next(
        item for item in store.list_instrument_capabilities() if item.code == "159567"
    )

    assert capability.historical_five_minute_days == 30
    assert len(store.bars_for_day("159567", date(2026, 7, 28), interval_minutes=5)) == 2


def test_store_persists_reproducible_descriptive_trend_intervals(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    parameters = TrendDetectionParameters(
        version="m3-uptrend-v1",
        minimum_duration_bars=3,
        minimum_rise_bps=20,
        maximum_pullback_bps=20,
    )
    intervals = detect_completed_uptrends(
        (
            TrendBar("2026-07-28T09:30:00+08:00", Decimal("1.000")),
            TrendBar("2026-07-28T09:31:00+08:00", Decimal("1.002")),
            TrendBar("2026-07-28T09:32:00+08:00", Decimal("1.004")),
            TrendBar("2026-07-28T09:33:00+08:00", Decimal("1.005")),
            TrendBar("2026-07-28T09:34:00+08:00", Decimal("1.001")),
        ),
        parameters=parameters,
    )

    store.store_completed_uptrends(
        code="159567",
        trade_date=date(2026, 7, 28),
        interval_minutes=1,
        parameters=parameters,
        input_bar_sha256="a" * 64,
        input_latest_bar_end="2026-07-28T15:00:00+08:00",
        calculated_at="2026-07-28T15:10:00+08:00",
        intervals=intervals,
    )

    stored = store.completed_uptrends_for_day("159567", date(2026, 7, 28))
    assert len(stored) == 1
    assert stored[0].detection_version == "m3-uptrend-v1"
    assert stored[0].executable_profit_status == "NO_EXECUTABLE_QUOTES"

    store.store_completed_uptrends(
        code="159567",
        trade_date=date(2026, 7, 28),
        interval_minutes=1,
        parameters=parameters,
        input_bar_sha256="b" * 64,
        input_latest_bar_end="2026-07-28T15:00:00+08:00",
        calculated_at="2026-07-28T15:11:00+08:00",
        intervals=(),
    )
    assert store.completed_uptrends_for_day("159567", date(2026, 7, 28)) == ()


def test_store_ingests_target_isolated_quote_snapshots_with_raw_lineage(
    tmp_path: Path,
) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    quotes = tmp_path / "quotes.csv"
    raw_payload = tmp_path / "snapshots.jsonl"
    quotes.write_text(
        "capture_id,symbol,observed_at,last_price,bid1_price,ask1_price,iopv\n"
        "capture-a,159570,2026-07-28T13:18:23+08:00,1.405,1.404,1.405,1.4036\n"
        "capture-a,513780,2026-07-28T13:18:23+08:00,1.505,1.504,1.505,1.5044\n",
        encoding="utf-8",
    )
    raw_payload.write_text('{"source":"fixture"}\n', encoding="utf-8")

    assert (
        store.ingest_quote_csv(
            csv_path=quotes,
            raw_payload_path=raw_payload,
            acquired_at="2026-07-28T13:18:24+08:00",
            source_name="fixture_quote",
        )
        == 2
    )
    snapshot = store.latest_quote_snapshot("159570")

    assert snapshot.instrument_code == "159570"
    assert snapshot.bid1_price == "1.404"
    assert snapshot.ask1_price == "1.405"
    assert snapshot.raw_payload_path == str(raw_payload)
    assert store.latest_quote_snapshot("513780").last_price == "1.505"


def test_store_retains_manual_paper_execution_outcomes(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    record = PaperExecutionRecord(
        symbol="159567",
        observed_at=datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc),
        normal_overlap_day=True,
        intended_side=OrderSide.BUY,
        intended_price=Decimal("1.400"),
        intended_quantity=100,
        observed_bid1_price=Decimal("1.399"),
        observed_ask1_price=Decimal("1.400"),
        quote_source="manual broker-visible quote",
        fee_evidence="fee scenario v1",
        outcome=PaperExecutionOutcome.UNFILLED,
        outcome_reason="limit price was not reached",
    )

    store.store_paper_execution_record(record)

    assert store.paper_execution_records_for_symbol("159567") == (record,)
    assert store.paper_execution_records_for_symbol("159570") == ()
