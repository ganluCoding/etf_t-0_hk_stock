"""Supervised local forward collector for a permission-bearing scheduler."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from etf_t0.forward_collection import run_loop


class CollectorTermination(RuntimeError):
    """Turn an operating-system termination request into an auditable stop."""


def write_heartbeat(path: Path, payload: dict[str, Any]) -> None:
    """Publish one complete diagnostic heartbeat without exposing partial JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _termination_requested(_signum, _frame) -> None:
    raise CollectorTermination("SIGTERM")

def run_collector(
    *,
    workspace: Path,
    duration_minutes: int,
    heartbeat_interval_seconds: float = 15,
) -> dict[str, Any] | None:
    if heartbeat_interval_seconds <= 0:
        raise ValueError("heartbeat interval must be positive")
    generated = workspace / "reports" / "generated" / "forward_capture"
    generated.mkdir(parents=True, exist_ok=True)
    lock_path = generated / "collector.lock"
    heartbeat_path = generated / "collector_heartbeat.json"
    timezone = ZoneInfo("Asia/Shanghai")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None

        def heartbeat(manifest: dict[str, Any]) -> None:
            rows = manifest.get("latest_quote_rows", [])
            write_heartbeat(
                heartbeat_path,
                {
                    "state": "RUNNING",
                    "pid": os.getpid(),
                    "updated_at": datetime.now(timezone).isoformat(timespec="seconds"),
                    "capture_id": rows[0].get("capture_id") if rows else None,
                    "candidate_forward_sample_available": manifest.get(
                        "stage_status", {}
                    ).get("candidate_forward_sample_available", False),
                },
            )

        write_heartbeat(
            heartbeat_path,
            {
                "state": "STARTING",
                "pid": os.getpid(),
                "updated_at": datetime.now(timezone).isoformat(timespec="seconds"),
            },
        )
        pulse_stop = threading.Event()

        def pulse_heartbeat() -> None:
            while not pulse_stop.is_set():
                write_heartbeat(
                    heartbeat_path,
                    {
                        "state": "RUNNING",
                        "pid": os.getpid(),
                        "updated_at": datetime.now(timezone).isoformat(
                            timespec="seconds"
                        ),
                    },
                )
                pulse_stop.wait(heartbeat_interval_seconds)

        pulse_thread = threading.Thread(
            target=pulse_heartbeat,
            name="collector-heartbeat",
            daemon=True,
        )
        pulse_thread.start()
        previous_sigterm = None
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _termination_requested)
        try:
            result = run_loop(
                config_path=workspace / "config" / "forward_collection.json",
                ledger_path=workspace / "config" / "universe" / "t0_etf_ledger.json",
                workspace=workspace,
                duration_minutes=duration_minutes,
                cycle_callback=heartbeat,
            )
        except (KeyboardInterrupt, CollectorTermination) as exc:
            pulse_stop.set()
            pulse_thread.join()
            write_heartbeat(
                heartbeat_path,
                {
                    "state": "INTERRUPTED",
                    "pid": os.getpid(),
                    "updated_at": datetime.now(timezone).isoformat(timespec="seconds"),
                    "reason": type(exc).__name__ if not str(exc) else str(exc),
                },
            )
            raise
        except Exception as exc:
            pulse_stop.set()
            pulse_thread.join()
            write_heartbeat(
                heartbeat_path,
                {
                    "state": "FAILED",
                    "pid": os.getpid(),
                    "updated_at": datetime.now(timezone).isoformat(timespec="seconds"),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
        finally:
            pulse_stop.set()
            pulse_thread.join()
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
        write_heartbeat(
            heartbeat_path,
            {
                "state": "COMPLETED",
                "pid": os.getpid(),
                "updated_at": datetime.now(timezone).isoformat(timespec="seconds"),
                "last_manifest_generated_at": result.get("generated_at"),
            },
        )
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--duration-minutes", type=int, default=135)
    args = parser.parse_args()
    result = run_collector(
        workspace=args.workspace.resolve(), duration_minutes=args.duration_minutes
    )
    if result is None:
        print("collector already running")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
