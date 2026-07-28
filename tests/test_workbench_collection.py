from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from etf_t0.research_store import ResearchStore
from etf_t0.universe import confirmed_t0_records, load_universe_ledger
from etf_t0.workbench_collection import collect_universe_one_minute

SHANGHAI = ZoneInfo("Asia/Shanghai")
LEDGER_PATH = Path("config/universe/t0_etf_ledger.json")


def test_one_minute_collection_persists_each_confirmed_etf_independently(
    tmp_path: Path,
) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    records = confirmed_t0_records(load_universe_ledger(LEDGER_PATH))
    store.sync_instruments(records)

    def fetcher(symbol: str, *, exchange: str):
        return {
            "rc": 0,
            "data": {
                "trends": [
                    "2026-07-28 09:30,1.000,1.001,1.002,0.999,100,100.1,1.0005",
                    "2026-07-28 09:31,1.001,1.002,1.003,1.000,110,110.2,1.0015",
                ]
            },
        }

    report = collect_universe_one_minute(
        workspace=tmp_path,
        store=store,
        records=records,
        fetcher=fetcher,
        now=lambda: datetime(2026, 7, 28, 15, 10, tzinfo=SHANGHAI),
    )

    assert report["succeeded"] == 16
    assert report["failed"] == 0
    assert len(store.bars_for_day("159567", date(2026, 7, 28), interval_minutes=1)) == 2
    assert len(store.bars_for_day("513780", date(2026, 7, 28), interval_minutes=1)) == 2
    capabilities = {item.code: item for item in store.list_instrument_capabilities()}
    assert capabilities["159567"].current_day_data_status == "1分钟数据截至 2026-07-28"
    assert capabilities["513780"].latest_one_minute_bar_end == "2026-07-28T09:31:00+08:00"
    with sqlite3.connect(tmp_path / "research.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM collection_runs").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM collection_run_items").fetchone()[0] == 16
    assert (tmp_path / "reports/generated/workbench_collection" / f"{report['capture_id']}.json").is_file()
