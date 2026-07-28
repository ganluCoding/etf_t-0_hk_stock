"""Workspace bootstrap and query facade for the multi-ETF research workbench."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from etf_t0.research_store import ResearchStore
from etf_t0.universe import confirmed_t0_records, load_universe_ledger

DATABASE_RELATIVE_PATH = Path("data/processed/research_workbench.sqlite3")


def bootstrap_workspace_database(*, workspace: Path) -> ResearchStore:
    """Import locally retained native 5-minute files without fetching or altering them."""

    store = ResearchStore(workspace / DATABASE_RELATIVE_PATH)
    records = confirmed_t0_records(
        load_universe_ledger(workspace / "config/universe/t0_etf_ledger.json")
    )
    store.sync_instruments(records)
    for record in records:
        report_path = workspace / "reports/generated/data_pilots" / f"{record.code}_5m_quality.json"
        if not report_path.exists():
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        normalized_path = workspace / report["normalized_path"]
        raw_payload_path = workspace / report["raw_path"]
        if not normalized_path.exists():
            continue
        store.ingest_native_bar_csv(
            code=record.code,
            interval_minutes=5,
            csv_path=normalized_path,
            raw_payload_path=raw_payload_path,
            acquired_at=report["acquired_at"],
            source_name="eastmoney_native_5m",
        )
        quality = report["quality"]
        store.record_data_quality(
            code=record.code,
            interval_minutes=5,
            assessed_at=report["acquired_at"],
            observed_trade_days=int(quality["observed_trade_days"]),
            complete_core_days=int(quality["complete_core_days"]),
            report_path=report_path,
        )
    for normalized_path in sorted(
        (workspace / "data/interim/workbench_one_minute").glob("*/*.csv")
    ):
        code = normalized_path.parent.name
        raw_payload_path = (
            workspace / "data/raw/workbench_one_minute" / code / normalized_path.with_suffix(".json").name
        )
        if not raw_payload_path.is_file():
            continue
        store.ingest_native_bar_csv(
            code=code,
            interval_minutes=1,
            csv_path=normalized_path,
            raw_payload_path=raw_payload_path,
            acquired_at=datetime.fromtimestamp(
                normalized_path.stat().st_mtime, tz=ZoneInfo("Asia/Shanghai")
            ).isoformat(timespec="seconds"),
            source_name="eastmoney_public_native_1m",
        )
    for quote_path in sorted(
        (workspace / "data/interim/forward_capture/quotes").glob("*/quotes.csv")
    ):
        raw_payload_path = (
            workspace
            / "data/raw/forward_capture/quotes"
            / quote_path.parent.name
            / "snapshots.jsonl"
        )
        store.ingest_quote_csv(
            csv_path=quote_path,
            raw_payload_path=raw_payload_path,
            acquired_at=datetime.fromtimestamp(
                quote_path.stat().st_mtime, tz=ZoneInfo("Asia/Shanghai")
            ).isoformat(timespec="seconds"),
            source_name="eastmoney_public_forward_quote",
        )
    collection_report_directory = workspace / "reports/generated/workbench_collection"
    legacy_latest_report = collection_report_directory / "latest.json"
    if legacy_latest_report.is_file():
        latest_payload = json.loads(legacy_latest_report.read_text(encoding="utf-8"))
        capture_id = latest_payload.get("capture_id")
        if isinstance(capture_id, str) and capture_id:
            immutable_report_path = collection_report_directory / f"{capture_id}.json"
            if not immutable_report_path.exists():
                immutable_report_path.write_text(
                    json.dumps(latest_payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
    for collection_report_path in sorted(collection_report_directory.glob("*.json")):
        if collection_report_path.name == "latest.json":
            continue
        collection_report = json.loads(collection_report_path.read_text(encoding="utf-8"))
        if {"capture_id", "collected_at", "requested", "succeeded", "failed", "results"}.issubset(
            collection_report
        ):
            store.record_collection_run(collection_report, report_path=collection_report_path)
    return store


def collect_one_minute_workspace(*, workspace: Path) -> dict:
    """Run one independent post-close/intraday one-minute pass for every confirmed ETF."""

    from etf_t0.workbench_collection import collect_universe_one_minute

    store = bootstrap_workspace_database(workspace=workspace)
    records = confirmed_t0_records(
        load_universe_ledger(workspace / "config/universe/t0_etf_ledger.json")
    )
    report = collect_universe_one_minute(workspace=workspace, store=store, records=records)
    from etf_t0.workbench_service import (
        ResearchWorkbenchService,
        load_trend_detection_parameters,
    )

    trade_date = datetime.fromisoformat(str(report["collected_at"])).date()
    service = ResearchWorkbenchService(
        store=store,
        parameters=load_trend_detection_parameters(workspace / "config/trend_detection.json"),
        clock=lambda: str(report["collected_at"]),
    )
    for record in records:
        service.persist_completed_trends(record.code, trade_date=trade_date)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("bootstrap", "collect-one-minute"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.command == "bootstrap":
        store = bootstrap_workspace_database(workspace=args.workspace)
        print(len(store.list_instrument_capabilities()))
        return
    print(json.dumps(collect_one_minute_workspace(workspace=args.workspace), ensure_ascii=False))


if __name__ == "__main__":
    main()
