"""Target-only paper-observation decisions for the local desktop prototype.

M1 uses an explicitly versioned fixture. It does not fetch market data, establish
broker executability, or expose order actions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from etf_t0.fees import (
    ETF_PRICE_TICK,
    break_even_for_round_trip,
    provisional_fee_scenarios,
)
from etf_t0.universe import (
    EligibilityStatus,
    EtfUniverseRecord,
    EvidenceSourceKind,
    load_universe_ledger,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
LOT_SIZE = 100
M1_CAPITAL_CNY = Decimal(10000)
M1_PROFILE_ID = "generic-provisional-minimum"


def _is_continuous_session(moment: datetime) -> bool:
    local_time = moment.astimezone(SHANGHAI).time()
    return time(9, 30) <= local_time < time(11, 30) or time(13) <= local_time < time(14, 57)


class ObservationMode(str, Enum):
    PAPER_OBSERVATION = "paper_observation"


class DecisionStatus(str, Enum):
    NO_GO = "no_go"
    WAIT_DATA = "wait_data"
    WAIT = "wait"


class FeeProfileStatus(str, Enum):
    PROVISIONAL = "PROVISIONAL"


@dataclass(frozen=True)
class PriceLevel:
    price: Decimal
    relation: str
    valid_until: datetime


@dataclass(frozen=True)
class TargetObservationRequest:
    etf_code: str
    profile_id: str = M1_PROFILE_ID


@dataclass(frozen=True)
class TargetObservationDecision:
    decision_id: str
    etf_code: str
    etf_name: str | None
    mode: ObservationMode
    status: DecisionStatus
    target_bid: Decimal | None
    target_ask: Decimal | None
    buy_observation_ceiling: PriceLevel | None
    strategy_exit_level: PriceLevel | None
    break_even_reference: Decimal | None
    estimated_quantity: int | None
    round_trip_cost_cny: Decimal | None
    fee_profile_status: FeeProfileStatus | None
    policy_version: str | None
    data_snapshot_id: str | None
    eligibility_evidence_id: str | None
    eligibility_reviewed_on: date | None
    policy_metadata: ObservationPolicyMetadata | None
    generated_at: datetime
    reasons: tuple[str, ...] = ()
    target_iopv: Decimal | None = None
    feed_label: str | None = None
    data_valid_until: datetime | None = None
    signal_bar_count: int | None = None
    signal_bar_required: int | None = None
    data_gate_reasons: tuple[str, ...] = ()
    config_sha256: str | None = None


@dataclass(frozen=True)
class ObservationPolicyMetadata:
    """Frozen research lineage; never rendered as another tradable instrument."""

    hypothesis_id: str
    target_symbol: str
    family: str
    anchor_formula: str
    signal_bar_interval_minutes: int
    signal_timing: str
    training_window: str
    parameter_search_log: str
    frozen_at: datetime
    forward_sample_start_date: date
    research_source_git_commit: str
    strategy_source_blob: str
    data_hash: str
    validation_status: str
    allowed_mode: ObservationMode

    def validate(self) -> None:
        required_strings = {
            "hypothesis_id": self.hypothesis_id,
            "target_symbol": self.target_symbol,
            "family": self.family,
            "anchor_formula": self.anchor_formula,
            "signal_timing": self.signal_timing,
            "training_window": self.training_window,
            "parameter_search_log": self.parameter_search_log,
            "research_source_git_commit": self.research_source_git_commit,
            "strategy_source_blob": self.strategy_source_blob,
            "data_hash": self.data_hash,
            "validation_status": self.validation_status,
        }
        missing = [name for name, value in required_strings.items() if not value.strip()]
        if missing:
            raise ValueError(f"policy metadata requires {', '.join(missing)}")
        if self.signal_bar_interval_minutes <= 0:
            raise ValueError("signal_bar_interval_minutes must be positive")
        if self.frozen_at.tzinfo is None:
            raise ValueError("policy frozen_at must be timezone-aware")
        if self.allowed_mode is not ObservationMode.PAPER_OBSERVATION:
            raise ValueError("M1 policy is allowed only in paper_observation mode")


@dataclass(frozen=True)
class M1ObservationFixture:
    etf_code: str
    etf_name: str
    observed_at: datetime
    target_bid: Decimal
    target_ask: Decimal
    buy_ceiling: Decimal
    exit_level: Decimal
    policy_version: str
    data_snapshot_id: str
    eligibility_record: EtfUniverseRecord
    policy_metadata: ObservationPolicyMetadata

    @property
    def computed_data_hash(self) -> str:
        payload = (
            f"{self.etf_code}|{self.observed_at.isoformat()}|{self.target_bid}|"
            f"{self.target_ask}|{self.buy_ceiling}|{self.exit_level}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def eligibility_evidence_id(self) -> str:
        evidence = self.eligibility_record.evidence
        if evidence is None:
            return ""
        identity = (
            f"{self.eligibility_record.code}|{evidence.issuer}|"
            f"{evidence.source_document_url}|{evidence.announcement_date.isoformat()}|"
            f"{evidence.same_day_turnaround_quote}"
        )
        fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        return (
            f"{self.eligibility_record.exchange.lower()}-"
            f"{evidence.announcement_date:%Y%m%d}-{self.eligibility_record.code}-"
            f"{fingerprint}"
        )

    def validate(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("fixture observed_at must be timezone-aware")
        prices = {
            "target_bid": self.target_bid,
            "target_ask": self.target_ask,
            "buy_ceiling": self.buy_ceiling,
            "exit_level": self.exit_level,
        }
        for name, value in prices.items():
            if value <= 0 or value % ETF_PRICE_TICK != 0:
                raise ValueError(f"{name} must be positive and align to the 0.001 ETF tick")
        if self.target_bid > self.target_ask:
            raise ValueError("target_bid cannot exceed target_ask")
        record = self.eligibility_record
        record.validate()
        if record.status is not EligibilityStatus.CONFIRMED:
            raise ValueError("fixture eligibility record must be confirmed")
        if record.code != self.etf_code:
            raise ValueError("eligibility record code must match fixture etf_code")
        evidence = record.evidence
        if evidence is None:
            raise ValueError("confirmed fixture eligibility record requires evidence")
        compact_quote = evidence.same_day_turnaround_quote.replace(" ", "")
        if record.code not in compact_quote:
            raise ValueError("T+0 evidence quote must identify the fixture ETF code")
        expected_issuer = {"SZSE": "深圳证券交易所", "SSE": "上海证券交易所"}[record.exchange]
        if evidence.issuer != expected_issuer:
            raise ValueError("T+0 evidence issuer must match the fixture exchange")
        if evidence.source_kind is EvidenceSourceKind.FIRST_PARTY:
            hostname = (urlparse(evidence.source_document_url).hostname or "").lower()
            exchange_domain = {"SZSE": "szse.cn", "SSE": "sse.com.cn"}[record.exchange]
            if hostname != exchange_domain and not hostname.endswith(f".{exchange_domain}"):
                raise ValueError("first-party T+0 evidence must use an official exchange host")
        for name, value in {
            "policy_version": self.policy_version,
            "data_snapshot_id": self.data_snapshot_id,
        }.items():
            if not value.strip():
                raise ValueError(f"fixture requires {name}")
        if record.last_review_date > self.observed_at.date():
            raise ValueError("eligibility review cannot postdate the fixture")
        if (self.observed_at.date() - record.last_review_date).days > 31:
            raise ValueError("fixture eligibility review is stale")
        self.policy_metadata.validate()
        if self.policy_metadata.data_hash != self.computed_data_hash:
            raise ValueError("policy data_hash does not match the fixed fixture snapshot")
        if self.policy_metadata.target_symbol != self.etf_code:
            raise ValueError("policy target_symbol must match fixture etf_code")


class TargetObservationService:
    """Evaluate one target code against the frozen M1 paper fixture."""

    def __init__(
        self,
        *,
        fixture: M1ObservationFixture,
        clock: Callable[[], datetime],
        capital_cny: Decimal,
    ) -> None:
        fixture.validate()
        self._fixture = fixture
        self._clock = clock
        self._capital_cny = capital_cny

    @staticmethod
    def _no_go(
        *,
        request: TargetObservationRequest,
        now: datetime,
        reason: str,
        etf_name: str | None = None,
        policy_version: str | None = None,
    ) -> TargetObservationDecision:
        return TargetObservationDecision(
            decision_id=(
                f"m1-{request.etf_code or 'empty'}-"
                f"{now.strftime('%Y%m%dT%H%M%S.%f%z')}"
            ),
            etf_code=request.etf_code,
            etf_name=etf_name,
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
            policy_version=policy_version,
            data_snapshot_id=None,
            eligibility_evidence_id=None,
            eligibility_reviewed_on=None,
            policy_metadata=None,
            generated_at=now,
            reasons=(reason,),
        )

    def evaluate(self, request: TargetObservationRequest) -> TargetObservationDecision:
        now = self._clock()
        fixture = self._fixture
        if len(request.etf_code) != 6 or not request.etf_code.isdigit():
            return self._no_go(
                request=request,
                now=now,
                reason="ETF代码必须是六位数字。",
            )
        if request.profile_id != M1_PROFILE_ID:
            return self._no_go(
                request=request,
                now=now,
                reason="未找到指定的本地费用档案。",
            )
        if request.etf_code != fixture.etf_code:
            return self._no_go(
                request=request,
                now=now,
                reason="该目标 ETF 尚未注册 M1 观察策略。",
            )
        if not _is_continuous_session(now):
            return self._no_go(
                request=request,
                now=now,
                reason="当前不在连续竞价观察窗口。",
                etf_name=fixture.etf_name,
                policy_version=fixture.policy_version,
            )
        valid_until = fixture.observed_at + timedelta(seconds=30)
        if now < fixture.observed_at:
            return self._no_go(
                request=request,
                now=now,
                reason="可信时钟早于行情夹具时间。",
                etf_name=fixture.etf_name,
                policy_version=fixture.policy_version,
            )
        if now >= valid_until:
            return self._no_go(
                request=request,
                now=now,
                reason="M1 固定行情夹具已过期。",
                etf_name=fixture.etf_name,
                policy_version=fixture.policy_version,
            )
        quantity = int(
            ((self._capital_cny - Decimal(5)) / fixture.buy_ceiling / LOT_SIZE)
            .to_integral_value(rounding=ROUND_DOWN)
            * LOT_SIZE
        )
        if quantity <= 0:
            return self._no_go(
                request=request,
                now=now,
                reason="研究资金不足以覆盖一手目标 ETF 及买入佣金。",
                etf_name=fixture.etf_name,
                policy_version=fixture.policy_version,
            )
        fee_schedule, _ = provisional_fee_scenarios()
        break_even = break_even_for_round_trip(
            schedule=fee_schedule,
            entry_price=fixture.buy_ceiling,
            quantity=quantity,
        )
        if fixture.exit_level < break_even.tick_aligned_exit_price:
            return self._no_go(
                request=request,
                now=now,
                reason="策略退出线不足以覆盖计划往返成本。",
                etf_name=fixture.etf_name,
                policy_version=fixture.policy_version,
            )
        return TargetObservationDecision(
            decision_id=f"m1-{request.etf_code}-{now.strftime('%Y%m%dT%H%M%S.%f%z')}",
            etf_code=request.etf_code,
            etf_name=fixture.etf_name,
            mode=ObservationMode.PAPER_OBSERVATION,
            status=DecisionStatus.WAIT,
            target_bid=fixture.target_bid,
            target_ask=fixture.target_ask,
            buy_observation_ceiling=PriceLevel(
                fixture.buy_ceiling, "at_or_below", valid_until
            ),
            strategy_exit_level=PriceLevel(
                fixture.exit_level, "at_or_above", valid_until
            ),
            break_even_reference=break_even.tick_aligned_exit_price,
            estimated_quantity=quantity,
            round_trip_cost_cny=break_even.round_trip_cost.economic_cost,
            fee_profile_status=FeeProfileStatus.PROVISIONAL,
            policy_version=fixture.policy_version,
            data_snapshot_id=fixture.data_snapshot_id,
            eligibility_evidence_id=fixture.eligibility_evidence_id,
            eligibility_reviewed_on=fixture.eligibility_record.last_review_date,
            policy_metadata=fixture.policy_metadata,
            generated_at=now,
        )


def default_m1_fixture(*, ledger_path: Path | None = None) -> M1ObservationFixture:
    """Return the audited, fixed M1 fixture and its frozen research lineage."""

    if ledger_path is None:
        ledger_path = Path(__file__).resolve().parents[2] / "config/universe/t0_etf_ledger.json"
    records = load_universe_ledger(ledger_path)
    try:
        eligibility_record = next(record for record in records if record.code == "159570")
    except StopIteration as exc:
        raise ValueError("M1 eligibility ledger has no 159570 record") from exc

    return M1ObservationFixture(
        etf_code="159570",
        etf_name="港股通创新药ETF汇添富",
        observed_at=datetime(2026, 7, 27, 10, 0, tzinfo=SHANGHAI),
        target_bid=Decimal("1.418"),
        target_ask=Decimal("1.419"),
        buy_ceiling=Decimal("1.413"),
        exit_level=Decimal("1.417"),
        policy_version="manual-pilot-v1",
        data_snapshot_id="m1-fixture-159570-20260727T100000",
        eligibility_record=eligibility_record,
        policy_metadata=ObservationPolicyMetadata(
            hypothesis_id="PROXY_RESIDUAL_L48_Z150_H12_MAX1",
            target_symbol="159570",
            family="proxy_residual_reversion",
            anchor_formula="log(target_close/proxy_close)",
            signal_bar_interval_minutes=5,
            signal_timing="completed_bar_then_next_quote",
            training_window="latest_30_complete_sessions_exploratory",
            parameter_search_log="reports/generated/multi_strategy/",
            frozen_at=datetime(2026, 7, 26, 13, 13, tzinfo=SHANGHAI),
            forward_sample_start_date=date(2026, 7, 27),
            research_source_git_commit="23b43d5eec84cddd7e8f848e2418e06d7a8632ad",
            strategy_source_blob="f3dda56a0bb48c47f59e7ff2c38abeb721156879",
            data_hash="2d6e08f1988292d67415ab93f4fae202087acd6876887cbd1c0759ebc3894527",
            validation_status="paper_observation_only; G0,G2-G7 blocked",
            allowed_mode=ObservationMode.PAPER_OBSERVATION,
        ),
    )


def create_m1_fixture_service(
    *,
    clock: Callable[[], datetime],
    capital_cny: Decimal = M1_CAPITAL_CNY,
) -> TargetObservationService:
    """Build the fixed M1 service; the injected clock is the only time boundary."""

    return TargetObservationService(
        fixture=default_m1_fixture(),
        clock=clock,
        capital_cny=capital_cny,
    )
