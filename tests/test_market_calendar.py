import json
from datetime import date
from pathlib import Path

import pytest

from etf_t0.market_calendar import load_normal_overlap_calendar

CALENDAR_PATH = Path("config/normal_overlap_calendar_2026.json")


def test_versioned_calendar_separates_normal_overlap_and_connect_closures() -> None:
    calendar = load_normal_overlap_calendar(CALENDAR_PATH)

    assert calendar.is_normal_overlap_day(date(2026, 7, 28)) is True
    assert calendar.is_normal_overlap_day(date(2026, 7, 1)) is False
    assert calendar.is_normal_overlap_day(date(2026, 12, 24)) is False
    assert calendar.is_normal_overlap_day(date(2027, 1, 4)) is False


def test_calendar_rejects_non_official_source(tmp_path: Path) -> None:
    payload = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    payload["source_urls"] = ["https://example.invalid/calendar"]
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="official SSE or HKEX"):
        load_normal_overlap_calendar(path)
