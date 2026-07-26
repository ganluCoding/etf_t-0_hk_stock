from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from etf_t0.universe import (
    EligibilityStatus,
    EtfUniverseRecord,
    EvidenceSourceKind,
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
    assert record.evidence.source_kind is EvidenceSourceKind.EXCHANGE_ISSUED_MIRROR
    assert len(record.evidence.source_content_sha256 or "") == 64
    assert "实施当日回转交易" in record.evidence.same_day_turnaround_quote
    assert record.last_review_date == date(2026, 7, 25)


def test_first_multi_symbol_batch_is_confirmed_across_both_exchanges() -> None:
    confirmed = confirmed_t0_records(load_universe_ledger(LEDGER_PATH))

    assert len(confirmed) == 16
    assert {record.exchange for record in confirmed} == {"SSE", "SZSE"}
    assert {record.code for record in confirmed} >= {
        "159567",
        "159920",
        "159792",
        "513180",
        "513330",
        "513780",
    }
    assert all(record.evidence is not None for record in confirmed)


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
            source_kind=EvidenceSourceKind.FIRST_PARTY,
        ),
    )

    with pytest.raises(ValueError, match="回转交易"):
        record.validate()


def test_confirmed_record_with_a_negative_t0_statement_is_rejected() -> None:
    record = EtfUniverseRecord(
        code="159997",
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
            same_day_turnaround_quote="本基金不实施当日回转交易。",
            source_access_note="test fixture",
            source_kind=EvidenceSourceKind.FIRST_PARTY,
        ),
    )

    with pytest.raises(ValueError, match="negative 当日回转交易"):
        record.validate()


def test_sse_intraday_turnaround_language_is_accepted() -> None:
    record = EtfUniverseRecord(
        code="513999",
        exchange="SSE",
        fund_name="示例 ETF",
        trading_name="示例ETF",
        manager="示例管理人",
        tracked_index="示例指数",
        listing_date=date(2026, 1, 2),
        status=EligibilityStatus.CONFIRMED,
        last_review_date=date(2026, 7, 26),
        security_status="listed",
        evidence=T0Evidence(
            issuer="上海证券交易所",
            source_document_url="https://example.invalid/notice",
            announcement_date=date(2026, 1, 1),
            same_day_turnaround_quote="本基金上市交易，并实施日内回转交易。",
            source_access_note="test fixture",
            source_kind=EvidenceSourceKind.FIRST_PARTY,
        ),
    )

    record.validate()


def test_exchange_issued_mirror_requires_a_content_fingerprint() -> None:
    evidence = T0Evidence(
        issuer="深圳证券交易所",
        source_document_url="https://example.invalid/notice",
        announcement_date=date(2026, 1, 1),
        same_day_turnaround_quote="本基金实施当日回转交易。",
        source_access_note="test fixture",
        source_kind=EvidenceSourceKind.EXCHANGE_ISSUED_MIRROR,
    )

    with pytest.raises(ValueError, match="SHA-256"):
        evidence.validate()


def test_confirmed_record_rejects_empty_audit_fields_and_mirror_note() -> None:
    record = next(item for item in load_universe_ledger(LEDGER_PATH) if item.code == "159567")
    assert record.evidence is not None

    for field in ("fund_name", "trading_name", "manager", "tracked_index", "security_status"):
        with pytest.raises(ValueError, match=field):
            replace(record, **{field: ""}).validate()

    with pytest.raises(ValueError, match="source access note"):
        replace(record, evidence=replace(record.evidence, source_access_note="")).validate()
