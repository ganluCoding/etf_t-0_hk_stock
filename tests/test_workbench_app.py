from __future__ import annotations

import os
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QTableWidget

from etf_t0.research_store import ResearchStore
from etf_t0.trend_research import TrendDetectionParameters
from etf_t0.universe import load_universe_ledger
from etf_t0.workbench_app import WorkbenchWindow
from etf_t0.workbench_service import ResearchWorkbenchService

LEDGER_PATH = Path("config/universe/t0_etf_ledger.json")


def test_workbench_shows_all_research_etfs_and_target_only_detail(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.sync_instruments(load_universe_ledger(LEDGER_PATH))
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
    app = QApplication.instance() or QApplication([])
    window = WorkbenchWindow(service=service, trade_date=date(2026, 7, 28))
    window.show()
    table = window.findChild(QTableWidget, "instrumentTable")

    assert table.rowCount() == 16
    window.show_target("159567")
    app.processEvents()
    assert "159567" in window.findChild(QLabel, "detailTitle").text()
    assert "连续上涨区间" in window.findChild(QLabel, "trendSummary").text()
    assert "513780" not in window.detail_text_snapshot()
