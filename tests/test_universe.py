from datetime import date
from pathlib import Path

import pytest

from etf_t0.universe import (
    EligibilityStatus,
    EtfUniverseRecord,
    T0Evidence,
    confirmed_t0_records,
    load_universe_ledger,
)

LEDGER_PATH = Path("config/universe/t0_etf_ledger.json")


def test_159567_is_confirmed_only_with_explicit_exchange_evidence() -> None:
    records = load_universe_ledger(LEDGER_PATH)

    record = next(item for item in records if item.code == "159567")
    assert record.status is EligibilityStatus.CONFIRMED
    assert record.evidence is not None
    assert record.evidence.issuer == "深圳证券交易所"
    assert "实施当日回转交易" in record.evidence.same_day_turnaround_quote
    assert record.last_review_date == date(2026, 7, 25)


def test_pending_record_does_not_enter_confirmed_universe() -> None:
    pending = EtfUniverseRecord(
        code="159999",
        exchange="SZSE",
        fund_name="待核实 ETF",
        trading_name="待核实ETF",
        manager="示例管理人",
        tracked_index="示例指数",
        listing_date=date(2026, 1, 2),
        status=EligibilityStatus.PENDING_REVIEW,
        last_review_date=date(2026, 7, 25),
        security_status="unknown",
        evidence=None,
    )

    assert confirmed_t0_records([pending]) == []


def test_confirmed_record_without_explicit_t0_language_is_rejected() -> None:
    record = EtfUniverseRecord(
        code="159998",
        exchange="SZSE",
        fund_name="示例 ETF",
        trading_name="示例ETF",
        manager="示例管理人",
        tracked_index="示例指数",
        listing_date=date(2026, 1, 2),
        status=EligibilityStatus.CONFIRMED,
        last_review_date=date(2026, 7, 25),
        security_status="listed",
        evidence=T0Evidence(
            issuer="深圳证券交易所",
            source_document_url="https://example.invalid/notice",
            announcement_date=date(2026, 1, 1),
            same_day_turnaround_quote="该基金上市交易。",
            source_access_note="test fixture",
        ),
    )

    with pytest.raises(ValueError, match="当日回转交易"):
        record.validate()
