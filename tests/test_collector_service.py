import json
from pathlib import Path
from time import sleep

import pytest

from etf_t0.collector_service import run_collector, write_heartbeat


def test_heartbeat_is_atomic_json_without_temporary_residue(tmp_path: Path) -> None:
    path = tmp_path / "reports/generated/forward_capture/collector_heartbeat.json"

    write_heartbeat(path, {"state": "RUNNING", "capture_id": "capture-1"})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "state": "RUNNING",
        "capture_id": "capture-1",
    }
    assert list(path.parent.glob("*.tmp")) == []


def test_interrupted_collector_publishes_terminal_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupting_loop(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("etf_t0.collector_service.run_loop", interrupting_loop)

    with pytest.raises(KeyboardInterrupt):
        run_collector(workspace=tmp_path, duration_minutes=1)

    heartbeat_path = (
        tmp_path
        / "reports"
        / "generated"
        / "forward_capture"
        / "collector_heartbeat.json"
    )
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["state"] == "INTERRUPTED"
    assert heartbeat["reason"] == "KeyboardInterrupt"


def test_collector_pulses_running_heartbeat_while_capture_loop_is_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat_path = (
        tmp_path
        / "reports"
        / "generated"
        / "forward_capture"
        / "collector_heartbeat.json"
    )
    states_seen_inside_loop: list[str] = []

    def idle_loop(**_kwargs):
        sleep(0.03)
        states_seen_inside_loop.append(
            json.loads(heartbeat_path.read_text(encoding="utf-8"))["state"]
        )
        return {"generated_at": "2026-07-28T09:25:30+08:00"}

    monkeypatch.setattr("etf_t0.collector_service.run_loop", idle_loop)

    run_collector(
        workspace=tmp_path,
        duration_minutes=1,
        heartbeat_interval_seconds=0.005,
    )

    assert states_seen_inside_loop == ["RUNNING"]
    assert json.loads(heartbeat_path.read_text(encoding="utf-8"))["state"] == "COMPLETED"


def test_failed_collector_publishes_error_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_loop(**_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("etf_t0.collector_service.run_loop", failing_loop)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        run_collector(workspace=tmp_path, duration_minutes=1)

    heartbeat_path = (
        tmp_path
        / "reports"
        / "generated"
        / "forward_capture"
        / "collector_heartbeat.json"
    )
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["state"] == "FAILED"
    assert heartbeat["error"] == "RuntimeError: provider unavailable"
