from decimal import Decimal

import pandas as pd
import pytest

from etf_t0.break_even_report import _decimal_list, _latest_native_close


def test_latest_native_close_uses_the_latest_timestamp_not_csv_row_order(tmp_path) -> None:
    bars = tmp_path / "bars.csv"
    pd.DataFrame(
        {
            "timestamp": ["2026-07-28 15:00:00", "2026-07-28 14:55:00"],
            "close": [1.234, 1.111],
        }
    ).to_csv(bars, index=False)

    price, timestamp = _latest_native_close(bars)

    assert price == Decimal("1.234")
    assert timestamp == "2026-07-28 15:00:00"


def test_decimal_list_rejects_empty_and_non_positive_values() -> None:
    assert _decimal_list("10000, 30000", field_name="amounts") == [
        Decimal(10000),
        Decimal(30000),
    ]

    with pytest.raises(ValueError, match="amounts"):
        _decimal_list("0", field_name="amounts")


def test_latest_native_close_rejects_an_off_tick_source_value(tmp_path) -> None:
    bars = tmp_path / "bars.csv"
    pd.DataFrame({"timestamp": ["2026-07-28 15:00:00"], "close": [1.2345]}).to_csv(
        bars, index=False
    )

    with pytest.raises(ValueError, match="off the 0.001"):
        _latest_native_close(bars)
