"""Independent local collection of native one-minute bars for the research universe."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from etf_t0.data_pilot import fetch_eastmoney_etf_1m, normalize_trends
from etf_t0.research_store import ResearchStore
from etf_t0.universe import EtfUniverseRecord

SHANGHAI = ZoneInfo("Asia/Shanghai")
OneMinuteFetcher = Callable[..., dict[str, Any]]


def collect_universe_one_minute(
    *,
    workspace: Path,
    store: ResearchStore,
    records: Sequence[EtfUniverseRecord],
    fetcher: OneMinuteFetcher = fetch_eastmoney_etf_1m,
    now: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
) -> dict[str, Any]:
    """Collect each requested ETF separately and retain immutable local payloads."""

    collected_at = now().astimezone(SHANGHAI)
    capture_id = f"{collected_at.strftime('%Y%m%dT%H%M%S.%f%z')}-{uuid.uuid4().hex[:8]}"
    results: list[dict[str, str | int]] = []
    for record in records:
        raw_path = (
            workspace
            / "data/raw/workbench_one_minute"
            / record.code
            / f"{capture_id}.json"
        )
        normalized_path = (
            workspace
            / "data/interim/workbench_one_minute"
            / record.code
            / f"{capture_id}.csv"
        )
        try:
            payload = fetcher(record.code, exchange=record.exchange)
            frame = normalize_trends(payload)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            normalized_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            frame.to_csv(normalized_path, index=False)
            inserted = store.ingest_native_bar_csv(
                code=record.code,
                interval_minutes=1,
                csv_path=normalized_path,
                raw_payload_path=raw_path,
                acquired_at=collected_at.isoformat(timespec="seconds"),
                source_name="eastmoney_public_native_1m",
            )
        except (OSError, RuntimeError, ValueError) as error:
            results.append(
                {
                    "symbol": record.code,
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
            continue
        results.append(
            {
                "symbol": record.code,
                "status": "succeeded",
                "inserted_bars": inserted,
                "raw_path": str(raw_path.relative_to(workspace)),
                "normalized_path": str(normalized_path.relative_to(workspace)),
            }
        )
    report = {
        "mode": "local native one-minute multi-ETF collection",
        "collected_at": collected_at.isoformat(timespec="seconds"),
        "capture_id": capture_id,
        "requested": len(records),
        "succeeded": sum(item["status"] == "succeeded" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "results": results,
    }
    report_directory = workspace / "reports/generated/workbench_collection"
    report_directory.mkdir(parents=True, exist_ok=True)
    immutable_report_path = report_directory / f"{capture_id}.json"
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    immutable_report_path.write_text(serialized, encoding="utf-8")
    (report_directory / "latest.json").write_text(serialized, encoding="utf-8")
    store.record_collection_run(report, report_path=immutable_report_path)
    return report
