"""Auditable eligibility records for ETFs researched for same-day turnaround trading."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any


class EligibilityStatus(str, Enum):
    """A record may be confirmed only when its exchange evidence is complete."""

    CONFIRMED = "confirmed"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"


@dataclass(frozen=True)
class T0Evidence:
    """Evidence quoted from an exchange-issued listing or trading announcement."""

    issuer: str
    source_document_url: str
    announcement_date: date
    same_day_turnaround_quote: str
    source_access_note: str

    def validate(self) -> None:
        if not self.issuer.strip():
            raise ValueError("T+0 evidence must identify its issuer")
        if not self.source_document_url.startswith("https://"):
            raise ValueError("T+0 evidence must use an HTTPS source document URL")
        if "当日回转交易" not in self.same_day_turnaround_quote:
            raise ValueError("T+0 evidence must explicitly state 当日回转交易")


@dataclass(frozen=True)
class EtfUniverseRecord:
    """One candidate ETF and the evidence needed to place it in the research universe."""

    code: str
    exchange: str
    fund_name: str
    trading_name: str
    manager: str
    tracked_index: str
    listing_date: date
    status: EligibilityStatus
    last_review_date: date
    security_status: str
    evidence: T0Evidence | None
    notes: str = ""

    def validate(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("ETF code must be a six-digit string")
        if self.exchange not in {"SZSE", "SSE"}:
            raise ValueError("exchange must be SZSE or SSE")
        if self.last_review_date < self.listing_date:
            raise ValueError("last review date cannot precede listing date")
        if self.status is EligibilityStatus.CONFIRMED:
            if self.evidence is None:
                raise ValueError("confirmed ETFs require T+0 evidence")
            self.evidence.validate()

    @property
    def is_confirmed_t0(self) -> bool:
        """Whether this record is eligible for downstream T+0 research."""

        return self.status is EligibilityStatus.CONFIRMED


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_evidence(value: dict[str, Any] | None) -> T0Evidence | None:
    if value is None:
        return None
    return T0Evidence(
        issuer=value["issuer"],
        source_document_url=value["source_document_url"],
        announcement_date=_parse_date(value["announcement_date"]),
        same_day_turnaround_quote=value["same_day_turnaround_quote"],
        source_access_note=value["source_access_note"],
    )


def load_universe_ledger(path: Path) -> list[EtfUniverseRecord]:
    """Load and validate a JSON ledger; malformed or incomplete records fail closed."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported universe ledger schema version")

    records: list[EtfUniverseRecord] = []
    for item in payload["records"]:
        record = EtfUniverseRecord(
            code=item["code"],
            exchange=item["exchange"],
            fund_name=item["fund_name"],
            trading_name=item["trading_name"],
            manager=item["manager"],
            tracked_index=item["tracked_index"],
            listing_date=_parse_date(item["listing_date"]),
            status=EligibilityStatus(item["status"]),
            last_review_date=_parse_date(item["last_review_date"]),
            security_status=item["security_status"],
            evidence=_parse_evidence(item.get("evidence")),
            notes=item.get("notes", ""),
        )
        record.validate()
        records.append(record)

    if len({record.code for record in records}) != len(records):
        raise ValueError("ETF codes must be unique in the universe ledger")
    return records


def confirmed_t0_records(records: Iterable[EtfUniverseRecord]) -> list[EtfUniverseRecord]:
    """Return only evidence-backed records; pending candidates never leak downstream."""

    return [record for record in records if record.is_confirmed_t0]
