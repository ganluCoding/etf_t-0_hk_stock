"""Current-paper observation service over an explicitly unverified research feed."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd

from etf_t0.fees import (
    ETF_PRICE_TICK,
    FeeSchedule,
    OrderSide,
    break_even_for_round_trip,
    cost_for_order,
    provisional_fee_scenarios,
)
from etf_t0.market_calendar import NormalOverlapCalendar, load_normal_overlap_calendar
from etf_t0.observation import (
    M1_PROFILE_ID,
    DecisionStatus,
    FeeProfileStatus,
    ObservationMode,
    ObservationPolicyMetadata,
    PriceLevel,
    TargetObservationDecision,
    TargetObservationRequest,
)
from etf_t0.universe import (
    EligibilityStatus,
    EtfUniverseRecord,
    SecurityStatus,
    load_universe_ledger,
)

REQUIRED_SIGNAL_BARS = 48
RESEARCH_FEED_LABEL = "UNVERIFIED RESEARCH FEED"
M2_TACTICAL_CASH_CNY = Decimal(5000)
EXPECTED_FORWARD_CONFIG_SHA256 = (
    "13682f8e25ca6da40422dac8b9e954ef72ae694ec43ce607d0b470a7f3e0b815"
)


def _is_continuous_auction(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minute < 11 * 60 + 30 or 13 * 60 <= minute < 14 * 60 + 57


def _eligibility_evidence_is_current(
    *, record: EtfUniverseRecord, requested_code: str, as_of: date
) -> bool:
    try:
        record.validate()
    except (TypeError, ValueError):
        return False
    if record.evidence is None:
        return False
    expected_exchange = {
        "SZSE": ("深圳证券交易所", "szse.cn"),
        "SSE": ("上海证券交易所", "sse.com.cn"),
    }.get(record.exchange)
    if expected_exchange is None:
        return False
    expected_issuer, expected_domain = expected_exchange
    hostname = (urlparse(record.evidence.source_document_url).hostname or "").lower()
    review_age = (as_of - record.last_review_date).days
    return (
        record.code == requested_code
        and record.evidence.issuer == expected_issuer
        and (hostname == expected_domain or hostname.endswith(f".{expected_domain}"))
        and record.evidence.asserts_same_day_turnaround_for(requested_code)
        and record.security_status is SecurityStatus.LISTED
        and record.evidence.announcement_date <= record.listing_date
        and 0 <= review_age <= 31
    )


@dataclass(frozen=True)
class ResearchQuote:
    symbol: str
    name: str
    bid: Decimal
    ask: Decimal
    iopv: Decimal
    provider_update_time: datetime


@dataclass(frozen=True)
class PairedFiveMinuteBar:
    timestamp: datetime
    target_close: Decimal
    reference_close: Decimal
    first_available_at: datetime


@dataclass(frozen=True)
class ResearchSnapshot:
    snapshot_id: str
    observed_at: datetime
    target: ResearchQuote
    reference: ResearchQuote
    paired_five_minute_bars: tuple[PairedFiveMinuteBar, ...]
    capture_candidate: bool
    desktop_eligible: bool
    valid_forward_sample_available: bool
    data_gate_reasons: tuple[str, ...] = ()
    g2_pass: bool = False
    g3_pass: bool = False
    policy_metadata: ObservationPolicyMetadata | None = None
    config_sha256: str | None = None
    feed_verified: bool = False


class ResearchSnapshotSource(Protocol):
    def refresh(self) -> ResearchSnapshot: ...


def maximum_affordable_lot_quantity(
    *, capital_cny: Decimal, buy_price: Decimal, schedule: FeeSchedule
) -> int:
    quantity = int(capital_cny / buy_price / 100) * 100
    while quantity > 0:
        notional = buy_price * quantity
        buy_cost = cost_for_order(
            schedule=schedule,
            side=OrderSide.BUY,
            reference_notional=notional,
        )
        if notional + buy_cost.economic_cost <= capital_cny:
            return quantity
        quantity -= 100
    return 0


def expected_completed_five_minute_slots(
    *,
    as_of: datetime,
    count: int,
    normal_overlap_calendar: NormalOverlapCalendar | None = None,
) -> tuple[datetime, ...]:
    if as_of.tzinfo is None or count <= 0:
        raise ValueError("as_of must be timezone-aware and count positive")
    timezone = ZoneInfo("Asia/Shanghai")
    local = as_of.astimezone(timezone)
    day = local.date()
    candidates: list[datetime] = []
    while len(candidates) < count:
        if (
            normal_overlap_calendar is not None
            and day < normal_overlap_calendar.valid_from
        ):
            break
        if day.weekday() < 5 and (
            normal_overlap_calendar is None
            or normal_overlap_calendar.is_normal_overlap_day(day)
        ):
            endpoints = (
                [(9, minute) for minute in range(35, 60, 5)]
                + [(10, minute) for minute in range(0, 60, 5)]
                + [(11, minute) for minute in range(0, 31, 5)]
                + [(13, minute) for minute in range(5, 60, 5)]
                + [(14, minute) for minute in range(0, 56, 5)]
                + [(15, 0)]
            )
            for hour, minute in endpoints:
                slot = datetime(
                    day.year, day.month, day.day, hour, minute, tzinfo=timezone
                )
                if slot < local:
                    candidates.append(slot)
        day -= timedelta(days=1)
    return tuple(sorted(candidates)[-count:])


class LocalCaptureRunner:
    """Reuse a healthy collector's atomic manifest, otherwise run one safe fallback."""

    def __init__(
        self,
        *,
        workspace: Path,
        fallback: Callable[[], dict[str, Any]],
        clock: Callable[[], datetime],
    ) -> None:
        self._workspace = workspace
        self._fallback = fallback
        self._clock = clock

    def __call__(self) -> dict[str, Any]:
        generated = self._workspace / "reports" / "generated" / "forward_capture"
        heartbeat_path = generated / "collector_heartbeat.json"
        manifest_path = generated / "latest_manifest.json"
        try:
            heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            updated_at = datetime.fromisoformat(str(heartbeat["updated_at"]))
            heartbeat_age = self._clock() - updated_at
            pid = heartbeat.get("pid")
            process_alive = isinstance(pid, int) and pid > 0
            if process_alive:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    process_alive = False
                except PermissionError:
                    process_alive = True
            collector_healthy = (
                heartbeat.get("state") in {"STARTING", "RUNNING"}
                and timedelta(seconds=-5) <= heartbeat_age <= timedelta(seconds=45)
                and process_alive
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            collector_healthy = False
        if collector_healthy:
            try:
                payload: dict[str, Any] = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                return payload
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "采集器正在运行，但尚无完整原子快照"
                ) from exc
        lock_path = generated / "collector.lock"
        generated.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "采集器仍在运行，心跳暂时陈旧；请等待下一次刷新"
                ) from exc
            return self._fallback()


def load_causal_paired_five_minute_bars(
    *,
    workspace: Path,
    as_of: datetime,
    normal_overlap_calendar: NormalOverlapCalendar | None = None,
) -> tuple[PairedFiveMinuteBar, ...]:
    """Build complete 5-minute bars only from point-in-time eligible 1-minute vintages."""

    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")

    def symbol_bars(symbol: str) -> dict[datetime, tuple[Decimal, datetime]]:
        path = (
            workspace
            / "data"
            / "interim"
            / "forward_capture"
            / "one_minute"
            / symbol
            / "bars.csv"
        )
        if not path.exists():
            return {}
        data = pd.read_csv(path)
        required = {
            "timestamp",
            "close",
            "is_valid_ohlc",
            "is_candidate_forward_pair_bar",
            "selected_vintage_received_at",
        }
        if not required.issubset(data.columns):
            return {}
        data["timestamp"] = pd.to_datetime(data["timestamp"], errors="coerce")
        data["selected_vintage_received_at"] = pd.to_datetime(
            data["selected_vintage_received_at"], errors="coerce", utc=True
        )
        data["close"] = pd.to_numeric(data["close"], errors="coerce")
        valid = (
            data["is_valid_ohlc"].astype(str).str.lower().eq("true")
            & data["is_candidate_forward_pair_bar"]
            .astype(str)
            .str.lower()
            .eq("true")
            & data["timestamp"].notna()
            & data["selected_vintage_received_at"].notna()
            & data["close"].gt(0)
            & data["selected_vintage_received_at"].le(pd.Timestamp(as_of).tz_convert("UTC"))
        )
        if normal_overlap_calendar is not None:
            valid &= data["timestamp"].dt.date.map(
                normal_overlap_calendar.is_normal_overlap_day
            )
        data = data.loc[valid].copy()
        data["bar_end"] = (
            (data["timestamp"] - pd.Timedelta(minutes=1)).dt.floor("5min")
            + pd.Timedelta(minutes=5)
        )
        result: dict[datetime, tuple[Decimal, datetime]] = {}
        for bar_end, group in data.groupby("bar_end", sort=True):
            ordered = group.sort_values("timestamp")
            expected = list(pd.date_range(bar_end - pd.Timedelta(minutes=4), bar_end, freq="1min"))
            if list(ordered["timestamp"]) != expected:
                continue
            local_end = bar_end.to_pydatetime().replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            available = (
                ordered["selected_vintage_received_at"]
                .max()
                .tz_convert("Asia/Shanghai")
                .to_pydatetime()
            )
            result[local_end] = (Decimal(str(ordered.iloc[-1]["close"])), available)
        return result

    target = symbol_bars("159570")
    reference = symbol_bars("513780")
    paired = []
    for timestamp in sorted(set(target) & set(reference)):
        target_close, target_available = target[timestamp]
        reference_close, reference_available = reference[timestamp]
        paired.append(
            PairedFiveMinuteBar(
                timestamp=timestamp,
                target_close=target_close,
                reference_close=reference_close,
                first_available_at=max(target_available, reference_available),
            )
        )
    return tuple(paired)


class ForwardResearchSnapshotSource:
    """Normalize one persisted forward-capture result for the desktop service."""

    def __init__(
        self,
        *,
        workspace: Path,
        capture_runner: Callable[[], dict[str, Any]],
        clock: Callable[[], datetime],
        normal_overlap_calendar: NormalOverlapCalendar | None = None,
    ) -> None:
        self._workspace = workspace
        self._capture_runner = capture_runner
        self._clock = clock
        self._normal_overlap_calendar = normal_overlap_calendar

    def refresh(self) -> ResearchSnapshot:
        manifest = self._capture_runner()
        rows = {row["symbol"]: row for row in manifest["latest_quote_rows"]}
        target_row = rows["159570"]
        reference_row = rows["513780"]
        if (
            target_row.get("capture_id") != reference_row.get("capture_id")
            or target_row.get("observed_at") != reference_row.get("observed_at")
        ):
            raise ValueError("paired quote rows must share the same capture and observed_at")
        observed_at = datetime.fromisoformat(str(target_row["observed_at"]))
        target = self._quote(target_row)
        reference = self._quote(reference_row)
        stage = manifest.get("stage_status", {})
        valid_forward = bool(stage.get("valid_forward_sample_available", False))
        g2_pass = str(stage.get("G2", "")).startswith("PASS")
        g3_pass = str(stage.get("G3", "")).startswith("PASS")
        capture_candidate = bool(
            target_row.get("is_candidate_forward_pair")
            and reference_row.get("is_candidate_forward_pair")
        )
        gate_reasons = tuple(
            reason
            for gate, reason in (
                ("G2", "G2 数据验收未通过：尚缺独立数据源与交易日历交叉核对。"),
                ("G3", "G3 执行数据验收未通过：尚缺盘中传输时延与可成交深度验证。"),
            )
            if stage.get(gate) and not str(stage[gate]).startswith("PASS")
        )
        bars = load_causal_paired_five_minute_bars(
            workspace=self._workspace,
            as_of=observed_at,
            normal_overlap_calendar=self._normal_overlap_calendar,
        )
        policy_metadata = self._policy_metadata(manifest=manifest, bars=bars)
        return ResearchSnapshot(
            snapshot_id=str(target_row["capture_id"]),
            observed_at=observed_at,
            target=target,
            reference=reference,
            paired_five_minute_bars=bars,
            capture_candidate=capture_candidate,
            desktop_eligible=(
                capture_candidate
                and len(bars) >= REQUIRED_SIGNAL_BARS
                and policy_metadata is not None
            ),
            valid_forward_sample_available=valid_forward,
            data_gate_reasons=gate_reasons,
            g2_pass=g2_pass,
            g3_pass=g3_pass,
            policy_metadata=policy_metadata,
            config_sha256=str(manifest.get("config_sha256") or "") or None,
            feed_verified=False,
        )

    def _policy_metadata(
        self,
        *,
        manifest: dict[str, Any],
        bars: tuple[PairedFiveMinuteBar, ...],
    ) -> ObservationPolicyMetadata | None:
        config_path = self._workspace / "config" / "forward_collection.json"
        if not config_path.exists():
            return None
        config = json.loads(config_path.read_text(encoding="utf-8"))
        computed_config_hash = hashlib.sha256(
            json.dumps(
                config,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if computed_config_hash != EXPECTED_FORWARD_CONFIG_SHA256:
            raise ValueError("committed forward configuration drifted from Issue #19")
        if computed_config_hash != manifest.get("config_sha256"):
            raise ValueError("manifest config hash does not match committed configuration")
        strategy = config["frozen_strategy"]
        if strategy != manifest.get("frozen_strategy"):
            raise ValueError("manifest strategy does not match committed frozen strategy")
        bar_fingerprint = "|".join(
            f"{bar.timestamp.isoformat()}:{bar.target_close}:{bar.reference_close}:"
            f"{bar.first_available_at.isoformat()}"
            for bar in bars[-REQUIRED_SIGNAL_BARS:]
        )
        return ObservationPolicyMetadata(
            hypothesis_id=strategy["hypothesis_id"],
            target_symbol=strategy["target_symbol"],
            family=strategy["family"],
            anchor_formula=strategy["residual_formula"],
            signal_bar_interval_minutes=int(
                strategy["signal_bar_interval_minutes"]
            ),
            signal_timing="completed causal 5m bar then next eligible quote",
            training_window="frozen exploration; rolling causal L48 forward observation",
            parameter_search_log="reports/t0_etf_multi_strategy_exploration.md",
            frozen_at=datetime.fromisoformat(config["frozen_at"]),
            forward_sample_start_date=date.fromisoformat(
                config["forward_sample_start_date"]
            ),
            research_source_git_commit=config["research_source_git_commit"],
            strategy_source_blob=config["strategy_source_blob"],
            data_hash=hashlib.sha256(bar_fingerprint.encode("utf-8")).hexdigest(),
            validation_status=(
                "G2_PASS_G3_PASS"
                if str(manifest.get("stage_status", {}).get("G2", "")).startswith(
                    "PASS"
                )
                and str(
                    manifest.get("stage_status", {}).get("G3", "")
                ).startswith("PASS")
                else "G2_G3_BLOCKED_RESEARCH_ONLY"
            ),
            allowed_mode=ObservationMode.PAPER_OBSERVATION,
        )

    @staticmethod
    def _quote(row: dict[str, Any]) -> ResearchQuote:
        return ResearchQuote(
            symbol=str(row["symbol"]),
            name=str(row["name"]),
            bid=Decimal(str(row["bid1_price"])),
            ask=Decimal(str(row["ask1_price"])),
            iopv=Decimal(str(row["iopv"])),
            provider_update_time=datetime.fromisoformat(
                str(row["provider_update_time"])
            ),
        )


class ResearchObservationService:
    """Return honest current-paper states without falling back to M1 fixtures."""

    def __init__(
        self,
        *,
        source: ResearchSnapshotSource,
        clock: Callable[[], datetime],
        eligibility_record: EtfUniverseRecord | None = None,
        normal_overlap_calendar: NormalOverlapCalendar | None = None,
    ) -> None:
        self._source = source
        self._clock = clock
        self._eligibility_record = eligibility_record
        self._normal_overlap_calendar = normal_overlap_calendar

    @staticmethod
    def _no_go(
        *, request: TargetObservationRequest, now: datetime, reason: str
    ) -> TargetObservationDecision:
        return TargetObservationDecision(
            decision_id=f"m2-{request.etf_code or 'empty'}-{now.strftime('%Y%m%dT%H%M%S.%f%z')}",
            etf_code=request.etf_code,
            etf_name=None,
            mode=ObservationMode.PAPER_OBSERVATION,
            status=DecisionStatus.NO_GO,
            target_bid=None,
            target_ask=None,
            buy_observation_ceiling=None,
            strategy_exit_level=None,
            break_even_reference=None,
            estimated_quantity=None,
            round_trip_cost_cny=None,
            fee_profile_status=None,
            policy_version=None,
            data_snapshot_id=None,
            eligibility_evidence_id=None,
            eligibility_reviewed_on=None,
            policy_metadata=None,
            generated_at=now,
            reasons=(reason,),
            feed_label=RESEARCH_FEED_LABEL,
        )

    def evaluate(self, request: TargetObservationRequest) -> TargetObservationDecision:
        now = self._clock()
        if len(request.etf_code) != 6 or not request.etf_code.isdigit():
            return self._no_go(
                request=request,
                now=now,
                reason="ETF代码必须是六位数字。",
            )
        if request.etf_code != "159570":
            return self._no_go(
                request=request,
                now=now,
                reason="该目标 ETF 尚未注册 M2 当前纸面观察策略。",
            )
        if request.profile_id != M1_PROFILE_ID:
            return self._no_go(
                request=request,
                now=now,
                reason="未找到指定的本地资金与费用档案。",
            )
        if not _is_continuous_auction(now):
            return self._no_go(
                request=request,
                now=now,
                reason="当前不在连续竞价观察窗口。",
            )
        if (
            self._normal_overlap_calendar is not None
            and not self._normal_overlap_calendar.is_normal_overlap_day(now.date())
        ):
            return self._no_go(
                request=request,
                now=now,
                reason="当前日期不是版本化日历确认的正常重合日。",
            )
        try:
            snapshot = self._source.refresh()
        except Exception as exc:  # noqa: BLE001 - external data boundary must fail closed
            return self._no_go(
                request=request,
                now=now,
                reason=(
                    "行情刷新失败，请检查网络或本机采集状态后重试："
                    f"{exc}"
                ),
            )
        if now - snapshot.observed_at > timedelta(seconds=30):
            return self._no_go(
                request=request,
                now=now,
                reason="行情快照已超过30秒，请重新刷新。",
            )
        if snapshot.observed_at > now + timedelta(seconds=5):
            return self._no_go(
                request=request,
                now=now,
                reason="本机时钟早于行情快照，请校准时钟后重试。",
            )
        if (
            snapshot.target.symbol != "159570"
            or snapshot.reference.symbol != "513780"
            or snapshot.target.bid <= 0
            or snapshot.target.ask < snapshot.target.bid
            or snapshot.reference.bid <= 0
            or snapshot.reference.ask < snapshot.reference.bid
            or snapshot.target.iopv <= 0
            or snapshot.reference.iopv <= 0
        ):
            return self._no_go(
                request=request,
                now=now,
                reason="行情快照标的或价格字段未通过严格校验。",
            )
        if not snapshot.capture_candidate:
            return self._no_go(
                request=request,
                now=now,
                reason="当前快照未通过同步性、新鲜度或连续竞价门禁。",
            )
        provider_times = (
            snapshot.target.provider_update_time,
            snapshot.reference.provider_update_time,
        )
        provider_delays = tuple(
            (snapshot.observed_at - provider_time).total_seconds()
            for provider_time in provider_times
        )
        if (
            (max(provider_times) - min(provider_times)).total_seconds() > 5
            or any(delay < -5 or delay > 10 for delay in provider_delays)
            or any(
                provider_time.date() != snapshot.observed_at.date()
                for provider_time in provider_times
            )
        ):
            return self._no_go(
                request=request,
                now=now,
                reason="提供方报价已陈旧、非当日或成对时间差过大。",
            )
        bar_count = len(snapshot.paired_five_minute_bars)
        signal_bars = snapshot.paired_five_minute_bars[-REQUIRED_SIGNAL_BARS:]
        expected_slots = expected_completed_five_minute_slots(
            as_of=snapshot.observed_at,
            count=REQUIRED_SIGNAL_BARS,
            normal_overlap_calendar=self._normal_overlap_calendar,
        )
        signal_slots_complete = (
            bar_count >= REQUIRED_SIGNAL_BARS
            and tuple(bar.timestamp for bar in signal_bars) == expected_slots
        )
        progress_reason = (
            f"已连接当日研究行情，但因果5分钟bar为 "
            f"{bar_count}/{REQUIRED_SIGNAL_BARS}。"
        )
        reasons = (
            progress_reason,
            *(
                ()
                if bar_count < REQUIRED_SIGNAL_BARS or signal_slots_complete
                else ("最近48个预期5分钟时槽不连续或最新bar缺失。",)
            ),
            *snapshot.data_gate_reasons,
        )
        if (
            not snapshot.desktop_eligible
            or snapshot.policy_metadata is None
            or bar_count < REQUIRED_SIGNAL_BARS
            or not signal_slots_complete
        ):
            return TargetObservationDecision(
                decision_id=f"m2-{request.etf_code}-{now.strftime('%Y%m%dT%H%M%S.%f%z')}",
                etf_code=request.etf_code,
                etf_name=snapshot.target.name,
                mode=ObservationMode.PAPER_OBSERVATION,
                status=DecisionStatus.WAIT_DATA,
                target_bid=snapshot.target.bid,
                target_ask=snapshot.target.ask,
                buy_observation_ceiling=None,
                strategy_exit_level=None,
                break_even_reference=None,
                estimated_quantity=None,
                round_trip_cost_cny=None,
                fee_profile_status=None,
                policy_version=(
                    snapshot.policy_metadata.hypothesis_id
                    if snapshot.policy_metadata
                    else None
                ),
                data_snapshot_id=snapshot.snapshot_id,
                eligibility_evidence_id=None,
                eligibility_reviewed_on=None,
                policy_metadata=snapshot.policy_metadata,
                generated_at=now,
                reasons=reasons,
                target_iopv=snapshot.target.iopv,
                feed_label=RESEARCH_FEED_LABEL,
                data_valid_until=snapshot.observed_at + timedelta(seconds=30),
                signal_bar_count=bar_count,
                signal_bar_required=REQUIRED_SIGNAL_BARS,
                data_gate_reasons=snapshot.data_gate_reasons,
                config_sha256=snapshot.config_sha256,
            )

        if any(bar.first_available_at >= snapshot.observed_at for bar in signal_bars):
            return self._no_go(
                request=request,
                now=now,
                reason="行情快照不是信号bar完成后的下一个可用快照。",
            )
        if (
            snapshot.target.ask - snapshot.target.bid > Decimal("0.002")
            or snapshot.reference.ask - snapshot.reference.bid > Decimal("0.002")
        ):
            return self._no_go(
                request=request,
                now=now,
                reason="买卖价差或成对报价时间差未通过严格执行门禁。",
            )
        eligibility = self._eligibility_record
        if (
            eligibility is None
            or eligibility.code != request.etf_code
            or eligibility.status is not EligibilityStatus.CONFIRMED
            or eligibility.evidence is None
        ):
            return self._no_go(
                request=request,
                now=now,
                reason="交易所 T+0 资格台账证据缺失或未确认。",
            )
        if not _eligibility_evidence_is_current(
            record=eligibility,
            requested_code=request.etf_code,
            as_of=now.date(),
        ):
            return self._no_go(
                request=request,
                now=now,
                reason="交易所 T+0 资格证据语义未通过校验。",
            )
        policy_metadata = snapshot.policy_metadata
        assert policy_metadata is not None
        if snapshot.config_sha256 != EXPECTED_FORWARD_CONFIG_SHA256:
            return self._no_go(
                request=request,
                now=now,
                reason="快照未绑定 Issue #19 完整冻结配置。",
            )
        try:
            policy_metadata.validate()
        except ValueError as exc:
            return self._no_go(
                request=request,
                now=now,
                reason=f"冻结策略 lineage 未通过校验：{exc}",
            )
        if policy_metadata.hypothesis_id != "PROXY_RESIDUAL_L48_Z150_H12_MAX1":
            return self._no_go(
                request=request,
                now=now,
                reason="快照未绑定 Issue #19 冻结策略。",
            )

        residuals = [
            math.log(float(bar.target_close / bar.reference_close))
            for bar in signal_bars
        ]
        mean = math.fsum(residuals) / REQUIRED_SIGNAL_BARS
        variance = math.fsum((value - mean) ** 2 for value in residuals) / REQUIRED_SIGNAL_BARS
        sigma = math.sqrt(variance)
        if not math.isfinite(sigma) or sigma <= 0:
            return self._no_go(
                request=request,
                now=now,
                reason="48根因果bar的残差波动率无效，不生成观察价。",
            )
        raw_buy = Decimal(str(math.exp(mean - 1.5 * sigma)))
        raw_buy *= snapshot.reference.bid
        raw_exit = Decimal(str(math.exp(mean - 0.25 * sigma)))
        raw_exit *= snapshot.reference.ask
        buy_price = (
            (raw_buy / ETF_PRICE_TICK).to_integral_value(rounding=ROUND_FLOOR)
            * ETF_PRICE_TICK
        )
        exit_price = (
            (raw_exit / ETF_PRICE_TICK).to_integral_value(rounding=ROUND_CEILING)
            * ETF_PRICE_TICK
        )
        schedule = provisional_fee_scenarios()[1]
        quantity = maximum_affordable_lot_quantity(
            capital_cny=M2_TACTICAL_CASH_CNY,
            buy_price=buy_price,
            schedule=schedule,
        )
        if quantity <= 0:
            return self._no_go(
                request=request,
                now=now,
                reason="五成现金基线无法覆盖100份买入及买侧费用。",
            )
        execution_buffer = Decimal("0.002") * quantity
        break_even_result = break_even_for_round_trip(
            schedule=schedule,
            entry_price=buy_price,
            quantity=quantity,
            required_net_profit_cny=execution_buffer,
        )
        break_even = break_even_result.tick_aligned_exit_price
        sell_cost = cost_for_order(
            schedule=schedule,
            side=OrderSide.SELL,
            reference_notional=exit_price * quantity,
        )
        guarded_cost = (
            break_even_result.round_trip_cost.buy.economic_cost
            + sell_cost.economic_cost
            + execution_buffer
        )
        if exit_price < break_even:
            return self._no_go(
                request=request,
                now=now,
                reason="策略观察区间无法覆盖双边费用与2个ETF价格档执行缓冲。",
            )
        valid_until = snapshot.observed_at + timedelta(seconds=30)
        evidence_identity = (
            f"{eligibility.code}|{eligibility.evidence.issuer}|"
            f"{eligibility.evidence.source_document_url}|"
            f"{eligibility.last_review_date.isoformat()}"
        )
        evidence_id = "m2-" + hashlib.sha256(
            evidence_identity.encode("utf-8")
        ).hexdigest()[:16]
        return TargetObservationDecision(
            decision_id=f"m2-{request.etf_code}-{now.strftime('%Y%m%dT%H%M%S.%f%z')}",
            etf_code=request.etf_code,
            etf_name=snapshot.target.name,
            mode=ObservationMode.PAPER_OBSERVATION,
            status=DecisionStatus.WAIT,
            target_bid=snapshot.target.bid,
            target_ask=snapshot.target.ask,
            buy_observation_ceiling=PriceLevel(
                price=buy_price,
                relation="target_ask_lte",
                valid_until=valid_until,
            ),
            strategy_exit_level=PriceLevel(
                price=exit_price,
                relation="target_bid_gte",
                valid_until=valid_until,
            ),
            break_even_reference=break_even,
            estimated_quantity=quantity,
            round_trip_cost_cny=guarded_cost,
            fee_profile_status=FeeProfileStatus.PROVISIONAL,
            policy_version=policy_metadata.hypothesis_id,
            data_snapshot_id=snapshot.snapshot_id,
            eligibility_evidence_id=evidence_id,
            eligibility_reviewed_on=eligibility.last_review_date,
            policy_metadata=policy_metadata,
            generated_at=now,
            reasons=(
                "观察价仅用于人工纸面观察，不是下单指令。",
                "费用采用159570尚未校准的通用临时单边最低5元情景、可能另收经手费，并加2个价格档执行缓冲。",
                *snapshot.data_gate_reasons,
            ),
            target_iopv=snapshot.target.iopv,
            feed_label=RESEARCH_FEED_LABEL,
            data_valid_until=valid_until,
            signal_bar_count=bar_count,
            signal_bar_required=REQUIRED_SIGNAL_BARS,
            data_gate_reasons=snapshot.data_gate_reasons,
            config_sha256=snapshot.config_sha256,
        )


def create_current_research_service(
    *,
    workspace: Path,
    capture_runner: Callable[[], dict[str, Any]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ResearchObservationService:
    """Build the M2 local research service; never substitutes the M1 fixture."""

    active_clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
    normal_overlap_calendar = load_normal_overlap_calendar(
        workspace / "config" / "normal_overlap_calendar_2026.json"
    )
    if capture_runner is None:
        from etf_t0.forward_collection import run_once

        fallback = lambda: run_once(
            config_path=workspace / "config" / "forward_collection.json",
            ledger_path=workspace / "config" / "universe" / "t0_etf_ledger.json",
            workspace=workspace,
            include_depth=False,
            include_minute=False,
        )
        capture_runner = LocalCaptureRunner(
            workspace=workspace,
            fallback=fallback,
            clock=active_clock,
        )
    source = ForwardResearchSnapshotSource(
        workspace=workspace,
        capture_runner=capture_runner,
        clock=active_clock,
        normal_overlap_calendar=normal_overlap_calendar,
    )
    eligibility_record = next(
        (
            record
            for record in load_universe_ledger(
                workspace / "config" / "universe" / "t0_etf_ledger.json"
            )
            if record.code == "159570"
        ),
        None,
    )
    return ResearchObservationService(
        source=source,
        clock=active_clock,
        eligibility_record=eligibility_record,
        normal_overlap_calendar=normal_overlap_calendar,
    )
