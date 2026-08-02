from __future__ import annotations

import os
import sqlite3
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import (
    QApplication,
    QDateEdit,
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTextEdit,
)

from etf_t0.forward_collection import EXPECTED_ONE_MINUTE_TIMES
from etf_t0.research_store import ResearchStore
from etf_t0.trend_research import TrendDetectionParameters
from etf_t0.universe import load_universe_ledger
from etf_t0.workbench_app import WorkbenchWindow, _format_day_stats
from etf_t0.workbench_service import ResearchWorkbenchService, TargetTrendDetail

LEDGER_PATH = Path("config/universe/t0_etf_ledger.json")
EXPECTED_CODES = frozenset(record.code for record in load_universe_ledger(LEDGER_PATH))


def test_workbench_shows_all_research_etfs_and_target_only_detail(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    records = load_universe_ledger(LEDGER_PATH)
    store.sync_instruments(records)
    bars = tmp_path / "159567_1m.csv"
    raw_payload = tmp_path / "159567.json"
    bars.write_text(
        "timestamp,open,close,high,low,volume,turnover\n"
        "2026-07-28 09:30:00,1.000,1.000,1.000,1.000,100,100\n"
        "2026-07-28 09:31:00,1.000,1.002,1.002,1.000,100,100\n"
        "2026-07-28 09:32:00,1.002,1.004,1.004,1.002,100,100\n"
        "2026-07-28 09:33:00,1.004,1.005,1.005,1.004,100,100\n"
        "2026-07-28 09:34:00,1.005,1.001,1.005,1.001,100,100\n",
        encoding="utf-8",
    )
    raw_payload.write_text('{"source":"fixture"}', encoding="utf-8")
    store.ingest_native_bar_csv(
        code="159567",
        interval_minutes=1,
        csv_path=bars,
        raw_payload_path=raw_payload,
        acquired_at="2026-07-28T15:10:00+08:00",
        source_name="fixture",
    )
    read_only_store = ResearchStore.open_read_only(tmp_path / "research.sqlite3")
    service = ResearchWorkbenchService(
        store=read_only_store,
        parameters=TrendDetectionParameters(
            version="m3-uptrend-v1",
            minimum_duration_bars=3,
            minimum_rise_bps=20,
            maximum_pullback_bps=20,
        ),
        clock=lambda: "2026-07-28T15:10:00+08:00",
        expected_instrument_codes=EXPECTED_CODES,
    )
    app = QApplication.instance() or QApplication([])
    window = WorkbenchWindow(service=service, trade_date=date(2026, 7, 28))
    window.show()
    table = window.findChild(QTableWidget, "instrumentTable")

    assert table.rowCount() == 16
    assert table.columnCount() == 2
    assert table.horizontalHeaderItem(0).text() == "代码"
    assert table.horizontalHeaderItem(1).text() == "ETF名称"
    assert table.item(0, 0).text().isdigit()
    assert table.item(0, 1).text()
    window.show_target("159567")
    app.processEvents()
    assert "159567" in window.findChild(QLabel, "detailTitle").text()
    assert "连续上涨区间" in window.findChild(QLabel, "trendSummary").text()
    assert "513780" not in window.detail_text_snapshot()
    assert window.findChild(QFrame, "overviewPanel") is not None
    assert window.findChild(QFrame, "chartPanel") is not None
    assert window.findChild(QFrame, "intervalPanel") is not None
    assert window.findChild(QFrame, "researchBoundaryPanel") is not None
    assert window.findChild(QScrollArea, "detailScrollArea") is not None
    interval_list = window.findChild(QTextEdit, "intervalList")
    assert interval_list is not None
    assert interval_list.minimumHeight() >= 170
    assert interval_list.isReadOnly()


def test_zero_provider_open_is_shown_as_data_quality_problem_not_divided_by_zero(
    tmp_path: Path,
) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    records = load_universe_ledger(LEDGER_PATH)
    store.sync_instruments(records)
    capability = next(item for item in store.list_instrument_capabilities() if item.code == "159567")
    detail = TargetTrendDetail(
        capability=capability,
        status="WAIT_COMPLETE_DAY",
        trade_date=date(2026, 7, 22),
        interval_minutes=1,
        bars=(("2026-07-22T09:30:00+08:00", "0.0", "0.688", "0.688", "0.688"),),
        completed_uptrends=(),
    )

    assert "数据质量异常" in _format_day_stats(detail)


def test_workbench_opens_on_latest_complete_day_and_labels_empty_day_as_not_run(
    tmp_path: Path,
) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    records = load_universe_ledger(LEDGER_PATH)
    store.sync_instruments(records)
    bars = tmp_path / "159567_1m.csv"
    raw_payload = tmp_path / "159567.json"
    rows = ["timestamp,open,close,high,low,volume,turnover"]
    rows.extend(
        f"2026-07-28 {clock}:00,1.000,1.000,1.000,1.000,100,100"
        for clock in sorted(EXPECTED_ONE_MINUTE_TIMES)
    )
    bars.write_text("\n".join(rows) + "\n", encoding="utf-8")
    raw_payload.write_text('{"source":"fixture"}', encoding="utf-8")
    for record in records:
        store.ingest_native_bar_csv(
            code=record.code, interval_minutes=1, csv_path=bars,
            raw_payload_path=raw_payload, acquired_at="2026-07-28T15:10:00+08:00",
            source_name="fixture",
        )
    read_only_store = ResearchStore.open_read_only(tmp_path / "research.sqlite3")
    service = ResearchWorkbenchService(
        store=read_only_store,
        parameters=TrendDetectionParameters("m3-uptrend-v1", 3, 20, 20),
        clock=lambda: "2026-08-01T09:00:00+08:00",
        expected_instrument_codes=EXPECTED_CODES,
    )
    app = QApplication.instance() or QApplication([])
    window = WorkbenchWindow(service=service, trade_date=date(2026, 8, 1))
    window.show()
    app.processEvents()

    picker = window.findChild(QDateEdit, "tradeDatePicker")
    assert picker.date().toPython() == date(2026, 7, 28)
    assert "2026-07-28" in window.findChild(QLabel, "latestDataLabel").text()
    assert "覆盖 16/16" in window.findChild(QLabel, "latestDataLabel").text()
    assert picker.displayFormat() == "yyyy-MM-dd"

    picker.setDate(picker.date().addDays(4))
    window.show_target("159567")
    app.processEvents()

    empty_text = window.findChild(QTextEdit, "intervalList").toPlainText()
    assert "识别未运行" in empty_text
    assert "未达到当前预设" not in empty_text


def test_minimum_window_wraps_long_status_text_and_reload_reports_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
    read_only_store = ResearchStore.open_read_only(tmp_path / "research.sqlite3")
    service = ResearchWorkbenchService(
        store=read_only_store,
        parameters=TrendDetectionParameters("m3-uptrend-v1", 3, 20, 20),
        clock=lambda: "2026-08-01T09:00:00+08:00",
        expected_instrument_codes=EXPECTED_CODES,
    )
    app = QApplication.instance() or QApplication([])
    window = WorkbenchWindow(service=service, trade_date=date(2026, 8, 1))
    window.resize(1180, 760)
    window.show_target("159567")
    window.show()
    app.processEvents()

    detail_status = window.findChild(QLabel, "detailStatus")
    assert detail_status.wordWrap()
    assert detail_status.height() >= detail_status.fontMetrics().lineSpacing() * 7
    assert window.findChild(QLabel, "disclaimerLabel").wordWrap()
    scroll = window.findChild(QScrollArea, "detailScrollArea")
    assert scroll.verticalScrollBar().maximum() > 0
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    app.processEvents()
    boundary = window.findChild(QFrame, "researchBoundaryPanel")
    boundary_top = boundary.mapTo(scroll.viewport(), QPoint(0, 0)).y()
    boundary_bottom = boundary_top + boundary.height()
    assert boundary_top < scroll.viewport().height()
    assert boundary_bottom > 0
    button = window.findChild(QPushButton, "reloadButton")
    button.click()
    app.processEvents()
    assert window.findChild(QLabel, "reloadStatusLabel").text().startswith("读取成功：")

    def fail_to_list() -> None:
        raise sqlite3.OperationalError("fixture read failure")

    monkeypatch.setattr(service, "list_instruments", fail_to_list)
    button.click()
    app.processEvents()
    assert window.findChild(QLabel, "reloadStatusLabel").text().startswith("读取失败：")
