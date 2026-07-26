from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from etf_t0.observation import (
    DecisionStatus,
    FeeProfileStatus,
    M1ObservationFixture,
    ObservationMode,
    TargetObservationRequest,
    TargetObservationService,
    create_m1_fixture_service,
    default_m1_fixture,
)
from etf_t0.universe import EligibilityStatus

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_registered_target_returns_target_only_paper_observation_levels() -> None:
    assert TargetObservationRequest(etf_code="159570").profile_id == (
        "generic-provisional-minimum"
    )
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.etf_code == "159570"
    assert decision.etf_name == "港股通创新药ETF汇添富"
    assert decision.mode is ObservationMode.PAPER_OBSERVATION
    assert decision.status is DecisionStatus.WAIT
    assert decision.buy_observation_ceiling.price == Decimal("1.413")
    assert decision.strategy_exit_level.price == Decimal("1.417")
    assert decision.break_even_reference == Decimal("1.415")
    assert decision.estimated_quantity == 7000
    assert decision.policy_version == "manual-pilot-v1"
    assert decision.data_snapshot_id == "m1-fixture-159570-20260727T100000"
    assert decision.round_trip_cost_cny == Decimal("10.00")
    assert decision.fee_profile_status is FeeProfileStatus.PROVISIONAL
    assert decision.eligibility_evidence_id.startswith("szse-20240117-159570-")
    assert decision.eligibility_reviewed_on == date(2026, 7, 26)
    assert decision.policy_metadata is not None
    assert decision.policy_metadata.hypothesis_id == "PROXY_RESIDUAL_L48_Z150_H12_MAX1"
    assert decision.policy_metadata.anchor_formula == "log(target_close/proxy_close)"
    assert decision.policy_metadata.allowed_mode is ObservationMode.PAPER_OBSERVATION
    assert decision.policy_metadata.research_source_git_commit
    assert decision.policy_metadata.strategy_source_blob
    assert len(decision.policy_metadata.data_hash) == 64
    assert decision.decision_id == "m1-159570-20260727T100015.000000+0800"


def test_unregistered_target_fails_closed_without_reusable_prices() -> None:
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159567"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.target_bid is None
    assert decision.target_ask is None
    assert decision.buy_observation_ceiling is None
    assert decision.strategy_exit_level is None
    assert decision.break_even_reference is None
    assert decision.reasons == ("该目标 ETF 尚未注册 M1 观察策略。",)


def test_lunch_break_clears_the_fixture_prices() -> None:
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 11, 31, tzinfo=SHANGHAI)
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.target_bid is None
    assert decision.target_ask is None
    assert decision.buy_observation_ceiling is None
    assert decision.strategy_exit_level is None
    assert decision.break_even_reference is None
    assert decision.reasons == ("当前不在连续竞价观察窗口。",)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 7, 27, 9, 20, tzinfo=SHANGHAI),
        datetime(2026, 7, 27, 11, 30, tzinfo=SHANGHAI),
        datetime(2026, 7, 27, 12, 59, 59, tzinfo=SHANGHAI),
        datetime(2026, 7, 27, 14, 57, tzinfo=SHANGHAI),
        datetime(2026, 7, 27, 15, 5, tzinfo=SHANGHAI),
    ],
    ids=["opening-auction", "lunch-boundary", "pre-afternoon", "closing-auction", "post-close"],
)
def test_non_continuous_trading_periods_fail_closed(now: datetime) -> None:
    service = create_m1_fixture_service(clock=lambda: now)

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.buy_observation_ceiling is None
    assert decision.strategy_exit_level is None
    assert decision.reasons == ("当前不在连续竞价观察窗口。",)


@pytest.mark.parametrize("session_start", [(9, 30), (13, 0)])
def test_continuous_session_start_boundaries_are_included(
    session_start: tuple[int, int],
) -> None:
    hour, minute = session_start
    observed_at = datetime(2026, 7, 27, hour, minute, tzinfo=SHANGHAI)
    fixture = replace(default_m1_fixture(), observed_at=observed_at)
    fixture = replace(
        fixture,
        policy_metadata=replace(
            fixture.policy_metadata, data_hash=fixture.computed_data_hash
        ),
    )
    service = TargetObservationService(
        fixture=fixture,
        clock=lambda: observed_at.replace(second=15),
        capital_cny=Decimal(10000),
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.WAIT


def test_expired_fixture_quote_is_not_reused() -> None:
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 31, tzinfo=SHANGHAI)
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.target_bid is None
    assert decision.target_ask is None
    assert decision.buy_observation_ceiling is None
    assert decision.strategy_exit_level is None
    assert decision.break_even_reference is None
    assert decision.reasons == ("M1 固定行情夹具已过期。",)


def test_fixture_is_expired_at_the_exact_valid_until_boundary() -> None:
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 30, tzinfo=SHANGHAI)
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.buy_observation_ceiling is None
    assert decision.reasons == ("M1 固定行情夹具已过期。",)


def test_entry_is_no_go_when_the_strategy_exit_cannot_cover_costs() -> None:
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI),
        capital_cny=Decimal(200),
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.target_bid is None
    assert decision.target_ask is None
    assert decision.buy_observation_ceiling is None
    assert decision.strategy_exit_level is None
    assert decision.break_even_reference is None
    assert decision.reasons == ("策略退出线不足以覆盖计划往返成本。",)


def test_capital_below_one_lot_fails_closed_without_prices() -> None:
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI),
        capital_cny=Decimal(100),
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.target_bid is None
    assert decision.target_ask is None
    assert decision.buy_observation_ceiling is None
    assert decision.strategy_exit_level is None
    assert decision.reasons == ("研究资金不足以覆盖一手目标 ETF 及买入佣金。",)


def test_invalid_etf_code_is_rejected_before_policy_lookup() -> None:
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="15957"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.reasons == ("ETF代码必须是六位数字。",)


def test_clock_rollback_cannot_make_a_future_fixture_look_fresh() -> None:
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 9, 59, 59, tzinfo=SHANGHAI)
    )

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.buy_observation_ceiling is None
    assert decision.strategy_exit_level is None
    assert decision.reasons == ("可信时钟早于行情夹具时间。",)


def test_unknown_fee_profile_is_not_silently_replaced_by_the_default() -> None:
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )

    decision = service.evaluate(
        TargetObservationRequest(etf_code="159570", profile_id="unknown-profile")
    )

    assert decision.status is DecisionStatus.NO_GO
    assert decision.break_even_reference is None
    assert decision.reasons == ("未找到指定的本地费用档案。",)


def test_fixture_rejects_a_price_that_is_not_on_the_0001_etf_tick() -> None:
    fixture = replace(default_m1_fixture(), buy_ceiling=Decimal("1.4135"))

    try:
        TargetObservationService(
            fixture=fixture,
            clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI),
            capital_cny=Decimal(10000),
        )
    except ValueError as exc:
        assert "0.001" in str(exc)
    else:
        raise AssertionError("invalid fixture tick must be rejected at startup")


def test_fixture_requires_confirmed_eligibility_evidence() -> None:
    fixture = default_m1_fixture()
    unconfirmed_record = replace(
        fixture.eligibility_record, status=EligibilityStatus.PENDING_REVIEW
    )
    fixture = replace(fixture, eligibility_record=unconfirmed_record)

    try:
        TargetObservationService(
            fixture=fixture,
            clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI),
            capital_cny=Decimal(10000),
        )
    except ValueError as exc:
        assert "confirmed" in str(exc)
    else:
        raise AssertionError("unverified eligibility must fail closed at startup")


def test_fixture_rejects_eligibility_evidence_bound_to_another_etf_code() -> None:
    fixture = default_m1_fixture()
    wrong_target_policy = replace(fixture.policy_metadata, target_symbol="159567")
    mismatched_fixture = replace(
        fixture,
        etf_code="159567",
        policy_metadata=wrong_target_policy,
    )

    with pytest.raises(ValueError, match="eligibility record code"):
        TargetObservationService(
            fixture=mismatched_fixture,
            clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI),
            capital_cny=Decimal(10000),
        )


def test_fixture_rejects_non_exchange_first_party_evidence_url() -> None:
    fixture: M1ObservationFixture = default_m1_fixture()
    assert fixture.eligibility_record.evidence is not None
    fake_evidence = replace(
        fixture.eligibility_record.evidence,
        source_document_url="https://example.invalid/fake-notice",
    )
    fake_record = replace(fixture.eligibility_record, evidence=fake_evidence)
    fixture = replace(fixture, eligibility_record=fake_record)

    with pytest.raises(ValueError, match="official exchange host"):
        TargetObservationService(
            fixture=fixture,
            clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI),
            capital_cny=Decimal(10000),
        )


def test_fixture_requires_complete_frozen_policy_lineage() -> None:
    fixture = default_m1_fixture()
    incomplete_policy = replace(fixture.policy_metadata, strategy_source_blob="")

    try:
        TargetObservationService(
            fixture=replace(fixture, policy_metadata=incomplete_policy),
            clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI),
            capital_cny=Decimal(10000),
        )
    except ValueError as exc:
        assert "strategy_source_blob" in str(exc)
    else:
        raise AssertionError("incomplete policy lineage must fail at startup")


def test_fixture_data_hash_detects_price_snapshot_tampering() -> None:
    fixture = replace(default_m1_fixture(), target_bid=Decimal("1.417"))

    with pytest.raises(ValueError, match="data_hash"):
        TargetObservationService(
            fixture=fixture,
            clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI),
            capital_cny=Decimal(10000),
        )
