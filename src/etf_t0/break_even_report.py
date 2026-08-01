"""Generate an immutable local break-even ledger from each target's native 5m close.

This command is intentionally an OHLC-conservative cost screen.  Its input price
is a historical bar close, not an executable quote; generated rows therefore
always carry ``NO_EXECUTION_CLAIM`` and remain local generated research output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from etf_t0.break_even_ledger import (
    BreakEvenLedgerRequest,
    ExecutionTier,
    build_break_even_ledger,
)
from etf_t0.fees import ETF_PRICE_TICK, cmb_user_reported_fee_scenarios
from etf_t0.universe import confirmed_t0_records, load_universe_ledger

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _decimal_list(value: str, *, field_name: str) -> list[Decimal]:
    values = [Decimal(part.strip()) for part in value.split(",") if part.strip()]
    if not values or any(item <= 0 for item in values):
        raise ValueError(f"{field_name} must contain positive comma-separated numbers")
    return values


def _latest_native_close(path: Path) -> tuple[Decimal, str]:
    frame = pd.read_csv(path)
    required = {"timestamp", "close"}
    if not required.issubset(frame.columns):
        raise ValueError(f"{path} lacks required columns: {sorted(required)}")
    ordered = frame.assign(timestamp=pd.to_datetime(frame["timestamp"], errors="raise")).sort_values(
        "timestamp"
    )
    if ordered.empty:
        raise ValueError(f"{path} contains no bars")
    row = ordered.iloc[-1]
    price = Decimal(str(row["close"]))
    if price <= 0:
        raise ValueError(f"{path} has a non-positive latest close")
    if price % ETF_PRICE_TICK != 0:
        raise ValueError(f"{path} has a latest close off the 0.001 ETF price tick")
    return price, row["timestamp"].isoformat(sep=" ")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_revision(workspace: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def generate_workspace_break_even_ledger(
    *,
    workspace: Path,
    order_amounts_cny: list[Decimal],
    grid_spacings_bps: list[Decimal],
) -> dict:
    """Build every available target/order/grid row without filling absent data."""

    records = confirmed_t0_records(
        load_universe_ledger(workspace / "config/universe/t0_etf_ledger.json")
    )
    user_reported_schedules = cmb_user_reported_fee_scenarios()
    requests: list[BreakEvenLedgerRequest] = []
    price_inputs: list[dict[str, str]] = []
    unavailable: list[dict[str, str]] = []
    for record in records:
        bars_path = workspace / "data/interim" / f"{record.code}_5m_latest" / "bars.csv"
        if not bars_path.is_file():
            unavailable.append(
                {
                    "symbol": record.code,
                    "reason": "WAIT_DATA: native 5-minute source file is unavailable",
                    "expected_path": str(bars_path.relative_to(workspace)),
                }
            )
            continue
        try:
            entry_price, bar_end = _latest_native_close(bars_path)
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            unavailable.append(
                {
                    "symbol": record.code,
                    "reason": f"WAIT_DATA: {exc}",
                    "expected_path": str(bars_path.relative_to(workspace)),
                }
            )
            continue
        price_inputs.append(
            {
                "symbol": record.code,
                "entry_price_cny": str(entry_price),
                "bar_end": bar_end,
                "source_path": str(bars_path.relative_to(workspace)),
                "source_sha256": _sha256(bars_path),
            }
        )
        for order_amount in order_amounts_cny:
            for grid_spacing in grid_spacings_bps:
                for fee_schedule in user_reported_schedules:
                    requests.append(
                        BreakEvenLedgerRequest(
                            symbol=record.code,
                            entry_price=entry_price,
                            order_amount_cny=order_amount,
                            grid_spacing_bps=grid_spacing,
                            fee_schedule=fee_schedule,
                            execution_tier=ExecutionTier.OHLC_CONSERVATIVE,
                            fee_scope_confirmed=record.code == "159567",
                        )
                    )
    rows = build_break_even_ledger(requests)
    generated_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    universe_path = workspace / "config/universe/t0_etf_ledger.json"
    run_material = json.dumps(
        {
            "generated_at": generated_at,
            "universe_sha256": _sha256(universe_path),
            "price_inputs": price_inputs,
            "order_amounts_cny": [str(value) for value in order_amounts_cny],
            "grid_spacings_bps": [str(value) for value in grid_spacings_bps],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "schema_version": 1,
        "run_id": hashlib.sha256(run_material.encode("utf-8")).hexdigest()[:20],
        "generated_at": generated_at,
        "research_source_git_revision": _git_revision(workspace),
        "purpose": "pre-backtest lower-bound cost screen; not an executable quote, fill model, or trading recommendation",
        "execution_tier": ExecutionTier.OHLC_CONSERVATIVE.value,
        "fee_scope": {
            "scenarios": [schedule.name for schedule in user_reported_schedules],
            "confirmed_target_scope": ["159567"],
            "other_symbols": "WAIT_FEE_SCOPE; minimum-fee scenario is not automatically broker-confirmed for another ETF",
        },
        "fee_schedule_snapshots": [asdict(schedule) for schedule in user_reported_schedules],
        "input": {
            "order_amounts_cny": [str(value) for value in order_amounts_cny],
            "grid_spacings_bps": [str(value) for value in grid_spacings_bps],
            "price_source": "last native 5-minute close from each local data/interim/<code>_5m_latest/bars.csv file",
            "price_inputs": price_inputs,
            "universe_ledger_path": str(universe_path.relative_to(workspace)),
            "universe_ledger_sha256": _sha256(universe_path),
        },
        "rows": [asdict(row) for row in rows],
        "unavailable_symbols": unavailable,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--order-amounts-cny", default="10000,30000")
    parser.add_argument("--grid-spacings-bps", default="10,20,50,100,200")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
    )
    args = parser.parse_args()
    report = generate_workspace_break_even_ledger(
        workspace=args.workspace,
        order_amounts_cny=_decimal_list(args.order_amounts_cny, field_name="order amounts"),
        grid_spacings_bps=_decimal_list(args.grid_spacings_bps, field_name="grid spacings"),
    )
    output = args.output
    if output is None:
        output = Path("reports/generated/break_even_ledger") / f"{report['run_id']}.json"
    output = output if output.is_absolute() else args.workspace / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(output)


if __name__ == "__main__":
    main()
