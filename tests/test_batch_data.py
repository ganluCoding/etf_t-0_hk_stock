from pathlib import Path

import pytest

from etf_t0.batch_data import collect_confirmed_universe


def test_batch_collection_uses_only_confirmed_records_and_compacts_quality(tmp_path: Path) -> None:
    calls: list[tuple[str, str, bool]] = []

    def fake_collector(
        symbol: str,
        workspace: Path,
        *,
        exchange: str,
        include_one_minute_probe: bool,
    ) -> dict[str, object]:
        calls.append((symbol, exchange, include_one_minute_probe))
        return {
            "normalized_path": f"data/interim/{symbol}_5m_latest/bars.csv",
            "raw_path": f"data/raw/{symbol}_5m_latest/eastmoney_response.json",
            "quality": {
                "observed_trade_days": 30,
                "complete_core_days": 30,
                "first_timestamp": "2026-06-01 09:35:00",
                "last_timestamp": "2026-07-24 15:00:00",
            },
            "acceptance": {"meets_30_day_target": True},
        }

    report = collect_confirmed_universe(
        ledger_path=Path("config/universe/t0_etf_ledger.json"),
        workspace=tmp_path,
        symbols=["159567", "513180"],
        collector=fake_collector,
    )

    assert calls == [("159567", "SZSE", False), ("513180", "SSE", False)]
    assert report["succeeded"] == 2
    assert report["failed"] == 0
    assert report["met_30_day_target"] == 2


def test_batch_collection_rejects_symbols_without_confirmed_evidence(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not confirmed"):
        collect_confirmed_universe(
            ledger_path=Path("config/universe/t0_etf_ledger.json"),
            workspace=tmp_path,
            symbols=["159999"],
        )


def test_batch_collection_records_one_failure_instead_of_aborting(tmp_path: Path) -> None:
    def failing_collector(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("provider unavailable")

    report = collect_confirmed_universe(
        ledger_path=Path("config/universe/t0_etf_ledger.json"),
        workspace=tmp_path,
        symbols=["159567"],
        collector=failing_collector,
    )

    assert report["succeeded"] == 0
    assert report["failed"] == 1
    assert report["results"][0]["error_type"] == "RuntimeError"
