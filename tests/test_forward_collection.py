from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from etf_t0 import forward_collection
from etf_t0.forward_collection import (
    EXPECTED_ONE_MINUTE_TIMES,
    capture_quote_snapshot,
    discover_symbol_pages,
    load_forward_config,
    normalize_depth,
    normalize_quote_row,
    one_minute_quality,
    session_label,
    sync_one_minute_bars,
    write_run_manifest,
)


def _quote_row(symbol: str, observed: datetime) -> dict[str, object]:
    return {
        "f2": 1.418 if symbol == "159570" else 1.510,
        "f5": 1000,
        "f6": 1500.0,
        "f12": symbol,
        "f14": f"ETF-{symbol}",
        "f31": 1.417 if symbol == "159570" else 1.509,
        "f32": 1.418 if symbol == "159570" else 1.510,
        "f124": int(observed.timestamp()),
        "f297": 20260727,
        "f402": -0.1,
        "f441": 1.416 if symbol == "159570" else 1.523,
    }


def test_committed_forward_config_freezes_symbols_and_strategy() -> None:
    config = load_forward_config(Path("config/forward_collection.json"))

    assert [item["symbol"] for item in config["symbols"]] == ["159570", "513780"]
    assert config["forward_sample_start_date"] == "2026-07-27"
    assert config["frozen_strategy"]["hypothesis_id"] == ("PROXY_RESIDUAL_L48_Z150_H12_MAX1")


def test_forward_config_rejects_same_id_with_changed_parameters(tmp_path: Path) -> None:
    source = Path("config/forward_collection.json").read_text(encoding="utf-8")
    changed = source.replace('"entry_z": 1.5', '"entry_z": 1.6')
    config_path = tmp_path / "changed.json"
    config_path.write_text(changed, encoding="utf-8")

    with pytest.raises(ValueError, match="strategy definition changed"):
        load_forward_config(config_path)


def test_symbol_page_discovery_scans_every_reported_page() -> None:
    calls: list[int] = []

    def fake_page(page: int) -> dict[str, object]:
        calls.append(page)
        rows = {
            1: [{"f12": "159570"}],
            2: [{"f12": "513780"}],
        }[page]
        return {"data": {"total": 200, "diff": rows}}

    pages = discover_symbol_pages(["159570", "513780"], page_fetcher=fake_page)

    assert pages == {"159570": 1, "513780": 2}
    assert sorted(calls) == [1, 2]


def test_quote_eligibility_requires_fresh_core_session_provider_date() -> None:
    observed = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    row = normalize_quote_row(
        _quote_row("159570", observed),
        exchange="SZSE",
        observed_at=observed,
        capture_id="capture",
        forward_start=observed.date(),
        freshness_limit_seconds=120,
        depth=None,
    )
    weekend = datetime(2026, 7, 26, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    stale = normalize_quote_row(
        _quote_row("159570", observed),
        exchange="SZSE",
        observed_at=weekend,
        capture_id="weekend",
        forward_start=observed.date(),
        freshness_limit_seconds=120,
        depth=None,
    )

    assert row["is_candidate_forward_quote"] is True
    assert row["spread_ticks"] == pytest.approx(1)
    assert row["iopv"] == 1.416
    assert stale["is_candidate_forward_quote"] is False
    assert session_label(weekend) == "weekend_outside_core"


def test_closing_call_auction_is_not_a_candidate_quote_session() -> None:
    closing = datetime(2026, 7, 27, 14, 57, tzinfo=ZoneInfo("Asia/Shanghai"))
    row = normalize_quote_row(
        _quote_row("159570", closing),
        exchange="SZSE",
        observed_at=closing,
        capture_id="closing",
        forward_start=closing.date(),
        freshness_limit_seconds=120,
        depth=None,
    )

    assert session_label(closing) == "closing_call_auction"
    assert row["is_candidate_forward_quote"] is False


def test_depth_parser_does_not_substitute_missing_levels() -> None:
    complete_data: dict[str, float] = {}
    buy_fields = (("f19", "f20"), ("f17", "f18"), ("f15", "f16"), ("f13", "f14"), ("f11", "f12"))
    sell_fields = (("f31", "f32"), ("f33", "f34"), ("f35", "f36"), ("f37", "f38"), ("f39", "f40"))
    for level, (price, size) in enumerate(buy_fields, start=1):
        complete_data[price] = 1.0 - level * 0.001
        complete_data[size] = 10
    for level, (price, size) in enumerate(sell_fields, start=1):
        complete_data[price] = 1.0 + level * 0.001
        complete_data[size] = 20

    complete = normalize_depth({"data": complete_data})
    missing = normalize_depth({"data": {}})

    assert complete["depth_status"] == "five_levels_available"
    assert complete["depth_bid1_size_provider_units"] == 10
    assert complete["depth_ask1_size_provider_units"] == 20
    assert complete["depth_ask1_price"] == pytest.approx(1.001)
    assert complete["depth_ask5_price"] == pytest.approx(1.005)
    assert missing["depth_status"] == "partial_or_unavailable"
    assert missing["depth_bid1_price"] is None


def test_one_minute_quality_expects_provider_native_241_rows() -> None:
    timestamps = pd.to_datetime(
        [f"2026-07-27 {clock}:00" for clock in sorted(EXPECTED_ONE_MINUTE_TIMES)]
    )
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.0,
            "is_candidate_forward_bar": True,
            "is_candidate_forward_pair_bar": True,
        }
    )

    quality = one_minute_quality(frame)

    assert len(EXPECTED_ONE_MINUTE_TIMES) == 241
    assert quality["candidate_forward_rows"] == 241
    assert quality["candidate_forward_pair_rows"] == 241
    assert quality["daily"][0]["missing_expected_times"] == []


def test_snapshot_preserves_raw_and_normalized_rows(tmp_path: Path) -> None:
    config = load_forward_config(Path("config/forward_collection.json"))
    observed = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def fake_page(page: int) -> dict[str, object]:
        symbol = "159570" if page == 1 else "513780"
        return {"rc": 0, "data": {"total": 200, "diff": [_quote_row(symbol, observed)]}}

    def fake_depth(symbol: str, exchange: str) -> dict[str, object]:
        assert (symbol, exchange) in {("159570", "SZSE"), ("513780", "SSE")}
        return {"rc": 0, "data": {}}

    rows = capture_quote_snapshot(
        config=config,
        workspace=tmp_path,
        symbol_pages={"159570": 1, "513780": 2},
        page_fetcher=fake_page,
        depth_fetcher=fake_depth,
        observed_at=observed,
    )

    assert len(rows) == 2
    assert all(row["is_candidate_forward_quote"] for row in rows)
    assert all(row["is_candidate_forward_pair"] for row in rows)
    raw = tmp_path / "data/raw/forward_capture/quotes/2026-07-27/snapshots.jsonl"
    normalized = tmp_path / "data/interim/forward_capture/quotes/2026-07-27/quotes.csv"
    assert raw.read_text(encoding="utf-8").count("\n") == 1
    assert len(pd.read_csv(normalized)) == 2


def test_snapshot_survives_optional_depth_failure(tmp_path: Path) -> None:
    config = load_forward_config(Path("config/forward_collection.json"))
    observed = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def fake_page(page: int) -> dict[str, object]:
        symbol = "159570" if page == 1 else "513780"
        return {"rc": 0, "data": {"total": 200, "diff": [_quote_row(symbol, observed)]}}

    def failed_depth(symbol: str, exchange: str) -> dict[str, object]:
        raise RuntimeError(f"depth unavailable for {exchange}.{symbol}")

    rows = capture_quote_snapshot(
        config=config,
        workspace=tmp_path,
        symbol_pages={"159570": 1, "513780": 2},
        page_fetcher=fake_page,
        depth_fetcher=failed_depth,
        observed_at=observed,
    )

    assert all(row["is_candidate_forward_quote"] for row in rows)
    assert all(row["depth_status"] == "partial_or_unavailable" for row in rows)
    assert all("depth unavailable" in row["depth_capture_error"] for row in rows)


def _minute_payload(timestamp: str) -> dict[str, object]:
    return {
        "rc": 0,
        "data": {"trends": [f"{timestamp},1.000,1.001,1.002,0.999,100,10000,1.001"]},
    }


def test_minute_sync_keeps_weekend_backfill_out_and_raw_vintages_immutable(
    tmp_path: Path,
) -> None:
    config = load_forward_config(Path("config/forward_collection.json"))
    sunday = datetime(2026, 7, 26, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def fake_fetch(symbol: str, *, exchange: str) -> dict[str, object]:
        assert (symbol, exchange) in {("159570", "SZSE"), ("513780", "SSE")}
        return _minute_payload("2026-07-24 14:59")

    for _ in range(2):
        reports = sync_one_minute_bars(
            config=config,
            workspace=tmp_path,
            fetcher=fake_fetch,
            clock=lambda: sunday,
        )

    assert all(report["quality"]["candidate_forward_rows"] == 0 for report in reports.values())
    for symbol in ("159570", "513780"):
        raw_files = list(
            (tmp_path / "data/raw/forward_capture/one_minute" / symbol).glob("*.json")
        )
        assert len(raw_files) == 2


def test_minute_sync_requires_same_recent_valid_bar_for_both_symbols(tmp_path: Path) -> None:
    config = load_forward_config(Path("config/forward_collection.json"))
    received = datetime(2026, 7, 27, 10, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

    reports = sync_one_minute_bars(
        config=config,
        workspace=tmp_path,
        fetcher=lambda symbol, exchange: _minute_payload("2026-07-27 10:00"),
        clock=lambda: received,
    )

    assert all(report["quality"]["candidate_forward_pair_rows"] == 1 for report in reports.values())


def test_minute_sync_selects_mature_candidate_vintage_with_its_own_prices(
    tmp_path: Path,
) -> None:
    config = load_forward_config(Path("config/forward_collection.json"))
    early = datetime(2026, 7, 27, 10, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    mature = datetime(2026, 7, 27, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    price = {"close": "1.001"}

    def changing_fetch(symbol: str, *, exchange: str) -> dict[str, object]:
        return {
            "rc": 0,
            "data": {
                "trends": [
                    f"2026-07-27 10:05,1.000,{price['close']},1.020,0.990,100,10000,1.001"
                ]
            },
        }

    sync_one_minute_bars(
        config=config,
        workspace=tmp_path,
        fetcher=changing_fetch,
        clock=lambda: early,
    )
    price["close"] = "1.010"
    sync_one_minute_bars(
        config=config,
        workspace=tmp_path,
        fetcher=changing_fetch,
        clock=lambda: mature,
    )

    bars = pd.read_csv(
        tmp_path / "data/interim/forward_capture/one_minute/159570/bars.csv"
    )
    assert bars.loc[0, "close"] == pytest.approx(1.010)
    assert bool(bars.loc[0, "is_candidate_forward_pair_bar"])
    assert str(bars.loc[0, "selected_vintage_received_at"]).startswith(
        "2026-07-27T10:10:00"
    )


@pytest.mark.parametrize(
    ("received_clock", "bar_clock"),
    [("11:31", "11:30"), ("15:01", "15:00")],
)
def test_minute_sync_accepts_session_end_bar_in_completion_grace(
    tmp_path: Path, received_clock: str, bar_clock: str
) -> None:
    config = load_forward_config(Path("config/forward_collection.json"))
    received = datetime.fromisoformat(f"2026-07-27T{received_clock}:00+08:00")

    reports = sync_one_minute_bars(
        config=config,
        workspace=tmp_path,
        fetcher=lambda symbol, exchange: _minute_payload(f"2026-07-27 {bar_clock}"),
        clock=lambda: received,
    )

    assert all(report["quality"]["candidate_forward_pair_rows"] == 1 for report in reports.values())


def test_manifest_write_failure_preserves_last_complete_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_forward_config(Path("config/forward_collection.json"))
    report_path = (
        tmp_path / "reports" / "generated" / "forward_capture" / "latest_manifest.json"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text('{"snapshot":"previous-complete"}', encoding="utf-8")
    original_write_text = Path.write_text

    def interrupted_write(path: Path, data: str, **kwargs) -> int:
        original_write_text(path, "partial", encoding="utf-8")
        raise OSError("simulated interruption")

    monkeypatch.setattr(Path, "write_text", interrupted_write)

    with pytest.raises(OSError, match="simulated interruption"):
        write_run_manifest(
            config=config,
            workspace=tmp_path,
            latest_rows=[],
            minute={},
        )

    assert report_path.read_text(encoding="utf-8") == '{"snapshot":"previous-complete"}'


def test_run_loop_publishes_each_completed_capture_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[list[dict[str, object]]] = []
    heartbeats: list[str] = []
    monkeypatch.setattr(forward_collection, "validate_configured_symbols", lambda *args: None)
    monkeypatch.setattr(
        forward_collection,
        "discover_symbol_pages",
        lambda symbols: {symbol: index + 1 for index, symbol in enumerate(symbols)},
    )
    monkeypatch.setattr(forward_collection, "session_label", lambda now: "afternoon_core")
    monkeypatch.setattr(forward_collection, "is_minute_capture_window", lambda now: False)
    monkeypatch.setattr(
        forward_collection,
        "capture_quote_snapshot",
        lambda **kwargs: [{"capture_id": "cycle-1", "symbol": "159570"}],
    )

    def record_manifest(**kwargs):
        published.append(kwargs["latest_rows"])
        return {"latest_quote_rows": kwargs["latest_rows"]}

    monkeypatch.setattr(forward_collection, "write_run_manifest", record_manifest)
    monkeypatch.setattr(forward_collection.time, "sleep", lambda seconds: None)

    result = forward_collection.run_loop(
        config_path=Path("config/forward_collection.json"),
        ledger_path=Path("config/universe/t0_etf_ledger.json"),
        workspace=tmp_path,
        duration_minutes=1,
        max_cycles=1,
        cycle_callback=lambda manifest: heartbeats.append(
            manifest["latest_quote_rows"][0]["capture_id"]
        ),
    )

    assert published == [[{"capture_id": "cycle-1", "symbol": "159570"}]]
    assert heartbeats == ["cycle-1"]
    assert result["latest_quote_rows"][0]["capture_id"] == "cycle-1"
