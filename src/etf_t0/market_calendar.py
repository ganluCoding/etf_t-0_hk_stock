"""Versioned normal-overlap calendar for conservative paper observation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class NormalOverlapCalendar:
    calendar_id: str
    valid_from: date
    valid_through: date
    reviewed_on: date
    source_urls: tuple[str, ...]
    excluded_dates: frozenset[date]

    def is_normal_overlap_day(self, trade_date: date) -> bool:
        return (
            self.valid_from <= trade_date <= self.valid_through
            and trade_date.weekday() < 5
            and trade_date not in self.excluded_dates
        )


def load_normal_overlap_calendar(path: Path) -> NormalOverlapCalendar:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported normal-overlap calendar schema")
    if payload.get("timezone") != "Asia/Shanghai":
        raise ValueError("normal-overlap calendar timezone must be Asia/Shanghai")

    source_urls = tuple(str(value) for value in payload.get("source_urls", ()))
    allowed_hosts = ("sse.com.cn", "hkex.com.hk")
    if not source_urls or any(
        not any(
            (urlparse(url).hostname or "").lower() == host
            or (urlparse(url).hostname or "").lower().endswith(f".{host}")
            for host in allowed_hosts
        )
        for url in source_urls
    ):
        raise ValueError("calendar requires an official SSE or HKEX source URL")

    valid_from = date.fromisoformat(payload["valid_from"])
    valid_through = date.fromisoformat(payload["valid_through"])
    reviewed_on = date.fromisoformat(payload["reviewed_on"])
    if valid_from > valid_through or not valid_from <= reviewed_on <= valid_through:
        raise ValueError("calendar validity and review dates are inconsistent")

    raw_exclusions = [
        *payload.get("full_day_closed_dates", ()),
        *payload.get("partial_day_not_normal_dates", ()),
    ]
    excluded_dates = frozenset(date.fromisoformat(value) for value in raw_exclusions)
    if len(excluded_dates) != len(raw_exclusions):
        raise ValueError("calendar exclusion dates must be unique")
    if any(day < valid_from or day > valid_through for day in excluded_dates):
        raise ValueError("calendar exclusion date lies outside validity range")

    return NormalOverlapCalendar(
        calendar_id=str(payload["calendar_id"]),
        valid_from=valid_from,
        valid_through=valid_through,
        reviewed_on=reviewed_on,
        source_urls=source_urls,
        excluded_dates=excluded_dates,
    )
