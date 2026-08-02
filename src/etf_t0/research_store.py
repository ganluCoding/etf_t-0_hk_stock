"""Local SQLite store for auditable multi-ETF research data.

Raw provider payloads remain immutable local files.  The database stores only
their path and content fingerprint together with normalized queryable records.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from etf_t0.break_even_ledger import PaperExecutionOutcome, PaperExecutionRecord
from etf_t0.fees import OrderSide
from etf_t0.trend_research import CompletedUptrend, TrendDetectionParameters
from etf_t0.universe import EtfUniverseRecord

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class InstrumentCapability:
    """One visible research-universe entry and its locally stored coverage."""

    code: str
    trading_name: str
    exchange: str
    security_status: str
    historical_five_minute_days: int
    current_day_data_status: str
    paper_policy_status: str
    latest_one_minute_bar_end: str | None
    t0_evidence_status: str
    last_review_date: str
    research_gate_status: str


@dataclass(frozen=True)
class BarLineage:
    """Immutable source identity for one normalized bar vintage."""

    source_name: str
    raw_payload_path: str
    raw_payload_sha256: str
    acquired_at: str


@dataclass(frozen=True)
class QuoteSnapshot:
    """Latest normalized target quote retained with its immutable raw lineage."""

    instrument_code: str
    observed_at: str
    last_price: str | None
    bid1_price: str | None
    ask1_price: str | None
    iopv: str | None
    source_name: str
    raw_payload_path: str
    raw_payload_sha256: str


class ResearchStore:
    """Small operational API around the local, per-workspace research database."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._read_only = False
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS instruments (
                    code TEXT PRIMARY KEY,
                    trading_name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    security_status TEXT NOT NULL,
                    listing_date TEXT NOT NULL,
                    last_review_date TEXT NOT NULL,
                    t0_evidence_status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS raw_payloads (
                    sha256 TEXT PRIMARY KEY,
                    local_path TEXT NOT NULL,
                    exists_on_disk INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingestion_runs (
                    id INTEGER PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    normalized_path TEXT NOT NULL,
                    raw_payload_sha256 TEXT NOT NULL REFERENCES raw_payloads(sha256),
                    raw_payload_path TEXT NOT NULL,
                    UNIQUE(source_name, acquired_at, normalized_path, raw_payload_sha256)
                );
                CREATE TABLE IF NOT EXISTS bar_vintages (
                    instrument_code TEXT NOT NULL REFERENCES instruments(code),
                    interval_minutes INTEGER NOT NULL CHECK(interval_minutes > 0),
                    bar_end TEXT NOT NULL,
                    open_price TEXT NOT NULL,
                    close_price TEXT NOT NULL,
                    high_price TEXT NOT NULL,
                    low_price TEXT NOT NULL,
                    volume TEXT,
                    turnover TEXT,
                    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
                    PRIMARY KEY(instrument_code, interval_minutes, bar_end, ingestion_run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_bar_vintages_lookup
                    ON bar_vintages(instrument_code, interval_minutes, bar_end);
                CREATE TABLE IF NOT EXISTS data_quality_reports (
                    instrument_code TEXT NOT NULL REFERENCES instruments(code),
                    interval_minutes INTEGER NOT NULL CHECK(interval_minutes > 0),
                    assessed_at TEXT NOT NULL,
                    observed_trade_days INTEGER NOT NULL,
                    complete_core_days INTEGER NOT NULL,
                    report_path TEXT NOT NULL,
                    PRIMARY KEY(instrument_code, interval_minutes, assessed_at, report_path)
                );
                CREATE TABLE IF NOT EXISTS quote_snapshots (
                    capture_id TEXT NOT NULL,
                    instrument_code TEXT NOT NULL REFERENCES instruments(code),
                    observed_at TEXT NOT NULL,
                    last_price TEXT,
                    bid1_price TEXT,
                    ask1_price TEXT,
                    iopv TEXT,
                    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
                    PRIMARY KEY(capture_id, instrument_code, ingestion_run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_quote_snapshots_lookup
                    ON quote_snapshots(instrument_code, observed_at);
                CREATE TABLE IF NOT EXISTS paper_execution_records (
                    instrument_code TEXT NOT NULL REFERENCES instruments(code),
                    observed_at TEXT NOT NULL,
                    normal_overlap_day INTEGER NOT NULL CHECK(normal_overlap_day IN (0, 1)),
                    intended_side TEXT NOT NULL,
                    intended_price TEXT NOT NULL,
                    intended_quantity INTEGER NOT NULL,
                    observed_bid1_price TEXT NOT NULL,
                    observed_ask1_price TEXT NOT NULL,
                    quote_source TEXT NOT NULL,
                    fee_evidence TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    filled_price TEXT,
                    filled_quantity INTEGER NOT NULL,
                    outcome_reason TEXT,
                    PRIMARY KEY(instrument_code, observed_at, intended_side, intended_price, intended_quantity)
                );
                CREATE TABLE IF NOT EXISTS collection_runs (
                    capture_id TEXT PRIMARY KEY,
                    collected_at TEXT NOT NULL,
                    requested INTEGER NOT NULL,
                    succeeded INTEGER NOT NULL,
                    failed INTEGER NOT NULL,
                    report_path TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_run_items (
                    capture_id TEXT NOT NULL REFERENCES collection_runs(capture_id),
                    instrument_code TEXT NOT NULL REFERENCES instruments(code),
                    status TEXT NOT NULL,
                    error_type TEXT,
                    error_message TEXT,
                    raw_path TEXT,
                    normalized_path TEXT,
                    PRIMARY KEY(capture_id, instrument_code)
                );
                CREATE TABLE IF NOT EXISTS completed_uptrends (
                    instrument_code TEXT NOT NULL REFERENCES instruments(code),
                    trade_date TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL CHECK(interval_minutes > 0),
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    duration_bars INTEGER NOT NULL,
                    start_close TEXT NOT NULL,
                    end_close TEXT NOT NULL,
                    rise_bps TEXT NOT NULL,
                    maximum_pullback_bps TEXT NOT NULL,
                    detection_version TEXT NOT NULL,
                    parameter_sha256 TEXT NOT NULL,
                    parameter_json TEXT NOT NULL,
                    input_bar_sha256 TEXT NOT NULL,
                    input_latest_bar_end TEXT NOT NULL,
                    calculated_at TEXT NOT NULL,
                    executable_profit_status TEXT NOT NULL,
                    PRIMARY KEY(instrument_code, start_at, end_at, parameter_sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_completed_uptrends_lookup
                    ON completed_uptrends(instrument_code, trade_date, interval_minutes);
                """
            )
            _ensure_completed_uptrend_parameter_json(connection)
            _ensure_completed_uptrend_input_fingerprint(connection)
            _ensure_ingestion_run_raw_payload_path(connection)
            _ensure_instrument_evidence_status(connection)

    @classmethod
    def open_read_only(cls, database_path: Path) -> ResearchStore:
        """Open an existing database without schema bootstrap or migration writes."""

        resolved_path = database_path.resolve()
        if not resolved_path.is_file():
            raise FileNotFoundError(
                f"research database does not exist; run explicit bootstrap first: {resolved_path}"
            )
        store = cls.__new__(cls)
        store._database_path = resolved_path
        store._read_only = True
        with store._connect() as connection:
            connection.execute("SELECT 1 FROM instruments LIMIT 1").fetchone()
        return store

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            connection = sqlite3.connect(
                f"{self._database_path.as_uri()}?mode=ro",
                uri=True,
            )
            connection.execute("PRAGMA query_only = ON")
        else:
            connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def sync_instruments(self, records: list[EtfUniverseRecord]) -> None:
        """Upsert exchange-evidenced instruments without granting a strategy."""

        rows = [
            (
                record.code,
                record.trading_name,
                record.exchange,
                record.security_status.value,
                record.listing_date.isoformat(),
                record.last_review_date.isoformat(),
                "交易所T+0证据已确认" if record.evidence is not None else "无确认T+0证据",
            )
            for record in records
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO instruments(
                    code, trading_name, exchange, security_status, listing_date, last_review_date,
                    t0_evidence_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    trading_name = excluded.trading_name,
                    exchange = excluded.exchange,
                    security_status = excluded.security_status,
                    listing_date = excluded.listing_date,
                    last_review_date = excluded.last_review_date,
                    t0_evidence_status = excluded.t0_evidence_status
                """,
                rows,
            )

    def ingest_native_bar_csv(
        self,
        *,
        code: str,
        interval_minutes: int,
        csv_path: Path,
        raw_payload_path: Path,
        acquired_at: str,
        source_name: str,
    ) -> int:
        """Ingest one immutable native-bar file and return inserted bar count."""

        raw_payload_path = raw_payload_path.resolve()
        csv_path = csv_path.resolve()
        payload_sha256 = _payload_fingerprint(raw_payload_path)
        normalized_path = str(csv_path)
        with self._connect() as connection:
            known = connection.execute(
                "SELECT 1 FROM instruments WHERE code = ?", (code,)
            ).fetchone()
            if known is None:
                raise ValueError(f"unknown instrument code: {code}")
            connection.execute(
                """
                INSERT INTO raw_payloads(sha256, local_path, exists_on_disk)
                VALUES (?, ?, ?)
                ON CONFLICT(sha256) DO NOTHING
                """,
                (payload_sha256, str(raw_payload_path), 1),
            )
            connection.execute(
                """
                INSERT INTO ingestion_runs(
                    source_name, acquired_at, normalized_path, raw_payload_sha256, raw_payload_path
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_name, acquired_at, normalized_path, raw_payload_sha256) DO NOTHING
                """,
                (source_name, acquired_at, normalized_path, payload_sha256, str(raw_payload_path)),
            )
            run = connection.execute(
                """
                SELECT id FROM ingestion_runs
                WHERE source_name = ? AND acquired_at = ? AND normalized_path = ?
                    AND raw_payload_sha256 = ?
                """,
                (source_name, acquired_at, normalized_path, payload_sha256),
            ).fetchone()
            assert run is not None
            rows = [
                (
                    code,
                    interval_minutes,
                    _normalize_bar_end(item["timestamp"]),
                    item["open"],
                    item["close"],
                    item["high"],
                    item["low"],
                    item.get("volume"),
                    item.get("turnover"),
                    run["id"],
                )
                for item in _read_native_bar_rows(csv_path)
            ]
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO bar_vintages(
                    instrument_code, interval_minutes, bar_end, open_price, close_price,
                    high_price, low_price, volume, turnover, ingestion_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_code, interval_minutes, bar_end, ingestion_run_id)
                DO NOTHING
                """,
                rows,
            )
            return connection.total_changes - before

    def record_data_quality(
        self,
        *,
        code: str,
        interval_minutes: int,
        assessed_at: str,
        observed_trade_days: int,
        complete_core_days: int,
        report_path: Path,
    ) -> None:
        """Persist source-reported coverage without inferring missing sessions."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO data_quality_reports(
                    instrument_code, interval_minutes, assessed_at, observed_trade_days,
                    complete_core_days, report_path
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_code, interval_minutes, assessed_at, report_path)
                DO UPDATE SET
                    observed_trade_days = excluded.observed_trade_days,
                    complete_core_days = excluded.complete_core_days
                """,
                (
                    code,
                    interval_minutes,
                    assessed_at,
                    observed_trade_days,
                    complete_core_days,
                    str(report_path),
                ),
            )

    def ingest_quote_csv(
        self,
        *,
        csv_path: Path,
        raw_payload_path: Path,
        acquired_at: str,
        source_name: str,
    ) -> int:
        """Ingest target quote snapshots, retaining quote-to-raw-file lineage."""

        raw_payload_path = raw_payload_path.resolve()
        csv_path = csv_path.resolve()
        payload_sha256 = _payload_fingerprint(raw_payload_path)
        normalized_path = str(csv_path)
        quote_rows = _read_quote_rows(csv_path)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO raw_payloads(sha256, local_path, exists_on_disk)
                VALUES (?, ?, ?)
                ON CONFLICT(sha256) DO NOTHING
                """,
                (payload_sha256, str(raw_payload_path), 1),
            )
            connection.execute(
                """
                INSERT INTO ingestion_runs(
                    source_name, acquired_at, normalized_path, raw_payload_sha256, raw_payload_path
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_name, acquired_at, normalized_path, raw_payload_sha256) DO NOTHING
                """,
                (source_name, acquired_at, normalized_path, payload_sha256, str(raw_payload_path)),
            )
            run = connection.execute(
                """
                SELECT id FROM ingestion_runs
                WHERE source_name = ? AND acquired_at = ? AND normalized_path = ?
                    AND raw_payload_sha256 = ?
                """,
                (source_name, acquired_at, normalized_path, payload_sha256),
            ).fetchone()
            assert run is not None
            rows = []
            for item in quote_rows:
                code = item["symbol"]
                known = connection.execute(
                    "SELECT 1 FROM instruments WHERE code = ?", (code,)
                ).fetchone()
                if known is None:
                    raise ValueError(f"unknown instrument code: {code}")
                rows.append(
                    (
                        item["capture_id"],
                        code,
                        _normalize_observed_at(item["observed_at"]),
                        _blank_to_none(item.get("last_price")),
                        _blank_to_none(item.get("bid1_price")),
                        _blank_to_none(item.get("ask1_price")),
                        _blank_to_none(item.get("iopv")),
                        run["id"],
                    )
                )
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO quote_snapshots(
                    capture_id, instrument_code, observed_at, last_price, bid1_price,
                    ask1_price, iopv, ingestion_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(capture_id, instrument_code, ingestion_run_id) DO NOTHING
                """,
                rows,
            )
            return connection.total_changes - before

    def record_collection_run(self, report: dict[str, object], *, report_path: Path) -> None:
        """Persist every requested target outcome, including failures, for audit."""

        capture_id = str(report["capture_id"])
        results = report["results"]
        if not isinstance(results, list):
            raise TypeError("collection report results must be a list")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO collection_runs(capture_id, collected_at, requested, succeeded, failed, report_path)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(capture_id) DO UPDATE SET
                    collected_at = excluded.collected_at, requested = excluded.requested,
                    succeeded = excluded.succeeded, failed = excluded.failed,
                    report_path = excluded.report_path
                """,
                (
                    capture_id,
                    str(report["collected_at"]),
                    int(report["requested"]),
                    int(report["succeeded"]),
                    int(report["failed"]),
                    str(report_path),
                ),
            )
            rows = [
                (
                    capture_id,
                    str(item["symbol"]),
                    str(item["status"]),
                    item.get("error_type"),
                    item.get("error"),
                    item.get("raw_path"),
                    item.get("normalized_path"),
                )
                for item in results
                if isinstance(item, dict)
            ]
            connection.executemany(
                """
                INSERT INTO collection_run_items(
                    capture_id, instrument_code, status, error_type, error_message, raw_path, normalized_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(capture_id, instrument_code) DO UPDATE SET
                    status = excluded.status, error_type = excluded.error_type,
                    error_message = excluded.error_message, raw_path = excluded.raw_path,
                    normalized_path = excluded.normalized_path
                """,
                rows,
            )

    def list_instrument_capabilities(self) -> tuple[InstrumentCapability, ...]:
        """List every tracked ETF, including data readiness but no inferred policy."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT instruments.code, instruments.trading_name, instruments.exchange,
                    instruments.security_status,
                    instruments.last_review_date, instruments.t0_evidence_status,
                    COALESCE(
                        (SELECT MAX(quality.complete_core_days)
                        FROM data_quality_reports AS quality
                        WHERE quality.instrument_code = instruments.code
                            AND quality.interval_minutes = 5),
                        (SELECT COUNT(DISTINCT substr(bars.bar_end, 1, 10))
                        FROM bar_vintages AS bars
                        WHERE bars.instrument_code = instruments.code
                            AND bars.interval_minutes = 5)
                    ) AS historical_days,
                    (SELECT MAX(minute_bars.bar_end)
                    FROM bar_vintages AS minute_bars
                    WHERE minute_bars.instrument_code = instruments.code
                        AND minute_bars.interval_minutes = 1) AS latest_one_minute_bar_end
                FROM instruments
                ORDER BY instruments.exchange, instruments.code
                """
            ).fetchall()
        return tuple(
            InstrumentCapability(
                code=row["code"],
                trading_name=row["trading_name"],
                exchange=row["exchange"],
                security_status=row["security_status"],
                historical_five_minute_days=row["historical_days"],
                current_day_data_status=(
                    f"1分钟数据截至 {row['latest_one_minute_bar_end'][:10]}"
                    if row["latest_one_minute_bar_end"] is not None
                    else "WAIT_DATA"
                ),
                paper_policy_status=(
                    "M2策略已冻结（仅159570纸面）"
                    if row["code"] == "159570"
                    else "未注册策略"
                ),
                latest_one_minute_bar_end=row["latest_one_minute_bar_end"],
                t0_evidence_status=row["t0_evidence_status"],
                last_review_date=row["last_review_date"],
                research_gate_status=(
                    "仅收盘研究；跨源数据门禁G2/"
                    "券商可执行门禁G3未通过，非实盘准入"
                ),
            )
            for row in rows
        )

    def available_trade_dates(self) -> tuple[date, ...]:
        """Return dates having native one- or five-minute bars, newest first."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT substr(bar_end, 1, 10) AS trade_date
                FROM bar_vintages
                WHERE interval_minutes IN (1, 5)
                ORDER BY trade_date DESC
                """
            ).fetchall()
        return tuple(date.fromisoformat(row["trade_date"]) for row in rows)

    def bars_for_day(
        self, code: str, trade_date: date, *, interval_minutes: int
    ) -> tuple[tuple[str, str, str, str, str], ...]:
        """Return one target's latest stored native bars for one day only."""

        start = f"{trade_date.isoformat()}T00:00:00+08:00"
        end = f"{trade_date.isoformat()}T23:59:59+08:00"
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH latest AS (
                    SELECT bar_end, MAX(ingestion_run_id) AS ingestion_run_id
                    FROM bar_vintages
                    WHERE instrument_code = ? AND interval_minutes = ?
                        AND bar_end BETWEEN ? AND ?
                    GROUP BY bar_end
                )
                SELECT bars.bar_end, bars.open_price, bars.close_price,
                    bars.high_price, bars.low_price
                FROM bar_vintages AS bars
                INNER JOIN latest
                    ON latest.bar_end = bars.bar_end
                    AND latest.ingestion_run_id = bars.ingestion_run_id
                WHERE bars.instrument_code = ? AND bars.interval_minutes = ?
                ORDER BY bars.bar_end
                """,
                (code, interval_minutes, start, end, code, interval_minutes),
            ).fetchall()
        return tuple(
            (
                row["bar_end"],
                row["open_price"],
                row["close_price"],
                row["high_price"],
                row["low_price"],
            )
            for row in rows
        )

    def bar_lineage(
        self, code: str, bar_end: str, interval_minutes: int
    ) -> BarLineage:
        """Return source lineage for the latest stored vintage of one bar."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT runs.source_name, runs.raw_payload_path, payloads.sha256, runs.acquired_at
                FROM bar_vintages AS bars
                INNER JOIN ingestion_runs AS runs ON runs.id = bars.ingestion_run_id
                INNER JOIN raw_payloads AS payloads ON payloads.sha256 = runs.raw_payload_sha256
                WHERE bars.instrument_code = ? AND bars.interval_minutes = ? AND bars.bar_end = ?
                ORDER BY bars.ingestion_run_id DESC
                LIMIT 1
                """,
                (code, interval_minutes, bar_end),
            ).fetchone()
        if row is None:
            raise ValueError(f"no stored lineage for {code} at {bar_end}")
        return BarLineage(
            source_name=row["source_name"],
            raw_payload_path=row["raw_payload_path"],
            raw_payload_sha256=row["sha256"],
            acquired_at=row["acquired_at"],
        )

    def bar_input_fingerprint(self, code: str, trade_date: date, interval_minutes: int) -> str:
        """Fingerprint the exact latest bar vintages selected for one target/day."""

        start = f"{trade_date.isoformat()}T00:00:00+08:00"
        end = f"{trade_date.isoformat()}T23:59:59+08:00"
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH latest AS (
                    SELECT bar_end, MAX(ingestion_run_id) AS ingestion_run_id
                    FROM bar_vintages
                    WHERE instrument_code = ? AND interval_minutes = ?
                        AND bar_end BETWEEN ? AND ?
                    GROUP BY bar_end
                )
                SELECT bars.bar_end, bars.ingestion_run_id, runs.raw_payload_sha256
                FROM bar_vintages AS bars
                INNER JOIN latest ON latest.bar_end = bars.bar_end
                    AND latest.ingestion_run_id = bars.ingestion_run_id
                INNER JOIN ingestion_runs AS runs ON runs.id = bars.ingestion_run_id
                WHERE bars.instrument_code = ? AND bars.interval_minutes = ?
                ORDER BY bars.bar_end
                """,
                (code, interval_minutes, start, end, code, interval_minutes),
            ).fetchall()
        payload = [tuple(row) for row in rows]
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def latest_quote_snapshot(self, code: str) -> QuoteSnapshot:
        """Return only the requested target's newest stored quote snapshot."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT snapshots.instrument_code, snapshots.observed_at, snapshots.last_price,
                    snapshots.bid1_price, snapshots.ask1_price, snapshots.iopv,
                    runs.source_name, runs.raw_payload_path, payloads.sha256
                FROM quote_snapshots AS snapshots
                INNER JOIN ingestion_runs AS runs ON runs.id = snapshots.ingestion_run_id
                INNER JOIN raw_payloads AS payloads ON payloads.sha256 = runs.raw_payload_sha256
                WHERE snapshots.instrument_code = ?
                ORDER BY snapshots.observed_at DESC, snapshots.ingestion_run_id DESC
                LIMIT 1
                """,
                (code,),
            ).fetchone()
        if row is None:
            raise ValueError(f"no stored quote snapshot for {code}")
        return QuoteSnapshot(
            instrument_code=row["instrument_code"],
            observed_at=row["observed_at"],
            last_price=row["last_price"],
            bid1_price=row["bid1_price"],
            ask1_price=row["ask1_price"],
            iopv=row["iopv"],
            source_name=row["source_name"],
            raw_payload_path=row["raw_payload_path"],
            raw_payload_sha256=row["sha256"],
        )

    def store_paper_execution_record(self, record: PaperExecutionRecord) -> None:
        """Persist one validated manual observation; this never contacts a broker."""

        record.validate()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_execution_records(
                    instrument_code, observed_at, normal_overlap_day, intended_side,
                    intended_price, intended_quantity, observed_bid1_price, observed_ask1_price,
                    quote_source, fee_evidence, outcome, filled_price, filled_quantity,
                    outcome_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_code, observed_at, intended_side, intended_price, intended_quantity)
                DO UPDATE SET
                    normal_overlap_day = excluded.normal_overlap_day,
                    observed_bid1_price = excluded.observed_bid1_price,
                    observed_ask1_price = excluded.observed_ask1_price,
                    quote_source = excluded.quote_source,
                    fee_evidence = excluded.fee_evidence,
                    outcome = excluded.outcome,
                    filled_price = excluded.filled_price,
                    filled_quantity = excluded.filled_quantity,
                    outcome_reason = excluded.outcome_reason
                """,
                (
                    record.symbol,
                    record.observed_at.isoformat(),
                    int(record.normal_overlap_day),
                    record.intended_side.value,
                    str(record.intended_price),
                    record.intended_quantity,
                    str(record.observed_bid1_price),
                    str(record.observed_ask1_price),
                    record.quote_source,
                    record.fee_evidence,
                    record.outcome.value,
                    str(record.filled_price) if record.filled_price is not None else None,
                    record.filled_quantity,
                    record.outcome_reason,
                ),
            )

    def paper_execution_records_for_symbol(self, code: str) -> tuple[PaperExecutionRecord, ...]:
        """Return one target's manual records with every non-fill outcome retained."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT instrument_code, observed_at, normal_overlap_day, intended_side,
                    intended_price, intended_quantity, observed_bid1_price, observed_ask1_price,
                    quote_source, fee_evidence, outcome, filled_price, filled_quantity,
                    outcome_reason
                FROM paper_execution_records
                WHERE instrument_code = ?
                ORDER BY observed_at, intended_side, intended_price
                """,
                (code,),
            ).fetchall()
        return tuple(
            PaperExecutionRecord(
                symbol=row["instrument_code"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                normal_overlap_day=bool(row["normal_overlap_day"]),
                intended_side=OrderSide(row["intended_side"]),
                intended_price=Decimal(row["intended_price"]),
                intended_quantity=row["intended_quantity"],
                observed_bid1_price=Decimal(row["observed_bid1_price"]),
                observed_ask1_price=Decimal(row["observed_ask1_price"]),
                quote_source=row["quote_source"],
                fee_evidence=row["fee_evidence"],
                outcome=PaperExecutionOutcome(row["outcome"]),
                filled_price=(
                    Decimal(row["filled_price"]) if row["filled_price"] is not None else None
                ),
                filled_quantity=row["filled_quantity"],
                outcome_reason=row["outcome_reason"],
            )
            for row in rows
        )

    def store_completed_uptrends(
        self,
        *,
        code: str,
        trade_date: date,
        interval_minutes: int,
        parameters: TrendDetectionParameters,
        input_bar_sha256: str,
        input_latest_bar_end: str,
        calculated_at: str,
        intervals: tuple[CompletedUptrend, ...],
    ) -> None:
        """Persist descriptive trend intervals with their exact detection contract."""

        parameter_json, parameter_sha256 = _trend_parameter_contract(parameters)
        rows = [
            (
                code,
                trade_date.isoformat(),
                interval_minutes,
                interval.start_at,
                interval.end_at,
                interval.duration_bars,
                str(interval.start_close),
                str(interval.end_close),
                str(interval.rise_bps),
                str(interval.maximum_pullback_bps),
                interval.detection_version,
                parameter_sha256,
                parameter_json,
                input_bar_sha256,
                input_latest_bar_end,
                calculated_at,
                interval.executable_profit_status,
            )
            for interval in intervals
        ]
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM completed_uptrends
                WHERE instrument_code = ? AND trade_date = ? AND interval_minutes = ?
                    AND parameter_sha256 = ?
                """,
                (code, trade_date.isoformat(), interval_minutes, parameter_sha256),
            )
            connection.executemany(
                """
                INSERT INTO completed_uptrends(
                    instrument_code, trade_date, interval_minutes, start_at, end_at,
                    duration_bars, start_close, end_close, rise_bps, maximum_pullback_bps,
                    detection_version, parameter_sha256, parameter_json, input_bar_sha256,
                    input_latest_bar_end, calculated_at,
                    executable_profit_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_code, start_at, end_at, parameter_sha256) DO UPDATE SET
                    duration_bars = excluded.duration_bars,
                    rise_bps = excluded.rise_bps,
                    maximum_pullback_bps = excluded.maximum_pullback_bps,
                    parameter_json = excluded.parameter_json,
                    input_bar_sha256 = excluded.input_bar_sha256,
                    input_latest_bar_end = excluded.input_latest_bar_end,
                    calculated_at = excluded.calculated_at,
                    executable_profit_status = excluded.executable_profit_status
                """,
                rows,
            )

    def completed_uptrends_for_day(
        self, code: str, trade_date: date
    ) -> tuple[CompletedUptrend, ...]:
        """Return descriptive intervals; callers must keep their research label visible."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT start_at, end_at, duration_bars, start_close, end_close, rise_bps,
                    maximum_pullback_bps, detection_version, executable_profit_status
                FROM completed_uptrends
                WHERE instrument_code = ? AND trade_date = ?
                ORDER BY start_at
                """,
                (code, trade_date.isoformat()),
            ).fetchall()
        return tuple(
            CompletedUptrend(
                start_at=row["start_at"],
                end_at=row["end_at"],
                duration_bars=row["duration_bars"],
                start_close=Decimal(row["start_close"]),
                end_close=Decimal(row["end_close"]),
                rise_bps=Decimal(row["rise_bps"]),
                maximum_pullback_bps=Decimal(row["maximum_pullback_bps"]),
                detection_version=row["detection_version"],
                executable_profit_status=row["executable_profit_status"],
            )
            for row in rows
        )


def _read_native_bar_rows(csv_path: Path) -> tuple[dict[str, str], ...]:
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = tuple(csv.DictReader(handle))
    required = {"timestamp", "open", "close", "high", "low"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"native bar CSV lacks required columns: {csv_path}")
    return rows


def _read_quote_rows(csv_path: Path) -> tuple[dict[str, str], ...]:
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = tuple(csv.DictReader(handle))
    required = {"capture_id", "symbol", "observed_at"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"quote CSV lacks required columns: {csv_path}")
    return rows


def _normalize_bar_end(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI).isoformat(timespec="seconds")


def _normalize_observed_at(value: str) -> str:
    return _normalize_bar_end(value)


def _blank_to_none(value: str | None) -> str | None:
    return value if value not in (None, "") else None


def _payload_fingerprint(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"immutable raw payload is unavailable: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trend_parameter_contract(parameters: TrendDetectionParameters) -> tuple[str, str]:
    payload = {
        "version": parameters.version,
        "minimum_duration_bars": parameters.minimum_duration_bars,
        "minimum_rise_bps": str(parameters.minimum_rise_bps),
        "maximum_pullback_bps": str(parameters.maximum_pullback_bps),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _ensure_completed_uptrend_parameter_json(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(completed_uptrends)").fetchall()
    }
    if "parameter_json" not in columns:
        connection.execute(
            "ALTER TABLE completed_uptrends ADD COLUMN parameter_json TEXT NOT NULL DEFAULT '{}'"
        )


def _ensure_completed_uptrend_input_fingerprint(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(completed_uptrends)").fetchall()
    }
    if "input_bar_sha256" not in columns:
        connection.execute(
            "ALTER TABLE completed_uptrends ADD COLUMN input_bar_sha256 TEXT NOT NULL DEFAULT ''"
        )


def _ensure_ingestion_run_raw_payload_path(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(ingestion_runs)").fetchall()
    }
    if "raw_payload_path" not in columns:
        connection.execute(
            "ALTER TABLE ingestion_runs ADD COLUMN raw_payload_path TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            """
            UPDATE ingestion_runs
            SET raw_payload_path = (
                SELECT local_path FROM raw_payloads
                WHERE raw_payloads.sha256 = ingestion_runs.raw_payload_sha256
            )
            """
        )


def _ensure_instrument_evidence_status(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(instruments)").fetchall()
    }
    if "t0_evidence_status" not in columns:
        connection.execute(
            "ALTER TABLE instruments ADD COLUMN t0_evidence_status TEXT NOT NULL DEFAULT '待复核'"
        )
