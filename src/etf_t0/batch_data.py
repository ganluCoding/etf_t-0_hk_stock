"""Batch acquisition for the exchange-evidenced T+0 ETF universe."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from etf_t0.data_pilot import run_pilot
from etf_t0.universe import confirmed_t0_records, load_universe_ledger

Collector = Callable[..., dict[str, Any]]


def collect_confirmed_universe(
    *,
    ledger_path: Path,
    workspace: Path,
    symbols: Iterable[str] | None = None,
    collector: Collector = run_pilot,
) -> dict[str, Any]:
    """Collect native 5-minute bars for confirmed records and preserve failures."""

    records = confirmed_t0_records(load_universe_ledger(ledger_path))
    requested = set(symbols) if symbols is not None else None
    confirmed_codes = {record.code for record in records}
    if requested is not None:
        unknown = requested - confirmed_codes
        if unknown:
            raise ValueError(
                "symbols are not confirmed in the T+0 evidence ledger: "
                + ", ".join(sorted(unknown))
            )
        records = [record for record in records if record.code in requested]
    if not records:
        raise ValueError("no confirmed T+0 ETFs selected for collection")

    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    results: list[dict[str, Any]] = []
    for record in records:
        base = {
            "symbol": record.code,
            "exchange": record.exchange,
            "trading_name": record.trading_name,
        }
        try:
            manifest = collector(
                record.code,
                workspace,
                exchange=record.exchange,
                include_one_minute_probe=False,
            )
        except (OSError, RuntimeError, ValueError) as error:
            results.append(
                {
                    **base,
                    "collection_status": "failed",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
            continue
        quality = manifest["quality"]
        results.append(
            {
                **base,
                "collection_status": "succeeded",
                "normalized_path": manifest["normalized_path"],
                "raw_path": manifest["raw_path"],
                "observed_trade_days": quality["observed_trade_days"],
                "complete_core_days": quality["complete_core_days"],
                "first_timestamp": quality["first_timestamp"],
                "last_timestamp": quality["last_timestamp"],
                "meets_30_day_target": manifest["acceptance"]["meets_30_day_target"],
            }
        )

    try:
        ledger_display = str(ledger_path.resolve().relative_to(workspace.resolve()))
    except ValueError:
        ledger_display = str(ledger_path)
    report = {
        "mode": "local native 5-minute batch acquisition",
        "started_at": started_at,
        "ledger_path": ledger_display,
        "selected_confirmed_symbols": [record.code for record in records],
        "succeeded": sum(item["collection_status"] == "succeeded" for item in results),
        "failed": sum(item["collection_status"] == "failed" for item in results),
        "met_30_day_target": sum(item.get("meets_30_day_target") is True for item in results),
        "results": results,
    }
    report_dir = workspace / "reports" / "generated" / "data_pilots"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "t0_etf_batch_latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger", type=Path, default=Path("config/universe/t0_etf_ledger.json")
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--symbols",
        help="optional comma-separated confirmed ETF codes; default collects all confirmed records",
    )
    args = parser.parse_args()
    symbols = args.symbols.split(",") if args.symbols else None
    print(
        json.dumps(
            collect_confirmed_universe(
                ledger_path=args.ledger,
                workspace=args.workspace,
                symbols=symbols,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
