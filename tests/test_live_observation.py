import fcntl
import json
import math
import os
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from etf_t0.fees import OrderSide, cost_for_order, provisional_fee_scenarios
from etf_t0.live_observation import (
    EXPECTED_FORWARD_CONFIG_SHA256,
    ForwardResearchSnapshotSource,
    LocalCaptureRunner,
    PairedFiveMinuteBar,
    ResearchObservationService,
    ResearchQuote,
    ResearchSnapshot,
    expected_completed_five_minute_slots,
    load_causal_paired_five_minute_bars,
    maximum_affordable_lot_quantity,
)
from etf_t0.market_calendar import load_normal_overlap_calendar
from etf_t0.observation import (
    DecisionStatus,
    ObservationMode,
    ObservationPolicyMetadata,
    TargetObservationRequest,
)
from etf_t0.universe import load_universe_ledger

SHANGHAI = ZoneInfo("Asia/Shanghai")


class SnapshotSource:
    def __init__(self, snapshot: ResearchSnapshot) -> None:
        self.snapshot = snapshot

    def refresh(self) -> ResearchSnapshot:
        return self.snapshot


class FailedSnapshotSource:
    def refresh(self) -> ResearchSnapshot:
        raise RuntimeError("provider timeout")


class RecordingSnapshotSource(FailedSnapshotSource):
    def __init__(self) -> None:
        self.calls = 0

    def refresh(self) -> ResearchSnapshot:
        self.calls += 1
        return super().refresh()


def test_fresh_research_feed_without_signal_history_returns_actionable_wait_data() -> None:
    observed_at = datetime(2026, 7, 28, 13, 18, 30, tzinfo=SHANGHAI)
    snapshot = ResearchSnapshot(
        snapshot_id="capture-20260728-131830",
        observed_at=observed_at,
        target=ResearchQuote(
            symbol="159570",
            name="港股通创新药ETF汇添富",
            bid=Decimal("1.404"),
            ask=Decimal("1.405"),
            iopv=Decimal("1.4036"),
            provider_update_time=observed_at,
        ),
        reference=ResearchQuote(
            symbol="513780",
            name="internal reference",
            bid=Decimal("1.504"),
            ask=Decimal("1.505"),
            iopv=Decimal("1.5044"),
            provider_update_time=observed_at,
        ),
        paired_five_minute_bars=(),
        capture_candidate=True,
        desktop_eligible=False,
        valid_forward_sample_available=False,
        data_gate_reasons=("G2/G3 尚未通过。",),
    )
    service = ResearchObservationService(
        source=SnapshotSource(snapshot),
        clock=lambda: observed_at,
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.WAIT_DATA
    assert decision.target_bid == Decimal("1.404")
    assert decision.target_ask == Decimal("1.405")
    assert decision.target_iopv == Decimal("1.4036")
    assert decision.buy_observation_ceiling is None
    assert decision.strategy_exit_level is None
    assert decision.break_even_reference is None
    assert decision.feed_label == "UNVERIFIED RESEARCH FEED"
    assert decision.signal_bar_count == 0
    assert decision.signal_bar_required == 48
    assert decision.data_valid_until == datetime(
        2026, 7, 28, 13, 19, tzinfo=SHANGHAI
    )
    assert decision.reasons == (
        "已连接当日研究行情，但因果5分钟bar为 0/48。",
        "G2/G3 尚未通过。",
    )

    stale_provider = ResearchObservationService(
        source=SnapshotSource(
            replace(
                snapshot,
                target=replace(
                    snapshot.target,
                    provider_update_time=observed_at - timedelta(seconds=100),
                ),
                reference=replace(
                    snapshot.reference,
                    provider_update_time=observed_at - timedelta(seconds=100),
                ),
            )
        ),
        clock=lambda: observed_at,
    ).evaluate(TargetObservationRequest(etf_code="159570"))
    assert stale_provider.status is DecisionStatus.NO_GO
    assert stale_provider.target_bid is None
    assert stale_provider.reasons == (
        "提供方报价已陈旧、非当日或成对时间差过大。",
    )


def test_data_source_error_is_a_retryable_no_go_without_any_prices() -> None:
    now = datetime(2026, 7, 28, 13, 20, tzinfo=SHANGHAI)
    service = ResearchObservationService(
        source=FailedSnapshotSource(),
        clock=lambda: now,
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.target_bid is None
    assert decision.target_ask is None
    assert decision.target_iopv is None
    assert decision.buy_observation_ceiling is None
    assert decision.strategy_exit_level is None
    assert decision.data_valid_until is None
    assert decision.reasons == (
        "行情刷新失败，请检查网络或本机采集状态后重试：provider timeout",
    )


def test_forward_manifest_is_normalized_into_a_target_only_research_snapshot(
    tmp_path,
) -> None:
    received_at = datetime(2026, 7, 28, 13, 18, 30, tzinfo=SHANGHAI)
    manifest = {
        "latest_quote_rows": [
            {
                "capture_id": "capture-live",
                "symbol": "159570",
                "name": "港股通创新药ETF汇添富",
                "observed_at": received_at.isoformat(),
                "provider_update_time": received_at.isoformat(),
                "is_candidate_forward_quote": True,
                "is_candidate_forward_pair": True,
                "bid1_price": 1.404,
                "ask1_price": 1.405,
                "iopv": 1.4036,
                "spread_ticks": 1.0,
                "pair_provider_timestamp_skew_seconds": 0.0,
            },
            {
                "capture_id": "capture-live",
                "symbol": "513780",
                "name": "internal reference",
                "observed_at": received_at.isoformat(),
                "provider_update_time": received_at.isoformat(),
                "is_candidate_forward_quote": True,
                "is_candidate_forward_pair": True,
                "bid1_price": 1.504,
                "ask1_price": 1.505,
                "iopv": 1.5044,
                "spread_ticks": 1.0,
                "pair_provider_timestamp_skew_seconds": 0.0,
            },
        ],
        "stage_status": {
            "valid_forward_sample_available": False,
            "G2": "BLOCKED until cross-source validation",
            "G3": "BLOCKED until executable depth validation",
        },
    }
    source = ForwardResearchSnapshotSource(
        workspace=tmp_path,
        capture_runner=lambda: manifest,
        clock=lambda: received_at,
    )

    snapshot = source.refresh()

    assert snapshot.snapshot_id == "capture-live"
    assert snapshot.target.symbol == "159570"
    assert snapshot.target.bid == Decimal("1.404")
    assert snapshot.target.ask == Decimal("1.405")
    assert snapshot.target.iopv == Decimal("1.4036")
    assert snapshot.capture_candidate is True
    assert snapshot.desktop_eligible is False
    assert snapshot.valid_forward_sample_available is False
    assert snapshot.g2_pass is False
    assert snapshot.g3_pass is False
    assert snapshot.data_gate_reasons == (
        "G2 数据验收未通过：尚缺独立数据源与交易日历交叉核对。",
        "G3 执行数据验收未通过：尚缺盘中传输时延与可成交深度验证。",
    )

    manifest["stage_status"]["valid_forward_sample_available"] = True
    inconsistent = source.refresh()
    assert inconsistent.valid_forward_sample_available is True
    assert inconsistent.desktop_eligible is False

    manifest["latest_quote_rows"][1]["capture_id"] = "different-capture"
    with pytest.raises(ValueError, match="same capture"):
        source.refresh()


def test_current_paper_service_clears_all_prices_during_closing_auction() -> None:
    now = datetime(2026, 7, 28, 14, 58, tzinfo=SHANGHAI)
    source = FailedSnapshotSource()
    service = ResearchObservationService(source=source, clock=lambda: now)

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.target_bid is None
    assert decision.target_ask is None
    assert decision.target_iopv is None
    assert decision.reasons == ("当前不在连续竞价观察窗口。",)


@pytest.mark.parametrize(
    ("clock", "expected_calls"),
    (("11:29:59", 1), ("11:30:00", 0), ("14:56:59", 1), ("14:57:00", 0)),
)
def test_continuous_auction_boundaries_are_second_exact(
    clock: str, expected_calls: int
) -> None:
    now = datetime.fromisoformat(f"2026-07-28T{clock}+08:00")
    source = RecordingSnapshotSource()

    ResearchObservationService(source=source, clock=lambda: now).evaluate(
        TargetObservationRequest(etf_code="159570")
    )

    assert source.calls == expected_calls


def test_snapshot_older_than_thirty_seconds_fails_closed() -> None:
    observed_at = datetime(2026, 7, 28, 13, 18, 0, tzinfo=SHANGHAI)
    now = datetime(2026, 7, 28, 13, 18, 31, tzinfo=SHANGHAI)
    snapshot = ResearchSnapshot(
        snapshot_id="capture-stale",
        observed_at=observed_at,
        target=ResearchQuote(
            symbol="159570",
            name="港股通创新药ETF汇添富",
            bid=Decimal("1.404"),
            ask=Decimal("1.405"),
            iopv=Decimal("1.4036"),
            provider_update_time=observed_at,
        ),
        reference=ResearchQuote(
            symbol="513780",
            name="internal reference",
            bid=Decimal("1.504"),
            ask=Decimal("1.505"),
            iopv=Decimal("1.5044"),
            provider_update_time=observed_at,
        ),
        paired_five_minute_bars=(),
        capture_candidate=True,
        desktop_eligible=False,
        valid_forward_sample_available=False,
    )
    service = ResearchObservationService(
        source=SnapshotSource(snapshot),
        clock=lambda: now,
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.target_bid is None
    assert decision.target_ask is None
    assert decision.target_iopv is None
    assert decision.data_valid_until is None
    assert decision.reasons == ("行情快照已超过30秒，请重新刷新。",)


def test_unsupported_symbol_fails_before_any_network_refresh() -> None:
    now = datetime(2026, 7, 28, 13, 18, tzinfo=SHANGHAI)
    source = RecordingSnapshotSource()
    service = ResearchObservationService(source=source, clock=lambda: now)

    decision = service.evaluate(TargetObservationRequest(etf_code="159567"))

    assert source.calls == 0
    assert decision.status is DecisionStatus.NO_GO
    assert decision.reasons == ("该目标 ETF 尚未注册 M2 当前纸面观察策略。",)


def test_current_paper_service_rejects_non_overlap_weekday_before_refresh() -> None:
    now = datetime(2026, 7, 1, 10, 0, tzinfo=SHANGHAI)
    source = RecordingSnapshotSource()
    calendar = load_normal_overlap_calendar(
        Path("config/normal_overlap_calendar_2026.json")
    )
    service = ResearchObservationService(
        source=source,
        clock=lambda: now,
        normal_overlap_calendar=calendar,
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.target_bid is None
    assert decision.reasons == ("当前日期不是版本化日历确认的正常重合日。",)
    assert source.calls == 0


def test_expected_slots_fail_closed_before_calendar_has_48_bars() -> None:
    calendar = load_normal_overlap_calendar(
        Path("config/normal_overlap_calendar_2026.json")
    )

    slots = expected_completed_five_minute_slots(
        as_of=datetime(2026, 1, 5, 10, 0, tzinfo=SHANGHAI),
        count=48,
        normal_overlap_calendar=calendar,
    )

    assert len(slots) < 48


def test_causal_resampling_requires_five_consecutive_mature_one_minute_vintages(
    tmp_path,
) -> None:
    clocks = ["10:01", "10:02", "10:03", "10:04", "10:05", "10:06"]
    for symbol, base in (("159570", 1.0), ("513780", 2.0)):
        path = (
            tmp_path
            / "data"
            / "interim"
            / "forward_capture"
            / "one_minute"
            / symbol
            / "bars.csv"
        )
        path.parent.mkdir(parents=True)
        pd.DataFrame(
            {
                "timestamp": [f"2026-07-28 {clock}:00" for clock in clocks],
                "close": [base + index / 1000 for index in range(1, 7)],
                "is_valid_ohlc": True,
                "is_candidate_forward_pair_bar": True,
                "selected_vintage_received_at": [
                    f"2026-07-28T{clock}:20+08:00" for clock in clocks
                ],
            }
        ).to_csv(path, index=False)

    bars = load_causal_paired_five_minute_bars(
        workspace=tmp_path,
        as_of=datetime(2026, 7, 28, 10, 6, tzinfo=SHANGHAI),
    )

    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2026, 7, 28, 10, 5, tzinfo=SHANGHAI)
    assert bars[0].target_close == Decimal("1.005")
    assert bars[0].reference_close == Decimal("2.005")
    assert bars[0].first_available_at == datetime(
        2026, 7, 28, 10, 5, 20, tzinfo=SHANGHAI
    )


def test_eligible_next_quote_produces_paper_levels_while_live_gates_are_blocked() -> None:
    observed_at = datetime(2026, 7, 28, 13, 18, 30, tzinfo=SHANGHAI)
    prior_day = date(2026, 7, 27)
    current_day = observed_at.date()
    prior_afternoon = (
        [(13, minute) for minute in range(20, 60, 5)]
        + [(14, minute) for minute in range(0, 60, 5)]
        + [(15, 0)]
    )
    current_completed = (
        [(9, minute) for minute in range(35, 60, 5)]
        + [(10, minute) for minute in range(0, 60, 5)]
        + [(11, minute) for minute in range(0, 31, 5)]
        + [(13, minute) for minute in range(5, 16, 5)]
    )
    slots = tuple(
        datetime(day.year, day.month, day.day, hour, minute, tzinfo=SHANGHAI)
        for day, endpoints in (
            (prior_day, prior_afternoon),
            (current_day, current_completed),
        )
        for hour, minute in endpoints
    )
    assert len(slots) == 48
    assert slots[20] == datetime(2026, 7, 27, 15, 0, tzinfo=SHANGHAI)
    bars = tuple(
        PairedFiveMinuteBar(
            timestamp=timestamp,
            target_close=Decimal(str(math.exp((index - 23.5) * 0.001))),
            reference_close=Decimal(1),
            first_available_at=timestamp + timedelta(minutes=1),
        )
        for index, timestamp in enumerate(slots)
    )
    policy_metadata = ObservationPolicyMetadata(
        hypothesis_id="PROXY_RESIDUAL_L48_Z150_H12_MAX1",
        target_symbol="159570",
        family="proxy_residual_reversion",
        anchor_formula="log(target_close/reference_close)",
        signal_bar_interval_minutes=5,
        signal_timing="completed_bar_then_next_eligible_quote",
        training_window="frozen_before_forward_start; causal L48",
        parameter_search_log="reports/t0_etf_multi_strategy_exploration.md",
        frozen_at=datetime(2026, 7, 26, 13, 13, tzinfo=SHANGHAI),
        forward_sample_start_date=date(2026, 7, 27),
        research_source_git_commit="23b43d5eec84cddd7e8f848e2418e06d7a8632ad",
        strategy_source_blob="f3dda56a0bb48c47f59e7ff2c38abeb721156879",
        data_hash="synthetic-causal-bars",
        validation_status="synthetic_paper_only_g2_g3_blocked",
        allowed_mode=ObservationMode.PAPER_OBSERVATION,
    )
    snapshot = ResearchSnapshot(
        snapshot_id="capture-eligible",
        observed_at=observed_at,
        target=ResearchQuote(
            symbol="159570",
            name="港股通创新药ETF汇添富",
            bid=Decimal("1.500"),
            ask=Decimal("1.501"),
            iopv=Decimal("1.5005"),
            provider_update_time=observed_at,
        ),
        reference=ResearchQuote(
            symbol="513780",
            name="internal reference",
            bid=Decimal("1.500"),
            ask=Decimal("1.501"),
            iopv=Decimal("1.5005"),
            provider_update_time=observed_at,
        ),
        paired_five_minute_bars=bars,
        capture_candidate=True,
        desktop_eligible=True,
        valid_forward_sample_available=False,
        data_gate_reasons=("G2/G3 仍阻断实盘准入。",),
        g2_pass=False,
        g3_pass=False,
        policy_metadata=policy_metadata,
        config_sha256=EXPECTED_FORWARD_CONFIG_SHA256,
        feed_verified=False,
    )
    eligibility = next(
        record
        for record in load_universe_ledger(Path("config/universe/t0_etf_ledger.json"))
        if record.code == "159570"
    )
    calendar = load_normal_overlap_calendar(
        Path("config/normal_overlap_calendar_2026.json")
    )
    service = ResearchObservationService(
        source=SnapshotSource(snapshot),
        clock=lambda: observed_at,
        eligibility_record=eligibility,
        normal_overlap_calendar=calendar,
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.WAIT
    assert decision.buy_observation_ceiling is not None
    assert decision.buy_observation_ceiling.price == Decimal("1.469")
    assert decision.strategy_exit_level is not None
    assert decision.strategy_exit_level.price == Decimal("1.496")
    assert decision.estimated_quantity == 3400
    assert decision.round_trip_cost_cny is not None
    assert decision.round_trip_cost_cny >= Decimal("16.80")
    assert decision.break_even_reference is not None
    assert decision.break_even_reference <= decision.strategy_exit_level.price
    assert decision.feed_label == "UNVERIFIED RESEARCH FEED"
    assert decision.data_gate_reasons == ("G2/G3 仍阻断实盘准入。",)
    assert decision.reasons[-1] == "G2/G3 仍阻断实盘准入。"

    wide_spread = ResearchObservationService(
        source=SnapshotSource(
            replace(
                snapshot,
                target=replace(snapshot.target, ask=Decimal("1.510")),
            )
        ),
        clock=lambda: observed_at,
        eligibility_record=eligibility,
        normal_overlap_calendar=calendar,
    ).evaluate(TargetObservationRequest(etf_code="159570"))

    assert wide_spread.status is DecisionStatus.NO_GO
    assert wide_spread.target_bid is None
    assert wide_spread.buy_observation_ceiling is None
    assert wide_spread.reasons == ("买卖价差或成对报价时间差未通过严格执行门禁。",)

    drifted_policy = ResearchObservationService(
        source=SnapshotSource(
            replace(snapshot, config_sha256="same-id-but-parameters-drifted")
        ),
        clock=lambda: observed_at,
        eligibility_record=eligibility,
        normal_overlap_calendar=calendar,
    ).evaluate(TargetObservationRequest(etf_code="159570"))

    assert drifted_policy.status is DecisionStatus.NO_GO
    assert drifted_policy.buy_observation_ceiling is None
    assert drifted_policy.reasons == ("快照未绑定 Issue #19 完整冻结配置。",)

    narrow_bars = tuple(
        replace(
            bar,
            target_close=Decimal(str(math.exp((index - 23.5) * 0.000001))),
        )
        for index, bar in enumerate(bars)
    )
    insufficient_cost_space = ResearchObservationService(
        source=SnapshotSource(replace(snapshot, paired_five_minute_bars=narrow_bars)),
        clock=lambda: observed_at,
        eligibility_record=eligibility,
        normal_overlap_calendar=calendar,
    ).evaluate(TargetObservationRequest(etf_code="159570"))

    assert insufficient_cost_space.status is DecisionStatus.NO_GO
    assert insufficient_cost_space.target_bid is None
    assert insufficient_cost_space.buy_observation_ceiling is None
    assert insufficient_cost_space.reasons == (
        "策略观察区间无法覆盖双边费用与2个ETF价格档执行缓冲。",
    )

    assert eligibility.evidence is not None
    wrong_security_evidence = replace(
        eligibility,
        evidence=replace(
            eligibility.evidence,
            same_day_turnaround_quote=(
                "另一只基金（证券代码：159567）上市并实施当日回转交易。"
            ),
        ),
    )
    invalid_eligibility = ResearchObservationService(
        source=SnapshotSource(snapshot),
        clock=lambda: observed_at,
        eligibility_record=wrong_security_evidence,
        normal_overlap_calendar=calendar,
    ).evaluate(TargetObservationRequest(etf_code="159570"))

    assert invalid_eligibility.status is DecisionStatus.NO_GO
    assert invalid_eligibility.buy_observation_ceiling is None
    assert invalid_eligibility.reasons == ("交易所 T+0 资格证据语义未通过校验。",)

    mixed_security_evidence = replace(
        eligibility,
        evidence=replace(
            eligibility.evidence,
            same_day_turnaround_quote=(
                "159570仅作参考、159567实施当日回转交易。"
            ),
        ),
    )
    mixed_eligibility = ResearchObservationService(
        source=SnapshotSource(snapshot),
        clock=lambda: observed_at,
        eligibility_record=mixed_security_evidence,
        normal_overlap_calendar=calendar,
    ).evaluate(TargetObservationRequest(etf_code="159570"))

    assert mixed_eligibility.status is DecisionStatus.NO_GO
    assert mixed_eligibility.buy_observation_ceiling is None

    for ambiguous_status in ("delisted", "not listed"):
        invalid_status = ResearchObservationService(
            source=SnapshotSource(snapshot),
            clock=lambda: observed_at,
            eligibility_record=replace(
                eligibility, security_status=ambiguous_status
            ),
            normal_overlap_calendar=calendar,
        ).evaluate(TargetObservationRequest(etf_code="159570"))

        assert invalid_status.status is DecisionStatus.NO_GO
        assert invalid_status.buy_observation_ceiling is None


def test_quantity_reserves_buy_side_fees_inside_five_thousand_cash_sleeve() -> None:
    schedule = provisional_fee_scenarios()[1]

    quantity = maximum_affordable_lot_quantity(
        capital_cny=Decimal(5000),
        buy_price=Decimal("1.500"),
        schedule=schedule,
    )

    assert quantity == 3300
    notional = Decimal("1.500") * quantity
    buy_cost = cost_for_order(
        schedule=schedule,
        side=OrderSide.BUY,
        reference_notional=notional,
    )
    assert notional + buy_cost.economic_cost <= Decimal(5000)
    next_lot_notional = Decimal("1.500") * (quantity + 100)
    next_lot_cost = cost_for_order(
        schedule=schedule,
        side=OrderSide.BUY,
        reference_notional=next_lot_notional,
    )
    assert next_lot_notional + next_lot_cost.economic_cost > Decimal(5000)


def test_app_reuses_atomic_manifest_while_supervised_collector_is_healthy(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 28, 14, 18, tzinfo=SHANGHAI)
    generated = tmp_path / "reports/generated/forward_capture"
    generated.mkdir(parents=True)
    manifest = {"generated_at": now.isoformat(), "latest_quote_rows": []}
    (generated / "latest_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (generated / "collector_heartbeat.json").write_text(
        json.dumps(
            {
                "state": "RUNNING",
                "pid": os.getpid(),
                "updated_at": now.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    fallback_calls = 0

    def fallback():
        nonlocal fallback_calls
        fallback_calls += 1
        return {"fallback": True}

    runner = LocalCaptureRunner(workspace=tmp_path, fallback=fallback, clock=lambda: now)

    assert runner() == manifest
    assert fallback_calls == 0

    (generated / "collector_heartbeat.json").unlink()
    lock_path = generated / "collector.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="采集器仍在运行"):
            runner()
    assert fallback_calls == 0


def test_app_ignores_fresh_running_heartbeat_when_pid_is_dead(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 28, 14, 18, tzinfo=SHANGHAI)
    generated = tmp_path / "reports/generated/forward_capture"
    generated.mkdir(parents=True)
    stale_manifest = {"generated_at": "stale", "latest_quote_rows": []}
    (generated / "latest_manifest.json").write_text(
        json.dumps(stale_manifest), encoding="utf-8"
    )
    (generated / "collector_heartbeat.json").write_text(
        json.dumps(
            {
                "state": "RUNNING",
                "pid": 999_999_999,
                "updated_at": now.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    fresh_manifest = {"generated_at": "fresh", "latest_quote_rows": []}

    runner = LocalCaptureRunner(
        workspace=tmp_path,
        fallback=lambda: fresh_manifest,
        clock=lambda: now,
    )

    assert runner() == fresh_manifest
